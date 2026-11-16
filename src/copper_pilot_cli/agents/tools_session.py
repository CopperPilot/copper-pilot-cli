"""Client for the CopperPilot tool session socket (``WS /api/copperpilot/tools/ws``).

One socket per chat. It lists the curated, read-only CopperPilot tools and the
guide (``tool_session_ready``), runs ``tool_call``s, and answers the server's
bridge ``tool_request``s (file reads, writes, shell) with the local tool
broker under the user's approval rules, exactly as the chat socket does.
Writes are compare-and-swap (``cas_writes_v1``).

The guide and the tool schemas live in RAM only: they are never logged and
never written to ``sessions.db``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import ssl
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import certifi
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from copper_pilot_cli._version import __version__
from copper_pilot_cli.copper_auth import DeviceCredential
from copper_pilot_cli.copper_protocol import (
    PING_INTERVAL_SECONDS,
    PING_TIMEOUT_SECONDS,
    ToolRequest,
    sanitize_status,
    tool_error_result,
    tool_result_frame,
)
from copper_pilot_cli.copper_workspace import build_initial_file_tree, discover_workspace

logger = logging.getLogger(__name__)

TOOLS_WS_PATH = "/api/copperpilot/tools/ws"
PROTOCOL_VERSION = 1
CAPABILITIES = ("parallel_tool_requests", "cas_writes_v1")
READY_TIMEOUT_SECONDS = 30.0
# A dropped socket reconnects; a pending call waits this long for it.
RECONNECT_WINDOW_SECONDS = 60.0
_RECONNECT_DELAYS = (0.5, 1.0, 2.0, 4.0, 8.0, 15.0)

ProgressCallback = Callable[[str, str], Awaitable[None] | None]
ToolExecutor = Callable[[ToolRequest], Awaitable[Any]]


class ToolSessionError(RuntimeError):
    """A session-level failure of the tool socket."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
        retryable: bool = False,
        block_reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.block_reason = block_reason


@dataclass(slots=True)
class ToolSessionInfo:
    session_id: str
    tools: list[dict[str, Any]] = field(default_factory=list)
    guide_title: str = "CopperPilot guide"
    guide_text: str = ""
    limits: dict[str, Any] = field(default_factory=dict)


def tools_ws_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse((scheme, parsed.netloc, TOOLS_WS_PATH, "", "", ""))


def detect_eda(workspace: Path, schematic: str | None, pcb: str | None) -> str | None:
    names = [str(item).lower() for item in (schematic, pcb) if item]
    if any(name.endswith((".kicad_sch", ".kicad_pcb")) for name in names):
        return "kicad"
    if any(name.endswith((".schdoc", ".pcbdoc")) for name in names):
        return "altium"
    with contextlib.suppress(OSError):
        for path in workspace.iterdir():
            suffix = path.suffix.lower()
            if suffix in {".kicad_pro", ".kicad_sch", ".kicad_pcb"}:
                return "kicad"
            if suffix in {".prjpcb", ".schdoc", ".pcbdoc"}:
                return "altium"
    return None


def _absolute(workspace: Path, value: str | None) -> str | None:
    return str((workspace / value).resolve()) if value else None


def content_text(result: dict[str, Any]) -> str:
    """Plain text of a ``tool_call_result`` (images summarized)."""
    parts = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
        elif isinstance(item, dict) and item.get("type") == "image":
            parts.append(f"[image {item.get('mime_type') or item.get('mimeType') or ''}]")
    error = result.get("error")
    if not parts and isinstance(error, dict):
        parts.append(str(error.get("message") or error.get("code") or "Tool failed."))
    return "\n".join(parts)


