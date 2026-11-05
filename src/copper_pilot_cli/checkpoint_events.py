"""The compact form of ``copper_events``, the thread's replay log in the checkpoint.

Every checkpoint row stores the whole graph state, so whatever this list keeps
is written again on every later turn. It used to keep every streamed frame,
one per text chunk. It now keeps only what reading a chat back needs:

* the TUI replays every event except text and tool frames when it reopens a
  chat (``CopperPilotApp._hydrate``); those frames are already in ``messages``;
* ``latest_todos`` (``chat_context``) reads the newest ``todo_list`` status event.

ACP ``session/load``, headless and MCP read ``messages`` only; the turn journal
and ``/resume`` live outside this list.

So text and tool frames are dropped, and consecutive reasoning chunks are
joined into one event. A join happens only where the TUI's ``TurnState`` would
accept every chunk (it drops repeated or older sequence numbers); the joined
event carries the last chunk's sequence number, so replay renders the same
text and leaves the turn state exactly as the chunks did. Every other event is
kept unchanged.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from langgraph.types import Overwrite

# The frames the TUI skips when it replays a stored chat (also in ``messages``).
REPLAY_SKIPPED_TYPES = frozenset({"copper.text", "copper.tool_request", "copper.tool_result"})
REASONING_TYPE = "copper.reasoning"


def _gate(event: dict[str, Any]) -> tuple[str | None, int | None, int | None]:
    """``(turn_id, sequence, resume_attempt)`` as ``CopperPilotApp._custom_event`` reads them."""
    sequence = event.get("sequence")
    attempt = event.get("resume_attempt")
    return (
        str(event.get("turn_id")) if event.get("turn_id") else None,
        sequence if isinstance(sequence, int) else None,
        attempt if isinstance(attempt, int) else None,
    )


def _join(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    joined = {**first, "data": f"{first.get('data')}{second.get('data')}"}
    if isinstance(second.get("sequence"), int):
        joined["sequence"] = second["sequence"]
    if "ts" in second:
        joined["ts"] = second["ts"]
    return joined


def compact_events(events: Iterable[Any]) -> list[dict[str, Any]]:
    """``events`` in compact form; replaying it renders the same transcript."""
    # Lazy: the presenter module pulls in Textual, which headless runs never load.
    from copper_pilot_cli.copper_presentation import TurnEvent, TurnState

    state = TurnState()
    compacted: list[dict[str, Any]] = []
    # Whether ``compacted[-1]`` is an accepted reasoning event a chunk may join.
    joinable = False
    for event in events:
        if not isinstance(event, dict) or str(event.get("type")) in REPLAY_SKIPPED_TYPES:
            continue
        turn_id, sequence, attempt = _gate(event)
        accepted = state.apply(
            TurnEvent("replay", turn_id=turn_id, sequence=sequence, resume_attempt=attempt)
        )
        reasoning = event.get("type") == REASONING_TYPE
        if accepted and reasoning and joinable and _gate(compacted[-1])[::2] == (turn_id, attempt):
            compacted[-1] = _join(compacted[-1], event)
            continue
        compacted.append(event)
        joinable = accepted and reasoning
    return compacted


def events_update(stored: Any, new: list[dict[str, Any]]) -> list[dict[str, Any]] | Overwrite:
    """The ``copper_events`` write for a turn that recorded ``new``.

    Normally the compact tail to append. A thread stored before the compact
    form (or whose last reasoning chunk the new ones join) is rewritten whole
    instead, once.
    """
    stored = list(stored) if isinstance(stored, list) else []
    full = compact_events([*stored, *new])
    if full[: len(stored)] == stored:
        return full[len(stored) :]
    return Overwrite(full)
