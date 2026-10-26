from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from copper_pilot_cli.copper_btw import (
    MAX_SNAPSHOT_TOOLS,
    MAX_TOOL_DENIALS,
    NOTHING_EXECUTED,
    TOOL_DENIED,
    LiveTool,
    LiveTurn,
    SideQuestionError,
    SideQuestionLog,
    build_side_request,
    live_snapshot,
    run_side_question,
)
from copper_pilot_cli.copper_protocol import (
    CopperEffort,
    CopperEvent,
    CopperMode,
    EventKind,
    ProtocolError,
    ToolRequest,
)

CREDENTIAL = SimpleNamespace(base_url="http://localhost", api_key="key", fingerprint="fp")


class FakeClient:
    def __init__(self, script, *, hang: bool = False, fail: BaseException | None = None):
        self.script = script
        self.hang = hang
        self.fail = fail
        self.cancelled = 0
        self.results: list[dict] = []
        self.requests = []

    async def cancel(self) -> None:
        self.cancelled += 1

    async def stream(self, request, handler):
        self.requests.append(request)
        for item in self.script:
            if isinstance(item, ToolRequest):
                self.results.append(await handler(item))
            else:
                yield CopperEvent(EventKind.TEXT, item)
        if self.fail is not None:
            raise self.fail
        if self.hang:
            await asyncio.sleep(10)


def _tool(index: int = 0) -> ToolRequest:
    return ToolRequest(tool_call_id=f"call-{index}", tool_name="read_file", arguments={})


def _request():
    return build_side_request("what?", history=[], snapshot="", earlier=[], workspace=Path("."))


def test_log_keeps_newest_last_and_bounds_history() -> None:
    log = SideQuestionLog(limit=3)
    entries = [log.add(f"q{index}") for index in range(5)]
    assert [entry.question for entry in log.entries] == ["q2", "q3", "q4"]
    assert log.current() is entries[-1]
    assert log.index == 2


def test_log_navigation_clamps_at_both_ends() -> None:
    log = SideQuestionLog()
    assert log.older() is None and log.newer() is None
    for index in range(3):
        log.add(f"q{index}")
    assert log.newer().question == "q2"
    assert log.older().question == "q1"
    assert log.older().question == "q0"
    assert log.older().question == "q0"
    assert [entry.question for entry in log.earlier_than_current(3)] == []
    assert log.latest().question == "q2"
    assert [entry.question for entry in log.earlier_than_current(1)] == ["q1"]


def test_log_resolve_pending_completed_and_clear() -> None:
    log = SideQuestionLog()
    first = log.add("one")
    second = log.add("two")
    assert log.pending() == 2
    log.resolve(first.id, "answer")
    log.resolve(second.id, "boom", failed=True)
    assert first.status == "done" and second.status == "error"
    assert log.completed() == [first]
    assert log.resolve(999, "missing") is None
    log.clear()
    assert len(log) == 0 and log.current() is None


def test_live_snapshot_summarizes_running_turn() -> None:
    tools = [
        LiveTool("read_file", {"path": f"f{index}.kicad_sch"}, "success") for index in range(40)
    ]
    tools.append(LiveTool("execute", {"command": "kicad-cli sch erc"}, "running"))
    text = live_snapshot(
        LiveTurn(
            running=True,
            query="Review the power tree",
            status="Running ERC",
            elapsed_s=42,
            text="x" * 5000 + "TAIL",
            tools=tools,
            subagents=[("Survey regulators", "running")],
        )
    )
    assert "(42s)" in text
    assert "Review the power tree" in text
    assert "Now: Running ERC" in text
    assert "`kicad-cli sch erc`: running" in text
    assert f"({len(tools) - MAX_SNAPSHOT_TOOLS} earlier tool calls omitted)" in text
    assert "f0.kicad_sch" not in text
    assert "Survey regulators: running" in text
    assert text.endswith("TAIL")


def test_live_snapshot_when_idle() -> None:
    text = live_snapshot(LiveTurn(running=False, query="last ask"))
    assert "No turn is running" in text
    assert "last ask" in text


