"""Protocol helpers at their exact limits: error classes, byte caps, the history ladder."""

from __future__ import annotations

import asyncio
import json

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

from copper_pilot_cli import copper_protocol as cp
from copper_pilot_cli.copper_protocol import (
    CAS_WRITES_CAPABILITY,
    CHAT_HISTORY_SEARCH_CAPABILITY,
    ChatMessage,
    ChatRequest,
    ErrorInfo,
    ProtocolError,
    bound_context,
    capacity_hold_seconds,
    classify_error,
    client_capabilities,
    error_message,
    idle_frame_timeout,
    make_chat_start,
    omitted_messages,
    sanitize_status,
    tool_error_result,
    tool_result_frame,
    websocket_url,
)

MARKER = "\n[truncated]"


def request(**changes) -> ChatRequest:
    values = {
        "query": "review this",
        "workspace_path": "/tmp/project",  # noqa: S108
        "conversation_id": "conversation",
    }
    values.update(changes)
    return ChatRequest(**values)


def messages(count: int, size: int) -> list[ChatMessage]:
    return [
        ChatMessage(
            role="user" if index % 2 == 0 else "assistant", content=f"{index:04d}" * (size // 4)
        )
        for index in range(count)
    ]


# -------------------------------------------------------------- classify_error


def refused(status: int) -> InvalidStatus:
    return InvalidStatus(Response(status, "refused", Headers()))


@pytest.mark.parametrize(
    ("exc", "kind", "retryable"),
    [
        (asyncio.CancelledError(), "aborted", False),
        (TimeoutError("slow"), "timeout", True),
        (ConnectionResetError("reset"), "network", True),
        (OSError("no route"), "network", True),
        (InvalidHandshake("bad upgrade"), "connection", True),
        (ValueError("odd"), "unknown", False),
        (refused(401), "authentication", False),
        (refused(403), "authentication", False),
        (refused(402), "usage_limit", False),
        (refused(429), "rate_limit", True),
        (refused(400), "invalid_request", False),
        (refused(404), "invalid_request", False),
        (refused(499), "invalid_request", False),
        (refused(500), "server", True),
        (refused(503), "server", True),
        (refused(399), "server", True),
        (ProtocolError("x", code="provider_busy", resumable=True), "provider_busy", True),
        (ProtocolError("x", code="other", resumable=True), "resumable", True),
        (ProtocolError("x", code="provider_busy"), "server", True),
        (ProtocolError("x", code="CHAT_CONNECT_TIMEOUT"), "timeout", True),
        (ProtocolError("x", code="CHAT_FIRST_FRAME_TIMEOUT"), "timeout", True),
        (ProtocolError("x", code="CHAT_IDLE_TIMEOUT"), "timeout", True),
        (ProtocolError("x", code="STREAM_ENDED_WITHOUT_FINAL"), "connection", True),
        (ProtocolError("x", code="CHAT_CONNECTION_ERROR"), "connection", True),
        (ProtocolError("x", code="chat_request_invalid"), "invalid_request", False),
        (ProtocolError("x", code="chat_request_too_large"), "payload_too_large", False),
        (ProtocolError("x", code="PAYLOAD_TOO_LARGE"), "payload_too_large", False),
        (ProtocolError("x", status=413), "payload_too_large", False),
        (ProtocolError("x"), "protocol", False),
        (ProtocolError("x", status=401), "authentication", False),
        (ProtocolError("x", status=403), "authentication", False),
        (ProtocolError("x", status=402), "usage_limit", False),
        (ProtocolError("x", status=429), "rate_limit", True),
        (ProtocolError("x", status=404), "invalid_request", False),
        (ProtocolError("x", status=500), "server", True),
        (ProtocolError("x", code="weird"), "server", True),
        # An explicit retryable flag from the server wins over the status.
        (ProtocolError("x", status=404, retryable=True), "invalid_request", True),
        (ProtocolError("x", status=500, retryable=False), "server", False),
    ],
)
def test_classify_error_table(exc, kind, retryable):
    info = classify_error(exc)
    assert (info.type, info.retryable) == (kind, retryable)


def test_classify_error_messages():
    assert classify_error(asyncio.CancelledError()) == ErrorInfo(
        "aborted", "Analysis interrupted by user.", False
    )
    assert classify_error(ValueError()).message == "ValueError"
    assert classify_error(TimeoutError("slow")).message == "slow"
    assert classify_error(InvalidHandshake("eof")).message == "Handshake failed (eof)"
    received = ConnectionClosed(Close(1011, "pod restart"), None)
    assert classify_error(received) == ErrorInfo(
        "connection", "Connection closed (close code 1011 received: pod restart)", True
    )
    sent = ConnectionClosed(None, Close(4000, ""))
    assert classify_error(sent).message == "Connection closed (close code 4000 sent)"
    neither = ConnectionClosed(None, None)
    assert classify_error(neither).message == (
        "Connection closed (no close frame received or sent)"
    )


def test_protocol_error_event_data_keeps_only_set_fields():
    error = ProtocolError(
        "blocked",
        code="usage",
        status=402,
        block_reason="weekly",
        can_purchase_overage=False,
        retryable=False,
        resets_at="2026-12-01T00:00:00Z",
        resumable=True,
        close_code=1006,
    )
    assert error.as_event_data() == {
        "message": "blocked",
        "code": "usage",
        "status": 402,
        "block_reason": "weekly",
        "can_purchase_overage": False,
        "retryable": False,
        "resets_at": "2026-12-01T00:00:00Z",
    }
    assert ProtocolError("plain").as_event_data() == {"message": "plain"}


# ------------------------------------------------------------- idle and capacity


@pytest.mark.parametrize(
    ("event", "hold"),
    [
        (None, None),
        ({"kind": "other"}, None),
        ({"kind": "capacity_wait", "phase": "resumed", "retry_in_s": 30}, 0.0),
        ({"kind": "capacity_wait", "phase": "queued", "retry_in_s": 30}, 30.0),
        ({"kind": "capacity_wait", "phase": "retrying", "budget_s": 90, "waited_s": 20}, 70.0),
        (
            {
                "kind": "capacity_wait",
                "phase": "queued",
                "retry_in_s": 5,
                "budget_s": 9,
                "waited_s": 2,
            },
            7.0,
        ),
        ({"kind": "capacity_wait", "phase": "queued", "budget_s": 10, "waited_s": 50}, 0.0),
        ({"kind": "capacity_wait", "phase": "queued", "retry_in_s": True}, 0.0),
        ({"kind": "capacity_wait", "phase": "queued", "retry_in_s": "30"}, 0.0),
        ({"kind": "capacity_wait", "phase": "queued", "retry_in_s": 2.5}, 2.5),
    ],
)
def test_capacity_hold_seconds(event, hold):
    assert capacity_hold_seconds(event) == hold


@pytest.mark.parametrize(
    ("raw", "timeout"),
    [
        (None, 330.0),
        ("", 330.0),
        ("  ", 330.0),
        ("abc", 330.0),
        ("45", 45.0),
        (" 1.5 ", 1.5),
        ("0", None),
        ("-3", None),
    ],
)
def test_idle_frame_timeout(monkeypatch, raw, timeout):
    if raw is None:
        monkeypatch.delenv(cp.IDLE_FRAME_TIMEOUT_ENV, raising=False)
    else:
        monkeypatch.setenv(cp.IDLE_FRAME_TIMEOUT_ENV, raw)
    assert idle_frame_timeout() == timeout


# ------------------------------------------------------------- byte clipping


def test_truncate_exactly_at_the_limit_is_unchanged():
    assert cp._truncate_utf8("a" * 100, 100) == "a" * 100


def test_truncate_one_byte_over_keeps_the_marker_inside_the_limit():
    clipped = cp._truncate_utf8("a" * 101, 100)
    assert clipped == "a" * (100 - len(MARKER)) + MARKER
    assert len(clipped.encode()) == 100


@pytest.mark.parametrize("limit", [len(MARKER), len(MARKER) - 1, 1])
def test_truncate_below_the_marker_size_cuts_plainly(limit):
    assert cp._truncate_utf8("b" * 50, limit) == "b" * limit


def test_truncate_one_byte_above_the_marker_size():
    assert cp._truncate_utf8("b" * 50, len(MARKER) + 1) == "b" + MARKER


def test_truncate_never_splits_a_character():
    text = "é" * 20  # two bytes each
    clipped = cp._truncate_utf8(text, 21)
    assert clipped == "é" * 4 + MARKER  # 9 bytes left, one is a half character
    assert len(clipped.encode()) <= 21


# ------------------------------------------------------------- bound_context


def test_bound_context_keeps_newest_within_the_total():
    history = messages(5, 100)
    bounded = bound_context(history, max_message_bytes=1000, max_total_bytes=300)
    assert [m.content for m in bounded[1:]] == [m.content for m in history[-3:]]
    assert bounded[0] == ChatMessage(role="system", content="[2 older messages omitted]")
    assert omitted_messages(history, bounded) == 2


def test_bound_context_total_is_inclusive():
    history = messages(3, 100)
    bounded = bound_context(history, max_message_bytes=1000, max_total_bytes=300)
    assert bounded == history
    assert omitted_messages(history, bounded) == 0


def test_bound_context_always_keeps_the_newest_message():
    history = messages(2, 400)
    bounded = bound_context(history, max_message_bytes=1000, max_total_bytes=10)
    assert [m.content for m in bounded] == ["[1 older messages omitted]", history[-1].content]


def test_bound_context_caps_the_number_of_entries(monkeypatch):
    monkeypatch.setattr(cp, "MAX_CONTEXT_ENTRIES", 3)
    history = messages(5, 4)
    bounded = bound_context(history)
    assert bounded[0].content == "[2 older messages omitted]"
    assert bounded[1:] == history[-3:]


def test_bound_context_strips_inline_data_before_measuring():
    image = "data:image/png;base64," + "A" * 5000
    history = [ChatMessage(role="user", content=f"see {image} here")]
    (only,) = bound_context(history, max_message_bytes=100, max_total_bytes=100)
    assert only.content.startswith("see [inline attachment omitted:")
    assert only.content.endswith("] here")


def test_omitted_messages_ignores_a_real_system_message():
    history = [ChatMessage(role="system", content="be brief"), *messages(2, 8)]
    assert omitted_messages(history, history) == 0
    assert omitted_messages(history, []) == 3
    assert omitted_messages([], history) == 0


# --------------------------------------------------------------- the ladder


@pytest.fixture
def ladder(monkeypatch):
    def set_limits(max_bytes: int, strict_bytes: int) -> None:
        monkeypatch.setattr(cp, "MAX_CHAT_START_BYTES", max_bytes)
        monkeypatch.setattr(cp, "STRICT_CHAT_START_BYTES", strict_bytes)

    return set_limits


def contents(frame):
    return [m["content"] for m in frame["request"]["context"]]


def test_ladder_rung_one_uses_the_loose_frame_limit(ladder):
    history = messages(3, 40 * 1024)
    ladder(130 * 1024, 50 * 1024)
    frame = make_chat_start(request(context=history))
    assert contents(frame) == [m.content for m in history]


def test_ladder_rung_two_halves_the_message_cap(ladder):
    history = messages(3, 40 * 1024)
    ladder(100 * 1024, 100 * 1024)
    kept = contents(make_chat_start(request(context=history)))
    assert len(kept) == 3
    assert all(len(text.encode()) == 32 * 1024 and text.endswith(MARKER) for text in kept)


def test_ladder_rung_three_is_8k_per_message_and_128k_total(ladder):
    history = messages(20, 10 * 1024)
    ladder(150 * 1024, 150 * 1024)
    kept = contents(make_chat_start(request(context=history)))
    assert kept[0] == "[4 older messages omitted]"
    assert len(kept) == 17
    assert all(len(text.encode()) == 8 * 1024 for text in kept[1:])


def test_ladder_rung_four_is_4k_per_message_and_64k_total(ladder):
    history = messages(20, 10 * 1024)
    ladder(100 * 1024, 100 * 1024)
    kept = contents(make_chat_start(request(context=history)))
    assert kept[0] == "[4 older messages omitted]"
    assert all(len(text.encode()) == 4 * 1024 for text in kept[1:])


def test_ladder_gives_up_with_the_last_size(ladder):
    history = messages(20, 10 * 1024)
    ladder(10 * 1024, 10 * 1024)
    with pytest.raises(ProtocolError) as caught:
        make_chat_start(request(context=history))
    assert caught.value.code == "PAYLOAD_TOO_LARGE"
    assert classify_error(caught.value).type == "payload_too_large"
    ladder(10**9, 10**9)
    final = make_chat_start(request(context=history))
    # Same size as the strictest rung would produce: 16 messages of 4 KB plus the note.
    assert len(contents(final)) == 20
    size = str(caught.value).split("(")[1].split(" ")[0]
    assert 64 * 1024 < int(size) < 80 * 1024


def test_frame_exactly_at_the_limit_is_sent(ladder):
    frame = make_chat_start(request(context=messages(2, 64)))
    size = len(json.dumps(frame, separators=(",", ":")).encode())
    ladder(size, size)
    assert make_chat_start(request(context=messages(2, 64))) == frame
    ladder(size - 1, size - 1)
    with pytest.raises(ProtocolError):
        make_chat_start(request(context=messages(2, 64)))


def test_session_state_reports_history_dropped_by_the_ladder(ladder):
    history = messages(20, 10 * 1024)
    ladder(150 * 1024, 150 * 1024)
    frame = make_chat_start(request(context=history, session_state={"version": 1}))
    assert frame["request"]["session_state"] == {
        "version": 1,
        "history_omitted": {"messages": 4},
    }
    assert CHAT_HISTORY_SEARCH_CAPABILITY in frame["capabilities"]
    plain = make_chat_start(request(context=history))
    assert "session_state" not in plain["request"]
    assert CHAT_HISTORY_SEARCH_CAPABILITY not in plain["capabilities"]


@pytest.mark.parametrize(
    ("parallel", "resume", "search", "expected"),
    [
        ("1", "1", False, [CAS_WRITES_CAPABILITY, "parallel_tool_requests", "turn_resume_v1"]),
        ("0", "0", False, [CAS_WRITES_CAPABILITY]),
        ("0", "1", True, [CAS_WRITES_CAPABILITY, "turn_resume_v1", CHAT_HISTORY_SEARCH_CAPABILITY]),
    ],
)
def test_client_capabilities(monkeypatch, parallel, resume, search, expected):
    monkeypatch.setenv("COPPER_PILOT_PARALLEL_TOOLS", parallel)
    monkeypatch.setenv("COPPER_PILOT_TURN_RESUME", resume)
    assert client_capabilities(history_search=search) == expected


# ----------------------------------------------------------- frames and text


def test_tool_result_frame_moves_the_hash_to_the_top():
    frame = tool_result_frame("c", "t1", {"content": "x", "content_sha256": "abc"})
    assert frame == {
        "type": "tool_result",
        "conversation_id": "c",
        "tool_call_id": "t1",
        "result": {"content": "x"},
        "content_sha256": "abc",
    }
    assert "content_sha256" not in tool_result_frame("c", "t1", {"content_sha256": 5})
    assert tool_result_frame("c", "t1", "text")["result"] == "text"


def test_tool_result_frame_replaces_results_over_the_limit(monkeypatch):
    small = tool_result_frame("c", "t1", {"content": "x" * 50, "content_sha256": "h"})
    size = len(
        json.dumps(
            {k: v for k, v in small.items() if k != "content_sha256"}, separators=(",", ":")
        ).encode()
    )
    monkeypatch.setattr(cp, "MAX_TOOL_RESULT_BYTES", size)
    assert tool_result_frame("c", "t1", {"content": "x" * 50, "content_sha256": "h"}) == small
    monkeypatch.setattr(cp, "MAX_TOOL_RESULT_BYTES", size - 1)
    over = tool_result_frame("c", "t1", {"content": "x" * 50, "content_sha256": "h"})
    assert over["result"] == {"error": "Tool result exceeded the client frame limit."}
    assert "content_sha256" not in over


def test_tool_error_result():
    class ToolRejected(Exception):
        pass

    assert tool_error_result(ValueError("bad")) == {"error": "ValueError: bad", "rejected": False}
    assert tool_error_result(ToolRejected("no")) == {"error": "ToolRejected: no", "rejected": True}


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"message": "quota"}, "quota"),
        ({"message": ""}, "Hosted chat failed."),
        ({}, "Hosted chat failed."),
        ("plain", "plain"),
        ("", "Hosted chat failed."),
        (None, "Hosted chat failed."),
    ],
)
def test_error_message(value, message):
    assert error_message(value) == message


