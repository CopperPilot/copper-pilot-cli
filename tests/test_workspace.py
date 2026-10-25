from __future__ import annotations

import json
import subprocess
import sys

import pytest

from copper_pilot_cli import copper_workspace
from copper_pilot_cli.copper_config import paths
from copper_pilot_cli.copper_preferences import load_preferences, save_preferences
from copper_pilot_cli.copper_workspace import (
    canonical_workspace,
    discover_workspace,
    remember_workspace,
)


def test_any_directory_is_a_valid_workspace(tmp_path) -> None:
    root = canonical_workspace(tmp_path)
    context = discover_workspace(root)
    assert context.root == tmp_path.resolve()
    assert context.project_files == ()
    assert context.schematic_path is None
    assert context.pcb_path is None


def test_discovers_matching_kicad_artifacts(tmp_path) -> None:
    board = tmp_path / "boards"
    board.mkdir()
    for name in ("amp.kicad_pro", "amp.kicad_sch", "amp.kicad_pcb"):
        (board / name).write_text("")
    context = discover_workspace(tmp_path.resolve())
    assert context.project_files == ("boards/amp.kicad_pro",)
    assert context.schematic_path == "boards/amp.kicad_sch"
    assert context.pcb_path == "boards/amp.kicad_pcb"


def test_launch_target_must_be_directory(tmp_path) -> None:
    file = tmp_path / "board.kicad_pro"
    file.write_text("")
    with pytest.raises(NotADirectoryError):
        canonical_workspace(file)


_REMEMBER_MANY = """
import sys
from pathlib import Path
from copper_pilot_cli.copper_workspace import discover_workspace, remember_workspace

base = Path(sys.argv[1])
for index in range(int(sys.argv[2])):
    folder = base / f"w{index}"
    folder.mkdir(exist_ok=True)
    remember_workspace(discover_workspace(folder.resolve()), limit=1000)
"""


def test_concurrent_processes_remember_workspaces_without_racing(tmp_path, monkeypatch) -> None:
    """Parallel `copper-pilot acp` session starts must not crash on recent-projects."""
    home = tmp_path / "home"
    monkeypatch.setenv("COPPER_PILOT_CLI_HOME", str(home))
    workers = []
    for worker in range(8):
        base = tmp_path / f"p{worker}"
        base.mkdir()
        workers.append(
            subprocess.Popen(  # noqa: S603 - fixed argv, our own interpreter
                [sys.executable, "-c", _REMEMBER_MANY, str(base), "15"],
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    for process in workers:
        _, stderr = process.communicate(timeout=120)
        assert process.returncode == 0, stderr
    destination = home / ".state" / "recent-projects.json"
    rows = json.loads(destination.read_text())
    assert isinstance(rows, list) and rows
    assert not list(destination.parent.glob("*.tmp"))
    if sys.platform != "win32":
        # The file lock serializes read-modify-write, so no process drops
        # another's entry by writing back a stale read.
        assert len(rows) == 8 * 15


def test_remember_workspace_failure_does_not_raise(tmp_path, monkeypatch, caplog) -> None:
    def refuse(*_args, **_kwargs):
        raise FileNotFoundError("tmp moved away")

    monkeypatch.setattr(copper_workspace, "atomic_write_text", refuse)
    remember_workspace(discover_workspace(tmp_path.resolve()))
    assert "Could not update recent projects" in caplog.text


def test_save_preferences_uses_a_unique_temp_file(monkeypatch) -> None:
    """A stray shared temp from another process must not be moved into place."""
    state = paths().state
    state.mkdir(parents=True, exist_ok=True)
    (state / "config.tmp").write_text("{not json")
    save_preferences({"tips": False})
    assert load_preferences() == {"tips": False}
    assert (state / "config.tmp").read_text() == "{not json"
