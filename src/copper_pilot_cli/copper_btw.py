"""`/btw` side questions: ask about the session without touching the main turn.

A side question is answered by a separate, single-turn hosted Ask stream with
every tool denied. Its question and answer live only in memory: they are never
checkpointed, never shown in the transcript, and never sent to the main turn.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any, Literal

from langchain_core.messages import BaseMessage, HumanMessage

from copper_pilot_cli.copper_api import usage_error_message
from copper_pilot_cli.copper_protocol import (
    ChatMessage,
    ChatRequest,
    CopperEffort,
    CopperMode,
    EventKind,
    HostedChatClient,
    ProtocolError,
    ToolRequest,
)
from copper_pilot_cli.langchain import _context

logger = logging.getLogger(__name__)

MAX_SIDE_QUESTIONS = 20
MAX_REPLAYED_EXCHANGES = 5
MAX_SNAPSHOT_TOOLS = 30
MAX_SNAPSHOT_TEXT = 4_000
MAX_IN_FLIGHT = 3
MAX_TOOL_DENIALS = 3
SIDE_QUESTION_TIMEOUT = 120.0
TOOL_DENIED = "Side questions cannot use tools. Answer from the conversation context only."
NOTHING_EXECUTED = "_The model asked for a tool; nothing was executed._"


class SideQuestionError(RuntimeError):
    """A side question could not be answered; the message is user-facing."""


SideStatus = Literal["pending", "done", "error"]

_ids = itertools.count(1)


@dataclass(slots=True)
class SideQuestion:
    question: str
    answer: str = ""
    status: SideStatus = "pending"
    id: int = field(default_factory=lambda: next(_ids))
    started_at: float = field(default_factory=monotonic)


class SideQuestionLog:
    """Bounded, in-memory side-question history. The newest entry is last."""

    def __init__(self, limit: int = MAX_SIDE_QUESTIONS) -> None:
        self.limit = limit
        self.entries: list[SideQuestion] = []
        self.index = -1

    def __len__(self) -> int:
        return len(self.entries)

    def add(self, question: str) -> SideQuestion:
        entry = SideQuestion(question)
        self.entries.append(entry)
        del self.entries[: max(0, len(self.entries) - self.limit)]
        self.index = len(self.entries) - 1
        return entry

    def resolve(self, entry_id: int, answer: str, *, failed: bool = False) -> SideQuestion | None:
        entry = self.get(entry_id)
        if entry is not None:
            entry.answer = answer
            entry.status = "error" if failed else "done"
        return entry

    def get(self, entry_id: int) -> SideQuestion | None:
        return next((entry for entry in self.entries if entry.id == entry_id), None)

    def current(self) -> SideQuestion | None:
        if 0 <= self.index < len(self.entries):
            return self.entries[self.index]
        return None

    def latest(self) -> SideQuestion | None:
        self.index = len(self.entries) - 1
        return self.current()

    def older(self) -> SideQuestion | None:
        if self.entries:
            self.index = max(0, self.index - 1)
        return self.current()

    def newer(self) -> SideQuestion | None:
        if self.entries:
            self.index = min(len(self.entries) - 1, self.index + 1)
        return self.current()

    def earlier_than_current(self, count: int) -> list[SideQuestion]:
        if self.index <= 0:
            return []
        return self.entries[max(0, self.index - count) : self.index]

    def pending(self) -> int:
        return sum(entry.status == "pending" for entry in self.entries)

    def completed(self, count: int = MAX_REPLAYED_EXCHANGES) -> list[SideQuestion]:
        done = [entry for entry in self.entries if entry.status == "done"]
        return done[-count:] if count > 0 else []

    def clear(self) -> None:
        self.entries.clear()
        self.index = -1


@dataclass(slots=True)
class LiveTool:
    name: str
    args: dict[str, Any]
    status: str


@dataclass(slots=True)
class LiveTurn:
    """What the running turn has done so far; the checkpoint has none of it yet."""

    running: bool = False
    query: str = ""
    status: str = ""
    elapsed_s: int | None = None
    text: str = ""
    tools: list[LiveTool] = field(default_factory=list)
    subagents: list[tuple[str, str]] = field(default_factory=list)


def _tool_line(tool: LiveTool) -> str:
    command = tool.args.get("command")
    target = command or tool.args.get("file_path") or tool.args.get("path")
    target = target or tool.args.get("pattern") or tool.args.get("description")
    detail = f" `{str(target)[:160]}`" if target else ""
    return f"- {tool.name}{detail}: {tool.status}"


def live_snapshot(turn: LiveTurn) -> str:
    """Markdown summary of the running turn, bounded for display and requests."""
    if not turn.running:
        lines = ["**No turn is running.**"]
        if turn.query:
            lines.append(f"Last request: {turn.query[:500]}")
        return "\n\n".join(lines)
    header = "**CopperPilot is working**"
    if turn.elapsed_s is not None:
        header += f" ({turn.elapsed_s}s)"
    lines = [header]
    if turn.query:
        lines.append(f"Request: {turn.query[:500]}")
    if turn.status:
        lines.append(f"Now: {turn.status[:200]}")
    if turn.tools:
        shown = turn.tools[-MAX_SNAPSHOT_TOOLS:]
        omitted = len(turn.tools) - len(shown)
        tool_lines = [_tool_line(tool) for tool in shown]
        if omitted:
            tool_lines.insert(0, f"- ({omitted} earlier tool calls omitted)")
        lines.append("Tools this turn:\n" + "\n".join(tool_lines))
    if turn.subagents:
        lines.append(
            "Subagents:\n"
            + "\n".join(f"- {task or 'Survey'}: {status}" for task, status in turn.subagents)
        )
    if turn.text.strip():
        tail = turn.text[-MAX_SNAPSHOT_TEXT:]
        prefix = "…" if len(turn.text) > MAX_SNAPSHOT_TEXT else ""
        lines.append(f"Reply so far:\n\n{prefix}{tail}")
    return "\n\n".join(lines)


def wrap_question(question: str) -> str:
    return (
        "This is a side question from the user. Answer it directly in a single response.\n\n"
        "IMPORTANT CONTEXT:\n"
        "- You are a separate, lightweight assistant spawned to answer this one question.\n"
        "- The main CopperPilot agent is NOT interrupted; it keeps working in the background.\n"
        "- You share the conversation context, including a live snapshot of the running "
        "turn, but you are a separate instance.\n"
        "- Do not describe yourself as interrupted or resuming anything.\n\n"
        "CRITICAL CONSTRAINTS:\n"
        "- You have NO tools: you cannot read files, run commands, search, or change "
        "anything.\n"
        "- This is a one-off reply; there are no follow-up turns.\n"
        "- Answer only from what is already in the conversation and the snapshot.\n"
        '- Never say "Let me check", "I\'ll now", or promise any action.\n'
        "- If you do not know, say so.\n\n"
        f"Question: {question}"
    )


def build_side_request(
    question: str,
    *,
    history: Sequence[BaseMessage],
    snapshot: str,
    earlier: Sequence[SideQuestion],
    workspace: Path,
    schematic_path: str | None = None,
    pcb_path: str | None = None,
) -> ChatRequest:
    """A tool-less Balanced Ask request on a throwaway conversation id."""
    query = wrap_question(question)
    context = _context([*history, HumanMessage(content=query)])
    context.append(
        ChatMessage(role="system", content=f"[Live snapshot of the running turn]\n{snapshot}")
    )
    for entry in earlier:
        context.append(ChatMessage(role="user", content=f"(side question) {entry.question}"))
        context.append(ChatMessage(role="assistant", content=entry.answer))
    return ChatRequest(
        query=query,
        context=context,
        workspace_path=str(workspace),
        conversation_id=str(uuid.uuid4()),
        mode=CopperMode.ASK,
        effort=CopperEffort.BALANCED,
        agentic_mode=False,
        schematic_path=schematic_path,
        pcb_path=pcb_path,
        project_data={"workspace_path": str(workspace)},
    )


def _hosted_client(credential: Any) -> HostedChatClient:
    return HostedChatClient(credential.base_url, credential.api_key, credential.fingerprint)


async def run_side_question(
    credential: Any,
    request: ChatRequest,
    *,
    limit_s: float = SIDE_QUESTION_TIMEOUT,
    client_factory: Callable[[Any], Any] = _hosted_client,
) -> str:
    """Stream one side answer on its own socket; every tool request is refused."""
    if credential is None:
        raise SideQuestionError("Not authenticated. Use `/auth login` first.")
    client = client_factory(credential)
    text: list[str] = []
    denials = 0
    stopped = False

    async def deny(tool: ToolRequest) -> dict[str, Any]:
        nonlocal denials, stopped
        denials += 1
        logger.info("Side question refused tool %s", tool.tool_name)
        if denials >= MAX_TOOL_DENIALS and not stopped:
            stopped = True
            await client.cancel()
        return {"error": TOOL_DENIED, "rejected": True}

    try:
        async with asyncio.timeout(limit_s):
            async for event in client.stream(request, deny):
                if event.kind is EventKind.TEXT:
                    text.append(str(event.data))
    except TimeoutError as exc:
        if not text:
            raise SideQuestionError(f"No answer within {limit_s:g}s.") from exc
    except ProtocolError as exc:
        if not (stopped and text):
            raise SideQuestionError(usage_error_message(exc.as_event_data())) from exc
    except (OSError, ConnectionError) as exc:
        raise SideQuestionError(f"Could not reach CopperPilot: {exc}") from exc
    answer = "".join(text).strip()
    if denials:
        answer = f"{answer}\n\n{NOTHING_EXECUTED}".strip()
    if not answer:
        raise SideQuestionError("No answer was returned. Try rephrasing.")
    return answer