def test_frame_error_reads_typed_fields_only():
    error = cp._frame_error(
        {
            "public_message": "<b>Limit</b> reached",
            "message": "internal",
            "code": 402,
            "status": "402",
            "block_reason": "window",
            "can_purchase_overage": "yes",
            "retryable": False,
            "detail": {"window_ends_at": " 2026-12-01T10:00:00Z "},
        }
    )
    assert str(error) == "**Limit** reached"
    assert (error.code, error.status, error.block_reason) == ("402", None, "window")
    assert error.can_purchase_overage is None and error.retryable is False
    assert error.resets_at == "2026-12-01T10:00:00Z"
    bare = cp._frame_error({"message": "oops", "status": 500, "can_purchase_overage": True})
    assert (str(bare), bare.code, bare.status, bare.can_purchase_overage) == (
        "oops",
        None,
        500,
        True,
    )
    assert str(cp._frame_error({})) == "Hosted chat failed."


def test_counted_frame_bytes_subtracts_echoed_fields():
    frame = {"chat_response": {"query": "q" * 100, "text": "hi"}}
    raw = json.dumps(frame)
    echoed = len(json.dumps("q" * 100).encode())
    assert cp._counted_frame_bytes(raw, frame) == len(raw.encode()) - echoed
    assert cp._counted_frame_bytes(raw.encode(), {"type": "x"}) == len(raw.encode())
    assert cp._counted_frame_bytes("{}", {"chat_response": {"query": "q" * 100}}) == 0


