"""Subagent statuses in the CLI turn journal."""

from copper_pilot_cli.turn_journal import TurnJournal


def test_subagent_statuses_are_journaled_and_sent_on_resume():
    journal = TurnJournal(query="q", original_len=1)

    def chip(unit, status, state=None):
        return {
            "kind": "subagent",
            "id": unit,
            "unit": unit,
            "status": status,
            "state": state or status,
            "scratch_path": f".copperpilot/scratch/{unit}",
        }

    journal.record_subagent(chip("usb", "running", "queued"))
    journal.record_subagent(chip("hdmi", "running"))
    journal.record_subagent(chip("usb", "success"))
    journal.record_subagent({"kind": "subagent", "status": "keepalive"})
    journal.record_subagent(chip("hdmi", "keepalive"))
    journal.record_subagent({"kind": "capacity_wait", "unit": "x", "status": "waiting"})
    assert journal.subagents == [
        {"unit": "usb", "status": "success", "scratch_path": ".copperpilot/scratch/usb"},
        {"unit": "hdmi", "status": "running", "scratch_path": ".copperpilot/scratch/hdmi"},
    ]
    journal.begin_resume("connection_lost")
    assert journal.resume_payload()["subagents"] == journal.subagents
    restored = TurnJournal.from_dict(journal.to_dict())
    assert restored is not None and restored.subagents == journal.subagents
    assert "subagents" not in TurnJournal(query="q", original_len=1).resume_payload()
