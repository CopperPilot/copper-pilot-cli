"""Stdio MCP server that delegates electronics tasks to the hosted agent."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import HumanMessage
from mcp.server.fastmcp import Context, FastMCP

from copper_pilot_cli.copper_auth import AuthenticationError, DeviceCredential, load_credential
from copper_pilot_cli.copper_graph import RETRY_EVENT, create_copper_graph
from copper_pilot_cli.copper_preferences import shell_auto_allow_settings
from copper_pilot_cli.copper_protocol import CopperEffort, CopperMode, EventKind, error_message
from copper_pilot_cli.copper_status_events import (
    SubagentStatus,
    normalize_subagent_status,
    subagent_progress_label,
)
from copper_pilot_cli.copper_tools import ApprovalMode, LocalToolBroker
from copper_pilot_cli.copper_workspace import (
    canonical_workspace,
    discover_workspace,
    remember_workspace,
)
from copper_pilot_cli.langchain import CopperPilotAgent
from copper_pilot_cli.sessions import generate_thread_id, list_threads

INSTRUCTIONS = (
    "Delegate electronics design tasks to CopperPilot. Prefer `ask` unless the "
    "user asked for a schematic or PCB change. Do not hand-edit KiCad files "
    "while a CopperPilot run is in flight. Run `copper-pilot auth login` in a "
    "terminal if a tool reports that login is required. Effort defaults to "
    "balanced; use intelligence only for paid-plan deep surveys. MCP cannot "
    "answer interactive clarifications; continue the thread in the terminal TUI. "
    "`validate` runs the read-only stage-gated review graph and returns "
    "next_actions to fix with `run`."
)

LOGIN_MESSAGE = "Login required; run `copper-pilot auth login` interactively."
ProgressFn = Callable[[str], Awaitable[None]]
ApprovalName = Literal["auto", "yolo"]
ModeName = Literal["ask", "plan", "agent"]
EffortName = Literal["balanced", "intelligence"]
GateName = Literal["stage", "full", "strict"]

_DIGEST_KINDS = {
    EventKind.STATUS.value,
    EventKind.STATUS_EVENT.value,
    EventKind.ERROR.value,
    EventKind.FINAL.value,
}


def resolve_workspace(workspace: str | None = None) -> Path:
    """Resolve an existing directory from the tool, Claude Code, or cwd."""
    if workspace:
        return canonical_workspace(workspace)
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return canonical_workspace(env)
    return canonical_workspace(Path.cwd())


def current_credential() -> DeviceCredential | None:
    """Return the stored credential, or None when missing or unreadable."""
    try:
        return load_credential()
    except AuthenticationError:
        return None


def _login_error() -> dict[str, Any]:
    return {"ok": False, "error": "login_required", "message": LOGIN_MESSAGE}


def _progress(ctx: Context | None) -> ProgressFn | None:
    if ctx is None:
        return None
    counter = {"n": 0}

    async def report(message: str) -> None:
        counter["n"] += 1
        with contextlib.suppress(Exception):
            await ctx.report_progress(progress=counter["n"], message=message[:240])

    return report


def _digest(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    digest: list[dict[str, Any]] = []
    subagents: dict[str, SubagentStatus] = {}
    for event in events:
        kind = str(event.get("type") or "").removeprefix("copper.")
        if kind not in _DIGEST_KINDS:
            continue
        item: dict[str, Any] = {"kind": kind, "title": event.get("title")}
        if kind in {EventKind.ERROR.value, EventKind.FINAL.value}:
            item["data"] = event.get("data")
        elif kind == EventKind.STATUS_EVENT.value:
            data = event.get("data")
            if not isinstance(data, dict):
                continue
            if data.get("kind") == "subagent":
                identifier = str(data.get("id") or "")
                status = normalize_subagent_status(data, subagents.get(identifier))
                if status is not None:
                    subagents[status.id] = status
                continue
            item["data"] = {
                key: data[key] for key in ("kind", "id", "task", "status", "error") if key in data
            }
        digest.append(item)
    digest.extend(
        {
            "kind": EventKind.STATUS_EVENT.value,
            "title": None,
            "data": {
                "kind": "subagent",
                "id": status.id,
                "task": status.task,
                "status": status.status,
                **({"error": status.error} if status.error else {}),
            },
        }
        for status in subagents.values()
    )
    return digest[-8:]


def _turn_failed(events: list[dict[str, Any]]) -> tuple[bool, str | None]:
    failed = False
    error: str | None = None
    for event in events:
        kind = str(event.get("type") or "").removeprefix("copper.")
        payload = event.get("data")
        if kind == EventKind.ERROR.value:
            failed = True
            error = error_message(payload)
        elif kind == EventKind.FINAL.value and isinstance(payload, dict):
            if payload.get("success") is False or payload.get("error"):
                failed = True
                error = error or str(payload.get("error") or "hosted failure")
    return failed, error


async def collect_turn(
    *,
    message: str,
    mode: CopperMode,
    workspace: Path,
    thread_id: str,
    approval: ApprovalMode,
    effort: CopperEffort | None = CopperEffort.BALANCED,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Run one hosted turn and return a compact conductor payload."""
    credential = current_credential()
    if credential is None:
        return _login_error()
    context = discover_workspace(workspace)
    remember_workspace(context)
    normal_shell, dangerous_shell = shell_auto_allow_settings()
    broker = LocalToolBroker(
        workspace,
        mode=approval,
        always_allow_shell_commands=normal_shell,
        always_allow_dangerous_shell_commands=dangerous_shell,
    )
    initial_effort = effort or CopperEffort.BALANCED
    agent = CopperPilotAgent(
        workspace,
        credential=credential,
        conversation_id=thread_id,
        mode=mode,
        effort=initial_effort,
        tool_broker=broker,
    )
    events: list[dict[str, Any]] = []
    subagents: dict[str, SubagentStatus] = {}
    if progress is not None:
        await progress(f"Starting CopperPilot {mode.value} turn")
    async with create_copper_graph(agent) as runtime:
        config = runtime.config(thread_id, workspace)
        values = {
            "messages": [HumanMessage(content=message)],
            "copper_mode": mode.value,
        }
        if effort is not None:
            values["copper_effort"] = effort.value
        async for item in runtime.astream(values, config):
            _namespace, stream_mode, data = item
            if stream_mode != "custom" or not isinstance(data, dict):
                continue
            events.append(data)
            kind = str(data.get("type") or "").removeprefix("copper.")
            if progress is not None and kind in {
                EventKind.STATUS.value,
                EventKind.REASONING.value,
            }:
                await progress(str(data.get("data") or kind))
            elif progress is not None and data.get("type") == RETRY_EVENT:
                await progress(str(data.get("title") or "Retrying the hosted turn"))
            elif (
                progress is not None
                and kind == EventKind.STATUS_EVENT.value
                and isinstance(data.get("data"), dict)
            ):
                payload = data["data"]
                if payload.get("kind") == "subagent":
                    identifier = str(payload.get("id") or "")
                    status = normalize_subagent_status(payload, subagents.get(identifier))
                    if status is not None:
                        subagents[status.id] = status
                        await progress(subagent_progress_label(status))
                elif payload.get("kind") == "plan_mode_switch":
                    await progress("Switched to Plan mode")
        state = await runtime.aget_state(config)
    messages = (getattr(state, "values", {}) or {}).get("messages", [])
    assistant = str(messages[-1].content) if messages else ""
    failed, error = _turn_failed(events)
    if progress is not None:
        await progress("CopperPilot turn complete")
    return {
        "ok": not failed,
        "text": assistant,
        "thread_id": thread_id,
        "mode": mode.value,
        "effort": agent.effort.value,
        "success": not failed,
        "error": error,
        "events": _digest(events),
    }


