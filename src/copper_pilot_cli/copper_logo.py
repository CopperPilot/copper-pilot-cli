"""Thin orange CopperPilot cube, drawn with Braille.

Six rows. Each row is right-padded to 13 cells. The mark is a single colour,
``orange-600``, and is hidden by the welcome layout when the terminal is
narrow, ``NO_COLOR`` is set, or ASCII mode is on.
"""

from __future__ import annotations

LOGO_WIDTH = 13

# Rows 0 and 5 are 12 cells in the source art; padding makes every row 13.
LOGO_ROWS: tuple[str, ...] = (
    " ⣀⡠⠤⠒⠊⠉⠒⠢⠤⣀⡀",
    "⡏⠒⠢⠤⣀⡀ ⣀⡠⠤⠒⠊⡇",
    "⡇    ⠈⡏     ⡇",
    "⡇     ⡇     ⡇",
    "⣇     ⡇    ⢀⡇",
    " ⠉⠑⠒⠤⢄⣇⠤⠔⠒⠉⠁",
)


def logo_lines() -> list[str]:
    """Return the six logo rows, each exactly 13 cells wide."""
    return [row.ljust(LOGO_WIDTH) for row in LOGO_ROWS]
