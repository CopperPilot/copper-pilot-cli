"""MCP server for the CopperPilot tools, in-process or over stdio, plus a local relay.

``build_mcp_server(backend)`` makes one low-level ``mcp`` server from a
backend with ``list_tools``, ``call_tool`` and ``guide``. It is used:

* in-process for Claude (the Agent SDK serves it with no port and no child);
* over stdio by ``copper-pilot mcp --tools-session`` for Codex and any other
  MCP host.

When the TUI launches Codex, the stdio server is a thin relay: it connects
back to the TUI process over a private local socket (``--relay``), so the
TUI keeps the one tool socket per chat, the approval prompts, the
``copperpilot_subagent`` cards and the agent usage report. Without
``--relay`` the stdio server opens its own tool socket (standalone use).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import sys
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from mcp import types
from mcp.server.lowlevel import Server

from copper_pilot_cli._version import __version__
from copper_pilot_cli.agents.tool_proxy import GUIDE_URI, ToolOutcome

logger = logging.getLogger(__name__)

RELAY_TOKEN_ENV = "COPPER_PILOT_TOOLS_RELAY_TOKEN"  # noqa: S105 - an env var name
SERVER_NAME = "copperpilot"
INSTRUCTIONS = (
    "CopperPilot electronics tools. Read the guide resource copperpilot://guide. Use "
    "copperpilot_subagent for schematic, PCB and library edits."
)
_MAX_LINE = 64 * 1024 * 1024

ProgressFn = Callable[[str], Awaitable[None]]


class ToolBackend(Protocol):
    async def list_tools(self) -> list[dict[str, Any]]: ...

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, progress: ProgressFn | None = None
    ) -> ToolOutcome: ...

    async def read_guide(self) -> str: ...


class ProxyBackend:
    """A ``ToolProxy`` as an MCP backend."""

    def __init__(self, proxy: Any) -> None:
        self.proxy = proxy

    async def list_tools(self) -> list[dict[str, Any]]:
        return await self.proxy.list_tools()

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, progress: ProgressFn | None = None
    ) -> ToolOutcome:
        return await self.proxy.call_tool(name, arguments, progress=progress)

    async def read_guide(self) -> str:
        await self.proxy.start()
        return self.proxy.guide


def _content(item: dict[str, Any]) -> types.TextContent | types.ImageContent:
    if item.get("type") == "image":
        return types.ImageContent(
            type="image", data=str(item["data"]), mimeType=str(item.get("mimeType") or "image/png")
        )
    return types.TextContent(type="text", text=str(item.get("text") or ""))


def build_mcp_server(backend: ToolBackend) -> Server:
    """One MCP server exposing the backend's tools and the guide resource."""
    server: Server = Server(SERVER_NAME, version=__version__, instructions=INSTRUCTIONS)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=item["name"],
                description=item.get("description") or "",
                inputSchema=item.get("inputSchema") or {"type": "object", "properties": {}},
            )
            for item in await backend.list_tools()
        ]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        progress = _progress_reporter(server)
        outcome = await backend.call_tool(name, dict(arguments or {}), progress=progress)
        return types.CallToolResult(
            content=[_content(item) for item in outcome.content], isError=outcome.is_error
        )

    @server.list_resources()
    async def list_resources() -> list[types.Resource]:
        return [
            types.Resource(
                uri=GUIDE_URI,
                name="CopperPilot guide",
                description="How to use the CopperPilot tools in this session.",
                mimeType="text/markdown",
            )
        ]

    @server.read_resource()
    async def read_resource(uri: Any) -> str:
        if str(uri) != GUIDE_URI:
            raise ValueError(f"Unknown resource: {uri}")
        return await backend.read_guide()

    return server


