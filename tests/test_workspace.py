from __future__ import annotations

import pytest

from copper_pilot_cli.copper_workspace import canonical_workspace, discover_workspace


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
