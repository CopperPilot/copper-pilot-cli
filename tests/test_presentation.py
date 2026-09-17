from __future__ import annotations

from copper_pilot_cli.copper_presentation import TurnEvent, TurnPhase, TurnState


def test_turn_state_deduplicates_by_attempt_and_sequence() -> None:
    state = TurnState()
    assert state.apply(TurnEvent("turn_started", turn_id="turn", resume_attempt=0, sequence=1))
    assert state.phase is TurnPhase.RUNNING
    assert not state.apply(TurnEvent("status", "duplicate", turn_id="turn", sequence=1))

    assert state.apply(TurnEvent("status", "resumed", turn_id="turn", resume_attempt=1, sequence=1))
    assert state.status == "resumed"
    assert not state.apply(
        TurnEvent("status", "stale", turn_id="turn", resume_attempt=0, sequence=20)
    )


def test_turn_state_normalizes_hosted_usage_names_without_inventing_context() -> None:
    state = TurnState()
    state.apply(
        TurnEvent(
            "usage",
            {"tokens_input": 120, "tokens_output": 24, "cache_tokens": 8},
        )
    )
    assert state.input_tokens == 120
    assert state.output_tokens == 24
    assert state.context_tokens is None
    assert state.context_limit is None
