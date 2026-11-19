"""Bring your own agent: run the user's own Claude Code or Codex with CopperPilot tools.

The user's agent runs as itself (its own system prompt, built-in tools,
permission modes and native subagents). CopperPilot adds its tools over MCP,
a short guide appended to the agent's prompt, and the app around it. The
agent never writes its own transcript to disk; ``sessions.db`` is the
transcript, and a fresh agent session is seeded with a capped handoff.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any


class Provider(StrEnum):
    """Who runs the chat's main agent."""

    COPPERPILOT = "copperpilot"
    CLAUDE = "claude"
    CODEX = "codex"


PROVIDER_LABELS = {
    Provider.COPPERPILOT: "CopperPilot",
    # Terms: the picker label is "Claude Agent"; no Anthropic or Claude marks.
    Provider.CLAUDE: "Claude Agent",
    Provider.CODEX: "Codex",
}
PROVIDER_HELP = {
    Provider.COPPERPILOT: "CopperPilot's hosted agent (Balanced or Intelligence)",
    Provider.CLAUDE: "uses your installed Claude Code and your own Claude login",
    Provider.CODEX: "uses your installed Codex and your own Codex login",
}
BYO_PROVIDERS = (Provider.CLAUDE, Provider.CODEX)


def normalize_provider(value: Any) -> Provider:
    """Unknown or missing providers stay on CopperPilot."""
    try:
        return Provider(str(value or Provider.COPPERPILOT).strip().lower())
    except ValueError:
        return Provider.COPPERPILOT


@dataclass(frozen=True, slots=True)
class AgentSelection:
    """Provider plus the provider's own model options for one chat.

    ``model`` and ``effort`` are the provider's ids (Claude ``claude-sonnet-5-5``
    and ``low``; Codex ids from ``model/list``). ``None`` means the provider's
    default. CopperPilot ignores them (its effort is ``copper_effort``).
    """

    provider: Provider = Provider.COPPERPILOT
    model: str | None = None
    effort: str | None = None
    fast_mode: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_byo(self) -> bool:
        return self.provider in BYO_PROVIDERS

    def options(self) -> dict[str, Any]:
        """The JSON-safe option dict stored per thread (``copper_agent``)."""
        result: dict[str, Any] = {}
        if self.model:
            result["model"] = self.model
        if self.effort:
            result["effort"] = self.effort
        if self.fast_mode:
            result["fast_mode"] = True
        return result

    def with_options(self, value: Any) -> AgentSelection:
        options = value if isinstance(value, dict) else {}
        model = options.get("model")
        effort = options.get("effort")
        return replace(
            self,
            model=str(model).strip() or None if isinstance(model, str) else None,
            effort=str(effort).strip().lower() or None if isinstance(effort, str) else None,
            fast_mode=options.get("fast_mode") is True,
        )

    def label(self) -> str:
        base = PROVIDER_LABELS[self.provider]
        if not self.is_byo:
            return base
        parts = [base]
        if self.model:
            parts.append(self.model)
        if self.effort:
            parts.append(self.effort)
        if self.fast_mode:
            parts.append("fast")
        return " · ".join(parts)


def selection_from_state(provider: Any, options: Any) -> AgentSelection:
    return AgentSelection(provider=normalize_provider(provider)).with_options(options)


__all__ = [
    "BYO_PROVIDERS",
    "PROVIDER_HELP",
    "PROVIDER_LABELS",
    "AgentSelection",
    "Provider",
    "normalize_provider",
    "selection_from_state",
]
