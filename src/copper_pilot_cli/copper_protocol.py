"""Typed CopperPilot client protocol derived from the desktop harness."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlparse, urlunparse

import certifi
from markdownify import markdownify
from pydantic import BaseModel, ConfigDict, Field
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

MAX_CHAT_START_BYTES = 12 * 1024 * 1024
STRICT_CHAT_START_BYTES = 8 * 1024 * 1024
MAX_TOOL_RESULT_BYTES = 8 * 1024 * 1024
MAX_CONTEXT_MESSAGE_BYTES = 64 * 1024
MAX_CONTEXT_TOTAL_BYTES = 4 * 1024 * 1024
MAX_CONTEXT_ENTRIES = 2_000
MAX_STREAM_BYTES = 64 * 1024 * 1024


class ProtocolError(RuntimeError):
    """The hosted protocol was malformed or incomplete."""


class CopperMode(StrEnum):
    """CopperPilot chat modes."""

    AGENT = "agent"
    PLAN = "plan"
    ASK = "ask"


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


def bound_context(messages: Sequence[ChatMessage]) -> list[ChatMessage]:
    """Apply the desktop client's bounded newest-first history policy."""
    bounded: list[ChatMessage] = []
    total = 0
    for message in reversed(messages[-MAX_CONTEXT_ENTRIES:]):
        content = _truncate_utf8(_strip_data_uris(message.content), MAX_CONTEXT_MESSAGE_BYTES)
        size = len(content.encode())
        if bounded and total + size > MAX_CONTEXT_TOTAL_BYTES:
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
    """Serialize and size-check a protocol-v1 chat start frame."""
    payload = request.model_copy(update={"context": bound_context(request.context)})
    frame = {
        "type": "chat_start",
        "protocol_version": 1,
        "request": payload.model_dump(mode="json", exclude_none=True),
    }
    size = len(json.dumps(frame, separators=(",", ":")).encode())
    if size > MAX_CHAT_START_BYTES:
        raise ProtocolError(f"Chat start frame is too large ({size} bytes).")
    return frame


def websocket_url(base_url: str, *, agentic: bool = True) -> str:
    parsed = urlparse(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = "/api/copperpilot/analyze/agent/ws" if agentic else "/api/copperpilot/analyze/chat/ws"
    return urlunparse((scheme, parsed.netloc, path, "", "", ""))


def sanitize_status(value: str) -> str:
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
    return _truncate_utf8(text, 16 * 1024)


ToolHandler = Callable[[ToolRequest], Awaitable[Any]]


class HostedChatClient:
    """One hosted CopperPilot chat stream."""

    def __init__(self, base_url: str, api_key: str, fingerprint: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.fingerprint = fingerprint
        self._socket: Any = None
        self._owner: str | None = None
        self.first_frame_timeout = 60.0
        self.idle_frame_timeout = 300.0
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
                async for event in self._stream_once(request, tool_handler):
                    observed = True
                    yield event
                return
            except (OSError, TimeoutError, ConnectionClosed) as exc:
                logger.warning(
                    "Hosted chat transport failed before completion (%s, observed=%s, attempt=%s)",
                    type(exc).__name__,
                    observed,
                    attempt + 1,
                )
                if observed or attempt == 1:
                    raise
                await asyncio.sleep(0.25)

    async def _stream_once(
        self,
        request: ChatRequest,
        tool_handler: ToolHandler,
    ) -> AsyncIterator[CopperEvent]:
        """Stream normalized events and fulfill local tool requests."""
        if self._socket is not None:
            raise ProtocolError("A chat stream is already active.")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "CopperPilot CLI/0.1.0",
            "X-CopperPilot-Fingerprint": self.fingerprint,
        }
        saw_final = False
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
                        message = f"Timed out waiting for the hosted chat {phase}."
                        raise ProtocolError(message) from exc
                    except ConnectionClosed:
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
                        yield CopperEvent(EventKind.ERROR, safe_message)
                        raise ProtocolError(safe_message)
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
                            EventKind.TEXT, sanitize_status(response.response), **metadata
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
                            result = {
                                "error": f"{type(exc).__name__}: {exc}",
                                "rejected": type(exc).__name__ == "ToolRejected",
                            }
                        result_frame = {
                            "type": "tool_result",
                            "conversation_id": request.conversation_id,
                            "tool_call_id": response.tool_request.tool_call_id,
                            "result": result,
                        }
                        encoded = json.dumps(result_frame, separators=(",", ":")).encode()
                        if len(encoded) > MAX_TOOL_RESULT_BYTES:
                            result_frame["result"] = {
                                "error": "Tool result exceeded the client frame limit."
                            }
                        if self._owner != request.conversation_id:
                            raise ProtocolError("Chat socket owner changed during tool execution.")
                        await socket.send(json.dumps(result_frame, separators=(",", ":")))
                        yield CopperEvent(
                            EventKind.TOOL_RESULT,
                            {
                                "tool_call_id": response.tool_request.tool_call_id,
                                "tool_name": response.tool_request.tool_name,
                                "result": result_frame["result"],
                            },
                            **metadata,
                        )
                    if frame_type == "final" or response.is_final:
                        saw_final = True
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
            raise ProtocolError("Chat socket closed before a final response.")
