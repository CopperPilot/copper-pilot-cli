"""Turn resume protocol v1: transport liveness, resumable finals, journal and resume."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from copper_pilot_cli.copper_protocol import (
    CLIENT_LIVENESS_CLOSE_CODE,
    DEFAULT_IDLE_FRAME_TIMEOUT,
    IDLE_FRAME_TIMEOUT_ENV,
    PING_INTERVAL_SECONDS,
    PING_TIMEOUT_SECONDS,
    ChatRequest,
    CopperMode,
    EventKind,
    HostedChatClient,
    ProtocolError,
    ToolRequest,
    classify_error,
    idle_frame_timeout,
    make_chat_start,
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