def test_build_side_request_is_tool_less_ask_on_fresh_conversation(tmp_path) -> None:
    log = SideQuestionLog()
    done = log.add("earlier?")
    log.resolve(done.id, "earlier answer")
    log.add("still pending")
    history = [
        HumanMessage(content="Check the LDO"),
        AIMessage(content="", tool_calls=[{"id": "t1", "name": "read_file", "args": {}}]),
        ToolMessage(content="file", tool_call_id="t1"),
        AIMessage(content="The LDO is fine."),
    ]
    request = build_side_request(
        "what is it doing?",
        history=history,
        snapshot="SNAPSHOT",
        earlier=log.completed(),
        workspace=tmp_path,
        schematic_path="board.kicad_sch",
    )
    assert request.mode is CopperMode.ASK
    assert request.effort is CopperEffort.BALANCED
    assert request.model is None
    assert request.agentic_mode is False
    uuid.UUID(request.conversation_id)
    assert request.workflow is None and request.plan_context is None
    assert request.runtime_context is None and request.skill_manifest_id is None
    assert request.initial_file_tree is None
    assert request.schematic_path == "board.kicad_sch"
    assert "what is it doing?" in request.query and "NO tools" in request.query
    roles = [(message.role, message.content) for message in request.context]
    assert roles[0] == ("user", "Check the LDO")
    assert roles[1] == ("assistant", "The LDO is fine.")
    assert roles[2][0] == "system" and "SNAPSHOT" in roles[2][1]
    assert roles[3] == ("user", "(side question) earlier?")
    assert roles[4] == ("assistant", "earlier answer")
    assert len(roles) == 5


def test_each_side_request_gets_its_own_conversation(tmp_path) -> None:
    first = build_side_request("a", history=[], snapshot="", earlier=[], workspace=tmp_path)
    second = build_side_request("b", history=[], snapshot="", earlier=[], workspace=tmp_path)
    assert first.conversation_id != second.conversation_id


@pytest.mark.asyncio
async def test_runner_collects_text_on_its_own_client() -> None:
    clients: list[FakeClient] = []

    def factory(credential):
        assert credential is CREDENTIAL
        clients.append(FakeClient(["It is ", "running ERC."]))
        return clients[-1]

    answer = await run_side_question(CREDENTIAL, _request(), client_factory=factory)
    assert answer == "It is running ERC."
    assert len(clients) == 1 and clients[0].cancelled == 0


@pytest.mark.asyncio
async def test_runner_denies_tools_and_notes_nothing_ran() -> None:
    client = FakeClient([_tool(), "From context: ERC."])
    answer = await run_side_question(CREDENTIAL, _request(), client_factory=lambda _c: client)
    assert client.results == [{"error": TOOL_DENIED, "rejected": True}]
    assert answer.startswith("From context: ERC.")
    assert answer.endswith(NOTHING_EXECUTED)
    assert client.cancelled == 0


@pytest.mark.asyncio
async def test_runner_cancels_after_repeated_tool_requests() -> None:
    script = [_tool(index) for index in range(MAX_TOOL_DENIALS + 1)]
    client = FakeClient(script)
    answer = await run_side_question(CREDENTIAL, _request(), client_factory=lambda _c: client)
    assert client.cancelled == 1
    assert len(client.results) == MAX_TOOL_DENIALS + 1
    assert answer == NOTHING_EXECUTED


@pytest.mark.asyncio
async def test_runner_keeps_text_when_stream_errors_after_cutoff() -> None:
    script = ["Partial answer.", *[_tool(index) for index in range(MAX_TOOL_DENIALS)]]
    client = FakeClient(script, fail=ProtocolError("closed", code="STREAM_ENDED_WITHOUT_FINAL"))
    answer = await run_side_question(CREDENTIAL, _request(), client_factory=lambda _c: client)
    assert answer.startswith("Partial answer.")


@pytest.mark.asyncio
async def test_runner_maps_usage_limit_errors() -> None:
    error = ProtocolError("Usage limit reached.", status=402, block_reason="weekly")
    client = FakeClient([], fail=error)
    with pytest.raises(SideQuestionError) as caught:
        await run_side_question(CREDENTIAL, _request(), client_factory=lambda _c: client)
    assert "limit" in str(caught.value).lower()


@pytest.mark.asyncio
async def test_runner_times_out_without_text() -> None:
    client = FakeClient([], hang=True)
    with pytest.raises(SideQuestionError, match="No answer within"):
        await run_side_question(
            CREDENTIAL, _request(), limit_s=0.05, client_factory=lambda _c: client
        )


@pytest.mark.asyncio
async def test_runner_requires_login_and_an_answer() -> None:
    with pytest.raises(SideQuestionError, match="Not authenticated"):
        await run_side_question(None, _request(), client_factory=lambda _c: FakeClient([]))
    with pytest.raises(SideQuestionError, match="No answer"):
        await run_side_question(CREDENTIAL, _request(), client_factory=lambda _c: FakeClient([]))


@pytest.mark.asyncio
async def test_runner_reports_network_failures() -> None:
    client = FakeClient([], fail=OSError("connection refused"))
    with pytest.raises(SideQuestionError, match="Could not reach CopperPilot"):
        await run_side_question(CREDENTIAL, _request(), client_factory=lambda _c: client)
