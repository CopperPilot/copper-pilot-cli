"""Narrow Textual compatibility patches retained from dcode 0.1.69."""

from __future__ import annotations

import logging

from textual.geometry import Offset
from textual.screen import Screen
from textual.widget import Widget

logger = logging.getLogger(__name__)

_original_get_widget_and_offset_at = Screen.get_widget_and_offset_at


def _get_widget_and_offset_at_attached(
    self: Screen[object],
    x: int,
    y: int,
) -> tuple[Widget | None, Offset | None]:
    """Ignore stale compositor hits left by a streamed widget refresh."""
    widget, offset = _original_get_widget_and_offset_at(self, x, y)
    if (
        widget is not None
        and not isinstance(widget, Screen)
        and (widget.parent is None or not widget.is_attached)
    ):
        return None, None
    return widget, offset


try:
    Screen.get_widget_and_offset_at = _get_widget_and_offset_at_attached
except (AttributeError, TypeError) as exc:  # pragma: no cover - defensive
    logger.warning("Textual detached-hit patch assignment rejected: %s", exc)
