"""Turn resume protocol v1: transport liveness, resumable finals, journal and resume."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from copper_pilot_cli import copper_graph
from copper_pilot_cli.copper_graph import (
    MAX_TURN_ATTEMPTS,
    NO_UNFINISHED_TURN,
    RESUME_EVENT,
    _TurnRun,
    create_copper_graph,
    load_unfinished_turn,
)
from copper_pilot_cli.copper_protocol import (
    CLIENT_LIVENESS_CLOSE_CODE,
    DEFAULT_IDLE_FRAME_TIMEOUT,
    IDLE_FRAME_TIMEOUT_ENV,
    PING_INTERVAL_SECONDS,
    PING_TIMEOUT_SECONDS,
    ChatRequest,
    CopperEffort,
    CopperEvent,
    CopperMode,
    EventKind,
    HostedChatClient,
    ProtocolError,
    ToolRequest,
    capacity_hold_seconds,
    classify_error,
    idle_frame_timeout,
    make_chat_start,
)
from copper_pilot_cli.turn_journal import (
    MAX_TOOL_EVENTS,
    MAX_TOOL_RESULT_BYTES,
    JournalStore,
    TurnJournal,
    is_resume_request,
)


def request(**extra: Any) -> ChatRequest:
    return ChatRequest(
        query="review this",
        workspace_path="/tmp/project",  # noqa: S108
        conversation_id="conversation",
        mode=CopperMode.AGENT,
        **extra,
    )


def frame(kind: str = "event", **response: Any) -> str:
    return json.dumps({"type": kind, "chat_response": response})


def dropped(code: int = 1011, reason: str = "pod restart") -> ConnectionClosedError:
    return ConnectionClosedError(Close(code, reason), None)


class Socket:
    """Fake websocket: ``recv`` hands out frames, then blocks like a quiet server."""

    def __init__(self, frames: list[Any], *, send_error: BaseException | None = None) -> None:
        self.frames = list(frames)
        self.send_error = send_error
        self.sent: list[dict[str, Any]] = []
        self.received = 0
        self.closed_with: tuple[int, str] | None = None
        self.more = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def send(self, value):
        self.sent.append(json.loads(value))
        if self.send_error is not None and '"tool_result"' in value:
            raise self.send_error

    async def recv(self):
        while not self.frames:
            self.more.clear()
            await self.more.wait()
        item = self.frames.pop(0)
        self.received += 1
        if isinstance(item, BaseException):
            raise item
        return item

    def push(self, *items: Any) -> None:
        self.frames.extend(items)
        self.more.set()

    async def close(self, code, reason):
        self.closed_with = (code, reason)


def install(monkeypatch, socket: Socket) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def connect(*_args, **kwargs):
        calls.append(kwargs)
        return socket

    monkeypatch.setattr("copper_pilot_cli.copper_protocol.connect", connect)
    return calls


def client() -> HostedChatClient:
    return HostedChatClient("https://example.test", "cf_live_key", "fingerprint")


# ----------------------------------------------------------------- transport


@pytest.mark.asyncio
async def test_socket_uses_protocol_keepalive(monkeypatch) -> None:
    socket = Socket([frame("final", success=True)])
    calls = install(monkeypatch, socket)
    _ = [event async for event in client().stream(request(), lambda _r: None)]
    assert calls[0]["ping_interval"] == PING_INTERVAL_SECONDS == 15
    assert calls[0]["ping_timeout"] == PING_TIMEOUT_SECONDS == 20


def test_idle_timeout_outlasts_the_server_quiet_timeout(monkeypatch) -> None:
    monkeypatch.delenv(IDLE_FRAME_TIMEOUT_ENV, raising=False)
    assert idle_frame_timeout() == DEFAULT_IDLE_FRAME_TIMEOUT == 330


def test_chat_start_carries_turn_id_and_resume() -> None:
    resume = {"root_turn_id": "root", "attempt": 1, "reason": "connection_lost"}
    started = make_chat_start(request(turn_id="t-2", resume=resume))
    assert started["request"]["turn_id"] == "t-2"
    assert started["request"]["resume"] == resume
    assert "turn_resume_v1" in started["capabilities"]
    assert "resume" not in make_chat_start(request())["request"]


@pytest.mark.asyncio
async def test_idle_server_is_closed_with_the_liveness_code(monkeypatch) -> None:
    monkeypatch.setenv(IDLE_FRAME_TIMEOUT_ENV, "0.05")
    socket = Socket([frame(status_update="Working")])
    install(monkeypatch, socket)
    with pytest.raises(ProtocolError) as caught:
        _ = [event async for event in client().stream(request(), lambda _r: None)]
    assert caught.value.code == "CHAT_IDLE_TIMEOUT"
    assert caught.value.resume_reason == "idle_timeout"
    assert socket.closed_with == (CLIENT_LIVENESS_CLOSE_CODE, "client_liveness_timeout")


@pytest.mark.asyncio
async def test_drop_reports_close_code_for_diagnostics(monkeypatch) -> None:
    install(monkeypatch, Socket([frame(response="hi"), dropped(1012, "service restart")]))
    with pytest.raises(ProtocolError) as caught:
        _ = [event async for event in client().stream(request(), lambda _r: None)]
    assert caught.value.close_code == 1012
    assert caught.value.close_reason == "service restart"
    assert caught.value.resume_reason == "connection_lost"


@pytest.mark.asyncio
async def test_frames_keep_being_read_while_a_tool_runs(monkeypatch) -> None:
    tool = {"tool_call_id": "c1", "tool_name": "bash", "arguments": {"command": "make"}}
    socket = Socket([frame(tool_request=tool)])
    install(monkeypatch, socket)
    release = asyncio.Event()

    async def handler(_request: ToolRequest):
        # While the tool runs the server reports progress and then ends the
        # turn; the reader must take those frames off the socket meanwhile.
        socket.push(frame(status_update="still here"), frame("final", success=True))
        for _ in range(50):
            if socket.received == 3:
                break
            await asyncio.sleep(0.01)
        release.set()
        return {"result": "built"}

    events = [event async for event in client().stream(request(turn_id="t-1"), handler)]
    assert release.is_set() and socket.received == 3
    assert [event.kind for event in events] == [
        EventKind.TOOL_REQUEST,
        EventKind.TOOL_RESULT,
        EventKind.STATUS,
        EventKind.FINAL,
    ]
    # Frames are still processed in order: the result went out before the final.
    assert [item["type"] for item in socket.sent] == ["chat_start", "tool_result"]
    assert all(event.turn_id == "t-1" for event in events)


@pytest.mark.asyncio
async def test_resumable_final_is_reported(monkeypatch) -> None:
    install(
        monkeypatch,
        Socket(
            [
                frame(
                    "final",
                    success=False,
                    is_final=True,
                    resumable=True,
                    error_code="socket_rotation",
                    error="Rotating the connection.",
                )
            ]
        ),
    )
    (final,) = [event async for event in client().stream(request(), lambda _r: None)]
    assert final.kind is EventKind.FINAL
    assert final.data["resumable"] is True
    assert final.data["error_code"] == "socket_rotation"


def capacity_wait(phase: str = "retrying", retry_in_s: float = 0.2, **extra: Any) -> dict:
    return {
        "kind": "capacity_wait",
        "phase": phase,
        "model": "gemini-3.8-flash",
        "retry_in_s": retry_in_s,
        "waited_s": extra.pop("waited_s", 0.0),
        "budget_s": extra.pop("budget_s", 0.0),
        "attempt": 1,
        "message": "CopperPilot is under high demand. Waiting to continue.",
        **extra,
    }


@pytest.mark.asyncio
async def test_capacity_wait_keeps_the_turn_alive_past_the_idle_bound(monkeypatch) -> None:
    monkeypatch.setenv(IDLE_FRAME_TIMEOUT_ENV, "0.05")
    socket = Socket([frame(status_event=capacity_wait(retry_in_s=1.0))])
    install(monkeypatch, socket)

    async def later() -> None:
        # Four idle windows of silence, then the model answers.
        await asyncio.sleep(0.2)
        socket.push(frame(response="Routed."), frame("final", success=True))

    pending = asyncio.create_task(later())
    events = [event async for event in client().stream(request(), lambda _r: None)]
    await pending
    assert [event.kind for event in events] == [
        EventKind.STATUS_EVENT,
        EventKind.TEXT,
        EventKind.FINAL,
    ]
    assert socket.closed_with is None


@pytest.mark.asyncio
async def test_capacity_hold_ends_with_the_next_normal_frame(monkeypatch) -> None:
    monkeypatch.setenv(IDLE_FRAME_TIMEOUT_ENV, "0.05")
    socket = Socket(
        [
            frame(status_event=capacity_wait(retry_in_s=5)),
            frame(status_event=capacity_wait("resumed", retry_in_s=0)),
        ]
    )
    install(monkeypatch, socket)
    with pytest.raises(ProtocolError) as caught:
        _ = [event async for event in client().stream(request(), lambda _r: None)]
    assert caught.value.code == "CHAT_IDLE_TIMEOUT"


def test_capacity_hold_covers_the_server_wait_budget() -> None:
    assert capacity_hold_seconds({"kind": "subagent"}) is None
    assert capacity_hold_seconds(capacity_wait("resumed")) == 0
    assert capacity_hold_seconds(capacity_wait(retry_in_s=20, waited_s=45, budget_s=600)) == 555
    assert capacity_hold_seconds(capacity_wait("queued", retry_in_s=0)) == 0
    assert capacity_hold_seconds(capacity_wait(retry_in_s=20)) == 20


@pytest.mark.asyncio
async def test_provider_busy_final_carries_retry_after(monkeypatch) -> None:
    install(
        monkeypatch,
        Socket(
            [
                frame(
                    "final",
                    success=False,
                    is_final=True,
                    resumable=True,
                    error_code="provider_busy",
                    retry_after_s=45,
                    error="CopperPilot is under high demand right now.",
                )
            ]
        ),
    )
    (final,) = [event async for event in client().stream(request(), lambda _r: None)]
    assert final.data["error_code"] == "provider_busy"
    assert final.data["retry_after_s"] == 45
    info = classify_error(ProtocolError("busy", code="provider_busy", resumable=True))
    assert (info.type, info.retryable) == ("provider_busy", True)


def test_resumable_protocol_errors_classify_as_resumable() -> None:
    info = classify_error(ProtocolError("rotating", code="socket_rotation", resumable=True))
    assert (info.type, info.retryable) == ("resumable", True)


@pytest.mark.asyncio
async def test_batch_members_finish_after_a_drop(monkeypatch) -> None:
    tools = [
        {"tool_call_id": "a", "tool_name": "read_file", "arguments": {"file_path": "a"}},
        {"tool_call_id": "b", "tool_name": "read_file", "arguments": {"file_path": "b"}},
    ]
    socket = Socket([frame(tool_requests=tools)], send_error=ConnectionClosedError(None, None))
    install(monkeypatch, socket)

    async def handler(tool_request: ToolRequest):
        await asyncio.sleep(0.01 if tool_request.tool_call_id == "a" else 0.05)
        return {"result": tool_request.tool_call_id}

    events = []
    with pytest.raises(ProtocolError):
        async for event in client().stream(request(), handler):
            events.append(event)
    results = [event.data for event in events if event.kind is EventKind.TOOL_RESULT]
    assert [(item["tool_call_id"], item["delivered"]) for item in results] == [
        ("a", False),
        ("b", False),
    ]


@pytest.mark.asyncio
async def test_heartbeat_reads_the_health_endpoint(monkeypatch) -> None:
    import httpx

    seen: list[str] = []

    def respond(http_request: httpx.Request) -> httpx.Response:
        seen.append(str(http_request.url))
        return httpx.Response(200 if len(seen) > 1 else 503)

    transport = httpx.MockTransport(respond)
    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    hosted = client()
    assert await hosted.heartbeat() is False
    assert await hosted.heartbeat() is True
    assert seen == ["https://example.test/api/heartbeat"] * 2


# ------------------------------------------------------------------- journal


def test_journal_maps_tool_outcomes_to_protocol_statuses() -> None:
    journal = TurnJournal(query="q", original_len=1)
    outcomes = {
        "ok": {"result": "fine"},
        "error": {"error": "boom"},
        "exit": {"result": "out", "exit_code": 2},
        "rejected": {"error": "no", "rejected": True},
    }
    for call_id, result in outcomes.items():
        journal.record_tool_request(call_id, "bash", {"command": call_id})
        journal.record_tool_result(call_id, "bash", result)
    journal.record_tool_request("running", "bash", {"command": "make"})
    events = {event["tool_call_id"]: event for event in journal.resume_payload()["tool_events"]}
    assert {key: event["status"] for key, event in events.items()} == {
        "ok": "completed",
        "error": "failed",
        "exit": "failed",
        "rejected": "failed",
        "running": "interrupted",
    }
    assert events["exit"]["result"] == "[exit code 2] out"
    assert "result" not in events["running"]


def test_journal_clips_results_and_caps_events() -> None:
    journal = TurnJournal(query="q", original_len=1)
    for index in range(MAX_TOOL_EVENTS + 5):
        journal.record_tool_request(f"c{index}", "read_file", {"file_path": "x" * 10_000})
        journal.record_tool_result(f"c{index}", "read_file", {"result": "y" * 10_000})
    payload = journal.resume_payload()
    assert len(payload["tool_events"]) == MAX_TOOL_EVENTS
    assert payload["tool_events"][0]["tool_call_id"] == "c5"
    assert len(json.dumps(payload).encode()) <= 1024 * 1024
    newest = payload["tool_events"][-1]
    assert len(newest["result"].encode()) <= MAX_TOOL_RESULT_BYTES + 32


def test_journal_round_trips_through_the_sidecar_store(tmp_path) -> None:
    store = JournalStore(tmp_path / "turns")
    journal = TurnJournal(query="q", original_len=3, thread_id="thread/1")
    journal.record_text("hello")
    store.save(journal)
    loaded = store.load("thread/1")
    assert loaded is not None and loaded.to_dict() == journal.to_dict()
    store.delete("thread/1")
    assert store.load("thread/1") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("continue", True),
        ("/resume", True),
        ("resume", True),
        ("  Continue. ", True),
        ("RESUME!", True),
        ("/continue", True),
        ("Continue where you left off", True),
        ("continue where you left off.", True),
        # Extra instructions start a new turn instead of being dropped.
        ("Continue where you left off with the plan.md", False),
        ("continue, but use 0603 resistors", False),
        ("resume and then run DRC", False),
        ("Please continue", False),
        ("continued fractions are neat", False),
        ("continue " + "x" * 300, False),
    ],
)
def test_resume_requests_are_recognised(text, expected) -> None:
    assert is_resume_request(text) is expected


def test_backoff_follows_the_protocol_schedule_with_jitter() -> None:
    run = _TurnRun(agent=object(), writer=lambda _e: None, original=[])  # type: ignore[arg-type]
    drop = ProtocolError("gone", code="STREAM_ENDED_WITHOUT_FINAL", retryable=True)
    delays = []
    for _ in range(8):
        plan = run._plan_resume(drop, classify_error(drop))
        assert plan is not None
        delays.append(plan[1])
        run.error_resumes += 1
    for delay, base in zip(delays, (1, 2, 5, 10, 20, 30, 60, 60), strict=True):
        assert base * 0.8 <= delay <= base * 1.2
    assert run._plan_resume(drop, classify_error(drop)) is None
    rotation = ProtocolError("rotate", code="socket_rotation", retryable=True, resumable=True)
    reason, delay, immediate = run._plan_resume(rotation, classify_error(rotation)) or ("", 9, 0)
    assert (reason, immediate) == ("socket_rotation", True) and 0 <= delay <= 2


# ------------------------------------------------------------------- graph


@dataclass
class Agent:
    """Fake hosted agent whose attempts are scripted lists of events or exceptions."""

    attempts: list[list[Any]]
    mode: CopperMode = CopperMode.AGENT
    effort: Any = None
    model: str | None = None
    cancel_requested: bool = False
    tool_broker: Any = None
    conversation_id: str = "conversation"
    payloads: list[dict[str, Any]] = field(default_factory=list)
    reachable: list[bool] = field(default_factory=list)
    probes: int = 0

    def __post_init__(self) -> None:
        from copper_pilot_cli.copper_protocol import CopperEffort

        self.effort = self.effort or CopperEffort.INTELLIGENCE

    async def astream_events_raw(self, value):
        self.payloads.append(dict(value))
        script = self.attempts.pop(0)
        for item in script:
            if isinstance(item, BaseException):
                raise item
            if callable(item):
                item()
                continue
            yield item

    async def server_reachable(self) -> bool:
        self.probes += 1
        return self.reachable.pop(0) if self.reachable else True

    async def acancel(self):
        self.cancel_requested = True


DROP = ProtocolError(
    "Chat socket closed before a final response (close code 1006).",
    code="STREAM_ENDED_WITHOUT_FINAL",
    retryable=True,
    close_code=1006,
)
DONE = [CopperEvent(EventKind.TEXT, " done"), CopperEvent(EventKind.FINAL, {"success": True})]


def tool(call_id: str = "call-1", name: str = "bash") -> list[CopperEvent]:
    return [
        CopperEvent(
            EventKind.TOOL_REQUEST,
            ToolRequest(tool_call_id=call_id, tool_name=name, arguments={"command": "make"}),
        ),
        CopperEvent(
            EventKind.TOOL_RESULT,
            {"tool_call_id": call_id, "tool_name": name, "result": {"result": "built"}},
        ),
    ]


def rotation(code: str = "socket_rotation") -> CopperEvent:
    return CopperEvent(
        EventKind.FINAL,
        {"success": False, "resumable": True, "error_code": code, "error": "Rotating."},
    )


@pytest.fixture
def graph_env(monkeypatch, tmp_path):
    monkeypatch.setenv("COPPER_PILOT_CLI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("copper_pilot_cli.sessions._db_path", None)
    monkeypatch.setattr(copper_graph, "RETRY_BACKOFF_SECONDS", (0.0,) * MAX_TURN_ATTEMPTS)
    monkeypatch.setattr(copper_graph, "IMMEDIATE_RESUME_MAX_DELAY", 0.0)
    monkeypatch.setattr(copper_graph, "HEARTBEAT_POLL_SECONDS", (0.0,))
    workspace = tmp_path / "project"
    workspace.mkdir()
    return workspace


async def run_turn(agent, workspace, values, *, thread: str = "t"):
    async with create_copper_graph(agent) as runtime:
        config = runtime.config(thread, workspace)
        events = [
            data
            async for _ns, mode, data in runtime.astream(values, config)
            if mode == "custom" and isinstance(data, dict)
        ]
        state = await runtime.aget_state(config)
        unfinished = await runtime.unfinished_turn(config)
    return events, state.values, unfinished


FIRST = {"messages": [HumanMessage(content="Route the board")], "copper_mode": "agent"}


@pytest.mark.asyncio
async def test_resumable_final_resumes_at_once_outside_the_error_budget(
    graph_env, monkeypatch
) -> None:
    monkeypatch.setattr(copper_graph, "MAX_AUTO_RESUMES", 0)
    agent = Agent(
        [
            [CopperEvent(EventKind.TEXT, "Half"), *tool(), rotation()],
            [CopperEvent(EventKind.TEXT, "way"), rotation("server_restarting")],
            DONE,
        ]
    )
    events, values, unfinished = await run_turn(agent, graph_env, FIRST)
    reasons = [p["resume"]["reason"] for p in agent.payloads[1:]]
    assert reasons == ["socket_rotation", "server_restarting"]
    assert [p["resume"]["attempt"] for p in agent.payloads[1:]] == [1, 2]
    assert agent.payloads[2]["resume"]["assistant_text"] == "Halfway"
    assert not [e for e in events if e["type"] in {"copper.error", "copper.retry"}]
    assert [e["data"]["will_retry"] for e in events if e["type"] == RESUME_EVENT] == [True, True]
    assert values["copper_turn_journal"] is None and unfinished is None
    texts = [m.content for m in values["messages"] if isinstance(m, AIMessage)]
    assert texts == ["Half", "way done"]


@pytest.mark.asyncio
async def test_idle_and_ping_timeouts_resume_with_their_reason(graph_env) -> None:
    idle = ProtocolError(
        "Timed out", code="CHAT_IDLE_TIMEOUT", retryable=True, resume_reason="idle_timeout"
    )
    ping = ProtocolError(
        "keepalive ping timeout",
        code="STREAM_ENDED_WITHOUT_FINAL",
        retryable=True,
        close_code=1011,
        resume_reason="connection_lost",
    )
    agent = Agent([[idle], [ping], DONE], reachable=[False, False, True])
    events, _values, _unfinished = await run_turn(agent, graph_env, FIRST)
    assert [p["resume"]["reason"] for p in agent.payloads[1:]] == [
        "idle_timeout",
        "connection_lost",
    ]
    # Two failed heartbeats before the first resume did not use up attempts.
    assert agent.probes == 4
    assert [p["resume"]["attempt"] for p in agent.payloads[1:]] == [1, 2]
    assert any(e.get("title") == "waiting_for_heartbeat" for e in events)


@pytest.mark.asyncio
async def test_budget_exhaustion_reports_and_persists_the_journal(graph_env, monkeypatch) -> None:
    monkeypatch.setattr(copper_graph, "MAX_AUTO_RESUMES", 2)
    agent = Agent([[CopperEvent(EventKind.TEXT, "Placed U1."), *tool(), DROP], [DROP], [DROP]])
    events, values, unfinished = await run_turn(agent, graph_env, FIRST)

    assert len(agent.payloads) == 3
    (error,) = [e for e in events if e["type"] == "copper.error"]
    assert error["data"]["resumable"] is True
    final_resume = [e for e in events if e["type"] == RESUME_EVENT][-1]
    assert final_resume["data"]["will_retry"] is False
    assert final_resume["data"]["manual_resume"] is True
    stored = values["copper_turn_journal"]
    assert stored["status"] == "unfinished"
    assert stored["last_error"]["type"] == "connection"
    assert stored["tool_events"][0]["status"] == "completed"
    assert unfinished is not None and unfinished.root_turn_id == stored["root_turn_id"]
    sidecar = JournalStore().load("t")
    assert sidecar is not None and sidecar.attempt == 2

    # /resume (copper_resume input) continues the same logical turn.
    agent.attempts = [DONE]
    events, values, unfinished = await run_turn(
        agent, graph_env, {"copper_resume": {"reason": "manual"}}
    )
    payload = agent.payloads[-1]
    assert [m.content for m in payload["messages"]] == ["Route the board"]
    assert payload["resume"]["reason"] == "manual"
    assert payload["resume"]["attempt"] == 3
    assert payload["resume"]["root_turn_id"] == stored["root_turn_id"]
    assert payload["resume"]["assistant_text"] == "Placed U1."
    assert values["copper_turn_journal"] is None and unfinished is None
    assert JournalStore().load("t") is None
    assert values["messages"][-1].content == " done"


@pytest.mark.asyncio
async def test_crash_mid_turn_resumes_with_app_restart(graph_env) -> None:
    class Crash(BaseException):
        pass

    agent = Agent([[CopperEvent(EventKind.TEXT, "Routing "), *tool(), Crash()]])
    with pytest.raises(Crash):
        await run_turn(agent, graph_env, FIRST)
    sidecar = JournalStore().load("t")
    assert sidecar is not None and sidecar.status == "running"

    agent.attempts = [DONE]
    _events, values, _unfinished = await run_turn(
        agent, graph_env, {"copper_resume": {}}, thread="t"
    )
    resume = agent.payloads[-1]["resume"]
    assert resume["reason"] == "app_restart"
    assert resume["tool_events"][0]["status"] == "completed"
    # The pre-crash text is carried into the transcript.
    texts = [m.content for m in values["messages"] if isinstance(m, AIMessage)]
    assert texts == ["Routing ", " done"]


@pytest.mark.asyncio
async def test_continue_message_resumes_the_unfinished_turn(graph_env, monkeypatch) -> None:
    monkeypatch.setattr(copper_graph, "MAX_AUTO_RESUMES", 0)
    agent = Agent([[CopperEvent(EventKind.TEXT, "Half"), DROP]])
    await run_turn(agent, graph_env, FIRST)
    agent.attempts = [DONE]
    await run_turn(
        agent,
        graph_env,
        {"messages": [HumanMessage(content="Continue where you left off")]},
    )
    payload = agent.payloads[-1]
    assert payload["resume"]["reason"] == "manual"
    assert [m.content for m in payload["messages"]] == ["Route the board"]


@pytest.mark.asyncio
async def test_new_message_starts_fresh_and_resume_without_journal_errors(graph_env) -> None:
    agent = Agent([[CopperEvent(EventKind.TEXT, "ok"), CopperEvent(EventKind.FINAL, {})]])
    await run_turn(agent, graph_env, FIRST)
    assert "resume" not in agent.payloads[-1]
    events, _values, _unfinished = await run_turn(agent, graph_env, {"copper_resume": {}})
    (error,) = [e for e in events if e["type"] == "copper.error"]
    assert error["data"]["message"] == NO_UNFINISHED_TURN


@pytest.mark.asyncio
async def test_non_retryable_failure_is_not_kept_for_resume(graph_env) -> None:
    invalid = ProtocolError("bad", code="chat_request_invalid", status=400)
    agent = Agent([[invalid]])
    _events, values, unfinished = await run_turn(agent, graph_env, FIRST)
    assert values["copper_turn_journal"] is None and unfinished is None
    assert load_unfinished_turn("t", values) is None


@pytest.mark.asyncio
async def test_events_carry_correlation_fields(graph_env) -> None:
    agent = Agent([[DROP], DONE])
    events, _values, _unfinished = await run_turn(agent, graph_env, FIRST)
    root = agent.payloads[0]["turn_id"]
    for event in events:
        assert event["root_turn_id"] == root
        assert isinstance(event["ts"], float)
    texts = [e for e in events if e["type"] == "copper.text"]
    assert texts[0]["turn_id"] == agent.payloads[1]["turn_id"]
    assert texts[0]["turn_attempt"] == 1


# ------------------------------------------------------------------ headless


@pytest.mark.asyncio
async def test_headless_json_carries_turn_fields_and_resume_events(
    graph_env, monkeypatch, capsys
) -> None:
    from copper_pilot_cli.copper_main import _headless, parser

    agent = Agent([[CopperEvent(EventKind.TEXT, "a", sequence=1)], DONE])
    agent.attempts[0].append(DROP)
    args = parser().parse_args([str(graph_env), "-n", "-m", "Route the board", "--json"])
    async with create_copper_graph(agent) as runtime:
        assert await _headless(runtime, graph_env, "t", "Route the board", args) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    root = agent.payloads[0]["turn_id"]
    for line in lines:
        assert {"type", "data", "title", "turn_id", "root_turn_id", "resume_attempt", "ts"} <= set(
            line
        )
        assert line["root_turn_id"] == root
    text = [line for line in lines if line["type"] == "text"]
    assert [(line["data"], line["resume_attempt"]) for line in text] == [("a", 0), (" done", 1)]
    assert text[0]["sequence"] == 1 and text[0]["turn_id"] == root
    (resume,) = [line for line in lines if line["type"] == "resume"]
    assert resume["data"]["attempt"] == 1
    assert resume["data"]["reason"] == "connection_lost"
    assert resume["data"]["will_retry"] is True
    assert {"delay_s", "will_retry", "attempt", "reason"} <= set(resume["data"])


@pytest.mark.asyncio
async def test_headless_resume_flag_resumes_the_unfinished_turn(
    graph_env, monkeypatch, capsys
) -> None:
    from copper_pilot_cli.copper_main import _headless, parser

    monkeypatch.setattr(copper_graph, "MAX_AUTO_RESUMES", 0)
    agent = Agent([[CopperEvent(EventKind.TEXT, "Half"), DROP], DONE])
    first = parser().parse_args([str(graph_env), "-n", "-m", "Route the board"])
    resumed = parser().parse_args([str(graph_env), "-n", "-r", "t"])
    async with create_copper_graph(agent) as runtime:
        assert await _headless(runtime, graph_env, "t", "Route the board", first) == 1
        assert "copper-pilot -n -r t" in capsys.readouterr().out
        assert await _headless(runtime, graph_env, "t", None, resumed) == 0
        assert await runtime.unfinished_turn(runtime.config("t", graph_env)) is None
        # Nothing left to resume: a clear error instead of a new turn.
        assert await _headless(runtime, graph_env, "t", None, resumed) == 2
    output = capsys.readouterr().out
    assert "Resuming the unfinished turn: Route the board" in output
    assert "no unfinished turn" in output
    assert agent.payloads[1]["resume"]["reason"] == "manual"
    assert len(agent.payloads) == 2


# ---------------------------------------------------------------- end to end


@pytest.mark.asyncio
async def test_wire_resume_after_a_drop_mid_tool(graph_env, monkeypatch) -> None:
    """Real agent + protocol: the second chat_start carries the journal."""
    from copper_pilot_cli.copper_auth import DeviceCredential
    from copper_pilot_cli.copper_tools import ApprovalMode, LocalToolBroker
    from copper_pilot_cli.langchain import CopperPilotAgent

    (graph_env / "a.txt").write_text("hello")
    read = {"tool_call_id": "r1", "tool_name": "read_file", "arguments": {"file_path": "a.txt"}}
    sockets = [
        Socket(
            [frame(response="Reading. ", turn_id="x"), frame(tool_request=read)],
            send_error=ConnectionClosedError(Close(1012, "service restart"), None),
        ),
        # The resumed server repeats the same tool call id; the broker cache answers it.
        Socket([frame(tool_request=read), frame("final", response="Done.", is_final=True)]),
    ]
    monkeypatch.setattr(
        "copper_pilot_cli.copper_protocol.connect", lambda *_a, **_k: sockets.pop(0)
    )
    opened = list(sockets)
    broker = LocalToolBroker(graph_env, mode=ApprovalMode.YOLO)
    runs = []
    original_execute = broker._execute_once

    async def counted(request):
        runs.append(request.tool_call_id)
        return await original_execute(request)

    monkeypatch.setattr(broker, "_execute_once", counted)
    agent = CopperPilotAgent(
        graph_env,
        credential=DeviceCredential(
            base_url="https://example.test", api_key="cf_live_x", fingerprint="f"
        ),
        conversation_id="t",
        effort=CopperEffort.INTELLIGENCE,
        tool_broker=broker,
    )

    async def reachable() -> bool:
        return True

    monkeypatch.setattr(agent, "server_reachable", reachable)
    _events, values, unfinished = await run_turn(agent, graph_env, FIRST)

    first, second = (socket.sent[0] for socket in opened)
    assert first["request"]["turn_id"] != second["request"]["turn_id"]
    assert "resume" not in first["request"]
    assert second["request"]["query"] == first["request"]["query"] == "Route the board"
    assert second["request"]["context"] == first["request"]["context"]
    assert second["request"]["effort"] == "intelligence"
    assert "turn_resume_v1" in second["capabilities"]
    resume = second["request"]["resume"]
    assert resume["root_turn_id"] == first["request"]["turn_id"]
    assert resume["reason"] == "tool_result_undeliverable"
    assert resume["assistant_text"] == "Reading."
    assert resume["tool_events"] == [
        {
            "tool_call_id": "r1",
            "tool_name": "read_file",
            "arguments": {"file_path": "a.txt"},
            "status": "completed",
            "result": resume["tool_events"][0]["result"],
        }
    ]
    assert "hello" in resume["tool_events"][0]["result"]
    assert runs == ["r1"]
    assert unfinished is None and values["copper_turn_journal"] is None


# ---------------------------------------------------------- provider capacity


def busy(retry_after_s: float | None = 45) -> CopperEvent:
    return CopperEvent(
        EventKind.FINAL,
        {
            "success": False,
            "resumable": True,
            "error_code": "provider_busy",
            "retry_after_s": retry_after_s,
            "error": "CopperPilot is under high demand right now.",
        },
    )


def waiting(phase: str = "retrying", retry_in_s: float = 20) -> CopperEvent:
    return CopperEvent(EventKind.STATUS_EVENT, capacity_wait(phase, retry_in_s, budget_s=600))


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    """Record the graph's waits instead of sleeping through them."""
    recorded: list[float] = []
    original = asyncio.sleep

    async def fake(delay, *args, **kwargs):
        recorded.append(delay)
        await original(0)

    monkeypatch.setattr(copper_graph.asyncio, "sleep", fake)
    return recorded


