from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from acp import RequestError
from acp.schema import (
    AcceptElicitationResponse,
    AllowedOutcome,
    CancelElicitationResponse,
    DeclineElicitationResponse,
    DeniedOutcome,
    EmbeddedResourceContentBlock,
    RequestPermissionResponse,
    TextContentBlock,
    TextResourceContents,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from copper_pilot_cli import copper_acp
from copper_pilot_cli.copper_acp import (
    EFFORT_COMMAND,
    EFFORT_CONFIG_ID,
    LOGIN_METHOD_ID,
    MODE_CONFIG_ID,
    OPTION_ALLOW,
    OPTION_ALLOW_SESSION,
    OPTION_REJECT,
    OPTION_SKIP,
    QUESTION_MESSAGE,
    QUESTION_TIMEOUT_MESSAGE,
    AcpSession,
    CopperAcpAgent,
    available_commands,
    config_options,
    effort_command,
    elicitation_questions,
    elicitation_schema,
    first_text_block,
    is_plan_artifact,
    mode_state,
    plan_entries_from_todo_list,
    prompt_text,
    question_options,
    replay_updates,
    run_acp,
    subagent_tool_update,
    tool_kind,
    tool_locations,
    tool_title,
    usage_update,
    user_request_text,
)
from copper_pilot_cli.copper_auth import DeviceCredential, save_credential
from copper_pilot_cli.copper_protocol import CopperEffort, CopperMode, ProtocolError
from copper_pilot_cli.copper_status_events import SubagentStatus
from copper_pilot_cli.copper_tools import ApprovalMode, ApprovalRequest, ToolRejected

CREDENTIAL = DeviceCredential(
    api_key="cf_live_test",
    fingerprint="fingerprint",
    base_url="https://example.test",
)

TEXT_EVENTS: list[dict[str, Any]] = [
    {"type": "copper.status", "data": "Inspecting", "title": "status"},
    {"type": "copper.reasoning", "data": "Thinking"},
    {"type": "copper.text", "data": "Looks "},
    {"type": "copper.text", "data": ""},
    {"type": "copper.text", "data": "good."},
    {
        "type": "copper.tool_request",
        "data": {
            "tool_call_id": "call-1",
            "tool_name": "read_file",
            "arguments": {"file_path": "board.kicad_sch"},
        },
    },
    {
        "type": "copper.tool_result",
        "data": {"tool_call_id": "call-1", "tool_name": "read_file", "result": "(kicad_sch)"},
    },
    {
        "type": "copper.tool_request",
        "data": {"tool_call_id": "call-2", "tool_name": "execute", "arguments": {"command": "ls"}},
    },
    {
        "type": "copper.tool_result",
        "data": {
            "tool_call_id": "call-2",
            "tool_name": "execute",
            "result": {"error": "boom", "exit_code": 1},
        },
    },
    {"type": "copper.status_event", "data": {"kind": "subagent", "id": "w1", "task": "Audit"}},
    {"type": "copper.status_event", "data": {"kind": "other"}},
    {"type": "copper.status_event", "data": "not-a-dict"},
    {"type": "copper.usage", "data": {"tokens": 1}},
    {"type": "copper.final", "data": {"success": True}},
]


class FakeAgent:
    last: FakeAgent | None = None

    def __init__(
        self, workspace, *, credential, conversation_id, mode=None, effort=None, tool_broker=None
    ):
        self.workspace = workspace
        self.credential = credential
        self.conversation_id = conversation_id
        self.mode = mode
        self.effort = effort
        self.tool_broker = tool_broker
        type(self).last = self


class FakeRuntime:
    def __init__(
        self,
        events: list[dict[str, Any]] | None = None,
        *,
        messages: list[Any] | None = None,
        state_values: dict[str, Any] | None = None,
        raise_exc: BaseException | None = None,
        block: bool = False,
    ) -> None:
        self.events = TEXT_EVENTS if events is None else events
        self.messages = messages if messages is not None else [AIMessage(content="Looks good.")]
        self.state_values = state_values
        self.raise_exc = raise_exc
        self.block = block
        self.values: dict[str, Any] | None = None
        self.cancelled = False
        self.started = asyncio.Event()

    def config(self, thread_id, workspace):
        return {"thread_id": thread_id, "workspace": workspace}

    async def astream(self, values, _config):
        self.values = values
        if self.raise_exc is not None:
            raise self.raise_exc
        for event in self.events:
            yield ((), "custom", event)
        yield ((), "updates", {"ignored": True})
        yield ((), "custom", "not a dict")
        self.started.set()
        if self.block:
            await asyncio.Event().wait()

    async def aget_state(self, _config):
        if self.state_values is not None:
            return SimpleNamespace(values=self.state_values)
        return SimpleNamespace(values={"messages": self.messages})

    async def acancel(self):
        self.cancelled = True


class FakeConn:
    def __init__(self, responses: list[Any] | None = None) -> None:
        self.updates: list[tuple[str, Any]] = []
        self.permissions: list[dict[str, Any]] = []
        self.elicitations: list[dict[str, Any]] = []
        self.responses = list(responses or [])

    async def session_update(self, session_id, update):
        self.updates.append((session_id, update))

    async def request_permission(self, session_id, tool_call, options):
        self.permissions.append(
            {"session_id": session_id, "tool_call": tool_call, "options": options}
        )
        return self.responses.pop(0)

    async def create_elicitation(self, message, mode):
        self.elicitations.append({"message": message, "mode": mode})
        return self.responses.pop(0)


def selected(option_id: str) -> RequestPermissionResponse:
    return RequestPermissionResponse(
        outcome=AllowedOutcome(outcome="selected", option_id=option_id)
    )


def cancelled_outcome() -> RequestPermissionResponse:
    return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))


def _patch(
    monkeypatch, runtime: FakeRuntime | None = None, *, credential=CREDENTIAL
) -> FakeRuntime:
    fake_runtime = runtime or FakeRuntime()

    @asynccontextmanager
    async def fake_graph(_agent):
        yield fake_runtime

    monkeypatch.setattr(copper_acp, "load_credential", lambda: credential)
    monkeypatch.setattr(copper_acp, "CopperPilotAgent", FakeAgent)
    monkeypatch.setattr(copper_acp, "create_copper_graph", fake_graph)
    return fake_runtime


def _invalid_status(status: int) -> InvalidStatus:
    return InvalidStatus(Response(status, "nope", Headers()))


async def _connected_agent(tmp_path, monkeypatch, runtime=None, responses=None, **kwargs):
    fake_runtime = _patch(monkeypatch, runtime)
    agent = CopperAcpAgent(**kwargs)
    conn = FakeConn(responses)
    agent.on_connect(conn)
    created = await agent.new_session(cwd=str(tmp_path))
    return agent, conn, fake_runtime, created.session_id


