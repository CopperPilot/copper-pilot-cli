"""Parallel tool_requests batches over the hosted chat socket."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from copper_pilot_cli.copper_protocol import (
    ChatRequest,
    CopperMode,
    EventKind,
    HostedChatClient,
    make_chat_start,
)
from copper_pilot_cli.deepagents_tools import ApprovalMode, LocalToolBroker
from copper_pilot_cli.tool_batch import run_tool_batch


def request(**changes) -> ChatRequest:
    values = {
        "query": "review this",
        "workspace_path": "/tmp/project",  # noqa: S108
        "conversation_id": "conversation",
        "mode": CopperMode.AGENT,
    }
    values.update(changes)
    return ChatRequest(**values)


def event(**chat_response) -> str:
    return json.dumps({"type": "event", "chat_response": chat_response})


def final() -> str:
    return json.dumps({"type": "final", "chat_response": {"response": "Done", "is_final": True}})


def read(call_id: str, path: str) -> dict:
    return {"tool_call_id": call_id, "tool_name": "read_file", "arguments": {"file_path": path}}


class FakeSocket:
    """Scripted hosted server that records every frame the client sends."""

    def __init__(self, frames: list[str]) -> None:
        self.frames = frames
        self.sent: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def send(self, value):
        self.sent.append(json.loads(value))

    async def recv(self):
        return self.frames.pop(0)


def install(monkeypatch, socket: FakeSocket) -> None:
    monkeypatch.setattr("copper_pilot_cli.copper_protocol.connect", lambda *_a, **_k: socket)


def client() -> HostedChatClient:
    return HostedChatClient("https://example.test", "cf_live_key", "fingerprint")


def test_chat_start_advertises_parallel_tool_requests(monkeypatch) -> None:
    monkeypatch.delenv("COPPER_PILOT_PARALLEL_TOOLS", raising=False)
    frame = make_chat_start(request())
    assert frame["capabilities"] == ["parallel_tool_requests"]
    assert frame["protocol_version"] == 1


def test_capability_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("COPPER_PILOT_PARALLEL_TOOLS", "0")
    assert "capabilities" not in make_chat_start(request())


@pytest.mark.asyncio
async def test_batch_round_trip_overlaps_reads_and_matches_results_by_id(monkeypatch) -> None:
    socket = FakeSocket(
        [
            event(
                tool_requests=[read("a", "a.txt"), read("b", "b.txt"), read("c", "c.txt")],
                turn_id="turn-1",
                sequence=4,
            ),
            final(),
        ]
    )
    install(monkeypatch, socket)
    delays = {"a": 0.3, "b": 0.2, "c": 0.1}
    spans: dict[str, tuple[float, float]] = {}

    async def execute(tool_request):
        call_id = tool_request.tool_call_id
        started_at = time.perf_counter()
        await asyncio.sleep(delays[call_id])
        spans[call_id] = (started_at, time.perf_counter())
        return {"result": f"contents of {tool_request.arguments['file_path']}"}

    events = [item async for item in client().stream(request(), execute)]

    # All three reads must be in flight together. A serial run finishes the
    # shortest read before the last one starts, independent of machine speed.
    assert max(start for start, _end in spans.values()) < min(end for _start, end in spans.values())
    assert [item.kind for item in events] == [
        EventKind.TOOL_REQUEST,
        EventKind.TOOL_REQUEST,
        EventKind.TOOL_REQUEST,
        EventKind.TOOL_RESULT,
        EventKind.TOOL_RESULT,
        EventKind.TOOL_RESULT,
        EventKind.TEXT,
        EventKind.FINAL,
    ]
    assert [item.data.tool_call_id for item in events[:3]] == ["a", "b", "c"]
    assert [item.data["tool_call_id"] for item in events[3:6]] == ["c", "b", "a"]
    assert all(item.turn_id == "turn-1" for item in events[:6])
    results = [frame for frame in socket.sent if frame["type"] == "tool_result"]
    assert [frame["tool_call_id"] for frame in results] == ["c", "b", "a"]
    for frame in results:
        assert frame["conversation_id"] == "conversation"
        assert frame["result"] == {"result": f"contents of {frame['tool_call_id']}.txt"}


@pytest.mark.asyncio
async def test_single_tool_requests_still_work_without_the_capability(monkeypatch) -> None:
    monkeypatch.setenv("COPPER_PILOT_PARALLEL_TOOLS", "0")
    socket = FakeSocket(
        [
            event(tool_request=read("one", "a.txt")),
            event(tool_request=read("two", "b.txt")),
            final(),
        ]
    )
    install(monkeypatch, socket)

    async def execute(tool_request):
        return {"result": tool_request.tool_call_id}

    events = [item async for item in client().stream(request(), execute)]
    assert "capabilities" not in socket.sent[0]
    assert [item.kind for item in events[:4]] == [
        EventKind.TOOL_REQUEST,
        EventKind.TOOL_RESULT,
        EventKind.TOOL_REQUEST,
        EventKind.TOOL_RESULT,
    ]
    assert [frame["tool_call_id"] for frame in socket.sent[1:]] == ["one", "two"]


@pytest.mark.asyncio
async def test_failing_tool_in_batch_reports_error_and_keeps_others(monkeypatch) -> None:
    socket = FakeSocket([event(tool_requests=[read("ok", "a.txt"), read("bad", "b.txt")]), final()])
    install(monkeypatch, socket)

    async def execute(tool_request):
        if tool_request.tool_call_id == "bad":
            raise OSError("unreadable")
        await asyncio.sleep(0.01)
        return {"result": "fine"}

    events = [item async for item in client().stream(request(), execute)]
    results = {frame["tool_call_id"]: frame["result"] for frame in socket.sent[1:]}
    assert results["ok"] == {"result": "fine"}
    assert results["bad"] == {"error": "OSError: unreadable", "rejected": False}
    assert events[-1].kind is EventKind.FINAL


@pytest.mark.asyncio
async def test_oversized_batch_result_is_replaced(monkeypatch) -> None:
    socket = FakeSocket([event(tool_requests=[read("big", "a"), read("small", "b")]), final()])
    install(monkeypatch, socket)

    async def execute(tool_request):
        size = 9 * 1024 * 1024 if tool_request.tool_call_id == "big" else 1
        return {"result": "x" * size}

    _ = [item async for item in client().stream(request(), execute)]
    results = {frame["tool_call_id"]: frame["result"] for frame in socket.sent[1:]}
    assert results["big"] == {"error": "Tool result exceeded the client frame limit."}
    assert results["small"] == {"result": "x"}


@pytest.mark.asyncio
async def test_cancelling_mid_batch_stops_tools_and_sends_nothing_more(monkeypatch) -> None:
    socket = FakeSocket(
        [event(tool_requests=[read("fast", "a.txt"), read("slow", "b.txt")]), final()]
    )
    install(monkeypatch, socket)
    cancelled = asyncio.Event()

    async def execute(tool_request):
        if tool_request.tool_call_id == "fast":
            return {"result": "fast"}
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {"result": "slow"}

    stream = client().stream(request(), execute)
    seen = []
    async for item in stream:
        seen.append(item.kind)
        if item.kind is EventKind.TOOL_RESULT:
            break
    await stream.aclose()
    assert cancelled.is_set()
    assert [frame["tool_call_id"] for frame in socket.sent[1:]] == ["fast"]
    assert seen == [EventKind.TOOL_REQUEST, EventKind.TOOL_REQUEST, EventKind.TOOL_RESULT]


@pytest.mark.asyncio
async def test_approval_prompts_are_not_raced(tmp_path) -> None:
    prompting = 0
    peak = 0
    prompted: list[str] = []

    async def approve(approval):
        nonlocal prompting, peak
        prompting += 1
        peak = max(peak, prompting)
        prompted.append(approval.tool_call_id)
        await asyncio.sleep(0.02)
        prompting -= 1
        return True

    broker = LocalToolBroker(tmp_path, mode=ApprovalMode.MANUAL, approve=approve)
    from copper_pilot_cli.copper_protocol import ToolRequest

    requests = [
        ToolRequest(
            tool_call_id=f"w{index}",
            tool_name="write",
            arguments={"file_path": f"file{index}.txt", "content": str(index)},
        )
        for index in range(3)
    ]
    outcomes = [
        outcome
        async for _request, outcome in run_tool_batch(
            requests, broker.execute, workspace=str(tmp_path)
        )
    ]
    assert peak == 1
    assert sorted(prompted) == ["w0", "w1", "w2"]
    assert all(not isinstance(outcome, BaseException) for outcome in outcomes)
    for index in range(3):
        assert (tmp_path / f"file{index}.txt").read_text() == str(index)
