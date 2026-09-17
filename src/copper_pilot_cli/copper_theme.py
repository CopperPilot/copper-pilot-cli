"""CopperPilot semantic terminal colors."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ThemeColors:
    background: str = "#11121D"
    surface: str = "#1A1B2E"
    panel: str = "#25283B"
    text: str = "#C0CAF5"
    muted: str = "#7A83A8"
    primary: str = "#EA580C"
    active: str = "#FB923C"
    success: str = "#9ECE6A"
    warning: str = "#F97316"
    error: str = "#F7768E"


COLORS = ThemeColors()


def css_variable_defaults() -> dict[str, str]:
    return {
        "mode-bash": COLORS.error,
        "mode-command": COLORS.active,
        "mode-incognito": COLORS.muted,
        "skill": COLORS.primary,
        "skill-hover": COLORS.active,
        "tool": COLORS.primary,
        "tool-hover": COLORS.active,
    }
