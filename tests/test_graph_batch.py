"""Transcript bookkeeping for parallel tool batches."""

from __future__ import annotations

from langchain_core.messages import AIMessage, ToolMessage

from copper_pilot_cli.copper_graph import _TurnRun
from copper_pilot_cli.copper_protocol import CopperEvent, EventKind, ToolRequest


def request_event(call_id: str) -> CopperEvent:
    return CopperEvent(
        EventKind.TOOL_REQUEST,
        ToolRequest(tool_call_id=call_id, tool_name="read_file", arguments={"file_path": call_id}),
    )


def result_event(call_id: str) -> CopperEvent:
    return CopperEvent(
        EventKind.TOOL_RESULT,
        {"tool_call_id": call_id, "tool_name": "read_file", "result": {"result": call_id}},
    )


def run() -> _TurnRun:
    return _TurnRun(agent=object(), writer=lambda _event: None, original=[])  # type: ignore[arg-type]


def test_batch_calls_share_one_assistant_message() -> None:
    turn = run()
    turn._track(CopperEvent(EventKind.TEXT, "Reading both. "))
    for event in (request_event("a"), request_event("b"), result_event("b"), result_event("a")):
        turn._track(event)
    assert len(turn.additions) == 3
    message = turn.additions[0]
    assert isinstance(message, AIMessage)
    assert message.content == "Reading both. "
    assert [call["id"] for call in message.tool_calls] == ["a", "b"]
    assert [m.tool_call_id for m in turn.additions[1:] if isinstance(m, ToolMessage)] == ["b", "a"]
    assert turn.pending_tools == {}


def test_serial_calls_keep_one_message_per_call() -> None:
    turn = run()
    for event in (request_event("a"), result_event("a"), request_event("b"), result_event("b")):
        turn._track(event)
    assert [type(message).__name__ for message in turn.additions] == [
        "AIMessage",
        "ToolMessage",
        "AIMessage",
        "ToolMessage",
    ]
