from __future__ import annotations

import asyncio
import json
import ssl

import pytest

from copper_pilot_cli.copper_protocol import (
    MAX_CHAT_START_BYTES,
    ChatMessage,
    ChatRequest,
    CopperMode,
    EventKind,
    HostedChatClient,
    ProtocolError,
    bound_context,
    make_chat_start,
    sanitize_status,
    websocket_url,
)


def request(**changes) -> ChatRequest:
    values = {
        "query": "review this",
        "workspace_path": "/tmp/project",  # noqa: S108
        "conversation_id": "conversation",
        "mode": CopperMode.AGENT,
    }
    values.update(changes)
    return ChatRequest(**values)


def test_chat_start_is_protocol_v1_and_disables_computer_use() -> None:
    frame = make_chat_start(request())
    assert frame["type"] == "chat_start"
    assert frame["protocol_version"] == 1
    assert frame["request"]["enable_computer_use"] is False
    assert frame["request"]["mode"] == "agent"


def test_context_strips_inline_attachments_and_keeps_newest() -> None:
    messages = [
        ChatMessage(role="user", content="old"),
        ChatMessage(role="assistant", content="data:image/png;base64," + "A" * 100),
        ChatMessage(role="user", content="new"),
    ]
    bounded = bound_context(messages)
    assert bounded[-1].content == "new"
    assert "data:image" not in bounded[-2].content
    assert "inline attachment omitted" in bounded[-2].content


def test_oversized_start_frame_is_rejected() -> None:
    with pytest.raises(ProtocolError):
        make_chat_start(request(query="x" * (MAX_CHAT_START_BYTES + 1)))


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("https://copperpilot.ai", "wss://copperpilot.ai/api/copperpilot/analyze/agent/ws"),
        ("http://localhost:80", "ws://localhost:80/api/copperpilot/analyze/agent/ws"),
    ],
)
def test_websocket_url(base, expected) -> None:
    assert websocket_url(base) == expected


def test_non_agent_modes_use_chat_websocket() -> None:
    assert websocket_url("https://copperpilot.ai", agentic=False).endswith(
        "/api/copperpilot/analyze/chat/ws"
    )


def test_frame_is_below_budget() -> None:
    frame = make_chat_start(request(context=[ChatMessage(role="user", content="hello")]))
    assert len(json.dumps(frame).encode()) < MAX_CHAT_START_BYTES


def test_status_html_is_sanitized_for_terminal() -> None:
    value = sanitize_status(
        "<b>Reading</b><script>steal()</script>\x1b[31m board\x1b]0;malicious title\x07"
    )
    assert "Reading" in value
    assert "steal" not in value
    assert "malicious title" not in value
    assert "\x1b" not in value


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", ["not-json", "[]", "null", '"text"'])
async def test_hosted_stream_rejects_non_object_frames(monkeypatch, frame) -> None:
    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def send(self, _value):
            return None

        async def recv(self):
            return frame

    monkeypatch.setattr("copper_pilot_cli.copper_protocol.connect", lambda *a, **k: Socket())
    client = HostedChatClient("https://example.test", "cf_live_key", "fingerprint")
    with pytest.raises(ProtocolError):
        _ = [event async for event in client.stream(request(), lambda _request: None)]


@pytest.mark.asyncio
async def test_hosted_stream_times_out_after_first_event(monkeypatch) -> None:
    class Socket:
        calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def send(self, _value):
            return None

        async def recv(self):
            self.calls += 1
            if self.calls == 1:
                return json.dumps({"type": "event", "chat_response": {"status_update": "Working"}})
            await asyncio.sleep(1)

    monkeypatch.setattr("copper_pilot_cli.copper_protocol.connect", lambda *a, **k: Socket())
    client = HostedChatClient("https://example.test", "cf_live_key", "fingerprint")
    client.idle_frame_timeout = 0.01
    with pytest.raises(ProtocolError, match="next frame"):
        _ = [event async for event in client.stream(request(), lambda _request: None)]


@pytest.mark.asyncio
async def test_hosted_stream_enforces_aggregate_byte_budget(monkeypatch) -> None:
    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def send(self, _value):
            return None

        async def recv(self):
            return json.dumps({"type": "event", "chat_response": {"response": "payload"}})

    monkeypatch.setattr("copper_pilot_cli.copper_protocol.MAX_STREAM_BYTES", 10)
    monkeypatch.setattr("copper_pilot_cli.copper_protocol.connect", lambda *a, **k: Socket())
    client = HostedChatClient("https://example.test", "cf_live_key", "fingerprint")
    with pytest.raises(ProtocolError, match="stream byte budget"):
        _ = [event async for event in client.stream(request(), lambda _request: None)]


@pytest.mark.asyncio
async def test_hosted_stream_correlates_tool_result(monkeypatch) -> None:
    class Socket:
        def __init__(self) -> None:
            self.frames = [
                json.dumps(
                    {
                        "type": "event",
                        "chat_response": {
                            "tool_request": {
                                "tool_call_id": "call-1",
                                "tool_name": "read_file",
                                "arguments": {"file_path": "board.kicad_sch"},
                            }
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "final",
                        "chat_response": {
                            "response": "<b>Done</b>",
                            "is_final": True,
                            "query": "must not be echoed",
                            "initial_file_tree": "x" * 100_000,
                        },
                    }
                ),
            ]
            self.sent = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def send(self, value):
            self.sent.append(json.loads(value))

        async def recv(self):
            return self.frames.pop(0)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.frames:
                raise StopAsyncIteration
            return self.frames.pop(0)

    socket = Socket()
    connect_options = {}

    def fake_connect(*_args, **kwargs):
        connect_options.update(kwargs)
        return socket

    monkeypatch.setattr("copper_pilot_cli.copper_protocol.connect", fake_connect)

    async def execute(tool_request):
        assert tool_request.tool_call_id == "call-1"
        return {"result": "file"}

    client = HostedChatClient("https://example.test", "cf_live_key", "fingerprint")
    events = [event async for event in client.stream(request(), execute)]
    assert [event.kind for event in events] == [
        EventKind.TOOL_REQUEST,
        EventKind.TOOL_RESULT,
        EventKind.TEXT,
        EventKind.FINAL,
    ]
    assert socket.sent[1]["type"] == "tool_result"
    assert socket.sent[1]["tool_call_id"] == "call-1"
    assert isinstance(connect_options["ssl"], ssl.SSLContext)
    assert events[-2].data == "**Done**"
    assert "query" not in events[-1].data
    assert "initial_file_tree" not in events[-1].data


@pytest.mark.asyncio
async def test_final_envelope_preserves_usage_and_turn_correlation(monkeypatch) -> None:
    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def send(self, _value):
            return None

        async def recv(self):
            return json.dumps(
                {
                    "type": "final",
                    "usage_details": {"cache_tokens": 12},
                    "chat_response": {
                        "is_final": True,
                        "success": True,
                        "turn_id": "turn-1",
                        "sequence": 9,
                        "resume_attempt": 2,
                        "usage": {"tokens_input": 100, "tokens_output": 20},
                    },
                }
            )

    monkeypatch.setattr("copper_pilot_cli.copper_protocol.connect", lambda *a, **k: Socket())
    client = HostedChatClient("https://example.test", "cf_live_key", "fingerprint")
    events = [event async for event in client.stream(request(), lambda _request: None)]

    usage = next(event for event in events if event.kind is EventKind.USAGE)
    assert usage.data == {
        "tokens_input": 100,
        "tokens_output": 20,
        "cache_tokens": 12,
    }
    assert (usage.turn_id, usage.sequence, usage.resume_attempt) == ("turn-1", 9, 2)
