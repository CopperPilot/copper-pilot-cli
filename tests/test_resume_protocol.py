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
    CopperEvent,
    CopperMode,
    EventKind,
    HostedChatClient,
    ProtocolError,
    ToolRequest,
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
        ("Continue where you left off with the plan.md", True),
        ("resume", True),
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
        {"messages": [HumanMessage(content="Continue where you left off with the plan.md")]},
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