def kinds(conn: FakeConn, *, commands: bool = False) -> list[str]:
    """Update kinds, hiding the one-time commands announcement unless asked."""
    return [
        update.session_update
        for _sid, update in conn.updates
        if commands or update.session_update != "available_commands_update"
    ]


# ----------------------------------------------------------------- helpers


def test_mode_state_lists_all_modes() -> None:
    state = mode_state(CopperMode.PLAN)
    assert state.current_mode_id == "plan"
    assert [mode.id for mode in state.available_modes] == ["ask", "plan", "agent"]


def test_config_options_expose_mode_and_effort() -> None:
    options = config_options(CopperMode.ASK, CopperEffort.INTELLIGENCE)
    by_id = {option.id: option for option in options}
    assert by_id[MODE_CONFIG_ID].current_value == "ask"
    assert by_id[MODE_CONFIG_ID].category == "mode"
    assert by_id[EFFORT_CONFIG_ID].current_value == "intelligence"
    assert [item.value for item in by_id[EFFORT_CONFIG_ID].options] == ["balanced", "intelligence"]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("read_file", "read"),
        ("read", "read"),
        ("bash", "execute"),
        ("write", "edit"),
        ("delete_file", "delete"),
        ("grep", "search"),
        ("ask_user_question", "other"),
        ("mystery", "other"),
    ],
)
def test_tool_kind_maps_canonical_names(name: str, expected: str) -> None:
    assert tool_kind(name) == expected


def test_tool_title_variants() -> None:
    assert tool_title("bash", {"command": " ls -la "}) == "$ ls -la"
    assert tool_title("execute", {}) == "Shell command"
    assert tool_title("write", {"file_path": "a.txt"}) == "write_file a.txt"
    assert tool_title("read_file", {"path": "b.txt"}) == "read_file b.txt"
    assert tool_title("ask_user_question", {"question": "Which MCU?"}) == "Which MCU?"
    assert tool_title("ask_user_question", {}) == "Clarification"
    assert tool_title("glob", {"pattern": "*.py"}) == "glob"


def test_tool_locations_prefers_file_path() -> None:
    assert tool_locations({"file_path": "a", "path": "b"})[0].path == "a"
    assert tool_locations({"path": "b"})[0].path == "b"
    assert tool_locations({"path": ""}) is None
    assert tool_locations({}) is None


def test_prompt_text_joins_text_and_resources() -> None:
    blocks = [
        TextContentBlock(type="text", text="Review"),
        TextContentBlock(type="text", text=""),
        EmbeddedResourceContentBlock(
            type="resource",
            resource=TextResourceContents(uri="file:///a.txt", text="contents"),
        ),
        EmbeddedResourceContentBlock(
            type="resource",
            resource=TextResourceContents(uri="file:///b.txt", text=""),
        ),
        SimpleNamespace(text=None, resource=None),
    ]
    assert prompt_text(blocks) == "Review\n\n[file:///a.txt]\ncontents"
    assert prompt_text([]) == ""


def test_question_options_parsing() -> None:
    assert question_options("not a dict") is None
    assert question_options({"questions": [{"question": "a"}]}) is None
    assert question_options({"question": "Pick", "options": []}) is None
    assert question_options({"question": "Pick", "options": "nope"}) is None
    assert question_options({"question": "Pick", "options": ["a", " ", 3, "b"]}) == (
        "Pick",
        ["a", "b"],
    )
    assert question_options({"options": ["a"]}) == ("Clarification", ["a"])


def test_tool_result_payload_variants() -> None:
    payload = copper_acp._tool_result_payload
    assert payload("text") == ("text", False, "text")
    assert payload({"error": "boom"}) == ("boom", True, {"error": "boom"})
    assert payload({"exit_code": 2, "result": "out"}) == (
        "out",
        True,
        {"exit_code": 2, "result": "out"},
    )
    assert payload({"exit_code": 0, "result": "out"}) == (
        "out",
        False,
        {"exit_code": 0, "result": "out"},
    )
    text, is_error, _raw = payload({"result": {"nested": True}})
    assert text == "{'nested': True}" and not is_error
    long_text, _err, _raw = payload("x" * 5000)
    assert long_text.endswith("[truncated]") and len(long_text) < 5000


def test_replay_updates_projects_history() -> None:
    messages = [
        HumanMessage(content="Review"),
        HumanMessage(content=""),
        AIMessage(
            content="Reading",
            tool_calls=[
                {"id": "c1", "name": "read_file", "args": {"file_path": "a"}, "type": "tool_call"}
            ],
        ),
        ToolMessage(content="data", tool_call_id="c1", name="read_file"),
        ToolMessage(content="", tool_call_id="c2", name="execute", status="error"),
        AIMessage(content=["block"]),
        SimpleNamespace(type="system", content="ignored"),
    ]
    updates = replay_updates(messages)
    assert [update.session_update for update in updates] == [
        "user_message_chunk",
        "agent_message_chunk",
        "tool_call",
        "tool_call_update",
        "tool_call_update",
        "agent_message_chunk",
    ]
    assert updates[2].status == "completed"
    assert updates[3].content[0].content.text == "data"
    assert updates[4].status == "failed" and updates[4].content is None


def test_selected_option_and_coerce_mode() -> None:
    assert copper_acp._selected_option(selected("allow")) == "allow"
    assert copper_acp._selected_option(cancelled_outcome()) is None
    assert copper_acp._selected_option(None) is None
    assert copper_acp._coerce_mode("plan", CopperMode.AGENT) is CopperMode.PLAN
    assert copper_acp._coerce_mode("bogus", CopperMode.AGENT) is CopperMode.AGENT
    assert copper_acp._coerce_mode(None, CopperMode.ASK) is CopperMode.ASK


def test_current_credential_swallows_auth_errors(monkeypatch) -> None:
    from copper_pilot_cli.copper_auth import AuthenticationError

    def broken():
        raise AuthenticationError("bad")

    monkeypatch.setattr(copper_acp, "load_credential", broken)
    assert copper_acp.current_credential() is None


# --------------------------------------------------------------- lifecycle


async def test_initialize_advertises_login_and_load_session() -> None:
    agent = CopperAcpAgent()
    response = await agent.initialize(protocol_version=7)
    assert response.protocol_version == 1
    assert response.agent_capabilities.load_session is True
    assert response.auth_methods[0].id == LOGIN_METHOD_ID
    assert response.auth_methods[0].args == ["auth", "login"]
    assert response.agent_info.name == "copper-pilot"


def test_conn_property_requires_connection() -> None:
    with pytest.raises(RuntimeError):
        _ = CopperAcpAgent().conn


async def test_authenticate_paths(monkeypatch) -> None:
    _patch(monkeypatch)
    agent = CopperAcpAgent()
    assert await agent.authenticate(method_id=LOGIN_METHOD_ID) is not None
    with pytest.raises(RequestError) as exc:
        await agent.authenticate(method_id="other")
    assert exc.value.code == -32602
    monkeypatch.setattr(copper_acp, "load_credential", lambda: None)
    with pytest.raises(RequestError) as exc:
        await agent.authenticate(method_id=LOGIN_METHOD_ID)
    assert exc.value.code == -32000


