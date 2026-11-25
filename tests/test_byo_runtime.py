"""BYO turns through the chat graph: transcript, session reuse, handoff, plans, canary audit."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from byo_fakes import CANARY, tools_server
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from copper_pilot_cli.agents import AgentSelection, Provider
from copper_pilot_cli.agents import errors as agent_errors
from copper_pilot_cli.agents.handoff import (
    HANDOFF_BYTE_CAP,
    build_handoff,
    handoff_budget,
    historical_messages,
    seeded_prompt,
)
from copper_pilot_cli.agents.runtime import AgentSessions, save_agent_plan, set_plan_status
from copper_pilot_cli.agents.session import (
    AgentSession,
    TurnContext,
    TurnUsage,
    final_event,
    plan_event,
    tool_request_event,
    tool_result_event,
)
from copper_pilot_cli.copper_config import paths
from copper_pilot_cli.copper_features import parse_plan
from copper_pilot_cli.copper_graph import create_copper_graph
from copper_pilot_cli.copper_main import parser, resolve_agent_selection
from copper_pilot_cli.copper_protocol import CopperEvent, CopperMode, EventKind
from copper_pilot_cli.copper_tools import ApprovalMode, LocalToolBroker
from copper_pilot_cli.diagnostics import configure_diagnostics
from copper_pilot_cli.langchain import CopperPilotAgent

# ----------------------------------------------------------------- handoff


def test_handoff_budget_matches_t3() -> None:
    assert handoff_budget(user_text="hi") == 16_000
    assert handoff_budget(user_text="hi", token_cap=100_000) == HANDOFF_BYTE_CAP
    # A small window keeps a work reserve of at least 16K or a quarter of it.
    assert handoff_budget(user_text="hi", model_context_window=30_000) == 30_000 - 4 - 15_000
    assert handoff_budget(user_text="hi", model_context_window=20_000) == 20_000 - 4 - 10_000
    assert handoff_budget(user_text="hi", model_context_window=20_000, native_tokens=20_000) == 0


def _history(turns: int, size: int = 50) -> list[Any]:
    messages: list[Any] = []
    for index in range(turns):
        messages.append(HumanMessage(content=f"question {index} " + "q" * size, id=f"h{index}"))
        messages.append(AIMessage(content=f"answer {index} " + "a" * size, id=f"a{index}"))
    return messages


def test_handoff_renders_blocks_and_tool_calls() -> None:
    messages = [
        HumanMessage(content="Which regulator?", id="h0"),
        AIMessage(
            content="Checking.",
            id="a0",
            tool_calls=[{"id": "c1", "name": "read_file", "args": {"file_path": "x"}}],
        ),
        ToolMessage(content="AMS1117", tool_call_id="c1", name="read_file"),
        AIMessage(content="It is an AMS1117.", id="a1"),
    ]
    items = historical_messages(messages)
    assert [item.kind for item in items] == [
        "user_message",
        "assistant_message",
        "tool_call",
        "assistant_message",
    ]
    handoff = build_handoff(messages, user_text="next", provider_label="Codex", thread_id="t1")
    assert handoff is not None
    assert "[Historical user; user_message; item=h0; status=completed]\nWhich regulator?" in (
        handoff.text
    )
    assert "Result: AMS1117" in handoff.text
    prompt = seeded_prompt(handoff, "next")
    assert prompt.startswith("CopperPilot chat handoff (chat t1). You are Codex")
    assert prompt.endswith("[Current user request]\nnext")


def test_handoff_is_capped_and_keeps_first_and_last() -> None:
    messages = _history(200, size=400)
    handoff = build_handoff(messages, user_text="next", provider_label="Claude Agent")
    assert handoff is not None
    assert len(handoff.text.encode()) <= 16_000
    assert handoff.omitted > 0
    assert "question 0 " in handoff.text  # first user message
    assert "question 199 " in handoff.text and "answer 199 " in handoff.text
    assert "omitted" in handoff.text
    assert build_handoff([], user_text="x", provider_label="Codex") is None


# --------------------------------------------------------- graph runtime


class FakeSession(AgentSession):
    """A scripted agent that uses the real tool socket and echoes what it got."""

    def __init__(self, selection: AgentSelection, *, workspace: Path, proxy: Any, broker: Any):
        super().__init__(selection)
        self.provider = selection.provider
        self.workspace = workspace
        self.proxy = proxy
        self.prompts: list[str] = []
        self.guides: list[str] = []
        self.fail_with: agent_errors.AgentError | None = None

    @property
    def context_window(self) -> int | None:
        return 200_000

    async def start(self) -> None:
        await self.proxy.start()
        self.guides.append(self.proxy.guide)

    async def run_turn(self, prompt: str, context: TurnContext) -> AsyncIterator[CopperEvent]:
        self.prompts.append(prompt)
        if self.fail_with is not None:
            raise self.fail_with
        outcome = await self.proxy.call_tool("query_design", {"query": "U1"})
        yield tool_request_event("t1", "query_design", {"query": "U1"})
        yield tool_result_event("t1", "query_design", outcome.plain())
        if context.mode is CopperMode.PLAN:
            yield plan_event("# Fix ERC\n\nAdd a pull-up on EN.")
        yield CopperEvent(EventKind.TEXT, f"{self.provider.value} says hi")
        yield final_event(TurnUsage(input_tokens=5, output_tokens=2), model="m")

    async def interrupt(self) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True


async def _run(runtime: Any, thread: str, workspace: Path, **values: Any) -> list[dict]:
    events = []
    async for item in runtime.astream(
        {"messages": [HumanMessage(content=values.pop("message"))], **values},
        runtime.config(thread, workspace),
    ):
        _ns, mode, data = item
        if mode == "custom":
            events.append(data)
    return events


async def test_byo_turns_persist_switch_with_handoff_and_never_store_the_guide(
    tmp_path: Path,
) -> None:
    configure_diagnostics()
    workspace = tmp_path / "board"
    workspace.mkdir()
    (workspace / "board.kicad_sch").write_text("(kicad_sch)")
    created: list[FakeSession] = []

    def factory(selection: AgentSelection, **kwargs: Any) -> FakeSession:
        session = FakeSession(selection, **kwargs)
        created.append(session)
        return session

    async with tools_server() as fake:
        credential = fake.credential
        broker = LocalToolBroker(workspace, mode=ApprovalMode.AUTO)
        agent = CopperPilotAgent(workspace, credential=credential, tool_broker=broker)
        agent.agent_sessions = AgentSessions(  # ty: ignore[missing-attribute]
            credential=lambda: credential,
            broker=lambda: broker,
            factories={Provider.CLAUDE: factory, Provider.CODEX: factory},
        )
        async with create_copper_graph(agent) as runtime:
            claude = {"copper_provider": "claude", "copper_agent": {"model": "claude-haiku-5-5"}}
            first = await _run(runtime, "chat-1", workspace, message="What is U1?", **claude)
            second = await _run(runtime, "chat-1", workspace, message="And U2?", **claude)
            third = await _run(
                runtime,
                "chat-1",
                workspace,
                message="Plan the ERC fix",
                copper_provider="codex",
                copper_agent={},
                copper_mode="plan",
            )
            state = await runtime.aget_state(runtime.config("chat-1", workspace))
        await agent.agent_sessions.aclose()

    # One Claude session reused, then a Codex session seeded with a handoff.
    assert [session.provider for session in created] == [Provider.CLAUDE, Provider.CODEX]
    assert created[0].prompts == ["What is U1?", "And U2?"]
    handoff = created[1].prompts[0]
    assert handoff.startswith("CopperPilot chat handoff") and "claude says hi" in handoff
    assert handoff.endswith("[Current user request]\nPlan the ERC fix")
    # The guide (with the canary) reached the agent ...
    assert all(CANARY in session.guides[0] for session in created)
    # ... and the transcript has the turns.
    values = state.values
    assert values["copper_provider"] == "codex"
    texts = [m.content for m in values["messages"] if isinstance(m, AIMessage) and m.content]
    assert texts == ["claude says hi", "claude says hi", "codex says hi"]
    assert any(isinstance(m, ToolMessage) for m in values["messages"])
    assert [e["type"] for e in first][-1] == "copper.final"
    assert any(e["type"] == "copper.tool_result" for e in second)
    ready = [
        e["data"]
        for e in third
        if e["type"] == "copper.status_event" and e["data"].get("kind") == "plan_ready"
    ]
    assert ready and ready[0]["file_path"].startswith(".copperpilot/plans/fix-erc")
    plan = parse_plan(workspace / ready[0]["file_path"], workspace)
    assert plan.name == "Fix ERC" and "pull-up" in (plan.body or "")

    # Canary audit: neither the guide nor schemas are in sessions.db or the logs.
    for handler in logging.getLogger("copper_pilot_cli").handlers:
        handler.flush()
    stored = paths().sessions
    for path in [*stored.parent.glob("sessions.db*"), *paths().logs.glob("*")]:
        assert CANARY.encode() not in path.read_bytes(), path
    assert stored.exists()
    assert b"Query the KiCad design." not in stored.read_bytes()


async def test_byo_failure_is_a_clear_error_event(tmp_path: Path) -> None:
    def factory(selection: AgentSelection, **kwargs: Any) -> FakeSession:
        session = FakeSession(selection, **kwargs)
        session.fail_with = agent_errors.auth_required(Provider.CLAUDE)
        return session

    async with tools_server() as fake:
        credential = fake.credential
        broker = LocalToolBroker(tmp_path)
        agent = CopperPilotAgent(tmp_path, credential=credential, tool_broker=broker)
        agent.agent_sessions = AgentSessions(  # ty: ignore[missing-attribute]
            credential=lambda: credential,
            broker=lambda: broker,
            factories={Provider.CLAUDE: factory},
        )
        async with create_copper_graph(agent) as runtime:
            events = await _run(runtime, "chat-2", tmp_path, message="hi", copper_provider="claude")
        await agent.agent_sessions.aclose()
    error = next(e for e in events if e["type"] == "copper.error")
    assert error["data"]["type"] == "auth_required"
    assert "claude auth login" in error["data"]["message"]
    assert error["data"]["retryable"] is False


async def test_tool_socket_refusal_is_classified(tmp_path: Path) -> None:
    async with tools_server() as fake:
        fake.refuse_with = {"code": "payment_required", "status": 402, "public_message": "Limit"}
        broker = LocalToolBroker(tmp_path)
        sessions = AgentSessions(
            credential=lambda: fake.credential,
            broker=lambda: broker,
            factories={Provider.CLAUDE: FakeSession},
        )
        with pytest.raises(agent_errors.AgentError) as caught:
            await sessions.session_for(
                thread_id="x", selection=AgentSelection(Provider.CLAUDE), workspace=tmp_path
            )
    assert caught.value.kind == agent_errors.TOOL_SOCKET
    assert "Limit" in str(caught.value)


def test_plan_files_round_trip(tmp_path: Path) -> None:
    relative, name = save_agent_plan(tmp_path, "## Add USB-C\nPlace J1.", Provider.CLAUDE)
    again, _ = save_agent_plan(tmp_path, "## Add USB-C\nOther.", Provider.CLAUDE)
    assert relative == ".copperpilot/plans/add-usb-c.plan.md" and again.endswith("-2.plan.md")
    set_plan_status(tmp_path, relative, "running")
    plan = parse_plan(tmp_path / relative, tmp_path)
    assert (plan.name, plan.overall_status) == (name, "running")


# ------------------------------------------------------------------ CLI


def test_provider_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    args = parser().parse_args(
        ["--provider", "claude", "--agent-model", "claude-haiku-5-5", "--agent-effort", "low"]
    )
    selection = resolve_agent_selection(args)
    assert selection is not None and selection.provider is Provider.CLAUDE
    assert selection.options() == {"model": "claude-haiku-5-5", "effort": "low"}
    assert resolve_agent_selection(parser().parse_args([])) is None
    with pytest.raises(ValueError):
        resolve_agent_selection(parser().parse_args(["--agent-model", "x"]))
    copper = resolve_agent_selection(parser().parse_args(["--provider", "copperpilot"]))
    assert copper is not None and not copper.is_byo


def test_mcp_tools_session_flags() -> None:
    from copper_pilot_cli.copper_main import command_parser

    args = command_parser("mcp").parse_args(
        ["--tools-session", "--workspace", "/tmp", "--approval", "yolo"]  # noqa: S108
    )
    assert args.tools_session and args.approval == "yolo" and args.workspace == "/tmp"  # noqa: S108
    assert command_parser("mcp").parse_args([]).tools_session is False


def test_saved_agent_selection_round_trip() -> None:
    from copper_pilot_cli.copper_preferences import (
        save_agent_selection,
        saved_agent_options,
        saved_agent_selection,
    )

    save_agent_selection("claude", {"model": "claude-sonnet-5-5", "effort": "low"})
    save_agent_selection("codex", {"model": "gpt-x"})
    assert saved_agent_selection()["provider"] == "codex"
    assert saved_agent_options("claude") == {"model": "claude-sonnet-5-5", "effort": "low"}
    assert json.dumps(saved_agent_options("nobody")) == "{}"


def test_reset_phrase_and_selection_label() -> None:
    from datetime import UTC, datetime

    phrase = agent_errors.reset_phrase("2030-01-01T02:05:00Z", now=datetime(2030, 1, 1, tzinfo=UTC))
    assert "(in 2h 5m)" in phrase
    label = AgentSelection(Provider.CLAUDE, model="claude-haiku-5-5", effort="low").label()
    assert label == "Claude Agent · claude-haiku-5-5 · low"
    assert agent_errors.classify_message(Provider.CLAUDE, "529 Overloaded").kind == "overloaded"


def test_status_line_shows_the_agent() -> None:
    from copper_pilot_cli.copper_chrome import ColorMode, status_content

    line = status_content(
        120, chat_mode="agent", effort="balanced", agent="Claude Agent · low", mode=ColorMode.NONE
    ).plain
    assert "Claude Agent · low" in line and "/provider" in line


async def test_turn_end_is_sent_after_each_turn_and_its_note_shown_before_final(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "board"
    workspace.mkdir()
    broker = LocalToolBroker(workspace, mode=ApprovalMode.AUTO)

    def factory(selection: AgentSelection, **kwargs: Any) -> FakeSession:
        return FakeSession(selection, **kwargs)

    async with tools_server() as fake:
        sessions = AgentSessions(
            credential=lambda: fake.credential,
            broker=lambda: broker,
            factories={Provider.CLAUDE: factory, Provider.CODEX: factory},
        )
        selection = AgentSelection(Provider.CLAUDE, "claude-haiku-5-5")

        async def turn(turn_id: str, mode: CopperMode = CopperMode.AGENT) -> list[CopperEvent]:
            return [
                event
                async for event in sessions.stream_turn(
                    thread_id="chat-te",
                    selection=selection,
                    workspace=workspace,
                    messages=[HumanMessage(content="go")],
                    mode=mode,
                    turn_id=turn_id,
                )
            ]

        try:
            first = await turn("turn-1")
            fake.turn_end_note = "Applied 2 staged Altium edits."
            second = await turn("turn-2")
            await sessions.discard("chat-te")
            fake.turn_end_silent = True
            fake.turn_end_note = None
        finally:
            await sessions.aclose()
    assert "turn_end_v1" in fake.starts[0]["capabilities"]
    assert [f["turn_id"] for f in fake.of_type("turn_end")] == ["turn-1", "turn-2"]
    assert not [e for e in first if e.kind is EventKind.STATUS and "Altium" in str(e.data)]
    kinds = [e.kind for e in second]
    note_index = next(
        i for i, e in enumerate(second) if e.kind is EventKind.STATUS and "Altium" in str(e.data)
    )
    assert kinds[-1] is EventKind.FINAL and note_index < len(second) - 1


async def test_agent_session_proxy_follows_the_turn_mode(tmp_path: Path) -> None:
    workspace = tmp_path / "board"
    workspace.mkdir()
    broker = LocalToolBroker(workspace, mode=ApprovalMode.AUTO)
    created: list[FakeSession] = []

    def factory(selection: AgentSelection, **kwargs: Any) -> FakeSession:
        created.append(FakeSession(selection, **kwargs))
        return created[-1]

    async with tools_server() as fake:
        sessions = AgentSessions(
            credential=lambda: fake.credential,
            broker=lambda: broker,
            factories={Provider.CLAUDE: factory},
        )
        try:
            async for _ in sessions.stream_turn(
                thread_id="chat-m",
                selection=AgentSelection(Provider.CLAUDE, "m"),
                workspace=workspace,
                messages=[HumanMessage(content="plan")],
                mode=CopperMode.ASK,
                turn_id="t",
            ):
                pass
            refused = await created[0].proxy.call_tool("place_symbol", {})
        finally:
            await sessions.aclose()
    assert refused.is_error and "Ask mode" in refused.plain()