@pytest.mark.asyncio
async def test_capacity_wait_is_shown_as_progress_not_an_error(graph_env, sleeps) -> None:
    agent = Agent([[waiting(), waiting("resumed", 0), *DONE]])
    events, values, _unfinished = await run_turn(agent, graph_env, FIRST)
    waits = [e for e in events if e["type"] == copper_graph.CAPACITY_WAIT_EVENT]
    assert [e["title"] for e in waits] == [
        "High demand — waiting to continue (next try in 20 s)",
        "High demand eased — continuing",
    ]
    assert waits[0]["data"]["phase"] == "retrying" and waits[0]["data"]["retry_in_s"] == 20
    assert all(e["root_turn_id"] == agent.payloads[0]["turn_id"] for e in waits)
    assert not [e for e in events if e["type"] in {"copper.error", "copper.retry"}]
    assert not [e for e in events if e["type"] == "copper.status_event"]
    assert len(agent.payloads) == 1 and values["copper_turn_journal"] is None


@pytest.mark.asyncio
async def test_provider_busy_waits_retry_after_then_resumes(graph_env, monkeypatch, sleeps) -> None:
    # The transport budget is spent; provider-busy resumes do not use it.
    monkeypatch.setattr(copper_graph, "MAX_AUTO_RESUMES", 0)
    agent = Agent([[CopperEvent(EventKind.TEXT, "Placed U1."), *tool(), busy(45)], DONE])
    events, values, unfinished = await run_turn(agent, graph_env, FIRST)

    resume = agent.payloads[1]["resume"]
    assert resume["reason"] == "provider_busy"
    assert resume["attempt"] == 1
    assert resume["assistant_text"] == "Placed U1."
    assert resume["tool_events"][0]["status"] == "completed"
    assert 45 <= sleeps[0] <= 45 * 1.2
    (scheduled,) = [e for e in events if e["type"] == RESUME_EVENT]
    assert scheduled["data"]["reason"] == "provider_busy"
    assert scheduled["data"]["delay_s"] == round(sleeps[0], 3)
    assert scheduled["data"]["will_retry"] is True
    assert scheduled["title"].startswith("CopperPilot is under high demand; continuing in")
    assert not [e for e in events if e["type"] in {"copper.error", "copper.retry"}]
    # The echoed error copy is not part of the answer.
    texts = [m.content for m in values["messages"] if isinstance(m, AIMessage)]
    assert "high demand" not in " ".join(str(t) for t in texts)
    assert values["copper_turn_journal"] is None and unfinished is None