def handle_status(workspace: str | None = None) -> dict[str, Any]:
    """Report login state and discovered project files. No hosted call."""
    try:
        path = resolve_workspace(workspace)
    except (FileNotFoundError, NotADirectoryError) as exc:
        return {"ok": False, "error": "invalid_workspace", "message": str(exc)}
    context = discover_workspace(path)
    credential = current_credential()
    return {
        "ok": True,
        "logged_in": credential is not None,
        "base_url": None if credential is None else credential.base_url,
        "workspace": str(path),
        "schematic_path": context.schematic_path,
        "pcb_path": context.pcb_path,
        "login_message": None if credential is not None else LOGIN_MESSAGE,
    }


async def handle_ask(
    *,
    message: str,
    workspace: str | None = None,
    thread_id: str | None = None,
    effort: EffortName = "balanced",
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    return await _handle_named_turn(
        message=message,
        mode=CopperMode.ASK,
        effort=CopperEffort(effort),
        workspace=workspace,
        thread_id=thread_id,
        approval=ApprovalMode.AUTO,
        progress=progress,
    )


async def handle_plan(
    *,
    message: str,
    workspace: str | None = None,
    thread_id: str | None = None,
    effort: EffortName = "balanced",
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    return await _handle_named_turn(
        message=message,
        mode=CopperMode.PLAN,
        effort=CopperEffort(effort),
        workspace=workspace,
        thread_id=thread_id,
        approval=ApprovalMode.AUTO,
        progress=progress,
    )


async def handle_run(
    *,
    message: str,
    workspace: str | None = None,
    thread_id: str | None = None,
    effort: EffortName = "balanced",
    approval: ApprovalName = "auto",
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    return await _handle_named_turn(
        message=message,
        mode=CopperMode.AGENT,
        effort=CopperEffort(effort),
        workspace=workspace,
        thread_id=thread_id,
        approval=_approval_mode(approval),
        progress=progress,
    )


async def handle_resume(
    *,
    thread_id: str,
    message: str,
    workspace: str | None = None,
    mode: ModeName = "agent",
    effort: EffortName | None = None,
    approval: ApprovalName = "auto",
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    if not str(thread_id or "").strip():
        return {
            "ok": False,
            "error": "thread_required",
            "message": "resume requires a thread_id from a previous CopperPilot turn.",
        }
    return await _handle_named_turn(
        message=message,
        mode=CopperMode(mode),
        effort=CopperEffort(effort) if effort is not None else None,
        workspace=workspace,
        thread_id=thread_id,
        approval=_approval_mode(approval),
        progress=progress,
    )


async def handle_threads_list(
    *,
    workspace: str | None = None,
    limit: int = 20,
    all_workspaces: bool = False,
) -> dict[str, Any]:
    try:
        path = resolve_workspace(workspace)
    except (FileNotFoundError, NotADirectoryError) as exc:
        return {"ok": False, "error": "invalid_workspace", "message": str(exc)}
    rows = await list_threads(
        limit=max(1, min(limit, 100)),
        include_message_count=True,
        cwd=None if all_workspaces else str(path),
    )
    return {
        "ok": True,
        "workspace": str(path),
        "threads": [
            {
                "thread_id": row["thread_id"],
                "updated_at": row.get("updated_at"),
                "message_count": row.get("message_count", 0),
                "initial_prompt": row.get("initial_prompt"),
                "cwd": row.get("cwd"),
            }
            for row in rows
        ],
    }


async def handle_validate(
    *,
    workspace: str | None = None,
    profile: str | None = None,
    gate: GateName = "stage",
    until: str | None = None,
    only: list[str] | None = None,
    effort: EffortName | None = None,
    no_cache: bool = False,
    progress: ProgressFn | None = None,
    **runner_kwargs: Any,
) -> dict[str, Any]:
    """Run `copper-pilot validate` and return the verdict, exit code and next actions.

    Reviewer approval is fixed to the validate policy (reads, scratch writes,
    non-dangerous shell) regardless of the host's approval mode.
    """
    from copper_pilot_cli.validate import avalidate
    from copper_pilot_cli.validate.config import ConfigError
    from copper_pilot_cli.validate.runner import EnvironmentError_

    try:
        path = resolve_workspace(workspace)
    except (FileNotFoundError, NotADirectoryError) as exc:
        return {"ok": False, "error": "invalid_workspace", "message": str(exc)}
    loop = asyncio.get_running_loop()
    pending: list[asyncio.Task[None]] = []

    async def _report(line: str) -> None:
        if progress is not None:
            await progress(line)

    def on_progress(line: str) -> None:
        pending.append(loop.create_task(_report(line)))

    try:
        report = await avalidate(
            path,
            profile=profile,
            gate=gate,
            until=until,
            only=list(only or []),
            effort=effort,
            no_cache=no_cache,
            on_progress=on_progress,
            **runner_kwargs,
        )
    except ConfigError as exc:
        return {"ok": False, "error": "config_error", "message": str(exc), "exit_code": 2}
    except EnvironmentError_ as exc:
        return {
            "ok": False,
            "error": "login_required" if "logged in" in str(exc) else "environment",
            "message": str(exc),
            "exit_code": 4,
        }
    finally:
        for task in pending:
            with contextlib.suppress(Exception):
                await task
    return {
        "ok": report.exit_code == 0,
        "verdict": report.verdict,
        "exit_code": report.exit_code,
        "report_path": str(report.paths["report"]),
        "report_markdown": str(report.paths["markdown"]),
        "next_actions": report.next_actions,
    }


def _approval_mode(value: ApprovalName) -> ApprovalMode:
    return ApprovalMode.YOLO if value == "yolo" else ApprovalMode.AUTO


async def _handle_named_turn(
    *,
    message: str,
    mode: CopperMode,
    effort: CopperEffort | None,
    workspace: str | None,
    thread_id: str | None,
    approval: ApprovalMode,
    progress: ProgressFn | None,
) -> dict[str, Any]:
    try:
        path = resolve_workspace(workspace)
    except (FileNotFoundError, NotADirectoryError) as exc:
        return {"ok": False, "error": "invalid_workspace", "message": str(exc)}
    return await collect_turn(
        message=message,
        mode=mode,
        effort=effort,
        workspace=path,
        thread_id=thread_id or generate_thread_id(),
        approval=approval,
        progress=progress,
    )


def create_server() -> FastMCP:
    """Build the stdio MCP server with the seven conductor tools."""
    mcp = FastMCP("copper-pilot", instructions=INSTRUCTIONS, log_level="WARNING")

    @mcp.tool()
    def status(workspace: str | None = None) -> dict[str, Any]:
        """Report CopperPilot login status and discovered schematic/PCB paths."""
        return handle_status(workspace)

    @mcp.tool()
    async def ask(
        message: str,
        workspace: str | None = None,
        thread_id: str | None = None,
        effort: EffortName = "balanced",
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Ask CopperPilot a read-oriented electronics question. Does not YOLO."""
        return await handle_ask(
            message=message,
            workspace=workspace,
            thread_id=thread_id,
            effort=effort,
            progress=_progress(ctx),
        )

    @mcp.tool()
    async def plan(
        message: str,
        workspace: str | None = None,
        thread_id: str | None = None,
        effort: EffortName = "balanced",
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Ask CopperPilot to propose a design plan without applying it."""
        return await handle_plan(
            message=message,
            workspace=workspace,
            thread_id=thread_id,
            effort=effort,
            progress=_progress(ctx),
        )

    @mcp.tool()
    async def run(
        message: str,
        workspace: str | None = None,
        thread_id: str | None = None,
        approval: ApprovalName = "auto",
        effort: EffortName = "balanced",
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Delegate a mutating electronics task. approval is auto or yolo."""
        return await handle_run(
            message=message,
            workspace=workspace,
            thread_id=thread_id,
            approval=approval,
            effort=effort,
            progress=_progress(ctx),
        )

    @mcp.tool()
    async def resume(
        thread_id: str,
        message: str,
        workspace: str | None = None,
        mode: ModeName = "agent",
        approval: ApprovalName = "auto",
        effort: EffortName | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Continue an existing CopperPilot thread. thread_id is required.

        mode defaults to agent (mutating). Pass mode="ask" to keep continuing
        a read-only conversation without escalating to write access.
        """
        return await handle_resume(
            thread_id=thread_id,
            message=message,
            workspace=workspace,
            mode=mode,
            approval=approval,
            effort=effort,
            progress=_progress(ctx),
        )

    @mcp.tool()
    async def validate(
        workspace: str | None = None,
        profile: str | None = None,
        gate: GateName = "stage",
        until: str | None = None,
        only: list[str] | None = None,
        effort: EffortName | None = None,
        no_cache: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Run the read-only validate graph; returns verdict, exit_code and next_actions."""
        return await handle_validate(
            workspace=workspace,
            profile=profile,
            gate=gate,
            until=until,
            only=only,
            effort=effort,
            no_cache=no_cache,
            progress=_progress(ctx),
        )

    @mcp.tool()
    async def threads_list(
        workspace: str | None = None,
        limit: int = 20,
        all_workspaces: bool = False,
    ) -> dict[str, Any]:
        """List CopperPilot chat threads for this workspace."""
        return await handle_threads_list(
            workspace=workspace,
            limit=limit,
            all_workspaces=all_workspaces,
        )

    return mcp


def registered_tool_names(server: FastMCP | None = None) -> set[str]:
    """Return the registered conductor tool names (for tests)."""
    mcp = server or create_server()
    manager = getattr(mcp, "_tool_manager", None)
    if manager is None:
        return set()
    tools = manager.list_tools()
    return {tool.name for tool in tools}


def run_mcp() -> None:
    """Serve the conductor API over stdin/stdout. Do not write to stdout."""
    create_server().run(transport="stdio")
