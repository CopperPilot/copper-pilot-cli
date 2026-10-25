"""Typed CopperPilot client protocol derived from the desktop harness."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import ssl
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlparse, urlunparse

import certifi
from markdownify import markdownify
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from copper_pilot_cli._version import __version__
from copper_pilot_cli.tool_batch import (
    PARALLEL_TOOL_REQUESTS_CAPABILITY,
    parallel_tools_enabled,
    run_tool_batch,
)

logger = logging.getLogger(__name__)

MAX_CHAT_START_BYTES = 12 * 1024 * 1024
STRICT_CHAT_START_BYTES = 8 * 1024 * 1024
MAX_TOOL_RESULT_BYTES = 8 * 1024 * 1024
MAX_CONTEXT_MESSAGE_BYTES = 64 * 1024
MAX_CONTEXT_TOTAL_BYTES = 4 * 1024 * 1024
MAX_CONTEXT_ENTRIES = 2_000
MAX_STREAM_BYTES = 64 * 1024 * 1024
MAX_STATUS_BYTES = 16 * 1024
# The answer itself can be long (a validate review's findings JSON easily
# passes 16 KB) and the hosted side may send it as one frame.
MAX_RESPONSE_TEXT_BYTES = 1024 * 1024
IDLE_FRAME_TIMEOUT_ENV = "COPPER_PILOT_IDLE_TIMEOUT_SECONDS"
DEFAULT_IDLE_FRAME_TIMEOUT = 10 * 60.0


def idle_frame_timeout() -> float | None:
    """Seconds a streaming turn may stay silent; ``None`` (env ``0``) waits forever."""
    raw = os.getenv(IDLE_FRAME_TIMEOUT_ENV, "").strip()
    try:
        value = float(raw) if raw else DEFAULT_IDLE_FRAME_TIMEOUT
    except ValueError:
        value = DEFAULT_IDLE_FRAME_TIMEOUT
    return value if value > 0 else None


class ProtocolError(RuntimeError):
    """The hosted protocol was malformed or incomplete."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
        block_reason: str | None = None,
        can_purchase_overage: bool | None = None,
        retryable: bool | None = None,
        resets_at: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.block_reason = block_reason
        self.can_purchase_overage = can_purchase_overage
        self.retryable = retryable
        self.resets_at = resets_at

    def as_event_data(self) -> dict[str, Any]:
        """Return a JSON-safe error payload for headless and MCP consumers."""
        result: dict[str, Any] = {"message": str(self)}
        for key in (
            "code",
            "status",
            "block_reason",
            "can_purchase_overage",
            "retryable",
            "resets_at",
        ):
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        return result


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    """Desktop-harness style failure classification for one hosted turn attempt."""

    type: str
    message: str
    retryable: bool


_TIMEOUT_CODES = frozenset(
    {"CHAT_CONNECT_TIMEOUT", "CHAT_FIRST_FRAME_TIMEOUT", "CHAT_IDLE_TIMEOUT"}
)
_CONNECTION_CODES = frozenset({"STREAM_ENDED_WITHOUT_FINAL", "CHAT_CONNECTION_ERROR"})
_DETERMINISTIC_CODES = frozenset(
    {"chat_request_invalid", "chat_request_too_large", "PAYLOAD_TOO_LARGE"}
)


def classify_error(exc: BaseException) -> ErrorInfo:
    """Port of the desktop client's ``_categorizeError``.

    Connection drops, timeouts, and transient server failures are retryable;
    deterministic client failures (4xx other than 429, invalid or oversized
    requests) are not. Cancellation is never retryable.
    """
    message = str(exc) or type(exc).__name__
    if isinstance(exc, asyncio.CancelledError):
        return ErrorInfo("aborted", "Analysis interrupted by user.", False)
    if isinstance(exc, ProtocolError):
        return _classify_protocol_error(exc, message)
    if isinstance(exc, TimeoutError):
        return ErrorInfo("timeout", message, True)
    if isinstance(exc, ConnectionClosed):
        return ErrorInfo("connection", f"Connection closed ({_close_detail(exc)})", True)
    if isinstance(exc, InvalidStatus):
        return _classify_refused_handshake(exc.response.status_code, message)
    if isinstance(exc, InvalidHandshake):
        # The upgrade failed without a usable HTTP status (EOF, bad upgrade header);
        # that is a transport fault, typically a proxy whose backend went away.
        return ErrorInfo("connection", f"Handshake failed ({message})", True)
    if isinstance(exc, OSError):
        return ErrorInfo("network", message, True)
    return ErrorInfo("unknown", message, False)