class ToolSessionClient:
    """One tool session socket for one chat (agent session)."""

    def __init__(
        self,
        credential: DeviceCredential,
        *,
        workspace: Path,
        conversation_id: str,
        executor: ToolExecutor,
        agent: dict[str, Any],
        on_progress: ProgressCallback | None = None,
        connect: Callable[..., Any] = ws_connect,
        reconnect_window: float = RECONNECT_WINDOW_SECONDS,
    ) -> None:
        self.credential = credential
        self.workspace = workspace.resolve()
        self.conversation_id = conversation_id
        self.executor = executor
        self.agent = agent
        self.on_progress = on_progress
        self._connect = connect
        self.reconnect_window = reconnect_window
        self.info: ToolSessionInfo | None = None
        self._socket: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._pending: dict[str, tuple[dict[str, Any], asyncio.Future[dict[str, Any]]]] = {}
        self._bridge: set[asyncio.Task[None]] = set()
        self._failure: ToolSessionError | None = None
        self._closing = False
        self._semaphore: asyncio.Semaphore | None = None
        self._send_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()

    # ------------------------------------------------------------- lifecycle

    @property
    def guide(self) -> str:
        return self.info.guide_text if self.info is not None else ""

    @property
    def tools(self) -> list[dict[str, Any]]:
        return list(self.info.tools) if self.info is not None else []

    def _start_frame(self, session_id: str) -> dict[str, Any]:
        context = discover_workspace(self.workspace)
        tree = build_initial_file_tree(self.workspace)
        return {
            "type": "tool_session_start",
            "protocol_version": PROTOCOL_VERSION,
            "conversation_id": self.conversation_id,
            "session_id": session_id,
            "workspace_path": str(self.workspace),
            "schematic_path": _absolute(self.workspace, context.schematic_path),
            "pcb_path": _absolute(self.workspace, context.pcb_path),
            "initial_file_tree": [line for line in tree.split("\n") if line],
            "eda": detect_eda(self.workspace, context.schematic_path, context.pcb_path),
            "agent": {"client": "cli", "version": __version__, **self.agent},
            "capabilities": list(CAPABILITIES),
        }

    async def start(self) -> ToolSessionInfo:
        """Open the socket and wait for ``tool_session_ready`` (idempotent)."""
        async with self._start_lock:
            if self._failure is not None:
                raise self._failure
            if self.info is not None and self._ready.is_set():
                return self.info
            await self._open()
            assert self.info is not None
            return self.info

    async def _open(self) -> None:
        url = tools_ws_url(self.credential.base_url)
        headers = {
            "Authorization": f"Bearer {self.credential.api_key}",
            "User-Agent": f"CopperPilot CLI/{__version__}",
            "X-CopperPilot-Fingerprint": self.credential.fingerprint,
        }
        ssl_context = (
            ssl.create_default_context(cafile=certifi.where()) if url.startswith("wss://") else None
        )
        session_id = str(uuid.uuid4())
        try:
            socket = await self._connect(
                url,
                additional_headers=headers,
                open_timeout=30,
                max_size=16 * 1024 * 1024,
                ping_interval=PING_INTERVAL_SECONDS,
                ping_timeout=PING_TIMEOUT_SECONDS,
                ssl=ssl_context,
            )
        except InvalidStatus as exc:
            status = exc.response.status_code
            raise ToolSessionError(
                f"the tool session was refused (HTTP {status})",
                code=f"http_{status}",
                status=status,
                retryable=status >= 500 or status == 429,
            ) from exc
        except (OSError, TimeoutError, InvalidHandshake) as exc:
            raise ToolSessionError(
                f"could not reach CopperPilot ({type(exc).__name__})", retryable=True
            ) from exc
        try:
            await socket.send(json.dumps(self._start_frame(session_id), separators=(",", ":")))
            async with asyncio.timeout(READY_TIMEOUT_SECONDS):
                while True:
                    frame = json.loads(await socket.recv())
                    if not isinstance(frame, dict):
                        continue
                    if frame.get("type") == "error":
                        raise _frame_error(frame)
                    if frame.get("type") == "tool_session_ready":
                        break
        except BaseException:
            with contextlib.suppress(Exception):
                await socket.close()
            raise
        guide = frame.get("guide") if isinstance(frame.get("guide"), dict) else {}
        tools = [
            item
            for item in frame.get("tools") or []
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]
        limits = frame.get("limits") if isinstance(frame.get("limits"), dict) else {}
        self.info = ToolSessionInfo(
            session_id=str(frame.get("session_id") or session_id),
            tools=tools,
            guide_title=str(guide.get("title") or "CopperPilot guide"),
            guide_text=str(guide.get("text") or ""),
            limits=limits,
        )
        concurrency = limits.get("max_concurrent_calls")
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(
                concurrency if isinstance(concurrency, int) and concurrency > 0 else 4
            )
        self._socket = socket
        self._ready.set()
        # Never log the guide or schemas: names and counts only.
        logger.info(
            "Tool session ready (session=%s, tools=%s, guide_chars=%s)",
            self.info.session_id,
            len(tools),
            len(self.info.guide_text),
        )
        self._reader = asyncio.create_task(self._read(socket))
        # Calls that were in flight when the previous socket dropped run again
        # (the tools are read-only; the server kept nothing).
        for call_id, (frame_out, future) in list(self._pending.items()):
            if not future.done():
                await self._send(frame_out)
                logger.info("Re-sent tool call %s after reconnect", call_id)

    async def aclose(self) -> None:
        self._closing = True
        self._ready.clear()
        for task in list(self._bridge):
            task.cancel()
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        if self._socket is not None:
            with contextlib.suppress(Exception):
                await self._socket.close()
        self._socket = None
        self._fail_pending(ToolSessionError("the tool session closed", retryable=True))

    # ---------------------------------------------------------------- calls

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run one CopperPilot tool; returns the ``tool_call_result`` frame."""
        await self.start()
        assert self._semaphore is not None
        async with self._semaphore:
            call_id = f"call_{uuid.uuid4().hex}"
            frame = {"type": "tool_call", "call_id": call_id, "name": name, "arguments": arguments}
            future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            self._pending[call_id] = (frame, future)
            try:
                await self._wait_ready()
                await self._send(frame)
                return await future
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    await self._send({"type": "cancel", "call_id": call_id})
                raise
            finally:
                self._pending.pop(call_id, None)

    async def cancel_all(self) -> None:
        if self._socket is not None and self._pending:
            with contextlib.suppress(Exception):
                await self._send({"type": "cancel"})

    async def send_usage(
        self,
        *,
        provider: str,
        model: str | None,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
        turn_id: str | None = None,
    ) -> None:
        """Report the agent's own token use (logged at zero cost; never billed)."""
        if self._socket is None or not self._ready.is_set():
            return
        with contextlib.suppress(Exception):
            await self._send(
                {
                    "type": "agent_usage",
                    "turn_id": turn_id or str(uuid.uuid4()),
                    "provider": provider,
                    "model": model or "",
                    "input_tokens": max(0, int(input_tokens)),
                    "output_tokens": max(0, int(output_tokens)),
                    "cached_input_tokens": max(0, int(cached_input_tokens)),
                }
            )

    async def _wait_ready(self) -> None:
        if self._failure is not None:
            raise self._failure
        if self._ready.is_set():
            return
        try:
            async with asyncio.timeout(self.reconnect_window):
                await self._ready.wait()
        except TimeoutError as exc:
            raise ToolSessionError(
                "the connection to CopperPilot did not come back", retryable=True
            ) from exc
        if self._failure is not None:
            raise self._failure

    async def _send(self, frame: dict[str, Any]) -> None:
        socket = self._socket
        if socket is None:
            raise ToolSessionError("the tool session is not connected", retryable=True)
        async with self._send_lock:
            await socket.send(json.dumps(frame, separators=(",", ":"), default=str))

    def _fail_pending(self, error: ToolSessionError) -> None:
        for _frame, future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)

    # --------------------------------------------------------------- reader

    async def _read(self, socket: Any) -> None:
        try:
            async for raw in socket:
                try:
                    frame = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    logger.warning("Tool session sent invalid JSON")
                    continue
                if isinstance(frame, dict):
                    await self._handle(frame)
        except ConnectionClosed as exc:
            logger.info("Tool session socket closed (%s)", exc.rcvd.code if exc.rcvd else None)
        except asyncio.CancelledError:
            raise
        except ToolSessionError as exc:
            self._ready.clear()
            if exc.retryable and not self._closing:
                asyncio.create_task(self._reconnect())
            else:
                self._failure = exc
                self._fail_pending(exc)
            return
        self._ready.clear()
        if not self._closing:
            asyncio.create_task(self._reconnect())

    async def _handle(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        if kind == "tool_call_result":
            call_id = str(frame.get("call_id") or "")
            entry = self._pending.get(call_id)
            if entry is not None and not entry[1].done():
                entry[1].set_result(frame)
        elif kind == "tool_request":
            task = asyncio.create_task(self._serve_bridge(frame))
            self._bridge.add(task)
            task.add_done_callback(self._bridge.discard)
        elif kind == "tool_progress":
            callback = self.on_progress
            if callback is not None:
                outcome = callback(
                    str(frame.get("call_id") or ""),
                    sanitize_status(str(frame.get("message") or "")),
                )
                if asyncio.iscoroutine(outcome):
                    await outcome
        elif kind == "error":
            raise _frame_error(frame)

    async def _serve_bridge(self, frame: dict[str, Any]) -> None:
        """Answer one bridge ``tool_request`` with the local broker."""
        raw_arguments = frame.get("arguments")
        arguments: dict[str, Any] = raw_arguments if isinstance(raw_arguments, dict) else {}
        request = ToolRequest(
            tool_call_id=str(frame.get("tool_call_id") or uuid.uuid4()),
            tool_name=str(frame.get("tool_name") or ""),
            arguments=arguments,
            parent_call_id=frame.get("parent_call_id")
            if isinstance(frame.get("parent_call_id"), str)
            else None,
            if_match=frame.get("if_match") if isinstance(frame.get("if_match"), str) else None,
            timeout_ms=frame.get("timeout_ms")
            if isinstance(frame.get("timeout_ms"), int)
            else None,
            agent_id=frame.get("agent_id") if isinstance(frame.get("agent_id"), str) else None,
        )
        try:
            result = await self.executor(request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported to the server per tool
            result = tool_error_result(exc)
        # cas_writes_v1: the hash of the whole file travels next to ``result``.
        digest = result.pop("content_sha256", None) if isinstance(result, dict) else None
        reply = tool_result_frame(self.conversation_id, request.tool_call_id, result)
        if isinstance(digest, str):
            reply["content_sha256"] = digest
        try:
            await self._send(reply)
        except (ConnectionClosed, ToolSessionError):
            logger.info("Bridge result %s was not delivered (socket closed)", request.tool_call_id)

    async def _reconnect(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.reconnect_window
        for delay in (0.0, *_RECONNECT_DELAYS):
            if self._closing:
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(delay, remaining))
            try:
                async with self._start_lock:
                    await self._open()
                logger.info("Tool session reconnected")
                return
            except ToolSessionError as exc:
                if not exc.retryable:
                    self._failure = exc
                    self._fail_pending(exc)
                    self._ready.set()
                    return
                logger.info("Tool session reconnect failed: %s", exc)
            except Exception as exc:  # noqa: BLE001 - keep trying inside the window
                logger.info("Tool session reconnect failed (%s)", type(exc).__name__)
        error = ToolSessionError("the connection to CopperPilot did not come back", retryable=True)
        self._failure = error
        self._fail_pending(error)
        self._ready.set()


def _frame_error(frame: dict[str, Any]) -> ToolSessionError:
    message = frame.get("public_message") or frame.get("message") or "Tool session failed."
    status = frame.get("status") if isinstance(frame.get("status"), int) else None
    code = str(frame["code"]) if frame.get("code") is not None else None
    retryable = frame.get("retryable") is True or code in {"server_restarting", "socket_rotation"}
    return ToolSessionError(
        sanitize_status(str(message)),
        code=code,
        status=status,
        retryable=retryable,
        block_reason=str(frame["block_reason"]) if frame.get("block_reason") else None,
    )


__all__ = [
    "CAPABILITIES",
    "TOOLS_WS_PATH",
    "ToolSessionClient",
    "ToolSessionError",
    "ToolSessionInfo",
    "content_text",
    "detect_eda",
    "tools_ws_url",
]
