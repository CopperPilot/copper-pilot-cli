"""Claude Agent driver through the real Claude Agent SDK with a scripted transport."""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("claude_agent_sdk")

from byo_fakes import GUIDE  # noqa: E402
from claude_agent_sdk import ClaudeSDKClient  # noqa: E402
from claude_agent_sdk._internal.transport import Transport  # noqa: E402

from copper_pilot_cli.agents import AgentSelection, Provider  # noqa: E402
from copper_pilot_cli.agents import errors as agent_errors  # noqa: E402
from copper_pilot_cli.agents.catalog import compile_claude_selection  # noqa: E402
from copper_pilot_cli.agents.claude_driver import (  # noqa: E402
    ClaudeSession,
    display_tool,
    permission_mode,
)
from copper_pilot_cli.agents.session import TurnContext  # noqa: E402
from copper_pilot_cli.agents.tool_proxy import ToolOutcome  # noqa: E402
from copper_pilot_cli.copper_protocol import CopperMode, EventKind  # noqa: E402
from copper_pilot_cli.copper_tools import ApprovalMode, LocalToolBroker  # noqa: E402

Script = Callable[["FakeClaudeTransport", dict[str, Any]], Awaitable[None]]


class FakeClaudeTransport(Transport):
    """The CLI side of the SDK's stream-json control protocol, scripted per prompt."""

    def __init__(self, script: Script) -> None:
        self.script = script
        self.out: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.written: list[dict[str, Any]] = []
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.ids = itertools.count(1)
        self.ready = False
        self.interrupted = False

    async def connect(self) -> None:
        self.ready = True

    async def write(self, data: str) -> None:
        message = json.loads(data)
        self.written.append(message)
        kind = message.get("type")
        if kind == "control_request":
            request = message["request"]
            response: dict[str, Any] = {}
            if request["subtype"] == "initialize":
                response = {"commands": [], "models": []}
            elif request["subtype"] == "interrupt":
                self.interrupted = True
            await self.out.put(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": message["request_id"],
                        "response": response,
                    },
                }
            )
        elif kind == "control_response":
            body = message["response"]
            future = self.pending.pop(body["request_id"], None)
            if future is not None:
                future.set_result(body)
        elif kind == "user":
            asyncio.create_task(self.script(self, message))

    async def read_messages(self):  # type: ignore[override]
        while (item := await self.out.get()) is not None:
            yield item

    async def close(self) -> None:
        self.ready = False
        await self.out.put(None)

    def is_ready(self) -> bool:
        return self.ready

    async def end_input(self) -> None:
        return None

    async def emit(self, message: dict[str, Any]) -> None:
        await self.out.put({"session_id": "s1", "uuid": f"u{next(self.ids)}", **message})

    async def ask(self, request: dict[str, Any]) -> dict[str, Any]:
        """A control request from the CLI to the SDK (permissions, in-process MCP)."""
        identifier = f"cli-{next(self.ids)}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        await self.out.put(
            {"type": "control_request", "request_id": identifier, "request": request}
        )
        return await future

    async def mcp(self, method: str, params: dict[str, Any], identifier: int) -> dict[str, Any]:
        reply = await self.ask(
            {
                "subtype": "mcp_message",
                "server_name": "copperpilot",
                "message": {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params},
            }
        )
        return reply["response"]["mcp_response"]


def assistant(content: list[dict[str, Any]], message_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "id": message_id,
            "model": "claude-haiku-5-5",
            "content": content,
            "usage": {"input_tokens": 50, "cache_read_input_tokens": 1000, "output_tokens": 20},
        },
        "parent_tool_use_id": None,
        **extra,
    }


def result(**extra: Any) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success",
        "duration_ms": 10,
        "duration_api_ms": 8,
        "is_error": False,
        "num_turns": 2,
        "usage": {"input_tokens": 100, "cache_read_input_tokens": 2000, "output_tokens": 40},
        "result": "done",
        **extra,
    }


class StubSession:
    def __init__(self) -> None:
        self.usage: list[dict[str, Any]] = []

    async def send_usage(self, **kwargs: Any) -> None:
        self.usage.append(kwargs)