def _classify_refused_handshake(status: int, message: str) -> ErrorInfo:
    """Classify an HTTP status returned instead of the websocket upgrade.

    A 503 here is the load balancer reporting that no backend is ready yet,
    which is exactly what a reconnect sees for a short while after a pod
    restart; it is retryable like any other 5xx. Auth and other 4xx are not.
    """
    if status in {401, 403}:
        return ErrorInfo("authentication", message, False)
    if status == 402:
        return ErrorInfo("usage_limit", message, False)
    if status == 429:
        return ErrorInfo("rate_limit", message, True)
    if 400 <= status < 500:
        return ErrorInfo("invalid_request", message, False)
    return ErrorInfo("server", message, True)


def _classify_protocol_error(exc: ProtocolError, message: str) -> ErrorInfo:
    code = exc.code or ""
    status = exc.status
    if code in _TIMEOUT_CODES:
        return ErrorInfo("timeout", message, True)
    if code in _CONNECTION_CODES:
        return ErrorInfo("connection", message, True)
    if code == "chat_request_invalid":
        return ErrorInfo("invalid_request", message, False)
    if code in _DETERMINISTIC_CODES or status == 413:
        return ErrorInfo("payload_too_large", message, False)
    if status is None and not code:
        # Local protocol violations (malformed frames, budgets) are not worth retrying.
        return ErrorInfo("protocol", message, False)
    client_failure = isinstance(status, int) and 400 <= status < 500 and status != 429
    kind = "server"
    if status in {401, 403}:
        kind = "authentication"
    elif status == 402:
        kind = "usage_limit"
    elif status == 429:
        kind = "rate_limit"
    elif client_failure:
        kind = "invalid_request"
    retryable = exc.retryable if exc.retryable is not None else not client_failure
    return ErrorInfo(kind, message, retryable)


def _close_detail(exc: ConnectionClosed) -> str:
    """Describe the websocket close code and reason the way the desktop client logs them."""
    frame = exc.rcvd or exc.sent
    if frame is None:
        return "no close frame received or sent"
    reason = f": {frame.reason}" if frame.reason else ""
    origin = "received" if exc.rcvd is not None else "sent"
    return f"close code {frame.code} {origin}{reason}"


class CopperMode(StrEnum):
    """CopperPilot chat modes."""

    AGENT = "agent"
    PLAN = "plan"
    ASK = "ask"


class CopperEffort(StrEnum):
    """CopperPilot conversational effort."""

    BALANCED = "balanced"
    INTELLIGENCE = "intelligence"


def normalize_effort(value: Any) -> CopperEffort:
    """Unknown or missing effort stays on the free default."""
    try:
        return CopperEffort(str(value or CopperEffort.BALANCED).strip().lower())
    except ValueError:
        return CopperEffort.BALANCED


class ChatMessage(BaseModel):
    """Portable context message."""

    role: Literal["system", "user", "assistant"]
    content: str


class PlanTodo(BaseModel):
    id: str
    content: str
    status: Literal["open", "running", "done"] = "open"


class PlanContext(BaseModel):
    file_path: str
    name: str | None = None
    overview: str | None = None
    overall_status: Literal["open", "running", "done"] = "open"
    todos: list[PlanTodo] = Field(default_factory=list)
    body: str | None = None


