"""CopperPilot colours, theme presets, and glyphs.

``black`` is the default preset; ``/theme`` switches to ``warm``, ``light``,
or ``high-contrast``. Truecolor when ``COLORTERM`` is ``truecolor`` or ``24bit``. Otherwise the
ANSI-256 indexes on each token. ``NO_COLOR`` removes colour and leaves the
layout unchanged. ``COPPERPILOT_ASCII=1``, or a locale that is not UTF-8,
switches glyphs to ASCII.
"""

from __future__ import annotations

import functools
import locale
import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class ColorMode(StrEnum):
    TRUECOLOR = "truecolor"
    ANSI = "ansi"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class Token:
    hex: str
    ansi: int | None = None


_ORANGE_AND_STATUS: dict[str, Token] = {
    "orange-600": Token("#EA580C", 166),
    "orange-500": Token("#F97316", 208),
    "orange-900": Token("#7C2D12", 94),
    "on-fill": Token("#FFFFFF", 231),
    "status-ok": Token("#7FC98F", 114),
    "status-warn": Token("#E8B04B", 179),
    "status-error": Token("#E06C75", 167),
}

# Every preset defines the same keys. One orange; status hues are for tool results only.
PRESETS: dict[str, dict[str, Token]] = {
    "black": {
        "bg": Token("#0A0A0A", 232),
        "surface": Token("#171717", 234),
        "rule": Token("#404040", 238),
        "divider": Token("#262626", 235),
        "heading": Token("#FAFAFA", 231),
        "text": Token("#F5F5F5", 255),
        "text-soft": Token("#D4D4D4", 252),
        "muted": Token("#A3A3A3", 248),
        "stone-500": Token("#737373", 243),
        **_ORANGE_AND_STATUS,
    },
    "warm": {
        "bg": Token("#1C1917", 234),
        "surface": Token("#292524", 235),
        "rule": Token("#57534E", 240),
        "divider": Token("#44403C", 238),
        "heading": Token("#FAFAF9", 231),
        "text": Token("#F4F4F0", 255),
        "text-soft": Token("#D6D3D1", 252),
        "muted": Token("#A8A29E", 248),
        "stone-500": Token("#78716C", 243),
        **_ORANGE_AND_STATUS,
    },
    "light": {
        "bg": Token("#FAFAFA", 231),
        "surface": Token("#EEEEEE", 255),
        "rule": Token("#A3A3A3", 248),
        "divider": Token("#D4D4D4", 252),
        "heading": Token("#0A0A0A", 232),
        "text": Token("#171717", 234),
        "text-soft": Token("#404040", 238),
        "muted": Token("#6B6B6B", 242),
        "stone-500": Token("#8A8A8A", 245),
        "orange-600": Token("#C2410C", 166),
        "orange-500": Token("#EA580C", 202),
        # Unlit working-indicator dot. A pale peach disappears on the light background.
        "orange-900": Token("#C47A4A", 173),
        "on-fill": Token("#FFFFFF", 231),
        "status-ok": Token("#15803D", 28),
        "status-warn": Token("#B45309", 130),
        "status-error": Token("#B91C1C", 124),
    },
    "high-contrast": {
        "bg": Token("#000000", 16),
        "surface": Token("#1C1C1C", 234),
        "rule": Token("#FFFFFF", 231),
        "divider": Token("#BFBFBF", 250),
        "heading": Token("#FFFFFF", 231),
        "text": Token("#FFFFFF", 231),
        "text-soft": Token("#FFFFFF", 231),
        "muted": Token("#E4E4E4", 254),
        "stone-500": Token("#BFBFBF", 250),
        "orange-600": Token("#FF7A1A", 208),
        "orange-500": Token("#FF9F43", 214),
        "orange-900": Token("#8A3A00", 94),
        "on-fill": Token("#000000", 16),
        "status-ok": Token("#5FFF87", 84),
        "status-warn": Token("#FFD75F", 221),
        "status-error": Token("#FF5F5F", 203),
    },
}
DARK_PRESETS: frozenset[str] = frozenset({"black", "warm", "high-contrast"})
DEFAULT_THEME = "black"
THEME_LABELS: dict[str, str] = {
    "black": "Black - neutral near-black (default)",
    "warm": "Warm - stone greys",
    "light": "Light - for light terminals",
    "high-contrast": "High contrast - maximum legibility",
}

# The active preset. Mutated in place so ``from copper_theme import TOKENS`` stays live.
TOKENS: dict[str, Token] = dict(PRESETS[DEFAULT_THEME])
_active = DEFAULT_THEME


def theme_names() -> tuple[str, ...]:
    return tuple(PRESETS)


