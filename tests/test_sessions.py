from __future__ import annotations

from dataclasses import dataclass

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from copper_pilot_cli.copper_graph import create_copper_graph
from copper_pilot_cli.copper_protocol import CopperEvent, CopperMode, EventKind
from copper_pilot_cli.sessions import list_threads


@dataclass
class FakeAgent:
    mode: CopperMode = CopperMode.AGENT

    async def astream_events_raw(self, _input):
        yield CopperEvent(EventKind.REASONING, "Checking the rails")
        yield CopperEvent(EventKind.TEXT, "Board looks good.")
        yield CopperEvent(EventKind.FINAL, {"is_final": True})

    async def acancel(self):
        return None


@pytest.mark.asyncio
async def test_graph_checkpoints_langchain_messages_and_copper_events(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("COPPER_PILOT_CLI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("copper_pilot_cli.sessions._db_path", None)
    workspace = tmp_path / "project"
    workspace.mkdir()
    async with create_copper_graph(FakeAgent()) as runtime:
        config = runtime.config("thread-1", workspace)
        streamed = [
            item
            async for item in runtime.astream(
                {"messages": [HumanMessage(content="Review it")], "copper_mode": "ask"},
                config,
            )
        ]
        state = await runtime.aget_state(config)
        assert isinstance(state.values["messages"][-1], AIMessage)
        assert state.values["messages"][-1].content == "Board looks good."
        assert state.values["copper_mode"] == "ask"
        assert any(item[1] == "custom" for item in streamed)

    rows = await list_threads(cwd=str(workspace), include_message_count=True)
    assert rows[0]["thread_id"] == "thread-1"
    assert rows[0]["message_count"] == 2


@pytest.mark.asyncio
async def test_fresh_runtime_resumes_prior_messages_exactly_once(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("COPPER_PILOT_CLI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("copper_pilot_cli.sessions._db_path", None)
    workspace = tmp_path / "project"
    workspace.mkdir()

    first = FakeAgent()
    async with create_copper_graph(first) as runtime:
        config = runtime.config("resume-thread", workspace)
        _ = [
            item
            async for item in runtime.astream(
                {"messages": [HumanMessage(content="first turn")]},
                config,
            )
        ]

    @dataclass
    class CapturingAgent(FakeAgent):
        received: list | None = None

        async def astream_events_raw(self, input):
            self.received = list(input["messages"])
            yield CopperEvent(EventKind.TEXT, "second response")
            yield CopperEvent(EventKind.FINAL, {"success": True, "is_final": True})

    second = CapturingAgent()
    async with create_copper_graph(second) as runtime:
        config = runtime.config("resume-thread", workspace)
        _ = [
            item
            async for item in runtime.astream(
                {"messages": [HumanMessage(content="second turn")]},
                config,
            )
        ]

    assert second.received is not None
    assert [message.content for message in second.received] == [
        "first turn",
        "Board looks good.",
        "second turn",
    ]
