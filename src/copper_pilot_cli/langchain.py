"""LangChain and Deep Agents adapters for the hosted CopperPilot agent."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.runnables import Runnable, RunnableConfig

from copper_pilot_cli.chat_context import after_cut
from copper_pilot_cli.copper_auth import DeviceCredential, load_credential
from copper_pilot_cli.copper_models import request_model
from copper_pilot_cli.copper_protocol import (
    ChatMessage,
    ChatRequest,
    CopperEffort,
    CopperEvent,
    CopperMode,
    EventKind,
    HostedChatClient,
    PlanContext,
)
from copper_pilot_cli.copper_status_events import COPPER_CONTEXT_KEY
from copper_pilot_cli.copper_tools import LocalToolBroker
from copper_pilot_cli.copper_workspace import (
    build_initial_file_tree,
    canonical_workspace,
    discover_workspace,
)

AgentInput = Mapping[str, Any] | Sequence[BaseMessage]
AgentOutput = dict[str, Any]


def _messages(value: AgentInput) -> list[BaseMessage]:
    raw: Any = value.get("messages", []) if isinstance(value, Mapping) else value
    messages: list[BaseMessage] = []
    for item in raw:
        if isinstance(item, BaseMessage):
            messages.append(item)
        elif isinstance(item, Mapping):
            role = str(item.get("role") or item.get("type") or "")
            content = str(item.get("content") or "")
            message = (
                HumanMessage(content=content)
                if role in {"user", "human"}
                else AIMessage(content=content)
            )
            messages.append(message)
        else:
            raise TypeError(f"Unsupported message value: {type(item).__name__}")
    return messages


def _context(messages: Sequence[BaseMessage], cut: str | None = None) -> list[ChatMessage]:
    """Portable history for ``chat_start``: human and assistant prose before the query.

    With a compaction ``cut`` only the messages after it are sent; the summary
    travels in ``session_state``.
    """
    result: list[ChatMessage] = []
    for message in after_cut(messages[:-1], cut):
        role = "user" if message.type == "human" else "assistant"
        if message.type not in {"human", "ai"}:
            continue
        content = (
            message.content if isinstance(message.content, str) else json.dumps(message.content)
        )
        if message.type == "ai":
            hidden = message.additional_kwargs.get(COPPER_CONTEXT_KEY)
            if isinstance(hidden, str) and hidden:
                content = f"{content.rstrip()}\n{hidden}".strip()
            if not content.strip():
                # Tool-call-only assistant turns carry no text worth sending.
                continue
        result.append(ChatMessage(role=role, content=content))
    return result


class CopperPilotAgent(Runnable[AgentInput, Any]):
    """A hosted-agent Runnable; it is deliberately not a BaseChatModel."""

    def __init__(
        self,
        workspace: str | Path = ".",
        *,
        credential: DeviceCredential | None = None,
        conversation_id: str | None = None,
        mode: CopperMode = CopperMode.AGENT,
        effort: CopperEffort = CopperEffort.BALANCED,
        model: str | None = None,
        workflow: str | None = None,
        skill_manifest_id: str | None = None,
        plan_context: PlanContext | None = None,
        runtime_context: dict[str, Any] | None = None,
        tool_broker: LocalToolBroker | None = None,
        resume_policy: Any = None,
    ) -> None:
        self.workspace = canonical_workspace(workspace)
        # ``copper_graph.HeadlessResumePolicy`` for callers with no person to press
        # Resume (ACP, -n, MCP); None keeps the interactive budget.
        self.resume_policy = resume_policy
        self.context = discover_workspace(self.workspace)
        self.credential = credential or load_credential()
        if self.credential is None:
            raise RuntimeError("CopperPilot login is required. Run `copper-pilot auth login`.")
        self.conversation_id = conversation_id or str(uuid.uuid4())
        self.mode = mode
        self.effort = effort
        self.model = model
        self.workflow = workflow
        self.skill_manifest_id = skill_manifest_id
        self.plan_context = plan_context
        self.runtime_context = runtime_context
        self.tool_broker = tool_broker or LocalToolBroker(self.workspace)
        self.cancel_requested = False
        self._client = HostedChatClient(
            self.credential.base_url,
            self.credential.api_key,
            self.credential.fingerprint,
        )

    def _request(
        self,
        messages: list[BaseMessage],
        *,
        turn_id: str | None = None,
        resume: dict[str, Any] | None = None,
        session_state: dict[str, Any] | None = None,
        context_cut: str | None = None,
    ) -> ChatRequest:
        if not messages or messages[-1].type != "human":
            raise ValueError("The final input message must be a human message.")
        query = messages[-1].content
        if not isinstance(query, str):
            query = json.dumps(query)
        return ChatRequest(
            query=query,
            context=_context(messages, context_cut),
            workspace_path=str(self.workspace),
            conversation_id=self.conversation_id,
            mode=self.mode,
            effort=self.effort,
            model=request_model(self.effort, self.model),
            agentic_mode=self.mode is CopperMode.AGENT or self.plan_context is not None,
            workflow=self.workflow,
            skill_manifest_id=self.skill_manifest_id,
            plan_context=self.plan_context,
            runtime_context=self.runtime_context,
            schematic_path=self.context.schematic_path,
            pcb_path=self.context.pcb_path,
            project_data={"workspace_path": str(self.workspace)},
            initial_file_tree=build_initial_file_tree(self.workspace) or None,
            turn_id=turn_id,
            resume=resume,
            session_state=session_state,
        )

    async def astream_events_raw(
        self,
        input: AgentInput,
        config: RunnableConfig | None = None,
    ) -> AsyncIterator[CopperEvent]:
        """Stream one hosted attempt.

        A mapping input may carry ``turn_id`` (this attempt's id), ``resume``
        (the ``request.resume`` journal for a resume attempt) and
        ``continuation`` (a later attempt of the same logical turn). Later
        attempts keep the tool broker's results, so a repeated
        ``tool_call_id`` is answered from cache instead of running again.
        ``session_state`` and ``context_cut`` (chat context protocol v1) are
        the turn's pinned state and compaction cut, sent unchanged on every
        attempt.
        """
        del config
        options = input if isinstance(input, Mapping) else {}
        resume = options.get("resume")
        session_state = options.get("session_state")
        cut = options.get("context_cut")
        request = self._request(
            _messages(input),
            turn_id=options.get("turn_id"),
            resume=resume if isinstance(resume, dict) else None,
            session_state=session_state if isinstance(session_state, dict) else None,
            context_cut=cut if isinstance(cut, str) else None,
        )
        if not options.get("continuation") and request.resume is None:
            self.tool_broker.begin_turn()
        async for event in self._client.stream(request, self.tool_broker.execute):
            yield event

    async def acompact(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run ``POST /analyze/compact`` with this agent's credential."""
        return await self._client.compact(payload)

    async def acount_context(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run ``POST /analyze/context/count`` with this agent's credential."""
        return await self._client.count_context(payload)

    async def server_reachable(self) -> bool:
        """Whether the hosted service answers its heartbeat (checked before a resume)."""
        return await self._client.heartbeat()

    async def ainvoke(
        self,
        input: AgentInput,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> AgentOutput:
        del kwargs
        messages = _messages(input)
        text: list[str] = []
        events: list[dict[str, Any]] = []
        async for event in self.astream_events_raw(messages, config):
            if event.kind is EventKind.TEXT:
                text.append(str(event.data))
            events.append({"kind": event.kind.value, "data": event.data, "title": event.title})
        assistant = AIMessage(content="".join(text))
        return {"messages": [*messages, assistant], "events": events}

    def invoke(
        self,
        input: AgentInput,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> AgentOutput:
        return asyncio.run(self.ainvoke(input, config, **kwargs))

    async def astream(
        self,
        input: AgentInput,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[AIMessageChunk]:
        del kwargs
        async for event in self.astream_events_raw(input, config):
            if event.kind is EventKind.TEXT:
                yield AIMessageChunk(content=str(event.data))

    def stream(
        self,
        input: AgentInput,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> Iterator[AIMessageChunk]:
        async def collect() -> list[AIMessageChunk]:
            return [chunk async for chunk in self.astream(input, config, **kwargs)]

        yield from asyncio.run(collect())

    async def acancel(self) -> None:
        """Ask the hosted turn to stop; the graph will not retry after this."""
        self.cancel_requested = True
        await self._client.cancel()

    def as_langgraph(self) -> Any:
        """Compile the transport Runnable as a one-node LangGraph."""
        from langgraph.graph import END, START, MessagesState, StateGraph

        graph = StateGraph(MessagesState)  # ty: ignore[invalid-argument-type]

        async def run(state: MessagesState) -> dict[str, Any]:
            result = await self.ainvoke({"messages": state["messages"]})
            return {"messages": [result["messages"][-1]]}

        graph.add_node("copper_pilot", run)
        graph.add_edge(START, "copper_pilot")
        graph.add_edge("copper_pilot", END)
        return graph.compile()

    def as_compiled_subagent(
        self,
        name: str = "copper-pilot",
        description: str = "Delegate an electronics design task to CopperPilot.",
    ) -> Any:
        """Return an explicit Deep Agents compiled-subagent declaration."""
        try:
            from deepagents.middleware.subagents import CompiledSubAgent
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "The required `deepagents` runtime is unavailable; reinstall copper-pilot-cli."
            ) from exc

        return CompiledSubAgent(name=name, description=description, runnable=self.as_langgraph())