class StubProxy:
    def __init__(self) -> None:
        self.session = StubSession()
        self.emit: Any = None
        self.guide = GUIDE
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def start(self) -> None:
        return None

    async def list_tools(self) -> list[dict[str, Any]]:
        return [{"name": "query_design", "description": "q", "inputSchema": {"type": "object"}}]

    async def call_tool(self, name: str, arguments: dict[str, Any], **_kwargs: Any) -> ToolOutcome:
        self.calls.append((name, arguments))
        return ToolOutcome.text("U1 is an ESP32")


def _session(
    tmp_path: Path, broker: LocalToolBroker, script: Script, **selection: Any
) -> tuple[ClaudeSession, StubProxy, list[Any]]:
    proxy = StubProxy()
    captured: list[Any] = []

    def factory(options: Any) -> ClaudeSDKClient:
        transport = FakeClaudeTransport(script)
        captured.append((options, transport))
        return ClaudeSDKClient(options, transport=transport)

    session = ClaudeSession(
        AgentSelection(provider=Provider.CLAUDE, **selection),
        workspace=tmp_path,
        proxy=proxy,  # ty: ignore[invalid-argument-type]
        broker=lambda: broker,
        client_factory=factory,
    )
    return session, proxy, captured


def _context(tmp_path: Path, broker: LocalToolBroker, mode: CopperMode = CopperMode.AGENT):
    return TurnContext(
        workspace=tmp_path, broker=broker, mode=mode, thread_id="chat-1", turn_id="turn-1"
    )


async def full_turn(transport: FakeClaudeTransport, prompt: dict[str, Any]) -> None:
    del prompt
    await transport.emit(
        {
            "type": "stream_event",
            "event": {"type": "message_start", "message": {"id": "m1"}},
            "parent_tool_use_id": None,
        }
    )
    for chunk in ("Hello ", "world"):
        await transport.emit(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": chunk},
                },
                "parent_tool_use_id": None,
            }
        )
    await transport.emit(
        assistant(
            [
                {"type": "text", "text": "Hello world"},
                {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "make erc"}},
            ],
            "m1",
        )
    )
    permission = await transport.ask(
        {
            "subtype": "can_use_tool",
            "tool_name": "Bash",
            "input": {"command": "make erc"},
            "tool_use_id": "tu1",
        }
    )
    allowed = permission["response"]["behavior"] == "allow"
    await transport.emit(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu1",
                        "content": "removed" if allowed else "denied",
                        "is_error": not allowed,
                    }
                ]
            },
            "parent_tool_use_id": None,
        }
    )
    await transport.mcp(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "claude", "version": "1"},
        },
        1,
    )
    listed = await transport.mcp("tools/list", {}, 2)
    called = await transport.mcp(
        "tools/call", {"name": "query_design", "arguments": {"query": "U1"}}, 3
    )
    question = await transport.ask(
        {
            "subtype": "can_use_tool",
            "tool_name": "AskUserQuestion",
            "input": {
                "questions": [
                    {
                        "question": "Which rail?",
                        "header": "Rail",
                        "options": [{"label": "3V3"}, {"label": "5V"}],
                        "multiSelect": False,
                    }
                ]
            },
            "tool_use_id": "tu-q",
        }
    )
    await transport.emit(
        assistant(
            [
                {
                    "type": "tool_use",
                    "id": "todo1",
                    "name": "TodoWrite",
                    "input": {"todos": [{"content": "Check ERC", "status": "in_progress"}]},
                },
                {
                    "type": "tool_use",
                    "id": "task1",
                    "name": "Task",
                    "input": {"description": "Survey parts", "prompt": "..."},
                },
                {
                    "type": "tool_use",
                    "id": "mcp1",
                    "name": "mcp__copperpilot__query_design",
                    "input": {"query": "U1"},
                },
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "tools": [tool["name"] for tool in listed["result"]["tools"]],
                            "call": called["result"]["content"][0]["text"],
                            "answers": question.get("response", {})
                            .get("updatedInput", {})
                            .get("answers"),
                        }
                    ),
                },
            ],
            "m2",
        )
    )
    await transport.emit(
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "task1", "content": "3 parts found"},
                    {"type": "tool_result", "tool_use_id": "mcp1", "content": "U1 is an ESP32"},
                ]
            },
            "parent_tool_use_id": None,
        }
    )
    await transport.emit(result())


