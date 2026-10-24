"""The Braille cube is six rows of 13 cells."""

from copper_pilot_cli.copper_logo import LOGO_WIDTH, logo_lines


def test_logo_is_six_rows_of_thirteen() -> None:
    rows = logo_lines()
    assert len(rows) == 6
    assert LOGO_WIDTH == 13
    assert all(len(row) == 13 for row in rows)
