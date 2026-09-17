"""Local checkpointed graph that projects the hosted agent into dcode sessions."""

from __future__ import annotations

import json
import operator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from copper_pilot_cli.copper_protocol import EventKind
from copper_pilot_cli.langchain import CopperPilotAgent
from copper_pilot_cli.sessions import get_checkpointer


class CopperState(TypedDict, total=False):
    messages: Annotated[list[Any], add_messages]
    copper_mode: str
    copper_events: Annotated[list[dict[str, Any]], operator.add]


class CopperGraphRuntime:
    """Own one compiled transport graph and its SQLite checkpointer."""

    def __init__(
        self,
        graph: Any,
        checkpointer_context: Any,
        agent: CopperPilotAgent,
    ) -> None:
        self.graph = graph
        self._checkpointer_context = checkpointer_context
        self.agent = agent

    async def aclose(self) -> None:
        await self._checkpointer_context.__aexit__(None, None, None)

    def config(self, thread_id: str, workspace: Path) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": thread_id},
            "metadata": {
                "agent_name": "copper-pilot",
                "cwd": str(workspace.resolve()),
            },
        }

    async def astream(self, values: dict[str, Any], config: dict[str, Any]) -> Any:
        async for item in self.graph.astream(
            values,
            config=config,
            stream_mode=["messages", "updates", "custom"],
            subgraphs=True,
        ):
            yield item

    async def aget_state(self, config: dict[str, Any]) -> Any:
        return await self.graph.aget_state(config)

    async def aupdate_state(
        self,
        config: dict[str, Any],
        values: dict[str, Any] | None,
        *,
        as_node: str | None = None,
    ) -> Any:
        return await self.graph.aupdate_state(config, values, as_node=as_node)

    async def acancel(self) -> None:
        await self.agent.acancel()


@asynccontextmanager
async def create_copper_graph(agent: CopperPilotAgent) -> Any:
    """Compile a one-node, no-local-model graph using dcode's checkpointer."""
    checkpointer_context = get_checkpointer()
    checkpointer = await checkpointer_context.__aenter__()

    async def hosted_node(state: CopperState) -> dict[str, Any]:
        writer = get_stream_writer()
        additions: list[Any] = []
        recorded_events: list[dict[str, Any]] = []
        text: list[str] = []
        pending_tools: dict[str, str] = {}
        async for event in agent.astream_events_raw({"messages": state["messages"]}):
            projected = {
                "type": f"copper.{event.kind.value}",
                "data": event.data.model_dump(mode="json")
                if hasattr(event.data, "model_dump")
                else event.data,
                "title": event.title,
                "turn_id": event.turn_id,
                "sequence": event.sequence,
                "resume_attempt": event.resume_attempt,
            }
            writer(projected)
            recorded_events.append(projected)
            if event.kind is EventKind.TEXT:
                text.append(str(event.data))
            elif event.kind is EventKind.TOOL_REQUEST:
                tool = event.data
                additions.append(
                    AIMessage(
                        content="".join(text),
                        tool_calls=[
                            {
                                "id": tool.tool_call_id,
                                "name": tool.tool_name,
                                "args": tool.arguments,
                                "type": "tool_call",
                            }
                        ],
                    )
                )
                text.clear()
                pending_tools[tool.tool_call_id] = tool.tool_name
            elif event.kind is EventKind.TOOL_RESULT:
                payload = event.data
                result = payload["result"]
                is_error = isinstance(result, dict) and (
                    bool(result.get("error"))
                    or (isinstance(result.get("exit_code"), int) and result["exit_code"] != 0)
                )
                rejected = isinstance(result, dict) and result.get("rejected") is True
                if isinstance(result, dict):
                    content = result.get("error") or result.get("result", result)
                    artifact = {
                        key: result[key] for key in ("exit_code", "truncated") if key in result
                    }
                else:
                    content = result
                    artifact = {}
                additions.append(
                    ToolMessage(
                        content=(
                            content
                            if isinstance(content, (str, list))
                            else json.dumps(content, default=str)
                        ),
                        tool_call_id=payload["tool_call_id"],
                        name=payload["tool_name"],
                        status="error" if is_error else "success",
                        artifact=artifact or None,
                        additional_kwargs={"copper_rejected": rejected},
                    )
                )
                pending_tools.pop(payload["tool_call_id"], None)
        for tool_id, tool_name in pending_tools.items():
            additions.append(
                ToolMessage(
                    content="Tool stream ended before a result was returned.",
                    tool_call_id=tool_id,
                    name=tool_name,
                    status="error",
                )
            )
        if text or not additions:
            additions.append(AIMessage(content="".join(text)))
        return {
            "messages": additions,
            "copper_mode": state.get("copper_mode", agent.mode.value),
            "copper_events": recorded_events,
        }

    graph = StateGraph(CopperState)  # ty: ignore[invalid-argument-type]
    graph.add_node("copper_pilot", hosted_node)
    graph.add_edge(START, "copper_pilot")
    graph.add_edge("copper_pilot", END)
    compiled = graph.compile(checkpointer=checkpointer)
    runtime = CopperGraphRuntime(compiled, checkpointer_context, agent)
    try:
        yield runtime
    finally:
        await runtime.aclose()
