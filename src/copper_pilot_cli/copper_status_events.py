"""Typed hosted status events shared by graph, TUI, headless, and MCP."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

SUBAGENT_TRANSCRIPT_CHAR_CAP = 12_000
COPPER_CONTEXT_KEY = "copper_transcript_context"
SUBAGENT_STATUSES = {"running", "success", "failed", "cancelled"}


@dataclass(frozen=True, slots=True)
class SubagentStatus:
    id: str
    task: str
    goal: str = ""
    status: str = "failed"
    output: str = ""
    error: str = ""


def is_subagent_status_event(value: Any) -> bool:
    return isinstance(value, dict) and value.get("kind") == "subagent"


def normalize_subagent_status(
    value: Any,
    previous: SubagentStatus | None = None,
) -> SubagentStatus | None:
    """Normalize one worker update; keepalives and id-less events are ignored."""
    if not is_subagent_status_event(value) or value.get("status") == "keepalive":
        return None
    identifier = str(value.get("id") or "").strip()
    if not identifier:
        return None
    status = str(value.get("status") or (previous.status if previous else "failed"))
    if status not in SUBAGENT_STATUSES:
        status = "failed"
    current = previous or SubagentStatus(id=identifier, task="")
    return replace(
        current,
        task=str(value.get("task") or current.task).strip(),
        goal=str(value.get("goal") or current.goal).strip(),
        status=status,
        output=(str(value["output"]) if value.get("output") is not None else current.output),
        error=str(value["error"]) if value.get("error") is not None else current.error,
    )


def format_subagent_transcript(value: SubagentStatus) -> str:
    """Match the Electron client's model-facing worker transcript."""
    task = value.task or "Survey"
    if value.status in {"failed", "cancelled"}:
        reason = (value.error or "Subagent failed.").strip()
        return f"\n[Subagent: {task}]\nSubagent failed: {reason}\n"
    if value.status == "running" or not value.output:
        return ""
    body = value.output.strip()
    if len(body) > SUBAGENT_TRANSCRIPT_CHAR_CAP:
        body = (
            body[:SUBAGENT_TRANSCRIPT_CHAR_CAP].rstrip()
            + "\n[truncated; see notes under .copperpilot/]"
        )
    return f"\n[Subagent: {task}]\n{body}\n"


def subagent_progress_label(value: SubagentStatus) -> str:
    labels = {
        "running": "Running",
        "success": "Done",
        "failed": "Failed",
        "cancelled": "Stopped",
    }
    task = value.task or "Survey"
    return f"Subagent: {task} — {labels[value.status]}"