async def test_new_session_requires_login_and_valid_workspace(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, credential=None)
    agent = CopperAcpAgent()
    with pytest.raises(RequestError) as exc:
        await agent.new_session(cwd=str(tmp_path))
    assert exc.value.code == -32000
    _patch(monkeypatch)
    with pytest.raises(RequestError) as exc:
        await agent.new_session(cwd=str(tmp_path / "missing"))
    assert exc.value.code == -32602


async def test_new_session_binds_workspace_modes_and_broker(tmp_path, monkeypatch) -> None:
    agent, _conn, _runtime, session_id = await _connected_agent(tmp_path, monkeypatch)
    session = agent.sessions[session_id]
    assert session.workspace == tmp_path.resolve()
    assert session.mode is CopperMode.AGENT
    assert session.effort is CopperEffort.BALANCED
    assert session.broker.mode is ApprovalMode.AUTO
    assert session.busy is False


async def test_session_lookup_rejects_unknown_ids(tmp_path, monkeypatch) -> None:
    agent, _conn, _runtime, _session_id = await _connected_agent(tmp_path, monkeypatch)
    with pytest.raises(RequestError):
        await agent.set_session_mode(session_id="nope", mode_id="ask")


async def test_set_session_mode_and_config_options(tmp_path, monkeypatch) -> None:
    agent, _conn, _runtime, session_id = await _connected_agent(tmp_path, monkeypatch)
    await agent.set_session_mode(session_id=session_id, mode_id="plan")
    assert agent.sessions[session_id].mode is CopperMode.PLAN
    with pytest.raises(RequestError):
        await agent.set_session_mode(session_id=session_id, mode_id="bogus")
    response = await agent.set_config_option(
        config_id=MODE_CONFIG_ID, session_id=session_id, value="ask"
    )
    assert agent.sessions[session_id].mode is CopperMode.ASK
    assert response.config_options[0].current_value == "ask"
    response = await agent.set_config_option(
        config_id=EFFORT_CONFIG_ID, session_id=session_id, value="intelligence"
    )
    assert agent.sessions[session_id].effort is CopperEffort.INTELLIGENCE
    assert response.config_options[1].current_value == "intelligence"
    with pytest.raises(RequestError):
        await agent.set_config_option(
            config_id=EFFORT_CONFIG_ID, session_id=session_id, value="max"
        )
    with pytest.raises(RequestError):
        await agent.set_config_option(config_id="model", session_id=session_id, value="x")


async def test_load_session_replays_history_and_restores_options(tmp_path, monkeypatch) -> None:
    runtime = FakeRuntime(
        state_values={
            "messages": [HumanMessage(content="Hi"), AIMessage(content="Hello")],
            "copper_mode": "plan",
            "copper_effort": "intelligence",
        }
    )
    _patch(monkeypatch, runtime)
    agent = CopperAcpAgent()
    conn = FakeConn()
    agent.on_connect(conn)
    response = await agent.load_session(cwd=str(tmp_path), session_id="thread-1")
    assert response.modes.current_mode_id == "plan"
    assert response.config_options[1].current_value == "intelligence"
    assert kinds(conn, commands=True) == [
        "user_message_chunk",
        "agent_message_chunk",
        "available_commands_update",
    ]
    assert agent.sessions["thread-1"].mode is CopperMode.PLAN
    assert FakeAgent.last.conversation_id == "thread-1"


async def test_load_session_unknown_thread_is_not_found(tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, FakeRuntime(state_values={}))
    agent = CopperAcpAgent()
    agent.on_connect(FakeConn())
    with pytest.raises(RequestError) as exc:
        await agent.load_session(cwd=str(tmp_path), session_id="missing")
    assert exc.value.code == -32002


# ------------------------------------------------------------------ prompt


async def test_prompt_streams_all_event_kinds(tmp_path, monkeypatch) -> None:
    agent, conn, runtime, session_id = await _connected_agent(tmp_path, monkeypatch)
    response = await agent.prompt(
        session_id=session_id, prompt=[TextContentBlock(type="text", text="Review")]
    )
    assert response.stop_reason == "end_turn"
    assert runtime.values == {
        "messages": [HumanMessage(content="Review")],
        "copper_mode": "agent",
        "copper_effort": "balanced",
    }
    assert kinds(conn) == [
        "agent_thought_chunk",
        "agent_thought_chunk",
        "agent_message_chunk",
        "agent_message_chunk",
        "tool_call",
        "tool_call_update",
        "tool_call",
        "tool_call_update",
        "tool_call",
    ]
    updates = [update for _sid, update in conn.updates]
    assert updates[0].session_update == "available_commands_update"
    assert [command.name for command in updates[0].available_commands] == [EFFORT_COMMAND]
    updates = updates[1:]
    assert updates[4].kind == "read" and updates[4].locations[0].path == "board.kicad_sch"
    assert updates[5].status == "completed"
    assert updates[6].title == "$ ls" and updates[6].kind == "execute"
    assert updates[7].status == "failed" and updates[7].content[0].content.text == "boom"
    assert updates[8].title == "Survey: Audit" and updates[8].kind == "think"
    assert updates[8].status == "failed" and updates[8].tool_call_id == "subagent:w1"
    assert FakeAgent.last.tool_broker is agent.sessions[session_id].broker


async def test_prompt_rejects_empty_busy_and_logged_out(tmp_path, monkeypatch) -> None:
    agent, _conn, _runtime, session_id = await _connected_agent(tmp_path, monkeypatch)
    with pytest.raises(RequestError) as exc:
        await agent.prompt(session_id=session_id, prompt=[])
    assert exc.value.code == -32602
    session = agent.sessions[session_id]
    session.turn = asyncio.get_running_loop().create_future()
    with pytest.raises(RequestError) as exc:
        await agent.prompt(session_id=session_id, prompt=[TextContentBlock(type="text", text="x")])
    assert exc.value.code == -32600
    session.turn.cancel()
    session.turn = None
    monkeypatch.setattr(copper_acp, "load_credential", lambda: None)
    with pytest.raises(RequestError) as exc:
        await agent.prompt(session_id=session_id, prompt=[TextContentBlock(type="text", text="x")])
    assert exc.value.code == -32000


async def test_prompt_reports_hosted_error_events(tmp_path, monkeypatch) -> None:
    runtime = FakeRuntime(
        [
            {"type": "copper.text", "data": "partial"},
            {"type": "copper.error", "data": {"message": "quota exceeded"}},
        ]
    )
    agent, conn, _runtime, session_id = await _connected_agent(tmp_path, monkeypatch, runtime)
    with pytest.raises(RequestError) as exc:
        await agent.prompt(session_id=session_id, prompt=[TextContentBlock(type="text", text="x")])
    assert exc.value.code == -32603
    assert "quota exceeded" in json.dumps(exc.value.data)
    assert kinds(conn) == ["agent_message_chunk"]
    assert agent.sessions[session_id].busy is False