def _progress_reporter(server: Server) -> ProgressFn | None:
    """MCP progress notifications for the current request, when the client asked."""
    try:
        context = server.request_context
    except LookupError:
        return None
    token = context.meta.progressToken if context.meta is not None else None
    if token is None:
        return None
    counter = {"n": 0}

    async def report(message: str) -> None:
        counter["n"] += 1
        with contextlib.suppress(Exception):
            await context.session.send_progress_notification(
                token, float(counter["n"]), message=message[:240]
            )

    return report


async def serve_stdio(backend: ToolBackend) -> None:
    from mcp.server.stdio import stdio_server

    server = build_mcp_server(backend)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


# ------------------------------------------------------------------- relay


class RelayServer:
    """Serves one backend to ``mcp --tools-session --relay`` children of this process."""

    def __init__(self, backend: ToolBackend) -> None:
        self.backend = backend
        self.token = secrets.token_urlsafe(32)
        self.address = ""
        self._server: asyncio.AbstractServer | None = None
        self._directory: tempfile.TemporaryDirectory[str] | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> str:
        if self._server is not None:
            return self.address
        if hasattr(asyncio, "start_unix_server") and os.name != "nt":
            self._directory = tempfile.TemporaryDirectory(prefix="cp-relay-")
            os.chmod(self._directory.name, 0o700)
            path = Path(self._directory.name) / "tools.sock"
            self._server = await asyncio.start_unix_server(
                self._serve, path=str(path), limit=_MAX_LINE
            )
            self.address = f"unix:{path}"
        else:
            self._server = await asyncio.start_server(
                self._serve, host="127.0.0.1", port=0, limit=_MAX_LINE
            )
            port = self._server.sockets[0].getsockname()[1]
            self.address = f"tcp:127.0.0.1:{port}"
        return self.address

    async def aclose(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        for task in list(self._tasks):
            task.cancel()
        if self._directory is not None:
            self._directory.cleanup()
            self._directory = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        lock = asyncio.Lock()
        calls: dict[int, asyncio.Task[None]] = {}

        async def send(message: dict[str, Any]) -> None:
            async with lock:
                writer.write(json.dumps(message, default=str).encode() + b"\n")
                await writer.drain()

        try:
            hello = json.loads(await reader.readline() or b"{}")
            if not isinstance(hello, dict) or not secrets.compare_digest(
                str(hello.get("token") or ""), self.token
            ):
                logger.warning("Tools relay refused a client with a bad token")
                return
            await send({"id": hello.get("id"), "result": "ok"})
            while line := await reader.readline():
                message = json.loads(line)
                if not isinstance(message, dict):
                    continue
                if message.get("op") == "cancel":
                    task = calls.get(int(message.get("target") or -1))
                    if task is not None:
                        task.cancel()
                    continue
                identifier = int(message.get("id") or 0)
                task = asyncio.create_task(self._answer(message, identifier, send))
                calls[identifier] = task
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
                task.add_done_callback(lambda _t, key=identifier: calls.pop(key, None))
        except (ConnectionError, json.JSONDecodeError, ValueError) as exc:
            logger.info("Tools relay client left (%s)", type(exc).__name__)
        finally:
            for task in calls.values():
                task.cancel()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _answer(
        self,
        message: dict[str, Any],
        identifier: int,
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        op = message.get("op")
        try:
            if op == "list_tools":
                result: Any = await self.backend.list_tools()
            elif op == "guide":
                result = await self.backend.read_guide()
            elif op == "call_tool":

                async def progress(text: str) -> None:
                    await send({"id": identifier, "progress": text})

                arguments = message.get("arguments")
                outcome = await self.backend.call_tool(
                    str(message.get("name") or ""),
                    arguments if isinstance(arguments, dict) else {},
                    progress=progress,
                )
                result = {"content": outcome.content, "is_error": outcome.is_error}
            else:
                raise ValueError(f"unknown relay op {op!r}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - returned to the relay client
            with contextlib.suppress(Exception):
                await send({"id": identifier, "error": str(exc) or type(exc).__name__})
            return
        with contextlib.suppress(Exception):
            await send({"id": identifier, "result": result})


class RelayBackend:
    """The stdio child's view of a ``RelayServer`` in the parent process."""

    def __init__(self, address: str, token: str) -> None:
        self.address = address
        self.token = token
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[int, tuple[asyncio.Future[Any], ProgressFn | None]] = {}
        self._next = 0
        self._lock = asyncio.Lock()
        self._read_task: asyncio.Task[None] | None = None

    async def _connect(self) -> None:
        if self._writer is not None:
            return
        async with self._lock:
            if self._writer is not None:
                return
            kind, _, rest = self.address.partition(":")
            if kind == "unix":
                reader, writer = await asyncio.open_unix_connection(rest, limit=_MAX_LINE)
            elif kind == "tcp":
                host, _, port = rest.rpartition(":")
                reader, writer = await asyncio.open_connection(host, int(port), limit=_MAX_LINE)
            else:
                raise ValueError(f"Unknown relay address {self.address!r}")
            writer.write(json.dumps({"id": 0, "token": self.token}).encode() + b"\n")
            await writer.drain()
            hello = json.loads(await reader.readline() or b"{}")
            if hello.get("result") != "ok":
                writer.close()
                raise ConnectionError("The CopperPilot tools relay refused this process.")
            self._reader, self._writer = reader, writer
            self._read_task = asyncio.create_task(self._read())

    async def _read(self) -> None:
        assert self._reader is not None
        try:
            while line := await self._reader.readline():
                message = json.loads(line)
                entry = self._pending.get(int(message.get("id") or 0))
                if entry is None:
                    continue
                future, progress = entry
                if "progress" in message:
                    if progress is not None:
                        with contextlib.suppress(Exception):
                            await progress(str(message["progress"]))
                elif "error" in message:
                    if not future.done():
                        future.set_exception(RuntimeError(str(message["error"])))
                elif not future.done():
                    future.set_result(message.get("result"))
        finally:
            error = ConnectionError("The CopperPilot tools relay closed.")
            for future, _progress in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            self._writer = None

    async def _request(self, payload: dict[str, Any], progress: ProgressFn | None = None) -> Any:
        await self._connect()
        assert self._writer is not None
        self._next += 1
        identifier = self._next
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[identifier] = (future, progress)
        try:
            self._writer.write(json.dumps({"id": identifier, **payload}).encode() + b"\n")
            await self._writer.drain()
            return await future
        except asyncio.CancelledError:
            if self._writer is not None:
                with contextlib.suppress(Exception):
                    self._writer.write(
                        json.dumps({"op": "cancel", "target": identifier}).encode() + b"\n"
                    )
            raise
        finally:
            self._pending.pop(identifier, None)

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._request({"op": "list_tools"})
        return list(result or [])

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, progress: ProgressFn | None = None
    ) -> ToolOutcome:
        try:
            result = await self._request(
                {"op": "call_tool", "name": name, "arguments": arguments}, progress
            )
        except (ConnectionError, OSError, RuntimeError) as exc:
            return ToolOutcome.text(f"CopperPilot tools are unavailable: {exc}", is_error=True)
        if not isinstance(result, dict):
            return ToolOutcome.text("CopperPilot returned no result.", is_error=True)
        return ToolOutcome(list(result.get("content") or []), result.get("is_error") is True)

    async def read_guide(self) -> str:
        return str(await self._request({"op": "guide"}) or "")


def relay_backend_from_env(address: str) -> RelayBackend:
    token = os.environ.get(RELAY_TOKEN_ENV, "")
    if not token:
        print(
            f"copper-pilot: --relay needs {RELAY_TOKEN_ENV} in the environment",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return RelayBackend(address, token)


__all__ = [
    "RELAY_TOKEN_ENV",
    "ProxyBackend",
    "RelayBackend",
    "RelayServer",
    "ToolBackend",
    "build_mcp_server",
    "relay_backend_from_env",
    "serve_stdio",
]