@pytest.mark.asyncio
async def test_provider_busy_delay_has_a_floor(graph_env, sleeps) -> None:
    agent = Agent([[busy(2)], [busy(None)], DONE])
    await run_turn(agent, graph_env, FIRST)
    assert sleeps[0] == copper_graph.PROVIDER_BUSY_MIN_DELAY == 15
    default = copper_graph.PROVIDER_BUSY_DEFAULT_DELAY
    assert default <= sleeps[1] <= default * 1.2


@pytest.mark.asyncio
async def test_provider_busy_budget_is_separate_and_exhausts_to_manual_resume(
    graph_env, monkeypatch, sleeps
) -> None:
    monkeypatch.setattr(copper_graph, "MAX_AUTO_RESUMES", 1)
    attempts: list[list[Any]] = [[CopperEvent(EventKind.TEXT, "Half"), DROP]]
    attempts += [[busy()] for _ in range(copper_graph.PROVIDER_BUSY_MAX_RESUMES + 1)]
    agent = Agent(attempts)
    events, values, unfinished = await run_turn(agent, graph_env, FIRST)

    reasons = [p["resume"]["reason"] for p in agent.payloads[1:]]
    # One transport resume, then six provider-busy resumes, then a pause.
    assert reasons == ["connection_lost"] + ["provider_busy"] * 6
    (error,) = [e for e in events if e["type"] == "copper.error"]
    assert error["data"]["type"] == "provider_busy"
    assert error["data"]["resumable"] is True
    assert error["data"]["message"] == copper_graph.PROVIDER_BUSY_PAUSED_MESSAGE
    assert "ran into an error" not in error["data"]["message"]
    final = [e for e in events if e["type"] == RESUME_EVENT][-1]
    assert final["data"]["will_retry"] is False and final["data"]["manual_resume"] is True
    assert final["data"]["reason"] == "provider_busy"
    assert "high demand" in final["title"]
    retries = [e for e in events if e["type"] == "copper.retry"]
    assert all(e["data"]["error_type"] == "connection" for e in retries)
    stored = values["copper_turn_journal"]
    assert stored["status"] == "unfinished"
    assert stored["last_error"]["type"] == "provider_busy"
    assert unfinished is not None and unfinished.root_turn_id == stored["root_turn_id"]

    agent.attempts = [DONE]
    await run_turn(agent, graph_env, {"copper_resume": {"reason": "manual"}})
    assert agent.payloads[-1]["resume"]["reason"] == "manual"
    assert agent.payloads[-1]["resume"]["assistant_text"] == "Half"


