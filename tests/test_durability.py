"""Client-side turn durability: classification, close codes, retry, backgrounding."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidHandshake, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

from copper_pilot_cli import copper_graph
from copper_pilot_cli.copper_background import (
    OUTPUT_LIMIT_ERROR,
    BackgroundProcessManager,
    run_shell,
)
from copper_pilot_cli.copper_graph import (
    BACKGROUND_CONTINUATION_PREAMBLE,
    CANCELLED_CONTEXT,
    MAX_TURN_ATTEMPTS,
    RESUME_PREFIX,
    RETRY_EVENT,
    create_copper_graph,
    retry_label,
)
from copper_pilot_cli.copper_protocol import (
    ChatRequest,
    CopperEffort,
    CopperEvent,
    CopperMode,
    EventKind,
    HostedChatClient,
    ProtocolError,
    ToolRequest,
    classify_error,
)
from copper_pilot_cli.copper_status_events import COPPER_CONTEXT_KEY
from copper_pilot_cli.copper_tools import ApprovalMode, LocalToolBroker

PY = sys.executable


def py(code: str) -> str:
    return f'"{PY}" -c "{code}"'


def request() -> ChatRequest:
    return ChatRequest(
        query="review this",
        workspace_path="/tmp/project",  # noqa: S108
        conversation_id="conversation",
        mode=CopperMode.AGENT,
    )


def dropped(code: int = 1011, reason: str = "pod restart") -> ConnectionClosedError:
    return ConnectionClosedError(Close(code, reason), None)


def refused(status: int) -> InvalidStatus:
    """The websockets exception for a handshake answered with an HTTP status."""
    return InvalidStatus(Response(status, "Service Unavailable", Headers()))


# ------------------------------------------------------------ classification


@pytest.mark.parametrize(
    ("exc", "kind", "retryable"),
    [
        (ProtocolError("t", code="CHAT_FIRST_FRAME_TIMEOUT"), "timeout", True),
        (ProtocolError("t", code="CHAT_IDLE_TIMEOUT"), "timeout", True),
        (ProtocolError("d", code="STREAM_ENDED_WITHOUT_FINAL"), "connection", True),
        (ProtocolError("i", code="chat_request_invalid", status=400), "invalid_request", False),
        (ProtocolError("p", code="PAYLOAD_TOO_LARGE"), "payload_too_large", False),
        (ProtocolError("p", code="http_413", status=413), "payload_too_large", False),
        (ProtocolError("a", code="http_401", status=401), "authentication", False),
        (ProtocolError("u", code="http_402", status=402), "usage_limit", False),
        (ProtocolError("r", code="http_429", status=429), "rate_limit", True),
        (ProtocolError("s", code="http_500", status=500), "server", True),
        (ProtocolError("s", code="http_500", status=500, retryable=False), "server", False),
        (ProtocolError("b", code="http_422", status=422), "invalid_request", False),
        (ProtocolError("malformed"), "protocol", False),
        (TimeoutError(), "timeout", True),
        (dropped(), "connection", True),
        (ConnectionResetError("reset"), "network", True),
        (refused(503), "server", True),
        (refused(502), "server", True),
        (refused(429), "rate_limit", True),
        (refused(401), "authentication", False),
        (refused(403), "authentication", False),
        (refused(402), "usage_limit", False),
        (refused(404), "invalid_request", False),
        (InvalidHandshake("connection closed while reading HTTP status line"), "connection", True),
        (ValueError("odd"), "unknown", False),
        (asyncio.CancelledError(), "aborted", False),
    ],
)
def test_classify_error_mirrors_desktop_categories(exc, kind, retryable) -> None:
    info = classify_error(exc)
    assert info.type == kind
    assert info.retryable is retryable


def test_classify_connection_closed_names_the_close_code() -> None:
    assert "close code 1011 received: pod restart" in classify_error(dropped()).message
    assert "no close frame" in classify_error(ConnectionClosedError(None, None)).message


def test_retry_labels_read_naturally() -> None:
    info = classify_error(ProtocolError("gone", code="STREAM_ENDED_WITHOUT_FINAL"))
    assert retry_label("scheduled", 2, 4, 5.0, info, True) == "Retrying (1/3) in 5s: gone"
    assert retry_label("restarted", 2, 4, 0, None, True) == "Restarting attempt 2/4"
    assert retry_label("failed", 4, 4, 0, info, False) == "Attempt 4/4 failed: gone"


# --------------------------------------------------------------- transport


class _Socket:
    def __init__(self, frames: list[Any], *, send_error: BaseException | None = None) -> None:
        self.frames = list(frames)
        self.send_error = send_error
        self.sent: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def send(self, value):
        self.sent.append(value)
        if self.send_error is not None and '"tool_result"' in value:
            raise self.send_error

    async def recv(self):
        item = self.frames.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.mark.asyncio
async def test_drop_between_frames_reports_close_code(monkeypatch) -> None:
    socket = _Socket(
        [json.dumps({"type": "event", "chat_response": {"response": "hi"}}), dropped()]
    )
    monkeypatch.setattr("copper_pilot_cli.copper_protocol.connect", lambda *a, **k: socket)
    client = HostedChatClient("https://example.test", "cf_live_key", "fingerprint")
    assert client.idle_frame_timeout is None
    events = []
    with pytest.raises(ProtocolError) as caught:
        async for event in client.stream(request(), lambda _request: None):
            events.append(event)
    assert [event.kind for event in events] == [EventKind.TEXT]
    assert caught.value.code == "STREAM_ENDED_WITHOUT_FINAL"
    assert caught.value.retryable is True
    assert "close code 1011 received: pod restart" in str(caught.value)


@pytest.mark.asyncio
async def test_drop_while_sending_tool_result_reports_close_code(monkeypatch) -> None:
    socket = _Socket(
        [
            json.dumps(
                {
                    "type": "event",
                    "chat_response": {
                        "tool_request": {
                            "tool_call_id": "call-1",
                            "tool_name": "read_file",
                            "arguments": {"file_path": "a.txt"},
                        }
                    },
                }
            )
        ],
        send_error=ConnectionClosedError(None, None),
    )
    monkeypatch.setattr("copper_pilot_cli.copper_protocol.connect", lambda *a, **k: socket)
    client = HostedChatClient("https://example.test", "cf_live_key", "fingerprint")

    async def handler(_request):
        return {"result": "content"}

    kinds = []
    with pytest.raises(ProtocolError, match="no close frame"):
        async for event in client.stream(request(), handler):
            kinds.append(event.kind)
    assert kinds == [EventKind.TOOL_REQUEST]


@pytest.mark.asyncio
async def test_first_frame_timeout_carries_a_code(monkeypatch) -> None:
    class Socket(_Socket):
        async def recv(self):
            await asyncio.sleep(1)

    monkeypatch.setattr("copper_pilot_cli.copper_protocol.connect", lambda *a, **k: Socket([]))
    client = HostedChatClient("https://example.test", "cf_live_key", "fingerprint")
    client.first_frame_timeout = 0.01
    with pytest.raises(ProtocolError) as caught:
        _ = [event async for event in client.stream(request(), lambda _request: None)]
    assert caught.value.code == "CHAT_FIRST_FRAME_TIMEOUT"


@pytest.mark.asyncio
async def test_pre_first_event_retry_skips_deterministic_failures(monkeypatch) -> None:
    attempts = 0

    def connect(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        return _Socket(
            [
                json.dumps(
                    {
                        "type": "error",
                        "code": "chat_request_invalid",
                        "status": 400,
                        "message": "bad",
                    }
                )
            ]
        )

    monkeypatch.setattr("copper_pilot_cli.copper_protocol.connect", connect)
    client = HostedChatClient("https://example.test", "cf_live_key", "fingerprint")
    with pytest.raises(ProtocolError):
        _ = [event async for event in client.stream(request(), lambda _request: None)]
    assert attempts == 1


@pytest.mark.asyncio
async def test_pre_first_event_retry_covers_a_refused_handshake(monkeypatch) -> None:
    attempts = 0

    def connect(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise refused(503)
        return _Socket([json.dumps({"type": "final", "success": True})])

    monkeypatch.setattr("copper_pilot_cli.copper_protocol.connect", connect)
    client = HostedChatClient("https://example.test", "cf_live_key", "fingerprint")
    events = [event async for event in client.stream(request(), lambda _request: None)]
    assert attempts == 2
    assert events[-1].kind is EventKind.FINAL


@pytest.mark.asyncio
async def test_pre_first_event_retry_gives_up_on_a_refused_auth_handshake(monkeypatch) -> None:
    attempts = 0

    def connect(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise refused(401)

    monkeypatch.setattr("copper_pilot_cli.copper_protocol.connect", connect)
    client = HostedChatClient("https://example.test", "cf_live_key", "fingerprint")
    with pytest.raises(InvalidStatus):
        _ = [event async for event in client.stream(request(), lambda _request: None)]
    assert attempts == 1


# ------------------------------------------------------------------- graph


@dataclass
class ScriptedAgent:
    """Fake hosted agent whose attempts are scripted lists of events or exceptions."""

    attempts: list[list[Any]]
    mode: CopperMode = CopperMode.AGENT
    effort: CopperEffort = CopperEffort.BALANCED
    cancel_requested: bool = False
    tool_broker: Any = None
    inputs: list[list[Any]] = field(default_factory=list)
    cancel_on_attempt: int | None = None

    async def astream_events_raw(self, value):
        self.inputs.append(list(value["messages"]))
        script = self.attempts.pop(0)
        for item in script:
            if isinstance(item, BaseException):
                if self.cancel_on_attempt == len(self.inputs):
                    self.cancel_requested = True
                raise item
            yield item

    async def acancel(self):
        self.cancel_requested = True


def tool_request(identifier: str = "call-1") -> CopperEvent:
    return CopperEvent(
        EventKind.TOOL_REQUEST,
        ToolRequest(tool_call_id=identifier, tool_name="read_file", arguments={"file_path": "a"}),
    )


def tool_result(identifier: str = "call-1") -> CopperEvent:
    return CopperEvent(
        EventKind.TOOL_RESULT,
        {"tool_call_id": identifier, "tool_name": "read_file", "result": {"result": "(sch)"}},
    )


DROP = ProtocolError(
    "Chat socket closed before a final response (close code 1011 received).",
    code="STREAM_ENDED_WITHOUT_FINAL",
    retryable=True,
)


async def run_graph(agent, monkeypatch, tmp_path, *, thread: str = "t"):
    monkeypatch.setenv("COPPER_PILOT_CLI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("copper_pilot_cli.sessions._db_path", None)
    monkeypatch.setattr(copper_graph, "RETRY_BACKOFF_SECONDS", (0.0,) * MAX_TURN_ATTEMPTS)
    workspace = tmp_path / "project"
    workspace.mkdir(exist_ok=True)
    async with create_copper_graph(agent) as runtime:
        config = runtime.config(thread, workspace)
        events = [
            data
            async for _ns, mode, data in runtime.astream(
                {"messages": [HumanMessage(content="Review it")], "copper_mode": "agent"},
                config,
            )
            if mode == "custom" and isinstance(data, dict)
        ]
        state = await runtime.aget_state(config)
    return events, state.values["messages"]


def retry_stages(events: list[dict[str, Any]]) -> list[tuple[str, int, bool]]:
    return [
        (e["data"]["stage"], e["data"]["attempt"], e["data"]["will_retry"])
        for e in events
        if e["type"] == RETRY_EVENT
    ]


@pytest.mark.asyncio
async def test_dropped_stream_resumes_with_partial_transcript(monkeypatch, tmp_path) -> None:
    agent = ScriptedAgent(
        [
            [
                CopperEvent(EventKind.TEXT, "Partial "),
                tool_request(),
                tool_result(),
                CopperEvent(EventKind.TEXT, "tail"),
                DROP,
            ],
            [
                CopperEvent(EventKind.TEXT, " done."),
                CopperEvent(EventKind.FINAL, {"success": True}),
            ],
        ]
    )
    events, messages = await run_graph(agent, monkeypatch, tmp_path)

    assert retry_stages(events) == [
        ("failed", 1, True),
        ("scheduled", 2, True),
        ("restarted", 2, True),
    ]
    assert not [e for e in events if e["type"] == "copper.error"]

    resumed = agent.inputs[1]
    assert resumed[0].content == "Review it"
    assert isinstance(resumed[-1], HumanMessage)
    assert resumed[-1].content == f"{RESUME_PREFIX}Review it"
    assert isinstance(resumed[-2], AIMessage)
    assert "tail" in resumed[-2].content
    assert "[Completed before the interruption: read_file (ok)]" in resumed[-2].content
    assert any(isinstance(message, ToolMessage) for message in resumed)

    assert [type(message).__name__ for message in messages] == [
        "HumanMessage",
        "AIMessage",
        "ToolMessage",
        "AIMessage",
    ]
    assert messages[1].tool_calls[0]["id"] == "call-1"
    assert messages[-1].content == "tail done."
    assert COPPER_CONTEXT_KEY not in messages[-1].additional_kwargs


@pytest.mark.asyncio
async def test_deterministic_failure_persists_partial_work_without_retry(
    monkeypatch, tmp_path
) -> None:
    agent = ScriptedAgent(
        [
            [
                CopperEvent(EventKind.TEXT, "half"),
                tool_request(),
                ProtocolError("Request invalid.", code="chat_request_invalid", status=400),
            ]
        ]
    )
    events, messages = await run_graph(agent, monkeypatch, tmp_path)

    assert retry_stages(events) == [("failed", 1, False)]
    errors = [e for e in events if e["type"] == "copper.error"]
    assert len(errors) == 1
    assert errors[0]["data"]["message"] == "Request invalid."
    assert errors[0]["data"]["type"] == "invalid_request"
    assert errors[0]["data"]["status"] == 400
    assert len(agent.inputs) == 1

    assert messages[1].tool_calls[0]["id"] == "call-1"
    assert isinstance(messages[2], ToolMessage) and messages[2].status == "error"
    assert messages[-1].additional_kwargs[COPPER_CONTEXT_KEY] == (
        "[Turn interrupted before completion: Request invalid.]"
    )


@pytest.mark.asyncio
async def test_refused_reconnect_after_a_drop_is_retried(monkeypatch, tmp_path) -> None:
    # The T3 Code failure: the pod restarts mid-stream, the reconnect 5 s later is
    # refused with HTTP 503 while the backend is still coming back, and the next
    # attempt succeeds. Before the fix the 503 was "unknown" and ended the turn.
    agent = ScriptedAgent(
        [
            [CopperEvent(EventKind.TEXT, "Half"), DROP],
            [refused(503)],
            [CopperEvent(EventKind.TEXT, "done"), CopperEvent(EventKind.FINAL, {"success": True})],
        ]
    )
    events, messages = await run_graph(agent, monkeypatch, tmp_path)
    stages = retry_stages(events)
    assert ("failed", 1, True) in stages
    assert ("failed", 2, True) in stages
    assert stages[-1] == ("restarted", 3, True)
    assert len(agent.inputs) == 3
    assert not [e for e in events if e["type"] == "copper.error"]
    assert isinstance(messages[-1], AIMessage)
    assert "done" in str(messages[-1].content)


@pytest.mark.asyncio
async def test_retries_are_exhausted_after_max_attempts(monkeypatch, tmp_path) -> None:
    agent = ScriptedAgent([[DROP] for _ in range(MAX_TURN_ATTEMPTS)])
    events, messages = await run_graph(agent, monkeypatch, tmp_path)
    stages = retry_stages(events)
    assert stages[-1] == ("failed", MAX_TURN_ATTEMPTS, False)
    assert len(agent.inputs) == MAX_TURN_ATTEMPTS
    errors = [e for e in events if e["type"] == "copper.error"]
    assert len(errors) == 1
    assert "close code 1011" in errors[0]["data"]["message"]
    assert messages[-1].additional_kwargs[COPPER_CONTEXT_KEY].startswith("[Turn interrupted")


@pytest.mark.asyncio
async def test_hosted_error_frame_is_forwarded_once_when_terminal(monkeypatch, tmp_path) -> None:
    payload = {"message": "Upgrade to a paid plan.", "status": 402, "block_reason": "weekly"}
    agent = ScriptedAgent(
        [
            [
                CopperEvent(EventKind.ERROR, payload),
                ProtocolError("Upgrade to a paid plan.", status=402, retryable=False),
            ]
        ]
    )
    events, _messages = await run_graph(agent, monkeypatch, tmp_path)
    errors = [e for e in events if e["type"] == "copper.error"]
    assert [e["data"] for e in errors] == [payload]
    assert len(agent.inputs) == 1


@pytest.mark.asyncio
async def test_retryable_hosted_error_frame_is_held_back_until_retry_succeeds(
    monkeypatch, tmp_path
) -> None:
    agent = ScriptedAgent(
        [
            [
                CopperEvent(EventKind.ERROR, {"message": "overloaded", "status": 503}),
                ProtocolError("overloaded", status=503),
            ],
            [CopperEvent(EventKind.TEXT, "ok"), CopperEvent(EventKind.FINAL, {"success": True})],
        ]
    )
    events, messages = await run_graph(agent, monkeypatch, tmp_path)
    assert not [e for e in events if e["type"] == "copper.error"]
    assert messages[-1].content == "ok"


@pytest.mark.asyncio
async def test_cancelled_turn_is_not_retried_and_keeps_progress(monkeypatch, tmp_path) -> None:
    agent = ScriptedAgent(
        [[CopperEvent(EventKind.TEXT, "working"), DROP], [CopperEvent(EventKind.TEXT, "never")]],
        cancel_on_attempt=1,
    )
    events, messages = await run_graph(agent, monkeypatch, tmp_path)
    assert retry_stages(events) == []
    assert not [e for e in events if e["type"] == "copper.error"]
    assert len(agent.inputs) == 1
    assert messages[-1].content == "working"
    assert messages[-1].additional_kwargs[COPPER_CONTEXT_KEY] == CANCELLED_CONTEXT


@pytest.mark.asyncio
async def test_background_processes_feed_a_continuation_turn(monkeypatch, tmp_path) -> None:
    manager = BackgroundProcessManager()
    started = await run_shell(
        py("import time; time.sleep(0.3); print('bg done')"),
        tmp_path,
        manager=manager,
        foreground_seconds=0.05,
    )
    assert started["background"] is True
    agent = ScriptedAgent(
        [
            [CopperEvent(EventKind.TEXT, "Started it."), CopperEvent(EventKind.FINAL, {})],
            [CopperEvent(EventKind.TEXT, "Process finished."), CopperEvent(EventKind.FINAL, {})],
        ],
        tool_broker=SimpleNamespace(background=manager),
    )
    events, messages = await run_graph(agent, monkeypatch, tmp_path)

    waiting = [e for e in events if e.get("title") == "background_processes_waiting"]
    assert waiting and "1 background process" in waiting[0]["data"]
    continuation = agent.inputs[1][-1]
    assert isinstance(continuation, HumanMessage)
    assert continuation.content.startswith(BACKGROUND_CONTINUATION_PREAMBLE)
    assert "bg done" in continuation.content
    assert '"status": "completed"' in continuation.content
    assert messages[-1].content == "Started it.Process finished."
    assert "bg done" in messages[-1].additional_kwargs[COPPER_CONTEXT_KEY]
    assert not manager.has_running()
    assert manager.processes() == []


@pytest.mark.asyncio
async def test_background_process_that_finishes_before_the_final_frame_still_resumes(
    monkeypatch, tmp_path
) -> None:
    """A shell that exits while the model is still working must resume the agent.

    Waiting only for processes that are still running drops the output. The
    tool result already told the model the client would deliver it.
    """
    manager = BackgroundProcessManager()
    started = await run_shell(
        py("import time; time.sleep(0.15); print('early done')"),
        tmp_path,
        manager=manager,
        foreground_seconds=0.05,
    )
    assert started["background"] is True
    await manager.wait_all()
    assert not manager.has_running()
    assert manager.processes()[0]["status"] == "completed"

    agent = ScriptedAgent(
        [
            [CopperEvent(EventKind.TEXT, "Started it."), CopperEvent(EventKind.FINAL, {})],
            [CopperEvent(EventKind.TEXT, "Saw the output."), CopperEvent(EventKind.FINAL, {})],
        ],
        tool_broker=SimpleNamespace(background=manager),
    )
    events, messages = await run_graph(agent, monkeypatch, tmp_path)

    assert [e for e in events if e.get("title") == "background_processes_waiting"] == []
    assert len(agent.inputs) == 2
    continuation = agent.inputs[1][-1]
    assert isinstance(continuation, HumanMessage)
    assert continuation.content.startswith(BACKGROUND_CONTINUATION_PREAMBLE)
    assert "early done" in continuation.content
    assert '"status": "completed"' in continuation.content
    assert messages[-1].content == "Started it.Saw the output."
    assert "early done" in messages[-1].additional_kwargs[COPPER_CONTEXT_KEY]
    assert manager.processes() == []


@pytest.mark.asyncio
async def test_finished_and_running_background_processes_resume_together(
    monkeypatch, tmp_path
) -> None:
    manager = BackgroundProcessManager()
    early = await run_shell(
        py("import time; time.sleep(0.1); print('early')"),
        tmp_path,
        manager=manager,
        foreground_seconds=0.02,
    )
    assert early["background"] is True
    await manager.wait_all()
    assert not manager.has_running()
    late = await run_shell(
        py("import time; time.sleep(0.4); print('late')"),
        tmp_path,
        manager=manager,
        foreground_seconds=0.05,
    )
    assert late["background"] is True
    assert manager.has_running()

    agent = ScriptedAgent(
        [
            [CopperEvent(EventKind.TEXT, "Started both."), CopperEvent(EventKind.FINAL, {})],
            [CopperEvent(EventKind.TEXT, "Both done."), CopperEvent(EventKind.FINAL, {})],
        ],
        tool_broker=SimpleNamespace(background=manager),
    )
    events, messages = await run_graph(agent, monkeypatch, tmp_path)

    waiting = [e for e in events if e.get("title") == "background_processes_waiting"]
    assert waiting and "1 background process" in waiting[0]["data"]
    continuation = agent.inputs[1][-1].content
    assert "early" in continuation and "late" in continuation
    assert messages[-1].content == "Started both.Both done."
    assert manager.processes() == []


@pytest.mark.skipif(sys.platform == "win32", reason="process-group signals are POSIX-only")
@pytest.mark.asyncio
async def test_background_process_killed_before_the_final_frame_does_not_resume(
    monkeypatch, tmp_path
) -> None:
    manager = BackgroundProcessManager()
    started = await run_shell(
        py("import time; time.sleep(30)"),
        tmp_path,
        manager=manager,
        foreground_seconds=0.05,
    )
    assert started["background"] is True
    os.killpg(os.getpgid(started["pid"]), signal.SIGKILL)
    await manager.wait_all()
    assert manager.processes()[0]["status"] == "killed"

    agent = ScriptedAgent(
        [
            [CopperEvent(EventKind.TEXT, "Started it."), CopperEvent(EventKind.FINAL, {})],
            [CopperEvent(EventKind.TEXT, "should not run"), CopperEvent(EventKind.FINAL, {})],
        ],
        tool_broker=SimpleNamespace(background=manager),
    )
    _events, messages = await run_graph(agent, monkeypatch, tmp_path)

    assert len(agent.inputs) == 1
    assert messages[-1].content == "Started it."
    assert messages[-1].additional_kwargs[COPPER_CONTEXT_KEY] == CANCELLED_CONTEXT
    assert manager.processes() == []


# -------------------------------------------------------------- background


@pytest.mark.asyncio
async def test_run_shell_reports_exit_code_like_the_native_tool(tmp_path) -> None:
    manager = BackgroundProcessManager()
    ok = await run_shell(py("print('hi')"), tmp_path, manager=manager)
    assert ok == {"result": "hi", "exit_code": 0}
    failed = await run_shell(py("import sys; sys.exit(7)"), tmp_path, manager=manager)
    assert failed["exit_code"] == 7
    assert "Exit code: 7" in failed["result"]
    assert (await run_shell("   ", tmp_path, manager=manager))["error"]


@pytest.mark.asyncio
async def test_run_shell_kill_on_timeout(tmp_path) -> None:
    manager = BackgroundProcessManager()
    result = await run_shell(
        py("import time; print('tick', flush=True); time.sleep(5)"),
        tmp_path,
        manager=manager,
        foreground_seconds=0.3,
        kill_on_timeout=True,
    )
    assert result["error"] == "Command timed out"
    assert result["timed_out"] is True
    assert result["result"] == "tick"
    assert not manager.has_running()


@pytest.mark.asyncio
async def test_run_shell_backgrounds_and_manager_waits_or_kills(tmp_path) -> None:
    manager = BackgroundProcessManager()
    result = await run_shell(
        py("import time; print('start', flush=True); time.sleep(0.4); print('end')"),
        tmp_path,
        manager=manager,
        foreground_seconds=0.1,
    )
    assert result["background"] is True
    assert "running in the background" in result["result"]
    assert "start" in result["result"]
    assert manager.has_running()
    finished = await manager.wait_all()
    assert finished[0]["status"] == "completed"
    assert finished[0]["exit_code"] == 0
    assert finished[0]["output"] == "start\nend"
    assert manager.drain() == finished
    assert manager.processes() == []

    result = await run_shell(
        py("import time; time.sleep(10)"), tmp_path, manager=manager, foreground_seconds=0.05
    )
    assert manager.has_running()
    await manager.kill_all()
    assert not manager.has_running()
    assert manager.processes() == []


@pytest.mark.asyncio
async def test_run_shell_caps_output(tmp_path) -> None:
    manager = BackgroundProcessManager()
    result = await run_shell(py("print('x' * 1000)"), tmp_path, manager=manager, output_limit=10)
    assert result["error"] == OUTPUT_LIMIT_ERROR
    assert result["result"] == "x" * 10


@pytest.mark.asyncio
async def test_broker_execute_uses_the_background_contract(tmp_path) -> None:
    broker = LocalToolBroker(tmp_path, mode=ApprovalMode.YOLO)
    broker.foreground_seconds = 0.1
    result = await broker.execute(
        ToolRequest(
            tool_call_id="bg",
            tool_name="bash",
            arguments={"command": py("import time; time.sleep(5)"), "description": "wait"},
        )
    )
    assert result["background"] is True
    assert broker.background.has_running()
    await broker.cancel_all()
    assert not broker.background.has_running()

    killed = await broker.execute(
        ToolRequest(
            tool_call_id="kill",
            tool_name="bash",
            arguments={"command": py("import time; time.sleep(5)"), "kill_on_timeout": True},
            timeout_ms=100,
        )
    )
    assert killed["timed_out"] is True