class ChatRequest(BaseModel):
    """Path-first hosted chat request."""

    model_config = ConfigDict(extra="allow")

    query: str
    context: list[ChatMessage] = Field(default_factory=list)
    document_context: list[dict[str, Any]] = Field(default_factory=list)
    project_data: dict[str, Any] = Field(default_factory=dict)
    workspace_path: str
    conversation_id: str
    initial_file_tree: str | None = None
    mode: CopperMode = CopperMode.AGENT
    effort: CopperEffort = CopperEffort.BALANCED
    # Intelligence model id (see copper_models). Omitted for Balanced turns,
    # which the server pins to its default model regardless.
    model: str | None = None
    agentic_mode: bool = False
    schematic_path: str | None = None
    pcb_path: str | None = None
    schematic_info: dict[str, Any] | None = None
    pcb_info: dict[str, Any] | None = None
    workflow: str | None = None
    plan_context: PlanContext | None = None
    skill_manifest_id: str | None = None
    runtime_context: dict[str, Any] | None = None
    enable_computer_use: Literal[False] = False

    @field_validator("effort", mode="before")
    @classmethod
    def normalize_effort_field(cls, value: Any) -> CopperEffort:
        return normalize_effort(value)

    @field_validator("model", mode="before")
    @classmethod
    def normalize_model_field(cls, value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip().lower()
        return normalized or None

    @model_validator(mode="after")
    def drop_model_for_balanced(self) -> ChatRequest:
        """Only Intelligence turns carry a model, matching the desktop client."""
        if self.effort is not CopperEffort.INTELLIGENCE and self.model is not None:
            self.model = None
        return self


class ToolRequest(BaseModel):
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    timeout_ms: int | None = None


class ChatResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    response: str | None = None
    status_update: str | None = None
    status_event: dict[str, Any] | None = None
    reasoning_update: str | None = None
    reasoning_title: str | None = None
    tool_request: ToolRequest | None = None
    # A batch of independent requests from one model step; only sent to
    # clients that advertised ``parallel_tool_requests`` in chat_start.
    tool_requests: list[ToolRequest] | None = None
    usage: dict[str, Any] | None = None
    success: bool | None = None
    error: str | None = None
    is_final: bool = False
    suggestions: list[Any] | None = None
    turn_id: str | None = None
    sequence: int | None = None
    resume_attempt: int | None = None


class EventKind(StrEnum):
    TEXT = "text"
    REASONING = "reasoning"
    STATUS = "status"
    STATUS_EVENT = "status_event"
    TOOL_REQUEST = "tool_request"
    TOOL_RESULT = "tool_result"
    USAGE = "usage"
    FINAL = "final"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class CopperEvent:
    kind: EventKind
    data: Any
    title: str | None = None
    turn_id: str | None = None
    sequence: int | None = None
    resume_attempt: int | None = None


_DATA_URI = re.compile(r"data:([^;,]+)?(?:;[^,]*)?,([A-Za-z0-9+/=\\s]+)")
_TERMINAL_ESCAPE = re.compile(r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _strip_data_uris(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        digest = hashlib.sha256(match.group(0).encode()).hexdigest()[:12]
        return f"[inline attachment omitted:{digest}]"

    return _DATA_URI.sub(replace, text)


def _truncate_utf8(text: str, limit: int) -> str:
    encoded = text.encode()
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode(errors="ignore") + "\n[truncated]"


def bound_context(
    messages: Sequence[ChatMessage],
    *,
    max_message_bytes: int = MAX_CONTEXT_MESSAGE_BYTES,
    max_total_bytes: int = MAX_CONTEXT_TOTAL_BYTES,
) -> list[ChatMessage]:
    """Apply the desktop client's bounded newest-first history policy."""
    bounded: list[ChatMessage] = []
    total = 0
    for message in reversed(messages[-MAX_CONTEXT_ENTRIES:]):
        content = _truncate_utf8(_strip_data_uris(message.content), max_message_bytes)
        size = len(content.encode())
        if bounded and total + size > max_total_bytes:
            break
        bounded.append(ChatMessage(role=message.role, content=content))
        total += size
    bounded.reverse()
    if len(bounded) < len(messages):
        bounded.insert(
            0,
            ChatMessage(
                role="system",
                content=f"[{len(messages) - len(bounded)} older messages omitted]",
            ),
        )
    return bounded


def make_chat_start(request: ChatRequest) -> dict[str, Any]:
    """Serialize a protocol-v1 frame, progressively shrinking chat history."""
    attempts = (
        (MAX_CONTEXT_MESSAGE_BYTES, MAX_CONTEXT_TOTAL_BYTES, MAX_CHAT_START_BYTES),
        (MAX_CONTEXT_MESSAGE_BYTES // 2, MAX_CONTEXT_TOTAL_BYTES // 2, STRICT_CHAT_START_BYTES),
        (8 * 1024, 128 * 1024, STRICT_CHAT_START_BYTES),
        (4 * 1024, 64 * 1024, STRICT_CHAT_START_BYTES),
    )
    last_size = 0
    for max_message_bytes, max_total_bytes, frame_limit in attempts:
        payload = request.model_copy(
            update={
                "context": bound_context(
                    request.context,
                    max_message_bytes=max_message_bytes,
                    max_total_bytes=max_total_bytes,
                )
            }
        )
        frame: dict[str, Any] = {
            "type": "chat_start",
            "protocol_version": 1,
            "request": payload.model_dump(mode="json", exclude_none=True),
        }
        if parallel_tools_enabled():
            frame["capabilities"] = [PARALLEL_TOOL_REQUESTS_CAPABILITY]
        last_size = len(json.dumps(frame, separators=(",", ":")).encode())
        if last_size <= frame_limit:
            return frame
    raise ProtocolError(
        f"Chat history is too large to send ({last_size} bytes). "
        "Try starting a new chat or removing large inline images from prior turns.",
        code="PAYLOAD_TOO_LARGE",
    )


def websocket_url(base_url: str, *, agentic: bool = True) -> str:
    parsed = urlparse(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = "/api/copperpilot/analyze/agent/ws" if agentic else "/api/copperpilot/analyze/chat/ws"
    return urlunparse((scheme, parsed.netloc, path, "", "", ""))


def sanitize_status(value: str, limit: int = MAX_STATUS_BYTES) -> str:
    """Convert hosted status HTML to bounded terminal-safe plain markdown."""
    safe = re.sub(
        r"<(?:script|style)\b[^>]*>[\s\S]*?</(?:script|style)>",
        "",
        value,
        flags=re.IGNORECASE,
    )
    text = (
        markdownify(safe, escape_underscores=False).strip()
        if re.search(r"</?[A-Za-z][^>]*>", safe)
        else safe.strip()
    )
    text = _TERMINAL_ESCAPE.sub("", text)
    text = "".join(character for character in text if character in "\n\t" or ord(character) >= 32)
    return _truncate_utf8(text, limit)


def usage_reset_time(frame: dict[str, Any]) -> str | None:
    """Return when a usage block lifts, from an error frame's ``detail``.

    A 402/429 usage block carries the member-safe limits view in ``detail``:
    ``window_ends_at`` for a spent window and ``week_ends_at`` for a spent week.
    """
    detail = frame.get("detail")
    if not isinstance(detail, dict):
        return None
    reason = frame.get("block_reason") or detail.get("block_reason")
    key = {"window": "window_ends_at", "weekly": "week_ends_at"}.get(str(reason))
    value = detail.get(key) if key else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def error_message(value: Any) -> str:
    """Read a user-facing message from old string or structured error data."""
    if isinstance(value, dict):
        return str(value.get("message") or "Hosted chat failed.")
    return str(value or "Hosted chat failed.")


ToolHandler = Callable[[ToolRequest], Awaitable[Any]]


def tool_error_result(exc: BaseException) -> dict[str, Any]:
    """The tool_result payload reported when a local tool raises."""
    return {
        "error": f"{type(exc).__name__}: {exc}",
        "rejected": type(exc).__name__ == "ToolRejected",
    }


def tool_result_frame(conversation_id: str, tool_call_id: str, result: Any) -> dict[str, Any]:
    """Build one tool_result frame, replacing results over the frame limit."""
    frame = {
        "type": "tool_result",
        "conversation_id": conversation_id,
        "tool_call_id": tool_call_id,
        "result": result,
    }
    encoded = json.dumps(frame, separators=(",", ":")).encode()
    if len(encoded) > MAX_TOOL_RESULT_BYTES:
        frame["result"] = {"error": "Tool result exceeded the client frame limit."}
    return frame


class HostedChatClient:
    """One hosted CopperPilot chat stream."""

    def __init__(self, base_url: str, api_key: str, fingerprint: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.fingerprint = fingerprint
        self._socket: Any = None
        self._owner: str | None = None
        self.first_frame_timeout = 60.0
        # Long hosted reasoning or subagent runs can stay silent for minutes,
        # and websocket keepalive only detects a dead peer. A server that keeps
        # the socket open but stops sending would otherwise hang the turn
        # forever, so a generous idle bound turns that stall into an error.
        self.idle_frame_timeout: float | None = idle_frame_timeout()
        self.frame_budget = 10_000

    async def cancel(self) -> None:
        if self._socket is not None:
            await self._socket.send(json.dumps({"type": "cancel"}))

    async def stream(
        self,
        request: ChatRequest,
        tool_handler: ToolHandler,
    ) -> AsyncIterator[CopperEvent]:
        """Retry transient connection failures only before any event is observed."""
        for attempt in range(2):
            observed = False
            try:
                # Close the attempt deterministically so a batch's running tools
                # are cancelled as soon as the consumer stops listening.
                async with aclosing(self._stream_once(request, tool_handler)) as events:
                    async for event in events:
                        observed = True
                        yield event
                return
            except (
                OSError,
                TimeoutError,
                ConnectionClosed,
                InvalidHandshake,
                ProtocolError,
            ) as exc:
                info = classify_error(exc)
                logger.warning(
                    "Hosted chat transport failed before completion "
                    "(%s, type=%s, retryable=%s, observed=%s, attempt=%s): %s",
                    type(exc).__name__,
                    info.type,
                    info.retryable,
                    observed,
                    attempt + 1,
                    info.message,
                )
                if observed or attempt == 1 or not info.retryable:
                    raise
                await asyncio.sleep(0.25)

    async def _stream_once(
        self,
        request: ChatRequest,
        tool_handler: ToolHandler,
    ) -> AsyncGenerator[CopperEvent, None]:
        """Stream normalized events and fulfill local tool requests."""
        if self._socket is not None:
            raise ProtocolError("A chat stream is already active.")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": f"CopperPilot CLI/{__version__}",
            "X-CopperPilot-Fingerprint": self.fingerprint,
        }
        saw_final = False
        closed: ConnectionClosed | None = None
        url = websocket_url(self.base_url, agentic=request.agentic_mode)
        ssl_context = (
            ssl.create_default_context(cafile=certifi.where()) if url.startswith("wss://") else None
        )
        logger.info(
            "Opening hosted chat transport (host=%s, mode=%s)",
            urlparse(url).netloc,
            request.mode.value,
        )
        async with connect(
            url,
            additional_headers=headers,
            open_timeout=30,
            max_size=16 * 1024 * 1024,
            ssl=ssl_context,
        ) as socket:
            self._socket = socket
            self._owner = request.conversation_id
            await socket.send(json.dumps(make_chat_start(request), separators=(",", ":")))
            try:
                frame_count = 0
                stream_bytes = 0
                while not saw_final:
                    timeout = (
                        self.first_frame_timeout if frame_count == 0 else self.idle_frame_timeout
                    )
                    try:
                        raw = await asyncio.wait_for(socket.recv(), timeout)
                    except TimeoutError as exc:
                        phase = "first frame" if frame_count == 0 else "next frame"
                        message = (
                            f"Timed out waiting for the hosted chat {phase} "
                            f"(no data for {timeout:g} s)."
                        )
                        raise ProtocolError(
                            message,
                            code="CHAT_FIRST_FRAME_TIMEOUT"
                            if frame_count == 0
                            else "CHAT_IDLE_TIMEOUT",
                            retryable=True,
                        ) from exc
                    except ConnectionClosed as exc:
                        closed = exc
                        break
                    frame_count += 1
                    stream_bytes += len(raw.encode() if isinstance(raw, str) else raw)
                    if frame_count > self.frame_budget:
                        raise ProtocolError("Chat exceeded the client frame budget.")
                    if stream_bytes > MAX_STREAM_BYTES:
                        raise ProtocolError("Chat exceeded the client stream byte budget.")
                    try:
                        frame = json.loads(raw)
                    except (json.JSONDecodeError, TypeError) as exc:
                        raise ProtocolError("Hosted chat returned invalid JSON.") from exc
                    if not isinstance(frame, dict):
                        raise ProtocolError("Hosted chat frame must be a JSON object.")
                    frame_type = frame.get("type")
                    if frame_type == "error":
                        message = frame.get("public_message") or frame.get("message")
                        safe_message = sanitize_status(str(message or "Hosted chat failed."))
                        error = ProtocolError(
                            safe_message,
                            code=str(frame["code"]) if frame.get("code") is not None else None,
                            status=frame.get("status")
                            if isinstance(frame.get("status"), int)
                            else None,
                            block_reason=(
                                str(frame["block_reason"])
                                if frame.get("block_reason") is not None
                                else None
                            ),
                            can_purchase_overage=(
                                frame.get("can_purchase_overage")
                                if isinstance(frame.get("can_purchase_overage"), bool)
                                else None
                            ),
                            retryable=(
                                frame.get("retryable")
                                if isinstance(frame.get("retryable"), bool)
                                else None
                            ),
                            resets_at=usage_reset_time(frame),
                        )
                        yield CopperEvent(EventKind.ERROR, error.as_event_data())
                        raise error
                    if frame_type not in {"event", "final"}:
                        raise ProtocolError(f"Unknown frame type: {frame_type!r}")
                    try:
                        response = ChatResponse.model_validate(frame.get("chat_response") or {})
                    except ValueError as exc:
                        raise ProtocolError("Hosted chat response has an invalid shape.") from exc
                    metadata: dict[str, Any] = {
                        "turn_id": response.turn_id,
                        "sequence": response.sequence,
                        "resume_attempt": response.resume_attempt,
                    }
                    if response.response:
                        yield CopperEvent(
                            EventKind.TEXT,
                            sanitize_status(response.response, MAX_RESPONSE_TEXT_BYTES),
                            **metadata,
                        )
                    if response.reasoning_update:
                        yield CopperEvent(
                            EventKind.REASONING,
                            sanitize_status(response.reasoning_update),
                            sanitize_status(response.reasoning_title)
                            if response.reasoning_title
                            else None,
                            **metadata,
                        )
                    if response.status_update:
                        yield CopperEvent(
                            EventKind.STATUS, sanitize_status(response.status_update), **metadata
                        )
                    if response.status_event:
                        yield CopperEvent(EventKind.STATUS_EVENT, response.status_event, **metadata)
                    usage = response.usage
                    envelope_usage = frame.get("usage_details")
                    if isinstance(envelope_usage, dict):
                        usage = {**(usage or {}), **envelope_usage}
                    if usage:
                        yield CopperEvent(EventKind.USAGE, usage, **metadata)
                    if response.tool_request:
                        yield CopperEvent(EventKind.TOOL_REQUEST, response.tool_request, **metadata)
                        try:
                            result = await tool_handler(response.tool_request)
                        except Exception as exc:
                            result = tool_error_result(exc)
                        result_frame = tool_result_frame(
                            request.conversation_id, response.tool_request.tool_call_id, result
                        )
                        if self._owner != request.conversation_id:
                            raise ProtocolError("Chat socket owner changed during tool execution.")
                        try:
                            await socket.send(json.dumps(result_frame, separators=(",", ":")))
                        except ConnectionClosed as exc:
                            # The server went away while the tool ran; report it the
                            # same way as a drop between frames so the caller can resume.
                            closed = exc
                            break
                        yield CopperEvent(
                            EventKind.TOOL_RESULT,
                            {
                                "tool_call_id": response.tool_request.tool_call_id,
                                "tool_name": response.tool_request.tool_name,
                                "result": result_frame["result"],
                            },
                            **metadata,
                        )
                    if response.tool_requests:
                        for tool_request in response.tool_requests:
                            yield CopperEvent(EventKind.TOOL_REQUEST, tool_request, **metadata)
                        batch = run_tool_batch(
                            response.tool_requests,
                            tool_handler,
                            workspace=request.workspace_path,
                        )
                        try:
                            async for tool_request, outcome in batch:
                                result = (
                                    tool_error_result(outcome)
                                    if isinstance(outcome, BaseException)
                                    else outcome
                                )
                                result_frame = tool_result_frame(
                                    request.conversation_id, tool_request.tool_call_id, result
                                )
                                if self._owner != request.conversation_id:
                                    raise ProtocolError(
                                        "Chat socket owner changed during tool execution."
                                    )
                                try:
                                    await socket.send(
                                        json.dumps(result_frame, separators=(",", ":"))
                                    )
                                except ConnectionClosed as exc:
                                    closed = exc
                                    break
                                yield CopperEvent(
                                    EventKind.TOOL_RESULT,
                                    {
                                        "tool_call_id": tool_request.tool_call_id,
                                        "tool_name": tool_request.tool_name,
                                        "result": result_frame["result"],
                                    },
                                    **metadata,
                                )
                        finally:
                            await batch.aclose()
                        if closed is not None:
                            break
                    if frame_type == "final" or response.is_final:
                        saw_final = True
                        if response.success is False or response.error:
                            logger.warning(
                                "Hosted turn ended unsuccessfully (turn_id=%s, error=%r, "
                                "frame keys=%s, extra fields=%s)",
                                response.turn_id,
                                response.error,
                                sorted(frame),
                                sorted(response.model_extra or {}),
                            )
                        yield CopperEvent(
                            EventKind.FINAL,
                            {
                                "success": response.success,
                                "error": response.error,
                                "is_final": True,
                                "usage": response.usage,
                                "suggestions": response.suggestions,
                                "usage_details": envelope_usage
                                if isinstance(envelope_usage, dict)
                                else None,
                            },
                            **metadata,
                        )
                        break
            finally:
                self._socket = None
                self._owner = None
        if not saw_final:
            detail = _close_detail(closed) if closed is not None else "closed cleanly"
            logger.warning("Hosted chat socket closed before a final response (%s)", detail)
            raise ProtocolError(
                f"Chat socket closed before a final response ({detail}).",
                code="STREAM_ENDED_WITHOUT_FINAL",
                retryable=True,
            ) from closed