async def test_claude_turn_events_permissions_mcp_and_usage(tmp_path: Path) -> None:
    approvals: list[Any] = []

    async def approve(request: Any) -> bool:
        approvals.append(request)
        return True

    async def ask_user(arguments: Any) -> Any:
        assert arguments == {"question": "Which rail?", "options": ["3V3", "5V"]}
        return {"status": "answered", "answer": "3V3", "source": "option"}

    broker = LocalToolBroker(tmp_path, mode=ApprovalMode.MANUAL, approve=approve, ask_user=ask_user)
    session, proxy, captured = _session(
        tmp_path, broker, full_turn, model="claude-haiku-5-5", effort="low"
    )
    try:
        events = [event async for event in session.run_turn("hi", _context(tmp_path, broker))]
    finally:
        await session.aclose()
    options, transport = captured[0]
    assert options.system_prompt == {"type": "preset", "preset": "claude_code", "append": GUIDE}
    assert options.include_partial_messages is True
    assert options.model == "claude-haiku-5-5" and options.effort == "low"
    assert options.permission_mode == "default"
    assert options.setting_sources == ["user", "project", "local"]
    assert options.extra_args == {"no-session-persistence": None}
    assert options.cwd == str(tmp_path)
    assert options.mcp_servers["copperpilot"]["type"] == "sdk"
    assert "ANTHROPIC_API_KEY" not in options.env
    assert options.settings is None  # Haiku has no Fast mode

    texts = [str(event.data) for event in events if event.kind is EventKind.TEXT]
    assert texts[:2] == ["Hello ", "world"]  # streamed once, not repeated from the block
    payload = json.loads(texts[-1])
    assert payload == {
        "tools": ["query_design"],
        "call": "U1 is an ESP32",
        "answers": {"Which rail?": "3V3"},
    }
    requests = [event.data for event in events if event.kind is EventKind.TOOL_REQUEST]
    assert [item.tool_name for item in requests] == ["execute", "query_design"]
    results = [event.data for event in events if event.kind is EventKind.TOOL_RESULT]
    assert results[0]["result"] == {"result": "removed"}
    statuses = [event.data for event in events if event.kind is EventKind.STATUS_EVENT]
    assert statuses[0] == {
        "kind": "todo_list",
        "items": [{"content": "Check ERC", "status": "in_progress"}],
    }
    subagent = [item for item in statuses if item.get("kind") == "subagent"]
    assert [item["status"] for item in subagent] == ["running", "success"]
    context = next(item for item in statuses if item.get("kind") == "context_usage")
    assert context["context_tokens"] == 1070 and context["context_limit"] == 1_000_000
    assert events[-1].kind is EventKind.FINAL
    assert approvals[0].tool_name == "execute"
    assert approvals[0].arguments == {"command": "make erc"}
    assert proxy.calls == [("query_design", {"query": "U1"})]
    usage = proxy.session.usage[0]
    assert usage["provider"] == "claude" and usage["input_tokens"] == 2100
    assert usage["cached_input_tokens"] == 2000 and usage["output_tokens"] == 40
    sent = [item for item in transport.written if item.get("type") == "user"]
    assert sent[0]["message"]["content"] == "hi"


async def test_claude_auto_allows_workspace_shell_and_ask_mode_denies(tmp_path: Path) -> None:
    async def approve(_request: Any) -> bool:
        raise AssertionError("Auto must not prompt for a routine in-workspace command")

    broker = LocalToolBroker(tmp_path, mode=ApprovalMode.AUTO, approve=approve)
    session, _proxy, captured = _session(tmp_path, broker, full_turn)
    try:
        events = [event async for event in session.run_turn("hi", _context(tmp_path, broker))]
        options, _transport = captured[0]
        assert options.permission_mode == "default"
        result_data = next(event.data for event in events if event.kind is EventKind.TOOL_RESULT)
        assert result_data["result"] == {"result": "removed"}
        ask_events = [
            event
            async for event in session.run_turn("again", _context(tmp_path, broker, CopperMode.ASK))
        ]
    finally:
        await session.aclose()
    denied = next(event.data for event in ask_events if event.kind is EventKind.TOOL_RESULT)
    assert "error" in denied["result"]
    _options, transport = captured[0]
    modes = [
        item["request"].get("mode")
        for item in transport.written
        if item.get("type") == "control_request"
        and item["request"]["subtype"] == "set_permission_mode"
    ]
    assert modes == ["default", "default"]


