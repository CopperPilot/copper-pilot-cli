from __future__ import annotations

import os
import stat
from types import SimpleNamespace

from copper_pilot_cli import copper_preferences
from copper_pilot_cli.copper_tools import ApprovalMode


def test_shell_auto_allow_defaults_and_atomic_private_persistence(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        copper_preferences,
        "paths",
        lambda: SimpleNamespace(state=tmp_path / "state"),
    )
    assert copper_preferences.shell_auto_allow_settings() == (True, False)

    copper_preferences.save_shell_auto_allow_settings(normal=False, dangerous=True)

    assert copper_preferences.shell_auto_allow_settings() == (False, True)
    config = tmp_path / "state/config.json"
    # POSIX 0600 is not represented in Windows st_mode (typically 0666).
    if os.name != "nt":
        assert stat.S_IMODE(config.stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR
    assert not config.with_suffix(".tmp").exists()


def test_yolo_is_never_saved_as_startup_mode(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        copper_preferences,
        "paths",
        lambda: SimpleNamespace(state=tmp_path / "state"),
    )
    copper_preferences.save_approval_mode(ApprovalMode.YOLO)
    assert copper_preferences.saved_approval_mode() is ApprovalMode.MANUAL
