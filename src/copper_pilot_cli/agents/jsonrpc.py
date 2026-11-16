"""A small asyncio JSON-RPC client over a child process's stdio (newline-delimited).

Used for ``codex app-server``, whose messages are JSON-RPC 2.0 objects
without the ``"jsonrpc"`` member, one per line. The child may also send
requests (approvals, user input); ``on_request`` answers them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)

RequestHandler = Callable[[str, Any], Awaitable[Any]]
NotificationHandler = Callable[[str, Any], None]
_LINE_LIMIT = 64 * 1024 * 1024
_STDERR_TAIL = 40


class JsonRpcError(RuntimeError):
    """An error response from the peer."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class ProcessExited(RuntimeError):
    """The child exited while a request was pending."""

    def __init__(self, returncode: int | None, stderr_tail: str = "") -> None:
        super().__init__(f"process exited with code {returncode}")
        self.returncode = returncode
        self.stderr_tail = stderr_tail


class MethodNotFound(Exception):
    """Raise from ``on_request`` to answer -32601."""


class JsonRpcProcess:
    """One JSON-RPC peer running as a child process."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        on_request: RequestHandler | None = None,
        on_notification: NotificationHandler | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self.argv = list(argv)
        self.on_request = on_request
        self.on_notification = on_notification
        self.env = dict(env) if env is not None else None
        self.cwd = cwd
        self.process: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 0
        self._write_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._reader: asyncio.Task[None] | None = None
        self._stderr_reader: asyncio.Task[None] | None = None
        self._stderr: deque[str] = deque(maxlen=_STDERR_TAIL)
        self.exited: asyncio.Future[int | None] | None = None

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr)

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def start(self) -> None:
        kwargs: dict[str, Any] = {}
        if os.name != "nt":
            # Own process group, so closing kills the agent's whole tree.
            kwargs["start_new_session"] = True
        self.process = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
            cwd=self.cwd,
            limit=_LINE_LIMIT,
            **kwargs,
        )
        self.exited = asyncio.get_running_loop().create_future()
        self._reader = asyncio.create_task(self._read())
        self._stderr_reader = asyncio.create_task(self._read_stderr())

    async def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        with contextlib.suppress(Exception):
            while line := await self.process.stderr.readline():
                # Diagnostics only; may hold paths but never our guide or schemas.
                self._stderr.append(line.decode("utf-8", "replace").rstrip()[:500])

    async def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while line := await self.process.stdout.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Ignoring a non-JSON line from %s", self.argv[0])
                    continue
                if isinstance(message, dict):
                    self._dispatch(message)
        except (asyncio.CancelledError, ConnectionError):
            pass
        finally:
            code = await self._wait_exit()
            error = ProcessExited(code, self.stderr_tail)
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            self._pending.clear()
            if self.exited is not None and not self.exited.done():
                self.exited.set_result(code)

    async def _wait_exit(self) -> int | None:
        if self.process is None:
            return None
        with contextlib.suppress(Exception):
            async with asyncio.timeout(5):
                return await self.process.wait()
        return self.process.returncode

    def _dispatch(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if isinstance(method, str) and "id" in message:
            task = asyncio.create_task(self._answer(message["id"], method, message.get("params")))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        elif isinstance(method, str):
            if self.on_notification is not None:
                try:
                    self.on_notification(method, message.get("params"))
                except Exception:  # noqa: BLE001 - one bad handler must not stop the reader
                    logger.warning("Notification handler failed for %s", method, exc_info=True)
        elif "id" in message:
            future = (
                self._pending.pop(message["id"], None) if isinstance(message["id"], int) else None
            )
            if future is None or future.done():
                return
            error = message.get("error")
            if isinstance(error, dict):
                future.set_exception(
                    JsonRpcError(
                        int(error.get("code") or -32000),
                        str(error.get("message") or "error"),
                        error.get("data"),
                    )
                )
            else:
                future.set_result(message.get("result"))

    async def _answer(self, identifier: Any, method: str, params: Any) -> None:
        try:
            if self.on_request is None:
                raise MethodNotFound(method)
            result = await self.on_request(method, params)
        except MethodNotFound:
            await self._write(
                {"id": identifier, "error": {"code": -32601, "message": f"{method} unsupported"}}
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported to the peer
            await self._write({"id": identifier, "error": {"code": -32000, "message": str(exc)}})
        else:
            await self._write({"id": identifier, "result": result})

    async def _write(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.returncode is not None:
            return
        data = json.dumps(message, separators=(",", ":"), default=str).encode() + b"\n"
        async with self._write_lock:
            with contextlib.suppress(ConnectionError, BrokenPipeError):
                process.stdin.write(data)
                await process.stdin.drain()

    async def request(
        self,
        method: str,
        params: Any = None,
        *,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> Any:
        if not self.running:
            raise ProcessExited(self.process.returncode if self.process else None, self.stderr_tail)
        self._next_id += 1
        identifier = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[identifier] = future
        message: dict[str, Any] = {"id": identifier, "method": method}
        if params is not None:
            message["params"] = params
        await self._write(message)
        try:
            if timeout is None:
                return await future
            async with asyncio.timeout(timeout):
                return await future
        finally:
            self._pending.pop(identifier, None)

    async def notify(self, method: str, params: Any = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        await self._write(message)

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        process = self.process
        if process is not None and process.returncode is None:
            if process.stdin is not None:
                with contextlib.suppress(Exception):
                    process.stdin.close()
            try:
                async with asyncio.timeout(2):
                    await process.wait()
            except TimeoutError:
                kill_tree(process)
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(3):
                        await process.wait()
        for task in (self._reader, self._stderr_reader):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


def kill_tree(process: asyncio.subprocess.Process) -> None:
    """Kill the child and its process group (POSIX) or the child (Windows)."""
    with contextlib.suppress(ProcessLookupError, OSError):
        if os.name != "nt" and sys.platform != "win32":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.kill()


__all__ = ["JsonRpcError", "JsonRpcProcess", "MethodNotFound", "ProcessExited", "kill_tree"]
