"""Bounded chat storage: compact ``copper_events``, pruned checkpoints, ``compact-db``."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from copper_pilot_cli.chat_context import (
    COMPACTION_EVENT,
    build_session_state,
    compaction_event,
    latest_todos,
)
from copper_pilot_cli.checkpoint_events import compact_events, events_update
from copper_pilot_cli.copper_graph import (
    CAPACITY_WAIT_EVENT,
    RESUME_EVENT,
    RETRY_EVENT,
)


@pytest.fixture
def home(monkeypatch, tmp_path) -> Path:
    monkeypatch.setenv("COPPER_PILOT_CLI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("copper_pilot_cli.sessions._db_path", None)
    workspace = tmp_path / "project"
    workspace.mkdir()
    return workspace


def stamped(kind: str, data: Any, turn: str, sequence: int | None, attempt: int = 0, **extra):
    return {
        "type": f"copper.{kind}",
        "data": data,
        "title": extra.pop("title", None),
        "turn_id": turn,
        "sequence": sequence,
        "resume_attempt": attempt,
        "root_turn_id": turn,
        "turn_attempt": 1,
        "ts": 1_000.0 + (sequence or 0),
        **extra,
    }


def recorded_thread() -> list[Any]:
    """A raw event log as threads stored it before the compact form."""
    events: list[Any] = []
    seq = 0

    def add(kind: str, data: Any, turn: str = "t1", attempt: int = 0, **extra) -> None:
        nonlocal seq
        seq += 1
        events.append(stamped(kind, data, turn, seq, attempt, **extra))

    for index in range(5):
        add("reasoning", f"Checking rail {index}. ")
    add("status_event", {"kind": "context_usage", "context_tokens": 4_000, "context_limit": 10**6})
    for index in range(4):
        add("text", f"chunk {index} ")
    add(
        "tool_request",
        {"tool_call_id": "c1", "tool_name": "read_file", "arguments": {"file_path": "a.sch"}},
    )
    add("tool_result", {"tool_call_id": "c1", "tool_name": "read_file", "result": "x" * 500})
    add("reasoning", "More thought. ")
    add("reasoning", "And more. ")
    add("status_event", {"kind": "subagent", "id": "w1", "task": "ERC", "status": "running"})
    add("reasoning", "Between workers. ")
    add(
        "status_event",
        {"kind": "subagent", "id": "w1", "task": "ERC", "status": "success", "output": "clean"},
    )
    add("status_event", {"kind": "todo_list", "items": [{"content": "Route", "status": "pending"}]})
    add("status", "Routing")
    events.append({"type": RETRY_EVENT, "data": {"stage": "scheduled"}, "title": "Retrying"})
    # A resumed attempt re-sends what the first one streamed: sequences start over.
    seq = 0
    add("reasoning", "Resent thought. ", attempt=1)
    add("reasoning", "Resent more. ", attempt=1)
    seq = 1  # a duplicate frame: the TUI drops it, so it must not be joined
    add("reasoning", "Duplicate. ", attempt=1)
    add("reasoning", "Fresh again. ", attempt=1)
    add("status_event", {"kind": "plan_mode_switch"}, attempt=1)
    add(
        "status_event",
        {"kind": "todo_list", "items": [{"content": "Route", "status": "completed"}]},
    )
    add("final", {"success": True, "is_final": True, "context_usage": {"context_tokens": 9_000}})
    events.append(None)
    events.append(compaction_event("completed", trigger="manual", record={"post_tokens": 1_200}))
    seq = 0
    add("reasoning", "Second turn. ", turn="t2")
    add("reasoning", "Still second. ", turn="t2")
    add("text", "answer ", turn="t2")
    add("status_event", {"kind": "model_fallback", "to_model": "gemini-x"}, turn="t2")
    add("error", {"message": "Hosted turn failed.", "type": "server"}, turn="t2")
    events.append({"type": RESUME_EVENT, "data": {"will_retry": True}, "title": "Resuming"})
    events.append({"type": CAPACITY_WAIT_EVENT, "data": {"phase": "done"}, "title": "Wait"})
    return events


def test_compact_events_drops_replayed_frames_and_joins_reasoning() -> None:
    raw = recorded_thread()
    compact = compact_events(raw)

    types = [event["type"] for event in compact]
    assert not {"copper.text", "copper.tool_request", "copper.tool_result"} & set(types)
    reasoning = [event["data"] for event in compact if event["type"] == "copper.reasoning"]
    assert reasoning == [
        "Checking rail 0. Checking rail 1. Checking rail 2. Checking rail 3. Checking rail 4. ",
        # Text and tool frames between chunks are not replayed, so the chunks join.
        "More thought. And more. ",
        "Between workers. ",
        # A new resume attempt starts a new run; the duplicate frame stays apart.
        "Resent thought. Resent more. ",
        "Duplicate. ",
        "Fresh again. ",
        "Second turn. Still second. ",
    ]
    joined = next(event for event in compact if event["data"] == "More thought. And more. ")
    assert joined["sequence"] == 14 and joined["ts"] == 1_014.0
    assert compact_events(compact) == compact
    # Every other frame is kept as it was.
    others = [e for e in raw if isinstance(e, dict) and e["type"] != "copper.reasoning"]
    others = [e for e in others if e["type"] not in {"copper.text", "copper.tool_request"}]
    others = [e for e in others if e["type"] != "copper.tool_result"]
    assert [e for e in compact if e["type"] != "copper.reasoning"] == others
    assert len(compact) < len(raw)


def test_compact_events_keeps_the_session_state_inputs() -> None:
    raw = recorded_thread()
    compact = compact_events(raw)
    assert latest_todos(compact) == latest_todos(raw)
    values = {"messages": [HumanMessage(content="Route it", id="h1")]}
    assert build_session_state(
        {**values, "copper_events": compact}, workspace=None
    ) == build_session_state({**values, "copper_events": raw}, workspace=None)


def test_events_update_appends_a_compact_tail_or_rewrites_a_legacy_log_once() -> None:
    raw = recorded_thread()
    compact = compact_events(raw)
    tail = [stamped("status", "Next turn", "t3", 1), stamped("text", "hi", "t3", 2)]

    assert events_update(compact, tail) == [tail[0]]
    rewritten = events_update(raw, tail)
    assert type(rewritten).__name__ == "Overwrite"
    assert rewritten.value == [*compact, tail[0]]
    assert events_update(None, tail) == [tail[0]]


# ------------------------------------------------------------- TUI replay


def _transcript(app: Any) -> list[tuple[Any, ...]]:
    rows = []
    for widget in app.query_one("#transcript").walk_children():
        content = getattr(widget, "content", None)
        rows.append(
            (
                type(widget).__name__,
                content if isinstance(content, str) else None,
                getattr(widget, "status", None),
                str(getattr(widget, "output", None)),
                str(getattr(widget, "args", None)),
            )
        )
    return rows


async def _replay(tmp_path: Path, events: list[Any]) -> dict[str, Any]:
    from test_tui import FakeRuntime

    from copper_pilot_cli.copper_app import CopperPilotApp
    from copper_pilot_cli.copper_tools import LocalToolBroker

    runtime = FakeRuntime()
    messages = [
        HumanMessage(content="Check the rails"),
        AIMessage(
            content="Reading it",
            tool_calls=[{"id": "c1", "name": "read_file", "args": {"file_path": "a.sch"}}],
        ),
        ToolMessage(content="x" * 500, tool_call_id="c1"),
        AIMessage(content="chunk 0 chunk 1 chunk 2 chunk 3 "),
        HumanMessage(content="And now?"),
        AIMessage(content="answer "),
    ]

    async def state(_config):
        return SimpleNamespace(values={"messages": messages, "copper_events": events})

    runtime.aget_state = state
    app = CopperPilotApp(runtime, LocalToolBroker(tmp_path), tmp_path, "thread")
    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        return {
            "transcript": _transcript(app),
            "turn_state": asdict(app._turn_state),
            "mode": app.copper_mode,
            "compaction": app._compaction,
            "notifications": [note.message for note in app._notifications],
        }


@pytest.mark.asyncio
async def test_tui_replay_of_the_compact_log_matches_the_raw_log(tmp_path) -> None:
    raw = recorded_thread()
    before = await _replay(tmp_path, raw)
    after = await _replay(tmp_path, compact_events(raw))
    assert after == before
    names = [row[0] for row in before["transcript"]]
    assert "ReasoningMessage" in names and "ErrorMessage" in names


def test_compaction_event_type_is_kept() -> None:
    event = compaction_event("completed", trigger="manual", record={"post_tokens": 1})
    assert compact_events([event]) == [event] and event["type"] == COMPACTION_EVENT