async def test_prompt_reports_failed_final_event(tmp_path, monkeypatch) -> None:
    runtime = FakeRuntime([{"type": "copper.final", "data": {"success": False, "error": "nope"}}])
    agent, _conn, _runtime, session_id = await _connected_agent(tmp_path, monkeypatch, runtime)
    with pytest.raises(RequestError, match="nope"):
        await agent.prompt(session_id=session_id, prompt=[TextContentBlock(type="text", text="x")])


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ProtocolError("expired", status=401), -32000),
        (ProtocolError("forbidden", status=403), -32000),
        (ProtocolError("malformed"), -32603),
        (OSError("socket"), -32603),
        (RuntimeError("busy"), -32603),
        (_invalid_status(401), -32000),
        (_invalid_status(403), -32000),
        (_invalid_status(503), -32603),
        (KeyError("surprise"), -32603),
    ],
)
async def test_prompt_maps_transport_failures(tmp_path, monkeypatch, error, code) -> None:
    runtime = FakeRuntime(raise_exc=error)
    agent, _conn, _runtime, session_id = await _connected_agent(tmp_path, monkeypatch, runtime)
    with pytest.raises(RequestError) as exc:
        await agent.prompt(session_id=session_id, prompt=[TextContentBlock(type="text", text="x")])
    assert exc.value.code == code


async def test_prompt_failure_details_name_the_exception(tmp_path, monkeypatch, caplog) -> None:
    runtime = FakeRuntime(raise_exc=KeyError("surprise"))
    agent, _conn, _runtime, session_id = await _connected_agent(tmp_path, monkeypatch, runtime)
    with caplog.at_level(logging.ERROR, logger="copper_pilot_cli.copper_acp"):
        with pytest.raises(RequestError) as exc:
            await agent.prompt(
                session_id=session_id, prompt=[TextContentBlock(type="text", text="x")]
            )
    assert str(exc.value) == "KeyError: 'surprise'"
    assert exc.value.data == {"details": "KeyError: 'surprise'"}
    assert "Hosted turn failed with KeyError" in caplog.text


async def test_prompt_refused_handshake_names_the_status(tmp_path, monkeypatch) -> None:
    runtime = FakeRuntime(raise_exc=_invalid_status(503))
    agent, _conn, _runtime, session_id = await _connected_agent(tmp_path, monkeypatch, runtime)
    with pytest.raises(RequestError) as exc:
        await agent.prompt(session_id=session_id, prompt=[TextContentBlock(type="text", text="x")])
    assert str(exc.value) == "The hosted service refused the connection (HTTP 503)."
    assert exc.value.data == {"details": "The hosted service refused the connection (HTTP 503)."}


async def test_prompt_error_message_carries_the_hosted_explanation(tmp_path, monkeypatch) -> None:
    # Reproduces the T3 Code "Internal error" report: T3 shows only the error
    # message, so the hosted plan-gate text must live there, not in `data`.
    hosted = "Upgrade to a paid plan to use Intelligence."
    runtime = FakeRuntime(raise_exc=ProtocolError(hosted, status=402))
    agent, _conn, _runtime, session_id = await _connected_agent(tmp_path, monkeypatch, runtime)
    with pytest.raises(RequestError) as exc:
        await agent.prompt(session_id=session_id, prompt=[TextContentBlock(type="text", text="x")])
    assert exc.value.code == -32603
    assert str(exc.value) == hosted
    assert exc.value.data == {"details": hosted}


async def test_prompt_auth_error_message_carries_the_hosted_explanation(
    tmp_path, monkeypatch
) -> None:
    runtime = FakeRuntime(raise_exc=ProtocolError("Your login expired.", status=401))
    agent, _conn, _runtime, session_id = await _connected_agent(tmp_path, monkeypatch, runtime)
    with pytest.raises(RequestError) as exc:
        await agent.prompt(session_id=session_id, prompt=[TextContentBlock(type="text", text="x")])
    assert (exc.value.code, str(exc.value)) == (-32000, "Your login expired.")


async def test_prompt_switches_to_plan_mode_when_hosted_agent_does(tmp_path, monkeypatch) -> None:
    runtime = FakeRuntime(
        [{"type": "copper.status_event", "data": {"kind": "plan_mode_switch"}}],
        state_values={"messages": [AIMessage(content="plan")], "copper_mode": "plan"},
    )
    agent, conn, _runtime, session_id = await _connected_agent(tmp_path, monkeypatch, runtime)
    await agent.prompt(session_id=session_id, prompt=[TextContentBlock(type="text", text="x")])
    assert agent.sessions[session_id].mode is CopperMode.PLAN
    assert kinds(conn) == ["agent_thought_chunk", "current_mode_update"]
    assert conn.updates[-1][1].current_mode_id == "plan"


async def test_cancel_stops_a_running_turn(tmp_path, monkeypatch) -> None:
    runtime = FakeRuntime([{"type": "copper.text", "data": "partial"}], block=True)
    agent, conn, _runtime, session_id = await _connected_agent(tmp_path, monkeypatch, runtime)
    task = asyncio.create_task(
        agent.prompt(session_id=session_id, prompt=[TextContentBlock(type="text", text="x")])
    )
    await asyncio.wait_for(runtime.started.wait(), 5)
    await agent.cancel(session_id=session_id)
    response = await asyncio.wait_for(task, 5)
    assert response.stop_reason == "cancelled"
    assert runtime.cancelled is True
    assert kinds(conn) == ["agent_message_chunk"]
    assert agent.sessions[session_id].busy is False


async def test_cancel_is_a_noop_for_idle_or_unknown_sessions(tmp_path, monkeypatch) -> None:
    agent, _conn, _runtime, session_id = await _connected_agent(tmp_path, monkeypatch)
    await agent.cancel(session_id="unknown")
    await agent.cancel(session_id=session_id)
    assert agent.sessions[session_id].cancelled is False


async def test_cancel_survives_hosted_cancel_failure(tmp_path, monkeypatch) -> None:
    class ExplodingRuntime(FakeRuntime):
        async def acancel(self):
            raise OSError("socket closed")

    runtime = ExplodingRuntime([{"type": "copper.text", "data": "partial"}], block=True)
    agent, _conn, _runtime, session_id = await _connected_agent(tmp_path, monkeypatch, runtime)
    task = asyncio.create_task(
        agent.prompt(session_id=session_id, prompt=[TextContentBlock(type="text", text="x")])
    )
    await asyncio.wait_for(runtime.started.wait(), 5)
    await agent.cancel(session_id=session_id)
    assert (await asyncio.wait_for(task, 5)).stop_reason == "cancelled"


