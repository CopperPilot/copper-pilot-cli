from __future__ import annotations

import base64
import io
from typing import Self
from unittest.mock import MagicMock, PropertyMock, patch

from textual.dom import NoScreen

from copper_pilot_cli.clipboard import (
    _copy_osc52,
    copy_selection_to_clipboard,
    copy_text_to_clipboard,
)


def test_clipboard_backend_falls_through_in_dcode_order(monkeypatch) -> None:
    app = MagicMock()
    monkeypatch.setattr(
        "copper_pilot_cli.clipboard.pyperclip.copy",
        MagicMock(side_effect=RuntimeError("unavailable")),
    )
    app.copy_to_clipboard.side_effect = OSError("unavailable")
    osc52 = MagicMock()
    monkeypatch.setattr("copper_pilot_cli.clipboard._copy_osc52", osc52)

    assert copy_text_to_clipboard(app, "selected") == (True, None)
    app.copy_to_clipboard.assert_called_once_with("selected")
    osc52.assert_called_once_with("selected")


def test_selection_copy_skips_detached_and_reports_exact_toast() -> None:
    app = MagicMock()
    detached = MagicMock()
    detached.is_attached = False
    type(detached).text_selection = PropertyMock(
        side_effect=AssertionError("detached selection must not be read")
    )
    selected = MagicMock()
    selected.is_attached = True
    selected.text_selection = MagicMock()
    selected.get_selection.return_value = ("selected text", None)
    screen = MagicMock()
    screen.query.return_value = [detached, selected]

    with patch(
        "copper_pilot_cli.clipboard.copy_text_to_clipboard",
        return_value=(True, None),
    ) as copy:
        copy_selection_to_clipboard(app, screen=screen)

    copy.assert_called_once_with(app, "selected text")
    app.notify.assert_called_once_with(
        '"selected text" copied',
        severity="information",
        timeout=2,
        markup=False,
    )


def test_selection_copy_tolerates_detach_race() -> None:
    app = MagicMock()
    widget = MagicMock()
    widget.is_attached = True
    type(widget).text_selection = PropertyMock(side_effect=NoScreen("detached"))
    screen = MagicMock()
    screen.query.return_value = [widget]

    with patch("copper_pilot_cli.clipboard.copy_text_to_clipboard") as copy:
        copy_selection_to_clipboard(app, screen=screen)

    copy.assert_not_called()


def test_osc52_emits_tmux_compatible_terminal_sequence(monkeypatch) -> None:
    captured = io.StringIO()

    class DummyTTY:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def write(self, value: str) -> int:
            return captured.write(value)

        def flush(self) -> None:
            return None

    monkeypatch.setenv("TMUX", "1")
    with patch("pathlib.Path.open", return_value=DummyTTY()):
        _copy_osc52("hello")

    encoded = base64.b64encode(b"hello").decode("ascii")
    assert captured.getvalue() == f"\033Ptmux;\033\033]52;c;{encoded}\a\033\\"
