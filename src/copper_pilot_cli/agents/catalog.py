"""Model and effort catalog for the user's own agents.

The Claude part is ported from T3 Code's ``model-manifest.json`` and
``claudeModelOptions.ts`` (MIT, Copyright (c) T3 Tools Inc.; see NOTICE):
the model list with current/legacy status and aliases, each profile's
effort levels and default, Fast mode support, the effort map Claude Code
needs for older models, and the context window. Ultracode and Ultrathink
are not offered here. Codex models are not bundled: they come from the
user's own ``codex app-server`` (``model/list``) at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

EFFORT_LABELS = {
    "none": "None",
    "minimal": "Minimal",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra High",
    "max": "Max",
}
DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"


@dataclass(frozen=True, slots=True)
class AgentModel:
    """One selectable model of a provider."""

    id: str
    label: str
    efforts: tuple[str, ...] = ()
    default_effort: str | None = None
    fast_mode: bool = False
    legacy: bool = False
    aliases: tuple[str, ...] = ()
    context_window: int | None = None
    # Effort ids Claude Code expects instead of the picker's (older models).
    effort_map: dict[str, str] = field(default_factory=dict)
    description: str = ""
    is_default: bool = False


@dataclass(frozen=True, slots=True)
class _Profile:
    efforts: tuple[str, ...]
    default: str | None
    fast_mode: bool
    context_window: int
    effort_map: dict[str, str] = field(default_factory=dict)


_ALL = ("low", "medium", "high", "xhigh", "max")
_NO_XHIGH = ("low", "medium", "high", "max")
_PROFILES: dict[str, _Profile] = {
    "fable-5": _Profile(_ALL, "medium", False, 1_000_000),
    "opus-5-5": _Profile(_ALL, "medium", True, 1_000_000),
    "opus-5": _Profile(_ALL, "high", True, 1_000_000),
    "opus-4-8": _Profile(_ALL, "high", True, 1_000_000),
    "opus-4-7": _Profile(_ALL, "xhigh", True, 1_000_000, {"xhigh": "max"}),
    "opus-4-6": _Profile(_NO_XHIGH, "high", True, 1_000_000),
    "opus-4-5": _Profile(_NO_XHIGH, "high", True, 200_000),
    "sonnet-5": _Profile(_ALL, "high", False, 1_000_000),
    "sonnet-5-5": _Profile(_ALL, "high", False, 1_000_000),
    "sonnet-4-6": _Profile(_NO_XHIGH, "high", False, 1_000_000, {"max": "high"}),
    "haiku-5-5": _Profile(_ALL, "medium", False, 1_000_000),
    "haiku-4-5": _Profile((), None, False, 200_000),
}

# (slug, name, profile, legacy, aliases)
_CLAUDE_MODELS: tuple[tuple[str, str, str, bool, tuple[str, ...]], ...] = (
    ("claude-opus-5-5", "Claude Opus 5.5", "opus-5-5", False, ("opus", "opus-5.5")),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5", "sonnet-5-5", False, ("sonnet", "sonnet-5.5")),
    ("claude-fable-5-2", "Claude Fable 5.2", "fable-5", False, ("fable", "fable-5.2")),
    ("claude-haiku-5-5", "Claude Haiku 5.5", "haiku-5-5", False, ("haiku", "haiku-5.5")),
    ("claude-fable-5-1", "Claude Fable 5.1", "fable-5", True, ("fable-5.1",)),
    ("claude-fable-5", "Claude Fable 5", "fable-5", True, ()),
    ("claude-opus-5", "Claude Opus 5", "opus-5", True, ("opus-5",)),
    ("claude-opus-4-8", "Claude Opus 4.8", "opus-4-8", True, ("opus-4.8",)),
    ("claude-opus-4-7", "Claude Opus 4.7", "opus-4-7", True, ("opus-4.7",)),
    ("claude-opus-4-6", "Claude Opus 4.6", "opus-4-6", True, ("opus-4.6",)),
    ("claude-opus-4-5", "Claude Opus 4.5", "opus-4-5", True, ()),
    ("claude-sonnet-5", "Claude Sonnet 5", "sonnet-5", True, ("sonnet-5",)),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6", "sonnet-4-6", True, ("sonnet-4.6",)),
    ("claude-haiku-4-5", "Claude Haiku 4.5", "haiku-4-5", True, ("haiku-4.5",)),
)


def _claude_model(
    slug: str, name: str, profile: str, legacy: bool, aliases: tuple[str, ...]
) -> AgentModel:
    spec = _PROFILES[profile]
    return AgentModel(
        id=slug,
        label=name,
        efforts=spec.efforts,
        default_effort=spec.default,
        fast_mode=spec.fast_mode,
        legacy=legacy,
        aliases=aliases,
        context_window=spec.context_window,
        effort_map=dict(spec.effort_map),
        is_default=slug == DEFAULT_CLAUDE_MODEL,
    )


CLAUDE_MODELS: tuple[AgentModel, ...] = tuple(_claude_model(*row) for row in _CLAUDE_MODELS)


def claude_model(value: str | None) -> AgentModel | None:
    """A Claude catalog model by slug or alias; None for unknown ids."""
    key = str(value or "").strip().lower()
    if not key:
        return None
    for model in CLAUDE_MODELS:
        if key == model.id or key in model.aliases:
            return model
    return None


@dataclass(frozen=True, slots=True)
class ClaudeLaunch:
    """What the SDK is given for one Claude selection."""

    model: str
    effort: str | None
    fast_mode: bool | None
    context_window: int


def compile_claude_selection(
    model: str | None, effort: str | None, fast_mode: bool = False
) -> ClaudeLaunch:
    """Resolve the model, the effort Claude Code expects and Fast mode.

    Unknown model ids pass through unchanged (a newer Claude Code may know
    them) with the requested effort; known models clamp the effort to the
    profile's levels and map it the way Claude Code expects.
    """
    entry = claude_model(model) or (None if model else claude_model(DEFAULT_CLAUDE_MODEL))
    if entry is None:
        return ClaudeLaunch(
            model=str(model),
            effort=effort,
            fast_mode=fast_mode or None,
            context_window=200_000,
        )
    chosen = effort if effort in entry.efforts else entry.default_effort
    if chosen is not None:
        chosen = entry.effort_map.get(chosen, chosen)
    return ClaudeLaunch(
        model=entry.id,
        effort=chosen,
        fast_mode=fast_mode if entry.fast_mode else None,
        context_window=entry.context_window or 200_000,
    )


def codex_models(payload: Any) -> list[AgentModel]:
    """Models from a ``model/list`` response (hidden ones skipped)."""
    rows = payload.get("data") if isinstance(payload, dict) else None
    models: list[AgentModel] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("hidden") is True:
            continue
        identifier = str(row.get("id") or row.get("model") or "").strip()
        if not identifier:
            continue
        efforts = tuple(
            str(option.get("reasoningEffort"))
            for option in row.get("supportedReasoningEfforts") or []
            if isinstance(option, dict) and option.get("reasoningEffort")
        )
        default = row.get("defaultReasoningEffort")
        models.append(
            AgentModel(
                id=identifier,
                label=str(row.get("displayName") or identifier),
                efforts=efforts,
                default_effort=str(default) if default else None,
                description=str(row.get("description") or ""),
                is_default=row.get("isDefault") is True,
            )
        )
    return models


def effort_label(value: str | None) -> str:
    if not value:
        return "Default"
    return EFFORT_LABELS.get(value, value.replace("_", " ").title())


__all__ = [
    "CLAUDE_MODELS",
    "DEFAULT_CLAUDE_MODEL",
    "AgentModel",
    "ClaudeLaunch",
    "claude_model",
    "codex_models",
    "compile_claude_selection",
    "effort_label",
]