async def rate_limited(transport: FakeClaudeTransport, prompt: dict[str, Any]) -> None:
    del prompt
    await transport.emit(
        {
            "type": "rate_limit_event",
            "rate_limit_info": {"status": "rejected", "resetsAt": 1893456000},
        }
    )
    await transport.emit(assistant([{"type": "text", "text": "limit"}], "m1", error="rate_limit"))
    await transport.emit(result(is_error=True, api_error_status=429, errors=["rate limited"]))


async def auth_failed(transport: FakeClaudeTransport, prompt: dict[str, Any]) -> None:
    del prompt
    await transport.emit(
        assistant(
            [{"type": "text", "text": "Invalid API key"}], "m1", error="authentication_failed"
        )
    )
    await transport.emit(result(is_error=True, errors=["Invalid API key · Please run /login"]))


async def overloaded(transport: FakeClaudeTransport, prompt: dict[str, Any]) -> None:
    del prompt
    await transport.emit(result(is_error=True, api_error_status=529, errors=["Overloaded"]))


@pytest.mark.parametrize(
    ("script", "kind"),
    [
        (rate_limited, agent_errors.USAGE_LIMIT),
        (auth_failed, agent_errors.AUTH_REQUIRED),
        (overloaded, agent_errors.OVERLOADED),
    ],
)
async def test_claude_failures_are_classified(tmp_path: Path, script: Script, kind: str) -> None:
    broker = LocalToolBroker(tmp_path, mode=ApprovalMode.AUTO)
    session, _proxy, _captured = _session(tmp_path, broker, script)
    try:
        with pytest.raises(agent_errors.AgentError) as caught:
            async for _event in session.run_turn("hi", _context(tmp_path, broker)):
                pass
    finally:
        await session.aclose()
    assert caught.value.kind == kind
    if kind == agent_errors.USAGE_LIMIT:
        assert caught.value.resets_at == "2030-01-01T00:00:00Z"
    if kind == agent_errors.AUTH_REQUIRED:
        assert "claude auth login" in str(caught.value)


async def test_yolo_needs_explicit_choice_and_sets_bypass(tmp_path: Path) -> None:
    broker = LocalToolBroker(tmp_path, mode=ApprovalMode.YOLO)
    session, _proxy, captured = _session(tmp_path, broker, overloaded, fast_mode=True)
    try:
        with pytest.raises(agent_errors.AgentError):
            async for _event in session.run_turn("hi", _context(tmp_path, broker)):
                pass
    finally:
        await session.aclose()
    options, _transport = captured[0]
    # YOLO is decided by our callback; Claude itself never bypasses permissions.
    assert options.permission_mode == "default"
    assert options.extra_args == {"no-session-persistence": None}
    # Default model (Opus 5.5) supports Fast mode.
    assert json.loads(options.settings) == {"fastMode": True}


def test_permission_mode_mapping() -> None:
    assert permission_mode(CopperMode.AGENT) == "default"
    assert permission_mode(CopperMode.ASK) == "default"
    assert permission_mode(CopperMode.PLAN) == "plan"


