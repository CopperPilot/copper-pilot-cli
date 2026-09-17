"""Clipboard helpers adapted from Deep Agents Code 0.1.69."""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pyperclip
from textual.dom import NoScreen

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import App
    from textual.screen import Screen

logger = logging.getLogger(__name__)

_PREVIEW_MAX_LENGTH = 40


def _copy_osc52(text: str) -> None:
    """Copy text with OSC 52 for remote terminals and tmux."""
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    sequence = f"\033]52;c;{encoded}\a"
    if os.environ.get("TMUX"):
        sequence = f"\033Ptmux;\033{sequence}\033\\"
    with Path("/dev/tty").open("w", encoding="utf-8") as tty:
        tty.write(sequence)
        tty.flush()


def _shorten_preview(texts: list[str]) -> str:
    dense_text = "↵".join(texts).replace("\n", "↵")
    if len(dense_text) > _PREVIEW_MAX_LENGTH:
        return f"{dense_text[: _PREVIEW_MAX_LENGTH - 1]}…"
    return dense_text


def copy_text_to_clipboard(app: App[object], text: str) -> tuple[bool, str | None]:
    """Try the same clipboard backends as dcode, in reliability order."""
    methods: list[Callable[[str], object]] = [
        pyperclip.copy,
        app.copy_to_clipboard,
        _copy_osc52,
    ]
    last_error: str | None = None
    for copy in methods:
        try:
            copy(text)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            last_error = str(exc) or type(exc).__name__
            logger.debug(
                "Clipboard method %s failed: %s",
                getattr(copy, "__name__", repr(copy)),
                exc,
                exc_info=True,
            )
        else:
            return True, None
    return False, last_error


def copy_selection_to_clipboard(app: App[object], *, screen: Screen[object]) -> None:
    """Copy all non-empty selections owned by the screen that received mouse-up."""
    selected_texts: list[str] = []
    for widget in screen.query("*"):
        if not getattr(widget, "is_attached", False):
            continue
        try:
            selection = widget.text_selection
        except (NoScreen, AttributeError) as exc:
            logger.debug("Skipping detached selection widget: %s", exc)
            continue
        if not selection:
            continue
        try:
            result = widget.get_selection(selection)
        except (AttributeError, TypeError, ValueError, IndexError) as exc:
            logger.debug("Unable to extract widget selection: %s", exc, exc_info=True)
            continue
        if result:
            selected_text, _ = result
            if selected_text.strip():
                selected_texts.append(selected_text)

    if not selected_texts:
        return

    success, _ = copy_text_to_clipboard(app, "\n".join(selected_texts))
    if success:
        app.notify(
            f'"{_shorten_preview(selected_texts)}" copied',
            severity="information",
            timeout=2,
            markup=False,
        )
    else:
        app.notify(
            "Failed to copy - no clipboard method available",
            severity="warning",
            timeout=3,
            markup=False,
        )