def test_sanitize_status_drops_scripts_escapes_and_controls():
    text = sanitize_status("<script>alert(1)</script><p>ok\x1b[31m red\x07</p>\ttab")
    assert text == "ok red\n\ntab"
    assert sanitize_status("  plain  ") == "plain"
    assert sanitize_status("a" * 50, limit=20) == "a" * (20 - len(MARKER)) + MARKER


@pytest.mark.parametrize(
    ("base", "agentic", "expected"),
    [
        (
            "https://copperpilot.ai/x?y=1",
            True,
            "wss://copperpilot.ai/api/copperpilot/analyze/agent/ws",
        ),
        ("http://localhost:8000", False, "ws://localhost:8000/api/copperpilot/analyze/chat/ws"),
    ],
)
def test_websocket_url_scheme_and_path(base, agentic, expected):
    assert websocket_url(base, agentic=agentic) == expected


@pytest.mark.parametrize(
    ("frame_limit", "kept"),
    [
        (150 * 1024, 128),  # rung three: 128 KB of 1 KB messages
        (100 * 1024, 64),  # rung four: 64 KB of 1 KB messages
    ],
)
def test_ladder_total_caps_are_exact(ladder, frame_limit, kept):
    history = messages(200, 1024)
    ladder(frame_limit, frame_limit)
    frame = make_chat_start(request(context=history))
    assert contents(frame)[0] == f"[{200 - kept} older messages omitted]"
    assert contents(frame)[1:] == [m.content for m in history[-kept:]]


def test_ladder_rung_two_total_is_half_of_the_full_total(ladder):
    history = messages(58, 32 * 1024)  # 1.81 MB: fits the 2 MB rung-two total whole
    ladder(1_500_000, 1_950_000)
    assert len(contents(make_chat_start(request(context=history)))) == 58


@pytest.mark.parametrize(
    ("raw", "text"),
    [
        ("use *bold* and snake_case", "use *bold* and snake_case"),  # not HTML: left alone
        ("<p>snake_case</p>", "snake_case"),
        ("<B>loud</B>", "**loud**"),
        ("<SCRIPT>alert(1)</SCRIPT>ok", "ok"),
    ],
)
def test_sanitize_status_only_converts_real_html(raw, text):
    assert sanitize_status(raw) == text
