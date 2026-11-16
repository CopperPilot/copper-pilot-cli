"""What every agent driver provides, plus helpers shared by the Claude and Codex drivers."""

from __future__ import annotations

import abc
import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from copper_pilot_cli.agents import AgentSelection, Provider
from copper_pilot_cli.copper_protocol import CopperEvent, CopperMode, EventKind, ToolRequest
from copper_pilot_cli.copper_tools import ApprovalMode, LocalToolBroker

AGENT_PLAN_KIND = "agent_plan"
PLAN_READY_KIND = "plan_ready"
# Tool output kept in the transcript (sessions.db) per call.
MAX_STORED_TOOL_OUTPUT = 32 * 1024


@dataclass(slots=True)
class TurnContext:
    """Per-turn inputs a driver needs from the chat."""

    workspace: Path
    broker: LocalToolBroker
    mode: CopperMode
    thread_id: str
    turn_id: str


@dataclass(slots=True)
class TurnUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    context_tokens: int | None = None
    context_window: int | None = None
    model: str | None = None


class AgentSession(abc.ABC):
    """One live agent session (one chat), reused across turns."""

    provider: Provider

    def __init__(self, selection: AgentSelection) -> None:
        self.selection = selection
        self.closed = False

    @property
    def identity(self) -> str:
        """Options that need a new session (and a handoff) when they change."""
        return json.dumps(
            {"provider": self.provider.value, **self.selection.options()}, sort_keys=True
        )

    @property
    def context_window(self) -> int | None:
        return None

    @property
    def alive(self) -> bool:
        return not self.closed

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    def run_turn(self, prompt: str, context: TurnContext) -> AsyncIterator[CopperEvent]:
        """Stream one turn as CopperPilot events; raise ``AgentError`` on failure."""

    @abc.abstractmethod
    async def interrupt(self) -> None: ...

    async def compact(self) -> str:
        """Run the agent's own compaction; returns a one-line outcome."""
        return "This agent does not support compaction."

    @abc.abstractmethod
    async def aclose(self) -> None: ...


def clip(text: str, limit: int = MAX_STORED_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[{len(text) - limit} more characters not shown]"


def tool_request_event(call_id: str, name: str, arguments: dict[str, Any]) -> CopperEvent:
    return CopperEvent(
        EventKind.TOOL_REQUEST,
        ToolRequest(tool_call_id=call_id, tool_name=name, arguments=arguments),
    )


def tool_result_event(
    call_id: str, name: str, output: str, *, error: bool = False, exit_code: int | None = None
) -> CopperEvent:
    result: dict[str, Any] = (
        {"error": clip(output) or "Failed."} if error else {"result": clip(output)}
    )
    if exit_code is not None:
        result["exit_code"] = exit_code
    return CopperEvent(
        EventKind.TOOL_RESULT, {"tool_call_id": call_id, "tool_name": name, "result": result}
    )


def subagent_event(identifier: str, **fields: Any) -> CopperEvent:
    return CopperEvent(EventKind.STATUS_EVENT, {"kind": "subagent", "id": identifier, **fields})


def plan_event(plan: str) -> CopperEvent:
    """The agent finished a plan in Plan mode; the runtime saves it as a plan artifact."""
    return CopperEvent(EventKind.STATUS_EVENT, {"kind": AGENT_PLAN_KIND, "plan": plan})


def todo_event(items: list[dict[str, Any]]) -> CopperEvent:
    return CopperEvent(EventKind.STATUS_EVENT, {"kind": "todo_list", "items": items})


def context_event(usage: TurnUsage) -> CopperEvent | None:
    if usage.context_tokens is None:
        return None
    data: dict[str, Any] = {"kind": "context_usage", "context_tokens": usage.context_tokens}
    if usage.context_window:
        data["context_limit"] = usage.context_window
    if usage.model:
        data["model"] = usage.model
    return CopperEvent(EventKind.STATUS_EVENT, data)


def final_event(usage: TurnUsage, *, model: str | None) -> CopperEvent:
    data: dict[str, Any] = {
        "success": True,
        "error": None,
        "is_final": True,
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cached_input_tokens": usage.cached_input_tokens,
        },
        "model": model,
    }
    if usage.context_tokens is not None:
        data["context_usage"] = {
            "context_tokens": usage.context_tokens,
            **({"context_limit": usage.context_window} if usage.context_window else {}),
        }
    return CopperEvent(EventKind.FINAL, data)


class TurnQueue:
    """Merges the agent's stream with tool-side events (subagent cards) for one turn."""

    _END = object()

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Any] = asyncio.Queue()

    def put(self, event: Any) -> None:
        """A ``CopperEvent``, or a raw agent message the driver translates."""
        self._queue.put_nowait(event)

    def fail(self, error: BaseException) -> None:
        self._queue.put_nowait(error)

    def end(self) -> None:
        self._queue.put_nowait(self._END)

    async def events(self) -> AsyncIterator[Any]:
        while True:
            item = await self._queue.get()
            if item is self._END:
                return
            if isinstance(item, BaseException):
                raise item
            yield item


def approval_mode(broker: LocalToolBroker) -> ApprovalMode:
    return broker.mode


EmitFn = Callable[[CopperEvent], None]

__all__ = [
    "AgentSession",
    "TurnContext",
    "TurnQueue",
    "TurnUsage",
    "clip",
    "context_event",
    "final_event",
    "subagent_event",
    "todo_event",
    "tool_request_event",
    "tool_result_event",
]
