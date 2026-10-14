from __future__ import annotations

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.runnables import Runnable

from copper_pilot_cli.copper_auth import DeviceCredential
from copper_pilot_cli.copper_protocol import CopperEffort, CopperEvent, EventKind
from copper_pilot_cli.copper_status_events import COPPER_CONTEXT_KEY
from copper_pilot_cli.langchain import CopperPilotAgent


class FakeClient:
    async def stream(self, request, tool_handler):
        assert request.query == "Review"
        assert request.workspace_path
        assert request.effort is CopperEffort.BALANCED
        yield CopperEvent(EventKind.REASONING, "Inspecting")
        yield CopperEvent(EventKind.TEXT, "Looks ")
        yield CopperEvent(EventKind.TEXT, "good.")
        yield CopperEvent(EventKind.FINAL, {"is_final": True})

    async def cancel(self):
        return None


def agent(tmp_path) -> CopperPilotAgent:
    credential = DeviceCredential(
        api_key="cf_live_test",
        fingerprint="fingerprint",
        base_url="https://example.test",
    )
    result = CopperPilotAgent(tmp_path, credential=credential)
    result._client = FakeClient()
    return result


def test_agent_is_a_runnable_and_invokes(tmp_path) -> None:
    runnable = agent(tmp_path)
    assert isinstance(runnable, Runnable)
    result = runnable.invoke({"messages": [HumanMessage(content="Review")]})
    assert result["messages"][-1].content == "Looks good."


def test_stream_yields_langchain_chunks(tmp_path) -> None:
    chunks = list(agent(tmp_path).stream({"messages": [HumanMessage(content="Review")]}))
    assert all(isinstance(chunk, AIMessageChunk) for chunk in chunks)
    assert "".join(str(chunk.content) for chunk in chunks) == "Looks good."


def test_explicit_graph_and_compiled_subagent_adapters(tmp_path) -> None:
    runnable = agent(tmp_path)
    graph = runnable.as_langgraph()
    assert isinstance(graph, Runnable)
    subagent = runnable.as_compiled_subagent("pcb-review", "Review a PCB")
    assert subagent["name"] == "pcb-review"
    assert subagent["description"] == "Review a PCB"
    assert isinstance(subagent["runnable"], Runnable)


def test_hidden_status_transcript_is_sent_without_changing_visible_content(tmp_path) -> None:
    runnable = agent(tmp_path)
    prior = AIMessage(
        content="Parent synthesis",
        additional_kwargs={
            COPPER_CONTEXT_KEY: "[Subagent: Audit]\nEvidence\n\nSwitched to Plan mode"
        },
    )
    request = runnable._request(
        [HumanMessage(content="First"), prior, HumanMessage(content="Next")]
    )
    assert prior.content == "Parent synthesis"
    assert request.context[-1].content == (
        "Parent synthesis\n[Subagent: Audit]\nEvidence\n\nSwitched to Plan mode"
    )
