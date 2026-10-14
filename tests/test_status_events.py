from __future__ import annotations

from copper_pilot_cli.copper_status_events import (
    SUBAGENT_TRANSCRIPT_CHAR_CAP,
    SubagentStatus,
    format_subagent_transcript,
    normalize_subagent_status,
    subagent_progress_label,
)


def test_subagent_lifecycle_ignores_keepalive_and_merges_by_id() -> None:
    running = normalize_subagent_status(
        {"kind": "subagent", "id": "one", "task": "Audit power", "status": "running"}
    )
    assert running == SubagentStatus(id="one", task="Audit power", status="running")
    assert (
        normalize_subagent_status(
            {"kind": "subagent", "id": "one", "status": "keepalive"},
            running,
        )
        is None
    )
    completed = normalize_subagent_status(
        {"kind": "subagent", "id": "one", "status": "success", "output": "All good"},
        running,
    )
    assert completed is not None
    assert completed.task == "Audit power"
    assert completed.output == "All good"
    assert subagent_progress_label(completed) == "Subagent: Audit power — Done"


def test_subagent_transcript_matches_desktop_contract() -> None:
    assert format_subagent_transcript(SubagentStatus("one", "Audit", status="running")) == ""
    assert (
        format_subagent_transcript(
            SubagentStatus("one", "Audit", status="failed", error="No board")
        )
        == "\n[Subagent: Audit]\nSubagent failed: No board\n"
    )
    block = format_subagent_transcript(
        SubagentStatus(
            "one",
            "Audit",
            status="success",
            output="x" * (SUBAGENT_TRANSCRIPT_CHAR_CAP + 50),
        )
    )
    assert block.count("x") == SUBAGENT_TRANSCRIPT_CHAR_CAP
    assert "[truncated; see notes under .copperpilot/]" in block
