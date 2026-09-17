"""Workspace hook support routed through the normal approval broker."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Literal

from copper_pilot_cli.copper_protocol import ToolRequest
from copper_pilot_cli.copper_tools import LocalToolBroker

HookEvent = Literal["before_turn", "after_turn"]


class HookRunner:
    """Load bounded project hooks and execute them as approved shell tools."""

    def __init__(self, workspace: Path, broker: LocalToolBroker) -> None:
        self.workspace = workspace
        self.broker = broker

    def commands(self, event: HookEvent) -> list[str]:
        path = self.workspace / ".copperpilot" / "hooks.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return []
        raw = value.get(event) if isinstance(value, dict) else None
        if not isinstance(raw, list):
            return []
        return [str(command)[:4_000] for command in raw[:16] if str(command).strip()]

    async def run(self, event: HookEvent) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for command in self.commands(event):
            result = await self.broker.execute(
                ToolRequest(
                    tool_call_id=f"hook-{event}-{uuid.uuid4()}",
                    tool_name="bash",
                    arguments={
                        "command": command,
                        "description": f"CopperPilot {event.replace('_', ' ')} hook",
                        "background": False,
                    },
                )
            )
            results.append(result if isinstance(result, dict) else {"result": result})
        return results
