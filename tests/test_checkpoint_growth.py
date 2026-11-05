"""Bounded chat storage: compact ``copper_events``, pruned checkpoints, ``compact-db``."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import namedtuple
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import copper_pilot_cli.copper_graph as copper_graph_module
import copper_pilot_cli.sessions as sessions_module
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
    create_copper_graph,
)
from copper_pilot_cli.copper_protocol import (
    CopperEffort,
    CopperEvent,
    CopperMode,
    EventKind,
    ToolRequest,
)
from copper_pilot_cli.sessions import (
    KEEP_CHECKPOINTS,
    NotEnoughSpaceError,
    SessionsBusyError,
    compact_database,
    exclusive_sessions,
    get_db_path,
    list_threads,
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


# ------------------------------------------------------- graph and storage


@dataclass
class TurnAgent:
    mode: CopperMode = CopperMode.AGENT
    effort: CopperEffort = CopperEffort.BALANCED
    model: str | None = None
    turn: int = 0

    async def astream_events_raw(self, _payload):
        turn = f"turn-{self.turn}"
        seq = 0

        def event(kind: EventKind, data: Any) -> CopperEvent:
            nonlocal seq
            seq += 1
            return CopperEvent(kind, data, turn_id=turn, sequence=seq, resume_attempt=0)

        for index in range(10):
            yield event(EventKind.REASONING, f"think {index} ")
        call = f"call-{self.turn}"
        yield event(
            EventKind.TOOL_REQUEST,
            ToolRequest(tool_call_id=call, tool_name="read_file", arguments={"file_path": "a"}),
        )
        yield event(
            EventKind.TOOL_RESULT,
            {"tool_call_id": call, "tool_name": "read_file", "result": "y" * 2_000},
        )
        yield event(
            EventKind.STATUS_EVENT,
            {"kind": "todo_list", "items": [{"content": f"Step {self.turn}", "status": "pending"}]},
        )
        for index in range(30):
            yield event(EventKind.TEXT, f"word{index} ")
        yield event(EventKind.FINAL, {"success": True, "is_final": True})

    async def acancel(self):
        return None


async def _run_turns(workspace: Path, agent: TurnAgent, turns: range, thread: str = "t") -> Any:
    async with create_copper_graph(agent) as runtime:
        config = runtime.config(thread, workspace)
        for turn in turns:
            agent.turn = turn
            async for _ in runtime.astream(
                {"messages": [HumanMessage(content=f"Question {turn}")]}, config
            ):
                pass
        return await runtime.aget_state(config)


def _rows(table: str, thread: str = "t") -> int:
    with sqlite3.connect(get_db_path()) as conn:
        query = f"SELECT count(*) FROM {table} WHERE thread_id = ?"  # noqa: S608
        return conn.execute(query, (thread,)).fetchone()[0]


@pytest.mark.asyncio
async def test_turns_keep_a_bounded_checkpoint_history_and_a_compact_log(home) -> None:
    state = await _run_turns(home, TurnAgent(), range(8))

    assert _rows("checkpoints") == KEEP_CHECKPOINTS + 1
    messages = state.values["messages"]
    assert [m.content for m in messages if isinstance(m, HumanMessage)] == [
        f"Question {turn}" for turn in range(8)
    ]
    assert len(messages) == 8 * 4
    events = state.values["copper_events"]
    assert [event["type"] for event in events] == [
        "copper.reasoning",
        "copper.status_event",
        "copper.final",
    ] * 8
    assert events[0]["data"] == "".join(f"think {index} " for index in range(10))
    assert latest_todos(events) == {"items": [{"content": "Step 7", "status": "pending"}]}

    rows = await list_threads(cwd=str(home), include_message_count=True, limit=5)
    sessions_module._message_count_cache.clear()
    await sessions_module.populate_thread_checkpoint_details(
        rows, include_message_count=True, include_initial_prompt=True
    )
    assert rows[0]["message_count"] == 32
    assert rows[0]["initial_prompt"] == "Question 0"

    # A reopened runtime continues the thread from the kept checkpoints.
    state = await _run_turns(home, TurnAgent(), range(8, 9))
    assert len(state.values["messages"]) == 9 * 4


@pytest.mark.asyncio
async def test_a_legacy_thread_is_rewritten_on_its_next_turn(home, monkeypatch) -> None:
    def append_raw(_stored, new):
        return new

    async def keep_all(_saver, _thread_id):
        return 0

    with monkeypatch.context() as patch:
        patch.setattr(copper_graph_module, "events_update", append_raw)
        patch.setattr(copper_graph_module, "prune_checkpoints", keep_all)
        legacy = await _run_turns(home, TurnAgent(), range(3))
    raw = legacy.values["copper_events"]
    assert any(event["type"] == "copper.text" for event in raw)
    assert _rows("checkpoints") == 9

    state = await _run_turns(home, TurnAgent(), range(3, 4))
    events = state.values["copper_events"]
    assert not any(event["type"] == "copper.text" for event in events)
    assert len(events) == 4 * 3
    assert events[:9] == compact_events(raw)
    # Reading it back from disk decodes the rewritten log too.
    state = await _run_turns(home, TurnAgent(), range(0))
    assert state.values["copper_events"] == events


async def _legacy_db(home: Path, monkeypatch, turns: int = 6) -> dict[str, Any]:
    def append_raw(_stored, new):
        return new

    async def keep_all(_saver, _thread_id):
        return 0

    with monkeypatch.context() as patch:
        patch.setattr(copper_graph_module, "events_update", append_raw)
        patch.setattr(copper_graph_module, "prune_checkpoints", keep_all)
        state = await _run_turns(home, TurnAgent(), range(turns))
        await _run_turns(home, TurnAgent(), range(2), thread="other")
    # A thread another agent wrote: compact-db must leave it alone.
    with sqlite3.connect(get_db_path()) as conn:
        conn.execute(
            "INSERT INTO checkpoints SELECT 'foreign', checkpoint_ns, checkpoint_id, "
            "parent_checkpoint_id, type, checkpoint, "
            "json_set(metadata, '$.agent_name', 'dcode') FROM checkpoints WHERE thread_id = 't'"
        )
    return dict(state.values)


@pytest.mark.asyncio
async def test_compact_db_prunes_rewrites_and_vacuums_a_legacy_database(home, monkeypatch) -> None:
    values = await _legacy_db(home, monkeypatch)
    before = sessions_module.db_file_size(get_db_path())

    dry = compact_database(dry_run=True)
    assert dry.checkpoints_deleted == (18 - 4) + (6 - 4)
    assert sessions_module.db_file_size(get_db_path()) == before
    assert _rows("checkpoints") == 18

    with exclusive_sessions():
        report = compact_database()
    assert report.vacuumed and report.threads == 2
    assert report.size_after < before
    assert _rows("checkpoints") == KEEP_CHECKPOINTS + 1
    assert _rows("checkpoints", "foreign") == 18
    with sqlite3.connect(get_db_path()) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    async with create_copper_graph(TurnAgent()) as runtime:
        state = await runtime.aget_state(runtime.config("t", home))
    assert state.values["messages"] == values["messages"]
    assert state.values["copper_events"] == compact_events(values["copper_events"])
    rows = await list_threads(cwd=str(home), limit=5)
    await sessions_module.populate_thread_checkpoint_details(
        rows, include_message_count=True, include_initial_prompt=True
    )
    by_id = {row["thread_id"]: row for row in rows}
    assert by_id["t"]["initial_prompt"] == "Question 0"
    assert by_id["t"]["message_count"] == 24

    # Running it again finds nothing left to do.
    again = compact_database()
    assert (again.checkpoints_deleted, again.rows_rewritten) == (0, 0)


@pytest.mark.asyncio
async def test_compact_db_refuses_to_vacuum_without_free_space(home, monkeypatch) -> None:
    await _legacy_db(home, monkeypatch)
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr("shutil.disk_usage", lambda _path: usage(10**9, 10**9, 1024))

    with pytest.raises(NotEnoughSpaceError, match="Not enough free disk space"):
        compact_database()
    # The prune already ran; the database is intact, only not shrunk.
    assert _rows("checkpoints") == KEEP_CHECKPOINTS + 1
    with sqlite3.connect(get_db_path()) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.skipif(sys.platform == "win32", reason="open chats are detected on POSIX only")
@pytest.mark.asyncio
async def test_compact_db_never_runs_while_a_chat_is_open(home, capsys) -> None:
    from copper_pilot_cli.copper_main import _threads_command

    await _run_turns(home, TurnAgent(), range(1))
    args = argparse.Namespace(thread_action="compact-db", dry_run=False, yes=True)
    async with create_copper_graph(TurnAgent()):
        with pytest.raises(SessionsBusyError), exclusive_sessions():
            pass
        assert await _threads_command(args, home) == 1
    assert "A CopperPilot chat is open" in capsys.readouterr().out

    assert await _threads_command(args, home) == 0
    assert "Deleted 0 old checkpoints" in capsys.readouterr().out


def test_compact_db_command_is_registered() -> None:
    from copper_pilot_cli.copper_main import command_parser

    args = command_parser("threads").parse_args(["compact-db", "--dry-run"])
    assert args.thread_action == "compact-db" and args.dry_run and not args.yes


def test_compaction_event_type_is_kept() -> None:
    event = compaction_event("completed", trigger="manual", record={"post_tokens": 1})
    assert compact_events([event]) == [event] and event["type"] == COMPACTION_EVENT
