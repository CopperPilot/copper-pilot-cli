"""CopperPilot tools for the user's own agent: the tool socket plus ``copperpilot_subagent``.

``ToolProxy`` is what every front end exposes over MCP:

* the curated, read-only CopperPilot tools from ``tool_session_ready``,
  proxied to the tool socket;
* ``copperpilot_subagent``, which runs one ordinary hosted CopperPilot turn
  (a fresh conversation per call) through the same graph as the TUI. It is
  the main way the user's agent *writes* design files; under Auto its KiCad
  writes count as routine;
* the guide as the resource ``copperpilot://guide``.

Claude uses it in-process; Codex reaches it through ``copper-pilot mcp
--tools-session`` (a relay back to this process when the TUI launched Codex).
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from copper_pilot_cli.agents.tools_session import ToolSessionClient, ToolSessionError
from copper_pilot_cli.copper_protocol import CopperEffort, CopperEvent, CopperMode, EventKind
from copper_pilot_cli.copper_tools import ApprovalMode, LocalToolBroker

logger = logging.getLogger(__name__)

SUBAGENT_TOOL = "copperpilot_subagent"
GUIDE_URI = "copperpilot://guide"
SUBAGENT_DESCRIPTION = (
    "Run a CopperPilot electronics design agent turn (hosted). Use it for schematic, PCB "
    "and library edits: describe the change precisely (references, nets, values, files). "
    "It reads and edits the KiCad files itself and reports what it did and which files "
    "changed. mode: agent (edits, default), plan (proposes a plan) or ask (read-only "
    "answer). effort: balanced (default) or intelligence (paid plans). Each call is a "
    "fresh conversation, so include the context it needs."
)
SUBAGENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": "The design task, with the context and constraints it needs.",
        },
        "mode": {
            "type": "string",
            "enum": [item.value for item in CopperMode],
            "default": CopperMode.AGENT.value,
        },
        "effort": {
            "type": "string",
            "enum": [item.value for item in CopperEffort],
            "default": CopperEffort.BALANCED.value,
        },
    },
    "required": ["task"],
}
_WRITE_TOOLS = frozenset(
    {"write", "write_file", "edit", "edit_file", "write_binary_file", "delete", "delete_file"}
)

EventSink = Callable[[CopperEvent], None]
ProgressFn = Callable[[str], Awaitable[None]]


@dataclass(slots=True)
class ToolOutcome:
    """An MCP ``CallToolResult`` in plain form."""

    content: list[dict[str, Any]] = field(default_factory=list)
    is_error: bool = False

    @classmethod
    def text(cls, text: str, *, is_error: bool = False) -> ToolOutcome:
        return cls([{"type": "text", "text": text}], is_error)

    def plain(self) -> str:
        return "\n".join(
            str(item.get("text") or "") for item in self.content if item.get("type") == "text"
        )


SubagentRunner = Callable[..., Coroutine[Any, Any, dict[str, Any]]]


async def default_subagent_runner(**kwargs: Any) -> dict[str, Any]:
    from copper_pilot_cli.copper_mcp import collect_turn

    return await collect_turn(**kwargs)


class ToolProxy:
    """CopperPilot tools for one chat's agent session."""

    def __init__(
        self,
        session: ToolSessionClient,
        *,
        workspace: Path,
        broker: LocalToolBroker,
        emit: EventSink | None = None,
        subagent_runner: SubagentRunner = default_subagent_runner,
    ) -> None:
        self.session = session
        self.workspace = workspace
        # The chat's broker: its live approval mode, prompts and shell settings
        # also govern the subagent's turn.
        self.broker = broker
        self.emit = emit
        self.subagent_runner = subagent_runner
        self._progress: dict[str, ProgressFn] = {}
        session.on_progress = self._on_progress

    async def start(self) -> None:
        await self.session.start()

    @property
    def guide(self) -> str:
        return self.session.guide

    async def list_tools(self) -> list[dict[str, Any]]:
        await self.session.start()
        tools = [
            {
                "name": str(item["name"]),
                "description": str(item.get("description") or ""),
                "inputSchema": item.get("input_schema")
                if isinstance(item.get("input_schema"), dict)
                else {"type": "object", "properties": {}},
            }
            for item in self.session.tools
            if item.get("name") != SUBAGENT_TOOL
        ]
        tools.append(
            {
                "name": SUBAGENT_TOOL,
                "description": SUBAGENT_DESCRIPTION,
                "inputSchema": SUBAGENT_SCHEMA,
            }
        )
        return tools

    def tool_names(self) -> list[str]:
        return [str(item["name"]) for item in self.session.tools] + [SUBAGENT_TOOL]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        progress: ProgressFn | None = None,
        call_id: str | None = None,
    ) -> ToolOutcome:
        if name == SUBAGENT_TOOL:
            return await self.run_subagent(arguments, progress=progress, call_id=call_id)
        try:
            frame = await self._call_hosted(name, arguments, progress)
        except ToolSessionError as exc:
            return ToolOutcome.text(f"CopperPilot tools are unavailable: {exc}", is_error=True)
        return outcome_from_result(frame)

    async def _call_hosted(
        self, name: str, arguments: dict[str, Any], progress: ProgressFn | None
    ) -> dict[str, Any]:
        task = asyncio.ensure_future(self.session.call_tool(name, arguments))
        if progress is None:
            return await task
        # Progress frames carry the server's call id, which only the session knows.
        key = f"pending:{id(task)}"
        self._progress[key] = progress
        try:
            return await task
        finally:
            self._progress.pop(key, None)

    async def _on_progress(self, call_id: str, message: str) -> None:
        del call_id
        for callback in list(self._progress.values()):
            try:
                await callback(message)
            except Exception:  # noqa: BLE001 - progress is best effort
                logger.debug("Progress callback failed", exc_info=True)

    # ------------------------------------------------------------ subagent

    def _subagent_broker(self) -> LocalToolBroker:
        parent = self.broker
        mode = parent.mode
        if mode is ApprovalMode.MANUAL and not parent.has_approver:
            # Nobody can answer a prompt (headless MCP host): Auto, like `run`.
            mode = ApprovalMode.AUTO
        return LocalToolBroker(
            self.workspace,
            mode=mode,
            approve=parent.approve if parent.has_approver else None,
            ask_user=parent.ask_user,
            always_allow_shell_commands=parent.always_allow_shell_commands,
            always_allow_dangerous_shell_commands=parent.always_allow_dangerous_shell_commands,
            routine_kicad_writes=True,
        )

    def _emit(self, event: CopperEvent) -> None:
        if self.emit is not None:
            try:
                self.emit(event)
            except Exception:  # noqa: BLE001 - display only
                logger.debug("Subagent event sink failed", exc_info=True)

    async def run_subagent(
        self,
        arguments: dict[str, Any],
        *,
        progress: ProgressFn | None = None,
        call_id: str | None = None,
    ) -> ToolOutcome:
        task = str(arguments.get("task") or "").strip()
        if not task:
            return ToolOutcome.text("copperpilot_subagent needs a task.", is_error=True)
        try:
            mode = CopperMode(str(arguments.get("mode") or CopperMode.AGENT.value))
        except ValueError:
            mode = CopperMode.AGENT
        try:
            effort = CopperEffort(str(arguments.get("effort") or CopperEffort.BALANCED.value))
        except ValueError:
            effort = CopperEffort.BALANCED
        identifier = call_id or f"cp-sub-{uuid.uuid4().hex[:12]}"
        title = task.splitlines()[0][:120]
        self._emit(
            CopperEvent(
                EventKind.STATUS_EVENT,
                {"kind": "subagent", "id": identifier, "task": title, "status": "running"},
            )
        )

        async def report(message: str) -> None:
            self._emit(CopperEvent(EventKind.STATUS, f"CopperPilot subagent: {message}"))
            if progress is not None:
                await progress(message)

        from copper_pilot_cli.sessions import generate_thread_id

        broker = self._subagent_broker()
        # A fresh context: the hosted turn compiles its own graph and must not
        # attach itself to a graph run that is calling this tool.
        runner = self.subagent_runner(
            message=task,
            mode=mode,
            workspace=self.workspace,
            thread_id=generate_thread_id(),
            approval=broker.mode,
            effort=effort,
            progress=report,
            broker=broker,
            subagent=True,
        )
        job = asyncio.get_running_loop().create_task(runner, context=contextvars.Context())
        try:
            result = await job
        except asyncio.CancelledError:
            job.cancel()
            self._emit(
                CopperEvent(
                    EventKind.STATUS_EVENT,
                    {"kind": "subagent", "id": identifier, "status": "cancelled"},
                )
            )
            raise
        except Exception as exc:  # noqa: BLE001 - reported to the agent
            logger.warning("copperpilot_subagent failed (%s)", type(exc).__name__)
            result = {"ok": False, "error": str(exc) or type(exc).__name__}
        text = subagent_text(result)
        ok = result.get("ok") is True
        self._emit(
            CopperEvent(
                EventKind.STATUS_EVENT,
                {
                    "kind": "subagent",
                    "id": identifier,
                    "status": "success" if ok else "failed",
                    **({"output": text} if ok else {"error": text}),
                },
            )
        )
        return ToolOutcome.text(text, is_error=not ok)