@pytest.mark.asyncio
async def test_provider_busy_budget_resets_after_model_progress(graph_env, sleeps) -> None:
    limit = copper_graph.PROVIDER_BUSY_MAX_RESUMES
    attempts: list[list[Any]] = [[busy()] for _ in range(limit)]
    attempts.append([CopperEvent(EventKind.TEXT, "Progress. "), busy()])
    attempts += [[busy()] for _ in range(limit - 1)]
    attempts.append(DONE)
    agent = Agent(attempts)
    events, values, _unfinished = await run_turn(agent, graph_env, FIRST)
    assert len(agent.payloads) == 2 * limit + 1
    assert not [e for e in events if e["type"] == "copper.error"]
    assert values["copper_turn_journal"] is None


@pytest.mark.asyncio
async def test_provider_busy_stops_after_the_time_window(graph_env, monkeypatch, sleeps) -> None:
    monkeypatch.setattr(copper_graph, "PROVIDER_BUSY_WINDOW_SECONDS", 0.0)
    agent = Agent([[busy()]])
    events, _values, unfinished = await run_turn(agent, graph_env, FIRST)
    assert len(agent.payloads) == 1
    (error,) = [e for e in events if e["type"] == "copper.error"]
    assert error["data"]["type"] == "provider_busy"
    assert unfinished is not None


