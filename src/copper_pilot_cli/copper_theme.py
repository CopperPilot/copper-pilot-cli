"""CopperPilot v3 colours and glyphs.

Truecolor when ``COLORTERM`` is ``truecolor`` or ``24bit``. Otherwise the
ANSI-256 indexes on each token. ``NO_COLOR`` removes colour and leaves the
layout unchanged. ``COPPERPILOT_ASCII=1``, or a locale that is not UTF-8,
switches glyphs to ASCII.
"""

from __future__ import annotations

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


# Warm neutrals plus one orange. Status hues are for tool results only.
TOKENS: dict[str, Token] = {
    "bg": Token("#1C1917", 234),
    "surface": Token("#292524", 235),
    "rule": Token("#57534E", 240),
    "divider": Token("#44403C", 238),
    "orange-600": Token("#EA580C", 166),
    "orange-500": Token("#F97316", 208),
    "orange-900": Token("#7C2D12", 94),
    "heading": Token("#FAFAF9", 231),
    "text": Token("#F4F4F0", 255),
    "text-soft": Token("#D6D3D1", 252),
    "muted": Token("#A8A29E", 248),
    "stone-500": Token("#78716C", 243),
    "on-fill": Token("#FFFFFF", 231),
    "status-ok": Token("#7FC98F", 114),
    "status-warn": Token("#E8B04B", 179),
    "status-error": Token("#E06C75", 167),
}

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
    encoding = locale.getencoding()
    return "utf" not in encoding.lower()


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


def css_variables(mode: ColorMode | None = None) -> dict[str, str]:
    """Theme variables. CSS keeps hex; Textual downgrades them on the driver."""
    del mode
    variables = {name: token.hex for name, token in TOKENS.items()}
    variables.update(
        {
            "accent": TOKENS["orange-500"].hex,
            "accent-light": TOKENS["orange-500"].hex,
            "border": TOKENS["divider"].hex,
            "fg": TOKENS["text"].hex,
            "fg-muted": TOKENS["text-soft"].hex,
            "fg-dim": TOKENS["muted"].hex,
            "selection": TOKENS["surface"].hex,
            "input-bg": TOKENS["bg"].hex,
            "warn": TOKENS["orange-600"].hex,
            "ok": TOKENS["status-ok"].hex,
            "mode-bash": TOKENS["orange-600"].hex,
            "mode-command": TOKENS["orange-500"].hex,
            "mode-incognito": TOKENS["muted"].hex,
            "skill": TOKENS["orange-500"].hex,
            "skill-hover": TOKENS["orange-600"].hex,
            "tool": TOKENS["orange-500"].hex,
            "tool-hover": TOKENS["orange-600"].hex,
        }
    )
    return variables


def css_variable_defaults() -> dict[str, str]:
    return css_variables()