def subagent_text(result: dict[str, Any]) -> str:
    """What the user's agent reads back from a ``copperpilot_subagent`` call."""
    if result.get("error") == "login_required":
        return str(result.get("message") or "CopperPilot login is required.")
    lines = []
    body = str(result.get("text") or "").strip()
    if result.get("ok") is not True:
        lines.append(f"CopperPilot subagent failed: {result.get('error') or 'unknown error'}")
    if body:
        lines.append(body)
    changed = [str(item) for item in result.get("changed_files") or []]
    if changed:
        lines.append("Changed files:\n" + "\n".join(f"- {item}" for item in changed))
    elif result.get("ok") is True:
        lines.append("Changed files: none")
    return "\n\n".join(lines) or "CopperPilot subagent finished without a reply."


def changed_files(events: list[dict[str, Any]]) -> list[str]:
    """Paths the hosted turn wrote, from its tool request/result events."""
    requested: dict[str, str] = {}
    changed: list[str] = []
    for event in events:
        kind = str(event.get("type") or "")
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        if kind == "copper.tool_request" and data.get("tool_name") in _WRITE_TOOLS:
            arguments = data.get("arguments") if isinstance(data.get("arguments"), dict) else {}
            path = arguments.get("file_path") or arguments.get("path")
            if isinstance(path, str) and path:
                requested[str(data.get("tool_call_id"))] = path
        elif kind == "copper.tool_result":
            path = requested.get(str(data.get("tool_call_id")))
            result = data.get("result")
            failed = isinstance(result, dict) and bool(result.get("error"))
            if path and not failed and path not in changed:
                changed.append(path)
    return changed


def outcome_from_result(frame: dict[str, Any]) -> ToolOutcome:
    """A ``tool_call_result`` frame as MCP content."""
    content: list[dict[str, Any]] = []
    for item in frame.get("content") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            content.append({"type": "text", "text": str(item.get("text") or "")})
        elif item.get("type") == "image" and item.get("data"):
            content.append(
                {
                    "type": "image",
                    "data": str(item["data"]),
                    "mimeType": str(item.get("mime_type") or item.get("mimeType") or "image/png"),
                }
            )
    ok = frame.get("ok") is not False
    error = frame.get("error") if isinstance(frame.get("error"), dict) else None
    if not ok:
        message = str((error or {}).get("message") or "The CopperPilot tool failed.")
        if (error or {}).get("retryable"):
            message += " (retryable: try again shortly)"
        content.insert(0, {"type": "text", "text": message})
    if not content:
        content.append({"type": "text", "text": "(no output)"})
    return ToolOutcome(content, not ok)


__all__ = [
    "GUIDE_URI",
    "SUBAGENT_SCHEMA",
    "SUBAGENT_TOOL",
    "ToolOutcome",
    "ToolProxy",
    "changed_files",
    "outcome_from_result",
    "subagent_text",
]