async def test_external_cancellation_propagates(tmp_path, monkeypatch) -> None:
    runtime = FakeRuntime([{"type": "copper.text", "data": "partial"}], block=True)
    agent, _conn, _runtime, session_id = await _connected_agent(tmp_path, monkeypatch, runtime)
    task = asyncio.create_task(
        agent.prompt(session_id=session_id, prompt=[TextContentBlock(type="text", text="x")])
    )
    await asyncio.wait_for(runtime.started.wait(), 5)
    agent.sessions[session_id].turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------- broker handlers


def _request() -> ApprovalRequest:
    return ApprovalRequest(
        tool_call_id="call-9",
        tool_name="write_file",
        arguments={"file_path": "x.txt", "content": "hi"},
        reason="Local side effect",
        canonical_name="write_file",
    )


async def test_approval_handler_outcomes(tmp_path, monkeypatch) -> None:
    responses = [
        selected(OPTION_ALLOW),
        selected(OPTION_REJECT),
        cancelled_outcome(),
        selected(OPTION_ALLOW_SESSION),
    ]
    agent, conn, _runtime, session_id = await _connected_agent(
        tmp_path, monkeypatch, responses=responses
    )
    broker = agent.sessions[session_id].broker
    assert await broker.approve(_request()) is True
    assert await broker.approve(_request()) is False
    assert await broker.approve(_request()) is False
    assert broker.mode is ApprovalMode.AUTO
    assert await broker.approve(_request()) is True
    assert broker.mode is ApprovalMode.YOLO
    request = conn.permissions[0]
    assert request["session_id"] == session_id
    assert request["tool_call"].tool_call_id == "call-9"
    assert request["tool_call"].kind == "edit"
    assert request["tool_call"].locations[0].path == "x.txt"
    assert [option.kind for option in request["options"]] == [
        "allow_once",
        "allow_always",
        "reject_once",
    ]


async def test_question_handler_maps_options_onto_permissions(tmp_path, monkeypatch) -> None:
    responses = [
        selected("option:1"),
        selected(OPTION_SKIP),
        cancelled_outcome(),
        selected("option:9"),
    ]
    agent, conn, _runtime, session_id = await _connected_agent(
        tmp_path, monkeypatch, responses=responses
    )
    broker = agent.sessions[session_id].broker
    arguments = {"question": "Which MCU?", "options": ["ESP32", "STM32"]}
    assert await broker.ask_user(arguments) == {
        "status": "answered",
        "answer": "STM32",
        "source": "option",
    }
    assert await broker.ask_user(arguments) == {"status": "skipped"}
    assert await broker.ask_user(arguments) == {"status": "skipped"}
    assert await broker.ask_user(arguments) == {"status": "skipped"}
    request = conn.permissions[0]
    assert request["tool_call"].title == "Which MCU?"
    assert [option.name for option in request["options"]] == ["ESP32", "STM32", "Skip"]
    with pytest.raises(ToolRejected, match=QUESTION_MESSAGE[:20]):
        await broker.ask_user({"question": "Free text?"})


# --------------------------------------------------------------- entrypoint