@pytest.mark.parametrize(
    ("mode", "dangerous", "tool", "tool_input", "expected"),
    [
        (ApprovalMode.MANUAL, False, "Read", {"file_path": "a.kicad_sch"}, "allow"),
        (ApprovalMode.MANUAL, False, "Read", {"file_path": "/etc/passwd"}, "ask"),
        (ApprovalMode.AUTO, True, "Read", {"file_path": "/etc/passwd"}, "allow"),
        (ApprovalMode.MANUAL, False, "WebFetch", {"url": "https://x"}, "allow"),
        (ApprovalMode.MANUAL, False, "Task", {"prompt": "x"}, "allow"),
        (ApprovalMode.MANUAL, False, "Edit", {"file_path": "a.py"}, "ask"),
        (ApprovalMode.AUTO, False, "Edit", {"file_path": "a.py"}, "allow"),
        (ApprovalMode.AUTO, False, "Write", {"file_path": "/etc/x"}, "ask"),
        (ApprovalMode.YOLO, False, "Write", {"file_path": "/etc/x"}, "allow"),
        (ApprovalMode.AUTO, False, "Bash", {"command": "make erc"}, "allow"),
        (ApprovalMode.AUTO, False, "Bash", {"command": "rm -r build"}, "ask"),
        (ApprovalMode.AUTO, True, "Bash", {"command": "rm -r build"}, "allow"),
        (ApprovalMode.AUTO, False, "Bash", {"command": "sudo ls"}, "ask"),
        (ApprovalMode.MANUAL, False, "Bash", {"command": "make erc"}, "ask"),
        (ApprovalMode.YOLO, False, "Bash", {"command": "make erc"}, "allow"),
        (ApprovalMode.AUTO, False, "mcp__other__thing", {}, "ask"),
        (ApprovalMode.YOLO, False, "mcp__other__thing", {}, "allow"),
    ],
)
def test_claude_policy_follows_cli_approval_settings(
    tmp_path: Path, mode: ApprovalMode, dangerous: bool, tool: str, tool_input: dict, expected: str
) -> None:
    broker = LocalToolBroker(tmp_path, mode=mode, always_allow_dangerous_shell_commands=dangerous)
    session, _proxy, _captured = _session(tmp_path, broker, overloaded)
    assert session._policy(broker, tool, tool_input)[0] == expected


async def plan_turn(transport: FakeClaudeTransport, prompt: dict[str, Any]) -> None:
    del prompt
    reply = await transport.ask(
        {
            "subtype": "can_use_tool",
            "tool_name": "ExitPlanMode",
            "input": {"plan": "# Add USB-C\n\nPlace J1 and route VBUS."},
            "tool_use_id": "plan1",
        }
    )
    await transport.emit(assistant([{"type": "text", "text": reply["response"]["message"]}], "m1"))
    await transport.emit(result())


async def test_exit_plan_mode_is_captured_not_run(tmp_path: Path) -> None:
    broker = LocalToolBroker(tmp_path, mode=ApprovalMode.AUTO)
    session, _proxy, _captured = _session(tmp_path, broker, plan_turn)
    try:
        events = [
            event
            async for event in session.run_turn(
                "plan it", _context(tmp_path, broker, CopperMode.PLAN)
            )
        ]
    finally:
        await session.aclose()
    plans = [
        event.data
        for event in events
        if event.kind is EventKind.STATUS_EVENT and event.data.get("kind") == "agent_plan"
    ]
    assert plans == [{"kind": "agent_plan", "plan": "# Add USB-C\n\nPlace J1 and route VBUS."}]
    text = "".join(str(event.data) for event in events if event.kind is EventKind.TEXT)
    assert "Plan captured" in text


def test_display_tool_names() -> None:
    assert display_tool("Bash", {"command": "ls"}) == ("execute", {"command": "ls"})
    assert display_tool("mcp__copperpilot__run_erc", {}) == ("run_erc", {})
    assert display_tool("MultiEdit", {"file_path": "a", "edits": [1, 2]}) == (
        "edit_file",
        {"file_path": "a", "edits": 2},
    )


def test_claude_catalog_compile() -> None:
    default = compile_claude_selection(None, None)
    assert (default.model, default.effort, default.fast_mode) == (
        "claude-opus-5-5",
        "medium",
        False,
    )
    assert compile_claude_selection("sonnet", "low").model == "claude-sonnet-5-5"
    assert compile_claude_selection("claude-sonnet-5-5", "turbo").effort == "high"
    assert compile_claude_selection("claude-opus-4-7", "xhigh").effort == "max"
    assert compile_claude_selection("claude-sonnet-4-6", "max").effort == "high"
    assert compile_claude_selection("claude-sonnet-5-5", None, True).fast_mode is None
    assert compile_claude_selection("claude-haiku-4-5", "low").effort is None
    unknown = compile_claude_selection("claude-future-9", "high")
    assert unknown.model == "claude-future-9" and unknown.effort == "high"
