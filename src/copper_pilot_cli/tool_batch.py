"""Concurrent execution of one batch of hosted tool requests.

The hosted agent can ask for several tools in one model step. Servers that
know the client advertised ``parallel_tool_requests`` send them together in a
``tool_requests`` batch; this module decides which members may overlap and
runs them, reporting each outcome as soon as it is ready.

Rules, in the order the model gave the calls:

* READ tools (file reads, listings, searches, provably read-only shell
  commands) run concurrently, capped by ``MAX_CONCURRENT_READS``.
* MUTATING tools (write, edit, delete) wait for earlier members touching the
  same path, so same-file edits apply in model order.
* EXCLUSIVE tools (any other shell command, questions, unknown tools) wait for
  every earlier member, and every later member waits for them.
"""

from __future__ import annotations

import asyncio
import os
import posixpath
import re
import shlex
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, TypeVar

PARALLEL_TOOL_REQUESTS_CAPABILITY = "parallel_tool_requests"
PARALLEL_TOOLS_ENV = "COPPER_PILOT_PARALLEL_TOOLS"
MAX_CONCURRENT_READS = 8


class ToolLike(Protocol):
    @property
    def tool_name(self) -> str: ...

    @property
    def arguments(self) -> dict[str, Any]: ...


class ToolClass(StrEnum):
    READ = "read"
    MUTATING = "mutating"
    EXCLUSIVE = "exclusive"


@dataclass(frozen=True, slots=True)
class ToolPlan:
    """Scheduling class and path key for one batch member.

    ``path`` is ``None`` for tools that are not scoped to a single file (a
    listing or search over a directory, or a shell command); such READs are
    treated as touching every path.
    """

    kind: ToolClass
    path: str | None = None