def test_run_acp_runs_the_sdk_agent(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_run_agent(agent):
        captured["agent"] = agent

    monkeypatch.setattr(copper_acp, "run_agent", fake_run_agent)
    run_acp(ApprovalMode.MANUAL)
    assert isinstance(captured["agent"], CopperAcpAgent)
    assert captured["agent"].approval is ApprovalMode.MANUAL


async def test_stdio_server_round_trip(tmp_path) -> None:
    """Drive the real `copper-pilot acp` process over JSON-RPC on stdio."""
    home = tmp_path / "home"
    env = {**os.environ, "COPPER_PILOT_CLI_HOME": str(home)}
    (home / ".state").mkdir(parents=True)
    from copper_pilot_cli.copper_config import paths

    os.environ["COPPER_PILOT_CLI_HOME"] = str(home)
    try:
        save_credential(CREDENTIAL, paths().auth)
    finally:
        os.environ.pop("COPPER_PILOT_CLI_HOME", None)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "copper_pilot_cli",
        "acp",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

    async def rpc(request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        frame = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        proc.stdin.write((json.dumps(frame) + "\n").encode())
        await proc.stdin.drain()
        line = await asyncio.wait_for(proc.stdout.readline(), 30)
        return json.loads(line)

    try:
        init = await rpc(1, "initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        assert init["result"]["agentCapabilities"]["loadSession"] is True
        assert init["result"]["authMethods"][0]["id"] == LOGIN_METHOD_ID
        created = await rpc(2, "session/new", {"cwd": str(tmp_path), "mcpServers": []})
        session_id = created["result"]["sessionId"]
        assert created["result"]["modes"]["currentModeId"] == "agent"
        assert [option["id"] for option in created["result"]["configOptions"]] == ["mode", "effort"]
        mode = await rpc(3, "session/set_mode", {"sessionId": session_id, "modeId": "ask"})
        assert "error" not in mode
        bad = await rpc(4, "session/new", {"cwd": str(tmp_path / "missing"), "mcpServers": []})
        assert bad["error"]["code"] == -32602
        missing = await rpc(
            5, "session/load", {"cwd": str(tmp_path), "sessionId": "nope", "mcpServers": []}
        )
        assert missing["error"]["code"] == -32002
    finally:
        proc.stdin.close()
        await asyncio.wait_for(proc.wait(), 10)
    assert proc.returncode == 0
    assert b"Traceback" not in await proc.stderr.read()


def test_acp_session_dataclass_defaults(tmp_path) -> None:
    from copper_pilot_cli.copper_tools import LocalToolBroker

    session = AcpSession(
        session_id="s",
        workspace=tmp_path,
        mode=CopperMode.ASK,
        effort=CopperEffort.BALANCED,
        broker=LocalToolBroker(tmp_path),
    )
    assert session.busy is False
    assert session.subagents == {}
    assert session.tool_paths == {}
    assert session.last_usage is None
    assert session.commands_sent is False


# ---------------------------------------------------------- usage and plans


def test_effort_command_parsing() -> None:
    assert effort_command("/effort") == (True, None)
    assert effort_command("  /effort intelligence ") == (True, "intelligence")
    assert effort_command("/Effort balanced") == (True, "balanced")
    assert effort_command("/effort a b") == (False, None)
    assert effort_command("/efforts") == (False, None)
    assert effort_command("Set /effort") == (False, None)
    wrapped = (
        "<t3_code_instructions>\nUse tools.\n</t3_code_instructions>\n\n"
        "<user_request>\n/effort intelligence\n</user_request>"
    )
    assert effort_command(wrapped) == (True, "intelligence")
    assert user_request_text(wrapped) == "/effort intelligence"
    assert user_request_text("plain") == "plain"
    blocks = [
        TextContentBlock(type="text", text="  "),
        TextContentBlock(type="text", text="/effort"),
        TextContentBlock(type="text", text="<runtime_info>T3 Code</runtime_info>"),
    ]
    assert first_text_block(blocks) == "/effort"
    assert first_text_block([]) == ""


def test_available_commands_advertise_effort() -> None:
    (command,) = available_commands()
    assert command.name == EFFORT_COMMAND
    assert command.input.root.hint == "balanced | intelligence"


def test_usage_update_maps_prompt_tokens() -> None:
    update = usage_update({"tokens_input": 120, "tokens_output": 24, "cache_tokens": 8})
    assert update is not None
    assert (update.used, update.size) == (120, 0)
    assert update.field_meta == {"tokens_input": 120, "tokens_output": 24, "cache_tokens": 8}
    assert usage_update({"input_tokens": 7}).used == 7
    assert usage_update({"tokens": 1}) is None
    assert usage_update({"tokens_input": -1}) is None
    assert usage_update({"tokens_input": True}) is None
    assert usage_update({"tokens_input": "12"}) is None
    assert usage_update("usage") is None


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (".copperpilot/plans/board.plan.md", True),
        ("/abs/ws/.copperpilot/plans/nested-name.plan.md", True),
        (".copperpilot\\plans\\win.plan.md", True),
        (".copperpilot/plans/notes.md", False),
        ("docs/.copperpilot/plans/x.plan.md", True),
        ("plans/x.plan.md", False),
        ("", False),
    ],
)
def test_is_plan_artifact(path: str, expected: bool) -> None:
    assert is_plan_artifact(path) is expected


def test_plan_entries_from_todo_list_skips_junk() -> None:
    entries = plan_entries_from_todo_list(
        {
            "kind": "todo_list",
            "items": [
                {"index": 0, "content": "Check BOM", "status": "pending"},
                {"index": 1, "content": "Route power", "status": "done"},
                {"index": 2, "content": "  ", "status": "done"},
                "junk",
            ],
        }
    )
    assert [(entry.content, entry.status) for entry in entries] == [
        ("Check BOM", "pending"),
        ("Route power", "completed"),
    ]
    assert plan_entries_from_todo_list({"kind": "todo_list"}) == []


def test_subagent_tool_update_variants() -> None:
    running = SubagentStatus(id="w1", task="Audit power", goal="Find rail issues", status="running")
    start = subagent_tool_update(running, started=False)
    assert start.session_update == "tool_call"
    assert (start.tool_call_id, start.title, start.kind) == (
        "subagent:w1",
        "Survey: Audit power",
        "think",
    )
    assert start.status == "in_progress" and start.content is None
    assert start.raw_input == {"task": "Audit power", "goal": "Find rail issues"}
    done = subagent_tool_update(
        SubagentStatus(id="w1", task="Audit power", status="success", output="x" * 5000),
        started=True,
    )
    assert done.session_update == "tool_call_update" and done.status == "completed"
    assert len(done.content[0].content.text) == 4000
    failed = subagent_tool_update(
        SubagentStatus(id="w2", task="", status="cancelled", error="stopped"), started=False
    )
    assert failed.title == "Survey: Survey" and failed.status == "failed"
    assert failed.content[0].content.text == "stopped"


async def test_prompt_emits_usage_updates_once_per_distinct_payload(tmp_path, monkeypatch) -> None:
    runtime = FakeRuntime(
        [
            {"type": "copper.usage", "data": {"tokens_input": 10, "tokens_output": 2}},
            {"type": "copper.usage", "data": {"tokens_input": 10, "tokens_output": 2}},
            {"type": "copper.usage", "data": {"tokens_input": 30, "tokens_output": 9}},
            {"type": "copper.usage", "data": {"tokens": 1}},
            {"type": "copper.usage", "data": "nope"},
        ]
    )
    agent, conn, _runtime, session_id = await _connected_agent(tmp_path, monkeypatch, runtime)
    await agent.prompt(session_id=session_id, prompt=[TextContentBlock(type="text", text="x")])
    assert kinds(conn) == ["usage_update", "usage_update"]
    assert [update.used for _sid, update in conn.updates[1:]] == [10, 30]
    assert agent.sessions[session_id].last_usage == {"tokens_input": 30, "tokens_output": 9}


async def test_prompt_turns_todo_list_events_into_plans(tmp_path, monkeypatch) -> None:
    runtime = FakeRuntime(
        [
            {
                "type": "copper.status_event",
                "data": {
                    "kind": "todo_list",
                    "items": [
                        {"index": 0, "content": "Check BOM", "status": "pending"},
                        {"index": 1, "content": "Route power", "status": "done"},
                    ],
                },
            },
            {"type": "copper.status_event", "data": {"kind": "todo_list", "items": []}},
        ]
    )
    agent, conn, _runtime, session_id = await _connected_agent(tmp_path, monkeypatch, runtime)
    await agent.prompt(session_id=session_id, prompt=[TextContentBlock(type="text", text="x")])
    assert kinds(conn) == ["plan"]
    plan = conn.updates[-1][1]
    assert [(entry.content, entry.status, entry.priority) for entry in plan.entries] == [
        ("Check BOM", "pending", "medium"),
        ("Route power", "completed", "medium"),
    ]


PLAN_FILE = """---
name: CM5 camera
overview: Carrier board for the CM5 camera module.
overall_status: running
todos:
  - id: t1
    content: Pick the MIPI connector
    status: done
  - id: t2
    content: Route the CSI lanes
    status: running
  - id: t3
    content: Add decoupling
    status: open
---
# Plan body
"""


async def test_prompt_publishes_plan_files_written_by_the_agent(tmp_path, monkeypatch) -> None:
    plan_path = tmp_path / ".copperpilot" / "plans" / "cm5.plan.md"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(PLAN_FILE, encoding="utf-8")
    relative = ".copperpilot/plans/cm5.plan.md"
    runtime = FakeRuntime(
        [
            {
                "type": "copper.tool_request",
                "data": {
                    "tool_call_id": "w1",
                    "tool_name": "write_file",
                    "arguments": {"file_path": relative, "content": PLAN_FILE},
                },
            },
            {
                "type": "copper.tool_result",
                "data": {"tool_call_id": "w1", "tool_name": "write_file", "result": "ok"},
            },
            {
                "type": "copper.tool_request",
                "data": {
                    "tool_call_id": "w2",
                    "tool_name": "edit",
                    "arguments": {"file_path": relative, "old_string": "a", "new_string": "b"},
                },
            },
            {
                "type": "copper.tool_result",
                "data": {"tool_call_id": "w2", "tool_name": "edit", "result": {"error": "no"}},
            },
            {
                "type": "copper.tool_request",
                "data": {
                    "tool_call_id": "w3",
                    "tool_name": "write_file",
                    "arguments": {"file_path": "notes/todo.md", "content": "x"},
                },
            },
            {
                "type": "copper.tool_result",
                "data": {"tool_call_id": "w3", "tool_name": "write_file", "result": "ok"},
            },
            {
                "type": "copper.tool_request",
                "data": {
                    "tool_call_id": "w4",
                    "tool_name": "write_file",
                    "arguments": {"file_path": ".copperpilot/plans/missing.plan.md"},
                },
            },
            {
                "type": "copper.tool_result",
                "data": {"tool_call_id": "w4", "tool_name": "write_file", "result": "ok"},
            },
        ]
    )
    agent, conn, _runtime, session_id = await _connected_agent(tmp_path, monkeypatch, runtime)
    await agent.prompt(session_id=session_id, prompt=[TextContentBlock(type="text", text="x")])
    assert kinds(conn) == [
        "tool_call",
        "tool_call_update",
        "plan",
        "tool_call",
        "tool_call_update",
        "tool_call",
        "tool_call_update",
        "tool_call",
        "tool_call_update",
    ]
    plan = conn.updates[3][1]
    assert [(entry.content, entry.status) for entry in plan.entries] == [
        ("Pick the MIPI connector", "completed"),
        ("Route the CSI lanes", "in_progress"),
        ("Add decoupling", "pending"),
    ]
    assert agent.sessions[session_id].tool_paths == {}


async def test_prompt_renders_subagents_as_tool_calls(tmp_path, monkeypatch) -> None:
    runtime = FakeRuntime(
        [
            {
                "type": "copper.status_event",
                "data": {"kind": "subagent", "id": "w1", "task": "Audit", "status": "running"},
            },
            {"type": "copper.status_event", "data": {"kind": "subagent", "status": "keepalive"}},
            {
                "type": "copper.status_event",
                "data": {"kind": "subagent", "id": "w1", "status": "success", "output": "Fine."},
            },
            {
                "type": "copper.status_event",
                "data": {"kind": "subagent", "id": "w2", "status": "failed", "error": "boom"},
            },
        ]
    )
    agent, conn, _runtime, session_id = await _connected_agent(tmp_path, monkeypatch, runtime)
    await agent.prompt(session_id=session_id, prompt=[TextContentBlock(type="text", text="x")])
    assert kinds(conn) == ["tool_call", "tool_call_update", "tool_call"]
    updates = [update for _sid, update in conn.updates[1:]]
    assert updates[0].tool_call_id == "subagent:w1" and updates[0].status == "in_progress"
    assert updates[1].tool_call_id == "subagent:w1" and updates[1].status == "completed"
    assert updates[1].content[0].content.text == "Fine."
    assert updates[2].tool_call_id == "subagent:w2" and updates[2].status == "failed"
    assert updates[2].content[0].content.text == "boom"


# ------------------------------------------------------------ slash commands


async def test_effort_command_runs_locally(tmp_path, monkeypatch) -> None:
    agent, conn, runtime, session_id = await _connected_agent(tmp_path, monkeypatch)
    text = TextContentBlock(type="text", text="/effort intelligence")
    response = await agent.prompt(session_id=session_id, prompt=[text])
    assert response.stop_reason == "end_turn"
    assert runtime.values is None
    assert agent.sessions[session_id].effort is CopperEffort.INTELLIGENCE
    assert kinds(conn, commands=True) == [
        "available_commands_update",
        "config_option_update",
        "agent_message_chunk",
    ]
    options = {option.id: option for option in conn.updates[1][1].config_options}
    assert options[EFFORT_CONFIG_ID].current_value == "intelligence"
    assert conn.updates[2][1].content.text == "Effort set to Intelligence."

    conn.updates.clear()
    await agent.prompt(
        session_id=session_id, prompt=[TextContentBlock(type="text", text="/effort")]
    )
    assert kinds(conn, commands=True) == ["agent_message_chunk"]
    assert conn.updates[0][1].content.text.startswith("Effort is Intelligence.")

    conn.updates.clear()
    await agent.prompt(
        session_id=session_id, prompt=[TextContentBlock(type="text", text="/effort turbo")]
    )
    assert kinds(conn) == ["agent_message_chunk"]
    assert "Unknown effort 'turbo'" in conn.updates[0][1].content.text
    assert agent.sessions[session_id].effort is CopperEffort.INTELLIGENCE

    await agent.prompt(session_id=session_id, prompt=[TextContentBlock(type="text", text="Go")])
    assert runtime.values["copper_effort"] == "intelligence"

    # T3 Code appends runtime context as a second text block; the command still wins.
    conn.updates.clear()
    await agent.prompt(
        session_id=session_id,
        prompt=[
            TextContentBlock(type="text", text="/effort balanced"),
            TextContentBlock(type="text", text="<runtime_info>T3 Code</runtime_info>"),
        ],
    )
    assert agent.sessions[session_id].effort is CopperEffort.BALANCED
    assert kinds(conn) == ["config_option_update", "agent_message_chunk"]
    assert runtime.values["copper_effort"] == "intelligence"


# ---------------------------------------------------------------- elicitation


async def test_initialize_records_form_elicitation_support() -> None:
    agent = CopperAcpAgent()
    assert agent.elicitation_form is False
    await agent.initialize(protocol_version=1, client_capabilities={"elicitation": {"form": {}}})
    assert agent.elicitation_form is True
    await agent.initialize(protocol_version=1, client_capabilities={"elicitation": {"url": {}}})
    assert agent.elicitation_form is False
    await agent.initialize(
        protocol_version=1,
        client_capabilities=SimpleNamespace(elicitation=SimpleNamespace(form=SimpleNamespace())),
    )
    assert agent.elicitation_form is True
    await agent.initialize(
        protocol_version=1, client_capabilities=SimpleNamespace(elicitation=None)
    )
    assert agent.elicitation_form is False


def test_elicitation_questions_and_schema() -> None:
    assert elicitation_questions({"question": " Which MCU? "}) == [("answer", "Which MCU?")]
    assert elicitation_questions({"prompt": ""}) == [("answer", "Clarification")]
    assert elicitation_questions("raw text") == [("answer", "raw text")]
    assert elicitation_questions({"questions": ["junk"]}) is None
    rows = elicitation_questions(
        {"questions": [{"question": "Supply voltage?"}, {"prompt": ""}, "junk"]}
    )
    assert rows == [("q0", "Supply voltage?"), ("q1", "Question 2")]
    single = elicitation_schema([("answer", "Which MCU?")])
    assert single.properties["answer"].title == "Answer"
    assert single.properties["answer"].description == "Which MCU?"
    multi = elicitation_schema(rows)
    assert [prop.title for prop in multi.properties.values()] == ["Question 1", "Question 2"]
    assert all(prop.enum is None for prop in multi.properties.values())
    choice = elicitation_schema([("answer", "MCU?")], options=["ESP32", "STM32"])
    assert choice.properties["answer"].enum == ["ESP32", "STM32"]
    assert elicitation_schema(rows, options=["x"]).properties["q0"].enum is None


async def test_question_handler_uses_elicitation_for_free_text(tmp_path, monkeypatch) -> None:
    responses = [
        AcceptElicitationResponse(action="accept", content={"answer": " 3V3 rail "}),
        DeclineElicitationResponse(action="decline"),
        AcceptElicitationResponse(action="accept", content={"answer": "   "}),
        AcceptElicitationResponse(action="accept", content={"q0": "5V", "q1": 12}),
        CancelElicitationResponse(action="cancel"),
    ]
    agent, conn, _runtime, session_id = await _connected_agent(
        tmp_path, monkeypatch, responses=responses
    )
    agent.elicitation_form = True
    broker = agent.sessions[session_id].broker
    assert await broker.ask_user({"question": "Which rail?"}) == {
        "status": "answered",
        "answer": "3V3 rail",
        "source": "custom",
    }
    assert await broker.ask_user({"question": "Which rail?"}) == {"status": "skipped"}
    assert await broker.ask_user({"question": "Which rail?"}) == {"status": "skipped"}
    questions = {"questions": [{"question": "Input voltage?"}, {"question": "Current?"}]}
    assert await broker.ask_user(questions) == {
        "answers": [
            {"question": "Input voltage?", "answer": "5V"},
            {"question": "Current?", "answer": "12"},
        ]
    }
    assert await broker.ask_user(questions) == {
        "answers": [
            {"question": "Input voltage?", "answer": ""},
            {"question": "Current?", "answer": ""},
        ]
    }
    first = conn.elicitations[0]
    assert first["message"] == "Which rail?"
    assert first["mode"].session_id == session_id
    assert list(first["mode"].requested_schema.properties) == ["answer"]
    assert conn.elicitations[3]["message"] == "CopperPilot needs a few answers to continue."
    assert list(conn.elicitations[3]["mode"].requested_schema.properties) == ["q0", "q1"]
    assert conn.permissions == []
    # Option questions become enum forms too: permission prompts are auto-answered
    # by hands-off clients, a form is not.
    conn.responses.extend(
        [
            AcceptElicitationResponse(action="accept", content={"answer": "STM32"}),
            AcceptElicitationResponse(action="accept", content={"answer": "RP2040"}),
            DeclineElicitationResponse(action="decline"),
        ]
    )
    arguments = {"question": "MCU?", "options": ["ESP32", "STM32"]}
    assert await broker.ask_user(arguments) == {
        "status": "answered",
        "answer": "STM32",
        "source": "option",
    }
    assert await broker.ask_user(arguments) == {
        "status": "answered",
        "answer": "RP2040",
        "source": "custom",
    }
    assert await broker.ask_user(arguments) == {"status": "skipped"}
    assert conn.elicitations[-1]["mode"].requested_schema.properties["answer"].enum == [
        "ESP32",
        "STM32",
    ]
    assert conn.permissions == []


async def test_question_handler_without_forms_still_declines_free_text(
    tmp_path, monkeypatch
) -> None:
    agent, _conn, _runtime, session_id = await _connected_agent(tmp_path, monkeypatch)
    agent.elicitation_form = True
    with pytest.raises(ToolRejected, match=QUESTION_MESSAGE[:20]):
        await agent.sessions[session_id].broker.ask_user({"questions": ["junk"]})


async def test_questions_skip_when_no_answer_arrives_in_time(tmp_path, monkeypatch) -> None:
    agent, conn, _runtime, session_id = await _connected_agent(tmp_path, monkeypatch)
    monkeypatch.setattr(copper_acp, "QUESTION_TIMEOUT_SECONDS", 0.01)

    async def never(*_args, **_kwargs):
        await asyncio.Event().wait()

    conn.request_permission = never  # type: ignore[method-assign]
    conn.create_elicitation = never  # type: ignore[method-assign]
    broker = agent.sessions[session_id].broker
    expected = {"status": "skipped", "reason": QUESTION_TIMEOUT_MESSAGE}
    assert await broker.ask_user({"question": "MCU?", "options": ["ESP32"]}) == expected
    agent.elicitation_form = True
    assert await broker.ask_user({"question": "Voltage?"}) == expected
    assert await broker.ask_user({"question": "MCU?", "options": ["ESP32"]}) == expected


# ------------------------------------------------------------- durability


async def test_prompt_surfaces_retry_status_as_thoughts(tmp_path, monkeypatch) -> None:
    events = [
        {"type": "copper.text", "data": "partial"},
        {
            "type": "copper.retry",
            "data": {"stage": "scheduled", "attempt": 2, "will_retry": True},
            "title": "Retrying (1/2) in 5s: close code 1011",
        },
        {"type": "copper.text", "data": " done"},
        {"type": "copper.final", "data": {"success": True}},
    ]
    agent, conn, _runtime, session_id = await _connected_agent(
        tmp_path, monkeypatch, FakeRuntime(events)
    )
    response = await agent.prompt(
        session_id=session_id, prompt=[TextContentBlock(type="text", text="Review")]
    )
    assert response.stop_reason == "end_turn"
    assert kinds(conn) == ["agent_message_chunk", "agent_thought_chunk", "agent_message_chunk"]
    # updates[0] is the one-time available_commands_update.
    assert conn.updates[2][1].content.text == "Retrying (1/2) in 5s: close code 1011\n"


async def test_prompt_structured_error_event_keeps_auth_code(tmp_path, monkeypatch) -> None:
    events = [
        {
            "type": "copper.error",
            "data": {"message": "Session expired.", "status": 401, "code": "http_401"},
        }
    ]
    agent, _conn, _runtime, session_id = await _connected_agent(
        tmp_path, monkeypatch, FakeRuntime(events)
    )
    with pytest.raises(RequestError) as caught:
        await agent.prompt(
            session_id=session_id, prompt=[TextContentBlock(type="text", text="Review")]
        )
    assert caught.value.code == -32000
    assert caught.value.data == {"details": "Session expired."}


async def test_cancel_wins_when_the_socket_closes_first(tmp_path, monkeypatch) -> None:
    class RaceRuntime(FakeRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.release = asyncio.Event()
            self.turn: asyncio.Task[Any] | None = None

        async def astream(self, values, _config):
            self.values = values
            self.started.set()
            await self.release.wait()
            raise ProtocolError("Chat socket closed (close code 1011)", code="STREAM_ENDED")
            yield  # pragma: no cover - makes this an async generator

        async def acancel(self):
            self.cancelled = True
            self.release.set()
            assert self.turn is not None
            for _ in range(20):
                if self.turn.done():
                    break
                await asyncio.sleep(0)

    runtime = RaceRuntime()
    agent, _conn, _runtime, session_id = await _connected_agent(tmp_path, monkeypatch, runtime)
    task = asyncio.create_task(
        agent.prompt(session_id=session_id, prompt=[TextContentBlock(type="text", text="Go")])
    )
    await runtime.started.wait()
    runtime.turn = agent.sessions[session_id].turn
    await agent.cancel(session_id=session_id)
    response = await task
    assert response.stop_reason == "cancelled"
    assert runtime.cancelled
