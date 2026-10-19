from __future__ import annotations

from dataclasses import dataclass

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from copper_pilot_cli.copper_graph import create_copper_graph
from copper_pilot_cli.copper_protocol import CopperEffort, CopperEvent, CopperMode, EventKind
from copper_pilot_cli.copper_status_events import COPPER_CONTEXT_KEY
from copper_pilot_cli.sessions import list_threads


@dataclass
class FakeAgent:
    mode: CopperMode = CopperMode.AGENT
    effort: CopperEffort = CopperEffort.BALANCED
    model: str | None = None

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
                {
                    "messages": [HumanMessage(content="Review it")],
                    "copper_mode": "ask",
                    "copper_effort": "intelligence",
                    "copper_model": "GPT-6.1-Sol",
                },
                config,
            )
        ]
        state = await runtime.aget_state(config)
        assert isinstance(state.values["messages"][-1], AIMessage)
        assert state.values["messages"][-1].content == "Board looks good."
        assert state.values["copper_mode"] == "ask"
        assert state.values["copper_effort"] == "intelligence"
        assert state.values["copper_model"] == "gpt-6.1-sol"
        assert runtime.agent.model == "gpt-6.1-sol"
        assert any(item[1] == "custom" for item in streamed)
        # A follow-up turn without a model keeps the thread's stored choice.
        async for _ in runtime.astream(
            {"messages": [HumanMessage(content="And the PCB?")]}, config
        ):
            pass
        state = await runtime.aget_state(config)
        assert state.values["copper_model"] == "gpt-6.1-sol"
        # An explicit empty model clears it.
        async for _ in runtime.astream(
            {"messages": [HumanMessage(content="Again")], "copper_model": ""}, config
        ):
            pass
        state = await runtime.aget_state(config)
        assert state.values["copper_model"] == ""
        assert runtime.agent.model is None

    rows = await list_threads(cwd=str(workspace), include_message_count=True)
    assert rows[0]["thread_id"] == "thread-1"
    assert rows[0]["message_count"] == 6


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


@pytest.mark.asyncio
async def test_graph_persists_hidden_subagent_context_and_plan_handoff(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("COPPER_PILOT_CLI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("copper_pilot_cli.sessions._db_path", None)
    workspace = tmp_path / "project"
    workspace.mkdir()

    @dataclass
    class IntelligenceAgent(FakeAgent):
        async def astream_events_raw(self, _input):
            yield CopperEvent(
                EventKind.STATUS_EVENT,
                {
                    "kind": "subagent",
                    "id": "worker",
                    "task": "Audit power",
                    "status": "running",
                },
            )
            yield CopperEvent(
                EventKind.STATUS_EVENT,
                {"kind": "subagent", "id": "worker", "status": "keepalive"},
            )
            yield CopperEvent(
                EventKind.STATUS_EVENT,
                {
                    "kind": "subagent",
                    "id": "worker",
                    "status": "success",
                    "output": "Evidence",
                },
            )
            yield CopperEvent(EventKind.STATUS_EVENT, {"kind": "plan_mode_switch"})
            yield CopperEvent(EventKind.TEXT, "Parent synthesis")
            yield CopperEvent(EventKind.FINAL, {"success": True, "is_final": True})

    async with create_copper_graph(IntelligenceAgent()) as runtime:
        config = runtime.config("intelligence-thread", workspace)
        _ = [
            item
            async for item in runtime.astream(
                {
                    "messages": [HumanMessage(content="Plan it")],
                    "copper_mode": "agent",
                    "copper_effort": "intelligence",
                },
                config,
            )
        ]
        state = await runtime.aget_state(config)

    response = state.values["messages"][-1]
    assert response.content == "Parent synthesis"
    assert response.additional_kwargs[COPPER_CONTEXT_KEY] == (
        "\n[Subagent: Audit power]\nEvidence\n\nSwitched to Plan mode"
    )
    assert state.values["copper_mode"] == "plan"
    assert state.values["copper_effort"] == "intelligence"