@pytest.mark.asyncio
async def test_headless_shows_capacity_waits_and_busy_pauses(
    graph_env, monkeypatch, capsys, sleeps
) -> None:
    from copper_pilot_cli.copper_main import _headless, parser

    monkeypatch.setattr(copper_graph, "PROVIDER_BUSY_MAX_RESUMES", 1)
    agent = Agent([[waiting(), busy(20)], [busy(20)]])
    args = parser().parse_args([str(graph_env), "-n", "-m", "Route the board"])
    async with create_copper_graph(agent) as runtime:
        assert await _headless(runtime, graph_env, "t", "Route the board", args) == 1
    output = capsys.readouterr().out
    assert "High demand — waiting to continue (next try in 20 s)" in output
    assert "CopperPilot is under high demand; continuing in" in output
    assert "under high demand right now" in output
    assert "copper-pilot -n -r t" in output
    assert "ran into an error" not in output


@pytest.mark.asyncio
async def test_headless_json_capacity_wait_event_shape(graph_env, capsys, sleeps) -> None:
    from copper_pilot_cli.copper_main import _headless, parser

    agent = Agent([[waiting(), busy(30)], DONE])
    args = parser().parse_args([str(graph_env), "-n", "-m", "Route the board", "--json"])
    async with create_copper_graph(agent) as runtime:
        assert await _headless(runtime, graph_env, "t", "Route the board", args) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    (wait,) = [line for line in lines if line["type"] == "capacity_wait"]
    root = agent.payloads[0]["turn_id"]
    assert wait["phase"] == "retrying" and wait["retry_in_s"] == 20
    assert wait["model"] == "gemini-3.8-flash"
    assert {"waited_s", "budget_s", "attempt", "message"} <= set(wait)
    assert wait["data"]["phase"] == "retrying"
    assert wait["title"] == "High demand — waiting to continue (next try in 20 s)"
    assert wait["turn_id"] == wait["root_turn_id"] == root
    assert wait["resume_attempt"] == 0 and isinstance(wait["ts"], float)
    (resume,) = [line for line in lines if line["type"] == "resume"]
    assert resume["data"]["reason"] == "provider_busy"
    assert resume["data"]["delay_s"] >= 30
