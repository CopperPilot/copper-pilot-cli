"""Foreground-to-background shell execution, ported from the desktop harness.

The desktop client runs a shell command in the foreground for a bounded
window and then, instead of killing it, hands it to a background manager.
The hosted turn continues, and once the final frame arrives the client
reports every background process in a continuation turn. Processes still
running are awaited first. Processes that already exited while the model was
still working are delivered too, so their output is not dropped. This module
provides the same primitives for the CLI.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_FOREGROUND_SECONDS = 25.0
MAX_SHELL_OUTPUT_CHARS = 5_000_000
OUTPUT_LIMIT_ERROR = "Command output exceeded limit (5MB). Result was truncated before this point."


class OutputBuffer:
    """Bounded accumulator for subprocess output."""

    def __init__(self, limit: int = MAX_SHELL_OUTPUT_CHARS) -> None:
        self.limit = limit
        self._parts: list[str] = []
        self._size = 0
        self.exceeded = False

    def append(self, chunk: bytes | str) -> bool:
        """Append a chunk; return False once the limit has been exceeded."""
        if self.exceeded:
            return False
        text = chunk.decode(errors="replace") if isinstance(chunk, bytes) else chunk
        available = self.limit - self._size
        if len(text) > available:
            if available > 0:
                self._parts.append(text[:available])
                self._size += available
            self.exceeded = True
            return False
        self._parts.append(text)
        self._size += len(text)
        return True

    def text(self) -> str:
        return "".join(self._parts).replace("\r\n", "\n").strip()


async def drain(stream: asyncio.StreamReader | None, buffer: OutputBuffer) -> None:
    """Copy a subprocess stream into the buffer until EOF or the limit."""
    if stream is None:
        return
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return
        if not buffer.append(chunk):
            return


async def kill_process_tree(process: asyncio.subprocess.Process) -> None:
    """Terminate a child and everything it spawned."""
    if process.returncode is not None:
        return
    if sys.platform == "win32":
        await asyncio.to_thread(
            subprocess.run,
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except ProcessLookupError:
                return
    try:
        await asyncio.wait_for(process.wait(), 5)
    except TimeoutError:
        return


async def spawn_shell(command: str, cwd: Path) -> asyncio.subprocess.Process:
    """Start a shell command detached into its own process group."""
    return await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=os.environ.copy(),
        start_new_session=sys.platform != "win32",
    )


@dataclass
class BackgroundTask:
    """One shell command that outlived its foreground window."""

    task_id: str
    command: str
    process: asyncio.subprocess.Process
    buffer: OutputBuffer
    description: str = ""
    started_at: float = field(default_factory=time.monotonic)
    status: str = "running"
    exit_code: int | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    _reader: asyncio.Task[None] | None = None

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def running(self) -> bool:
        return self.status == "running"

    def snapshot(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "command": self.command,
            "pid": self.pid,
            "status": self.status,
            "exit_code": self.exit_code,
            "output": self.buffer.text(),
            "output_truncated": self.buffer.exceeded,
            "duration_s": round(time.monotonic() - self.started_at, 1),
        }


class BackgroundProcessManager:
    """Track background shell tasks for the current hosted turn."""

    def __init__(self) -> None:
        self._tasks: dict[str, BackgroundTask] = {}

    def register(
        self,
        command: str,
        process: asyncio.subprocess.Process,
        buffer: OutputBuffer,
        reader: asyncio.Task[None],
        *,
        description: str = "",
    ) -> BackgroundTask:
        task = BackgroundTask(
            task_id=uuid.uuid4().hex[:12],
            command=command,
            process=process,
            buffer=buffer,
            description=description,
        )
        task._reader = reader
        self._tasks[task.task_id] = task
        asyncio.create_task(self._watch(task))
        return task

    async def _watch(self, task: BackgroundTask) -> None:
        try:
            code = await task.process.wait()
            if task._reader is not None:
                await task._reader
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never let a watcher crash the loop
            code = task.process.returncode if task.process.returncode is not None else -1
        if task.status == "running":
            task.exit_code = code
            task.status = "killed" if code is not None and code < 0 else "completed"
        task.done.set()

    def has_running(self) -> bool:
        return any(task.running for task in self._tasks.values())

    def processes(self) -> list[dict[str, Any]]:
        return [task.snapshot() for task in self._tasks.values()]

    async def wait_all(self) -> list[dict[str, Any]]:
        """Wait for every tracked task to finish and return their snapshots."""
        tasks = list(self._tasks.values())
        if tasks:
            await asyncio.gather(*(task.done.wait() for task in tasks))
        return [task.snapshot() for task in tasks]

    def drain(self) -> list[dict[str, Any]]:
        """Return and forget all finished tasks."""
        finished = [task for task in self._tasks.values() if not task.running]
        for task in finished:
            self._tasks.pop(task.task_id, None)
        return [task.snapshot() for task in finished]

    async def kill_all(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            if task.running:
                task.status = "killed"
                await kill_process_tree(task.process)
                task.exit_code = task.process.returncode
                task.done.set()
        self._tasks.clear()


async def run_shell(
    command: str,
    cwd: Path,
    *,
    manager: BackgroundProcessManager,
    foreground_seconds: float = DEFAULT_FOREGROUND_SECONDS,
    kill_on_timeout: bool = False,
    description: str = "",
    output_limit: int = MAX_SHELL_OUTPUT_CHARS,
) -> dict[str, Any]:
    """Run a shell command with the desktop client's foreground/background contract."""
    if not command.strip():
        return {"error": "command is required"}
    try:
        process = await spawn_shell(command, cwd)
    except OSError as exc:
        return {"error": f"Could not start command: {exc}"}
    buffer = OutputBuffer(output_limit)
    reader = asyncio.create_task(drain(process.stdout, buffer))
    try:
        await asyncio.wait_for(asyncio.shield(process.wait()), foreground_seconds)
    except TimeoutError:
        if buffer.exceeded:
            await kill_process_tree(process)
            await reader
            return {"error": OUTPUT_LIMIT_ERROR, "timed_out": False, "result": buffer.text()}
        if kill_on_timeout:
            await kill_process_tree(process)
            await reader
            return {"error": "Command timed out", "timed_out": True, "result": buffer.text()}
        task = manager.register(command, process, buffer, reader, description=description)
        initial = buffer.text()
        initial_output = f"\n{initial}" if initial else " (no output yet)"
        return {
            "result": (
                f"Command '{command}' is running in the background "
                f"(PID: {task.pid}, Task ID: {task.task_id}). Initial output:{initial_output}\n"
                "The client is monitoring this process in the background and will provide "
                "full output once complete."
            ),
            "background": True,
            "task_id": task.task_id,
            "pid": task.pid,
        }
    except asyncio.CancelledError:
        await kill_process_tree(process)
        reader.cancel()
        raise
    await reader
    output = buffer.text()
    if buffer.exceeded:
        await kill_process_tree(process)
        return {"error": OUTPUT_LIMIT_ERROR, "timed_out": False, "result": output}
    code = process.returncode if process.returncode is not None else -1
    text = output or "<no output>"
    if code != 0:
        text = f"{text.rstrip()}\n\nExit code: {code}"
    return {"result": text, "exit_code": code}


__all__ = [
    "DEFAULT_FOREGROUND_SECONDS",
    "MAX_SHELL_OUTPUT_CHARS",
    "BackgroundProcessManager",
    "BackgroundTask",
    "OutputBuffer",
    "kill_process_tree",
    "run_shell",
]