_FILE_READ_TOOLS = frozenset({"read", "read_file", "read_binary_file", "read_image"})
_SCOPE_READ_TOOLS = frozenset({"ls", "list", "list_files", "glob", "grep"})
_MUTATING_TOOLS = frozenset(
    {
        "write",
        "write_file",
        "edit",
        "edit_file",
        "delete",
        "delete_file",
        "write_binary_file",
    }
)
_SHELL_TOOLS = frozenset({"bash", "execute"})
_SHELL_METACHARACTERS = re.compile(r"[\n\r;&|`<>]|\$\(|\$\{")
_READ_ONLY_EXECUTABLES = frozenset(
    {"ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "pwd", "git"}
)
_READ_ONLY_GIT = frozenset({"status", "log", "diff", "show", "ls-files", "rev-parse", "branch"})
_GIT_BRANCH_LIST_FLAGS = frozenset(
    {"--list", "-l", "-a", "--all", "-r", "--remotes", "-v", "-vv", "--show-current"}
)
_FIND_ACTIONS = frozenset(
    {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
)


def parallel_tools_enabled() -> bool:
    """Whether to advertise batches (``COPPER_PILOT_PARALLEL_TOOLS=0`` disables)."""
    value = os.environ.get(PARALLEL_TOOLS_ENV, "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def read_only_shell_command(command: object) -> bool:
    """Conservatively prove that ``command`` only reads the workspace."""
    if not isinstance(command, str) or not command.strip():
        return False
    if _SHELL_METACHARACTERS.search(command):
        return False
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if not parts or parts[0] not in _READ_ONLY_EXECUTABLES:
        return False
    executable, arguments = parts[0], parts[1:]
    if executable == "git":
        if not arguments or arguments[0] not in _READ_ONLY_GIT:
            return False
        if any(token.startswith("--output") for token in arguments):
            return False
        if arguments[0] == "branch":
            return all(token in _GIT_BRANCH_LIST_FLAGS for token in arguments[1:])
        return True
    if executable == "find":
        return not any(token in _FIND_ACTIONS for token in arguments)
    if executable == "rg":
        return not any(token.startswith("--pre") for token in arguments)
    if executable == "tail":
        return not any(token in {"-f", "-F", "--follow"} for token in arguments)
    return True


def _path_key(raw: object, workspace: str | None) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    path = raw.strip().replace("\\", "/")
    if not path.startswith("/") and workspace:
        path = posixpath.join(workspace.replace("\\", "/"), path)
    return posixpath.normpath(path)


def classify_tool(request: ToolLike, workspace: str | None = None) -> ToolPlan:
    """Return how one hosted tool request may be scheduled."""
    name = request.tool_name
    arguments = request.arguments if isinstance(request.arguments, dict) else {}
    if name in _FILE_READ_TOOLS:
        key = _path_key(arguments.get("file_path"), workspace)
        return ToolPlan(ToolClass.READ, key)
    if name in _SCOPE_READ_TOOLS:
        return ToolPlan(ToolClass.READ)
    if name in _MUTATING_TOOLS:
        key = _path_key(arguments.get("file_path"), workspace)
        if key is None:
            return ToolPlan(ToolClass.EXCLUSIVE)
        return ToolPlan(ToolClass.MUTATING, key)
    if name in _SHELL_TOOLS and read_only_shell_command(arguments.get("command")):
        return ToolPlan(ToolClass.READ)
    return ToolPlan(ToolClass.EXCLUSIVE)


def _overlaps(left: ToolPlan, right: ToolPlan) -> bool:
    """Two members conflict when one writes a path the other touches."""
    if ToolClass.MUTATING not in {left.kind, right.kind}:
        return False
    if left.path is None or right.path is None:
        return True
    return left.path == right.path


def plan_dependencies(plans: Sequence[ToolPlan]) -> list[set[int]]:
    """Return, for each member, the indexes of earlier members it must wait for."""
    dependencies: list[set[int]] = []
    last_exclusive: int | None = None
    for index, plan in enumerate(plans):
        waits: set[int] = set()
        if plan.kind is ToolClass.EXCLUSIVE:
            waits.update(range(index))
        else:
            if last_exclusive is not None:
                waits.add(last_exclusive)
            for earlier in range(index):
                if _overlaps(plans[earlier], plan):
                    waits.add(earlier)
        if plan.kind is ToolClass.EXCLUSIVE:
            last_exclusive = index
        dependencies.append(waits)
    return dependencies


ToolOutcome = Any
T = TypeVar("T", bound=ToolLike)


async def run_tool_batch(
    requests: Sequence[T],
    handler: Callable[[T], Awaitable[Any]],
    *,
    workspace: str | None = None,
    max_concurrent_reads: int = MAX_CONCURRENT_READS,
) -> AsyncGenerator[tuple[T, ToolOutcome], None]:
    """Run ``requests`` under the batch rules, yielding outcomes as they finish.

    A failing member yields its exception (an ``Exception`` instance) instead of
    a result; the others still run. Closing the iterator early (consumer
    cancellation or a dropped socket) cancels every unfinished member.
    """
    plans = [classify_tool(request, workspace) for request in requests]
    dependencies = plan_dependencies(plans)
    finished = [asyncio.Event() for _ in requests]
    reads = asyncio.Semaphore(max(1, max_concurrent_reads))
    completed: asyncio.Queue[tuple[int, ToolOutcome]] = asyncio.Queue()

    async def run_one(index: int) -> None:
        try:
            for dependency in sorted(dependencies[index]):
                await finished[dependency].wait()
            try:
                if plans[index].kind is ToolClass.READ:
                    async with reads:
                        outcome: ToolOutcome = await handler(requests[index])
                else:
                    outcome = await handler(requests[index])
            except Exception as exc:  # noqa: BLE001 - reported per tool
                outcome = exc
            completed.put_nowait((index, outcome))
        finally:
            finished[index].set()

    tasks = [asyncio.create_task(run_one(index)) for index in range(len(requests))]
    try:
        for _ in requests:
            index, outcome = await completed.get()
            yield requests[index], outcome
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


__all__ = [
    "MAX_CONCURRENT_READS",
    "PARALLEL_TOOLS_ENV",
    "PARALLEL_TOOL_REQUESTS_CAPABILITY",
    "ToolClass",
    "ToolPlan",
    "classify_tool",
    "parallel_tools_enabled",
    "plan_dependencies",
    "read_only_shell_command",
    "run_tool_batch",
]
