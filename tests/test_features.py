from __future__ import annotations

import pytest

from copper_pilot_cli.copper_features import (
    encode_runtime_context,
    looks_like_kicad_snippet,
    parse_plan,
    path_first_attachment,
    scan_local_skills,
)


def test_scans_bounded_local_skill(tmp_path) -> None:
    folder = tmp_path / ".copperpilot/skills/review-board"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        """---
required_inputs: A KiCad project
artifact_outputs: Review notes
---
# Skill: Review Board

## Objective
Review the board for electrical risks.
"""
    )
    skills, diagnostics = scan_local_skills(tmp_path)
    assert diagnostics == []
    assert skills[0].id == "review-board"
    assert skills[0].command_entry().display_name.endswith("[skill]")
    assert skills[0].manifest_entry()["source"]["path"].endswith("SKILL.md")


def test_invalid_skill_is_reported_not_loaded(tmp_path) -> None:
    folder = tmp_path / ".copperpilot/skills/BAD_NAME"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("# no frontmatter")
    skills, diagnostics = scan_local_skills(tmp_path)
    assert skills == []
    assert diagnostics


def test_parse_plan_build_context(tmp_path) -> None:
    path = tmp_path / ".copperpilot/plans/review.plan.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        """---
name: Review
overview: Check power
overall_status: running
todos:
  - id: rails
    content: Check rails
    status: open
---
## Steps
Check all rails.
"""
    )
    plan = parse_plan(path, tmp_path)
    assert plan.name == "Review"
    assert plan.todos[0].id == "rails"
    assert plan.file_path == ".copperpilot/plans/review.plan.md"


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ('(symbol (property "Reference" "U1"))', True),
        ('(text "parenthesis ) inside a string")', True),
        ("(symbol", False),
        ("(not_kicad)", False),
        ("plain text", False),
    ],
)
def test_kicad_snippet_validation(value, valid) -> None:
    assert looks_like_kicad_snippet(value) is valid


def test_path_first_attachments_are_workspace_scoped(tmp_path) -> None:
    image = tmp_path / "image.png"
    image.write_bytes(b"png")
    attachment = path_first_attachment(image, tmp_path)
    assert attachment == {"type": "file", "path": "image.png", "name": "image.png", "size": 3}
    with pytest.raises(ValueError):
        path_first_attachment(tmp_path.parent / "outside.png", tmp_path)


def test_runtime_context_contains_valid_snippet() -> None:
    snippet = "(wire (pts (xy 0 0) (xy 1 1)))"
    assert encode_runtime_context(snippet=snippet)["kicad_snippet"]["content"] == snippet
