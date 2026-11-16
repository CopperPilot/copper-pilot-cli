"""Codex driver against a scripted fake ``codex app-server``."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from byo_fakes import GUIDE

from copper_pilot_cli.agents import AgentSelection, Provider
from copper_pilot_cli.agents import errors as agent_errors
from copper_pilot_cli.agents.codex_driver import (
    CLIENT_NAME,
    CodexSession,
    account_label,
    mcp_server_config,
    turn_policy,
)
from copper_pilot_cli.agents.session import TurnContext
from copper_pilot_cli.agents.tool_proxy import ToolOutcome
from copper_pilot_cli.copper_protocol import CopperMode, EventKind
from copper_pilot_cli.copper_tools import ApprovalMode, LocalToolBroker

FAKE = Path(__file__).parent / "fakes" / "fake_codex_app_server.py"


class StubSession:
    def __init__(self) -> None:
        self.usage: list[dict[str, Any]] = []

    async def send_usage(self, **kwargs: Any) -> None:
        self.usage.append(kwargs)


class StubProxy:
    """The bits of ``ToolProxy`` a driver touches."""

    def __init__(self) -> None:
        self.session = StubSession()
        self.emit: Any = None
        self.guide = GUIDE

    async def start(self) -> None:
        return None

    async def list_tools(self) -> list[dict[str, Any]]:
        return []

    async def call_tool(self, name: str, arguments: dict[str, Any], **_kwargs: Any) -> ToolOutcome:
        return ToolOutcome.text(name)


def _session(
    tmp_path: Path, broker: LocalToolBroker, scenario: str = "ok", **selection: Any
) -> tuple[CodexSession, StubProxy, Path]:
    log = tmp_path / "codex.jsonl"
    proxy = StubProxy()
    session = CodexSession(
        AgentSelection(provider=Provider.CODEX, **selection),
        workspace=tmp_path,
        proxy=proxy,  # ty: ignore[invalid-argument-type]
        broker=lambda: broker,
        argv=[sys.executable, str(FAKE)],
        env={"FAKE_CODEX_LOG": str(log), "FAKE_CODEX_SCENARIO": scenario, "PATH": ""},
    )
    return session, proxy, log


def _log(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _context(tmp_path: Path, broker: LocalToolBroker, mode: CopperMode = CopperMode.AGENT):
    return TurnContext(
        workspace=tmp_path, broker=broker, mode=mode, thread_id="chat-1", turn_id="turn-x"
    )


async def test_codex_turn_maps_items_approvals_and_usage(tmp_path: Path) -> None:
    approvals: list[Any] = []

    async def approve(request: Any) -> bool:
        approvals.append(request)
        return True

    broker = LocalToolBroker(tmp_path, mode=ApprovalMode.AUTO, approve=approve)
    session, proxy, log = _session(tmp_path, broker, model="gpt-fake", effort="low")
    try:
        events = [event async for event in session.run_turn("hi", _context(tmp_path, broker))]
    finally:
        await session.aclose()
    messages = _log(log)
    initialize = next(item for item in messages if item.get("method") == "initialize")
    assert initialize["params"]["clientInfo"]["name"] == CLIENT_NAME == "CopperPilot CLI"
    assert any(item.get("method") == "initialized" for item in messages)
    start = next(item for item in messages if item.get("method") == "thread/start")["params"]
    assert start["ephemeral"] is True
    assert start["developerInstructions"] == GUIDE
    assert start["cwd"] == str(tmp_path)
    assert start["model"] == "gpt-fake"
    server = start["config"]["mcp_servers"]["copperpilot"]
    assert server["args"][:4] == ["-m", "copper_pilot_cli", "mcp", "--tools-session"]
    assert server["tool_timeout_sec"] == 900
    assert start["config"]["features"]["multi_agent"] is True
    # The relay token travels in the child's environment, not argv.
    assert server["env"]["COPPER_PILOT_TOOLS_RELAY_TOKEN"] not in server["args"]
    turn = next(item for item in messages if item.get("method") == "turn/start")["params"]
    assert turn["model"] == "gpt-fake" and turn["effort"] == "low"
    assert turn["input"][0]["text"] == "hi"
    assert turn["sandboxPolicy"] == {"type": "workspaceWrite"}

    text = "".join(str(event.data) for event in events if event.kind is EventKind.TEXT)
    assert text == "Hello board"
    assert any(event.kind is EventKind.REASONING for event in events)
    requests = [event.data for event in events if event.kind is EventKind.TOOL_REQUEST]
    assert [item.tool_name for item in requests] == ["execute", "query_design"]
    results = [event.data for event in events if event.kind is EventKind.TOOL_RESULT]
    assert results[0]["result"]["exit_code"] == 0
    assert results[1]["result"]["result"] == "U1 is an ESP32"
    statuses = [event.data for event in events if event.kind is EventKind.STATUS_EVENT]
    subagent = [item for item in statuses if item.get("kind") == "subagent"]
    assert [item["status"] for item in subagent] == ["running", "success"]
    assert subagent[1]["output"] == "Added C9"
    todo = next(item for item in statuses if item.get("kind") == "todo_list")
    assert todo["items"][0]["content"] == "Check U1"
    usage = next(item for item in statuses if item.get("kind") == "context_usage")
    assert usage["context_tokens"] == 150 and usage["context_limit"] == 272000
    assert events[-1].kind is EventKind.FINAL
    assert approvals[0].tool_name == "execute" and approvals[0].arguments["command"] == "ls"
    assert proxy.session.usage[0]["input_tokens"] == 120
    assert proxy.session.usage[0]["provider"] == "codex"


async def test_codex_declined_command_in_ask_mode(tmp_path: Path) -> None:
    async def approve(_request: Any) -> bool:
        raise AssertionError("Ask mode must not prompt")

    broker = LocalToolBroker(tmp_path, mode=ApprovalMode.AUTO, approve=approve)
    session, _proxy, log = _session(tmp_path, broker)
    try:
        events = [
            event
            async for event in session.run_turn("hi", _context(tmp_path, broker, CopperMode.ASK))
        ]
    finally:
        await session.aclose()
    turn = next(item for item in _log(log) if item.get("method") == "turn/start")["params"]
    assert turn["sandboxPolicy"] == {"type": "readOnly"}
    result = next(event.data for event in events if event.kind is EventKind.TOOL_RESULT)
    assert result["result"].get("exit_code") is None


@pytest.mark.parametrize(
    ("scenario", "kind"),
    [
        ("auth", agent_errors.AUTH_REQUIRED),
        ("usage", agent_errors.USAGE_LIMIT),
        ("exit", agent_errors.PROCESS_EXITED),
    ],
)
async def test_codex_failures_are_classified(tmp_path: Path, scenario: str, kind: str) -> None:
    broker = LocalToolBroker(tmp_path, mode=ApprovalMode.AUTO)
    session, _proxy, _log_path = _session(tmp_path, broker, scenario)
    try:
        with pytest.raises(agent_errors.AgentError) as caught:
            async for _event in session.run_turn("hi", _context(tmp_path, broker)):
                pass
    finally:
        await session.aclose()
    assert caught.value.kind == kind
    if kind == agent_errors.USAGE_LIMIT:
        assert caught.value.resets_at == "2030-01-01T00:00:00Z"
        assert "resets at" in str(caught.value)
    if kind == agent_errors.AUTH_REQUIRED:
        assert "codex login" in str(caught.value)


async def test_codex_user_input_goes_to_the_question_ui(tmp_path: Path) -> None:
    asked: list[Any] = []

    async def ask_user(arguments: Any) -> Any:
        asked.append(arguments)
        return {"status": "answered", "answer": "Rev A", "source": "option"}

    broker = LocalToolBroker(tmp_path, mode=ApprovalMode.AUTO, ask_user=ask_user)
    session, _proxy, _log_path = _session(tmp_path, broker, "input")
    try:
        events = [event async for event in session.run_turn("hi", _context(tmp_path, broker))]
    finally:
        await session.aclose()
    assert asked[0] == {"question": "Which board?", "options": ["Rev A"]}
    text = "".join(str(event.data) for event in events if event.kind is EventKind.TEXT)
    assert json.loads(text) == {"answers": {"board": {"answers": ["Rev A"]}}}


def test_turn_policy_table() -> None:
    assert turn_policy(ApprovalMode.MANUAL, CopperMode.AGENT) == {
        "approvalPolicy": "untrusted",
        "sandboxPolicy": {"type": "readOnly"},
    }
    assert turn_policy(ApprovalMode.AUTO, CopperMode.AGENT)["sandboxPolicy"] == {
        "type": "workspaceWrite"
    }
    yolo = turn_policy(ApprovalMode.YOLO, CopperMode.AGENT)
    assert yolo["sandboxPolicy"] == {"type": "dangerFullAccess"}
    assert yolo["approvalPolicy"]["granular"]["mcp_elicitations"] is True
    assert turn_policy(ApprovalMode.YOLO, CopperMode.PLAN)["sandboxPolicy"] == {"type": "readOnly"}


def test_account_labels() -> None:
    assert account_label({"account": {"type": "chatgpt", "planType": "plus"}}) == (
        True,
        "Authenticated · ChatGPT Plus",
    )
    assert account_label({"account": {"type": "apiKey"}})[1] == "Authenticated · API key"
    logged_out = account_label({"account": None, "requiresOpenaiAuth": True})
    assert logged_out[0] is False and "codex login" in logged_out[1]


def test_mcp_server_config_forwards_cli_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COPPER_PILOT_API_URL", "http://localhost:80")
    config = mcp_server_config("unix:/tmp/x.sock", "secret")
    assert config["command"] == sys.executable
    assert config["env"]["COPPER_PILOT_API_URL"] == "http://localhost:80"
    assert "secret" not in config["args"]