def active_theme() -> str:
    return _active


def is_dark(name: str | None = None) -> bool:
    return (name or _active) in DARK_PRESETS


def set_active_theme(name: str) -> str:
    """Switch the active preset. Unknown names raise ``KeyError``."""
    global _active
    preset = PRESETS[name]
    TOKENS.clear()
    TOKENS.update(preset)
    _active = name
    return name


_GLYPHS: dict[str, tuple[str, str]] = {
    "plan": ("✎", "/"),
    "ask": ("?", "?"),
    "agent": ("↻", "*"),
    "balanced": ("⇌", "="),
    "intelligent": ("∴", ":"),
    "dot": ("•", "*"),
    "tl": ("╭", "+"),
    "tr": ("╮", "+"),
    "bl": ("╰", "+"),
    "br": ("╯", "+"),
    "h": ("─", "-"),
    "v": ("│", "|"),
    "user": ("❯", ">"),
}


def detect_color_mode(environ: Mapping[str, str] | None = None) -> ColorMode:
    """Choose a colour mode from the environment."""
    env = os.environ if environ is None else environ
    if "NO_COLOR" in env:
        return ColorMode.NONE
    if env.get("COLORTERM", "").lower() in {"truecolor", "24bit"}:
        return ColorMode.TRUECOLOR
    return ColorMode.ANSI


def color_mode_for_app(app: object) -> ColorMode:
    """Prefer the app's NO_COLOR flag; Textual removes the env var at startup."""
    if getattr(app, "no_color", False):
        return ColorMode.NONE
    return detect_color_mode()


def ascii_mode(environ: Mapping[str, str] | None = None) -> bool:
    """ASCII glyphs when requested, or when the locale is not UTF-8."""
    env = os.environ if environ is None else environ
    if env.get("COPPERPILOT_ASCII", "").strip() == "1":
        return True
    for key in ("LC_ALL", "LC_CTYPE", "LANG"):
        value = env.get(key)
        if value:
            lowered = value.lower().replace("-", "")
            return "utf8" not in lowered
    return "utf" not in _locale_encoding().lower()


@functools.cache
def _locale_encoding() -> str:
    """The process locale encoding does not change after startup."""
    return locale.getencoding()


def glyph(name: str, *, ascii: bool | None = None) -> str:
    """Return one chrome glyph, or its ASCII fallback."""
    pair = _GLYPHS[name]
    use_ascii = ascii_mode() if ascii is None else ascii
    return pair[1] if use_ascii else pair[0]


def soften(text: str, *, ascii: bool | None = None) -> str:
    """Replace punctuation that ASCII mode cannot draw."""
    use_ascii = ascii_mode() if ascii is None else ascii
    if not use_ascii:
        return text
    return text.replace("—", " - ").replace("…", "...")


def style_for(token: str, mode: ColorMode) -> str:
    """Return a Textual content style for ``token``, or empty when uncoloured."""
    if mode is ColorMode.NONE:
        return ""
    item = TOKENS[token]
    if mode is ColorMode.ANSI:
        if item.ansi is None:
            return ""
        return f"color({item.ansi})"
    return item.hex


def textual_theme_name(name: str | None = None) -> str:
    """Textual theme id for a preset. The default keeps the historic ``copper-pilot`` id."""
    preset = name or _active
    return "copper-pilot" if preset == DEFAULT_THEME else f"copper-pilot-{preset}"


def css_variables(mode: ColorMode | None = None, *, theme: str | None = None) -> dict[str, str]:
    """Theme variables. CSS keeps hex; Textual downgrades them on the driver."""
    del mode
    tokens = PRESETS[theme] if theme else TOKENS
    variables = {name: token.hex for name, token in tokens.items()}
    variables.update(
        {
            "accent": tokens["orange-500"].hex,
            "accent-light": tokens["orange-500"].hex,
            "border": tokens["divider"].hex,
            "fg": tokens["text"].hex,
            "fg-muted": tokens["text-soft"].hex,
            "fg-dim": tokens["muted"].hex,
            "selection": tokens["surface"].hex,
            "input-bg": tokens["bg"].hex,
            "warn": tokens["orange-600"].hex,
            "ok": tokens["status-ok"].hex,
            "mode-bash": tokens["orange-600"].hex,
            "mode-command": tokens["orange-500"].hex,
            "mode-incognito": tokens["muted"].hex,
            "skill": tokens["orange-500"].hex,
            "skill-hover": tokens["orange-600"].hex,
            "tool": tokens["orange-500"].hex,
            "tool-hover": tokens["orange-600"].hex,
        }
    )
    return variables


def css_variable_defaults() -> dict[str, str]:
    return css_variables()
