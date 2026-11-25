"""Tool session socket, CAS writes, the CopperPilot MCP server, relay and subagent."""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from byo_fakes import CANARY, GUIDE, tools_server
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from copper_pilot_cli.agents.mcp_server import (
    RELAY_TOKEN_ENV,
    ProxyBackend,
    RelayServer,
    build_mcp_server,
)
from copper_pilot_cli.agents.runtime import BridgeBroker
from copper_pilot_cli.agents.tool_proxy import (
    CORE_TOOLS,
    GUIDE_URI,
    SUBAGENT_DESCRIPTION,
    SUBAGENT_TOOL,
    ToolOutcome,
    ToolProxy,
    changed_files,
    subagent_text,
)
from copper_pilot_cli.agents.tools_session import (
    CAPABILITIES,
    TURN_END_CAPABILITY,
    ToolSessionClient,
    ToolSessionError,
    detect_eda,
    tools_ws_url,
)
from copper_pilot_cli.copper_protocol import CopperEvent, CopperMode, EventKind, ToolRequest
from copper_pilot_cli.copper_tools import (
    ApprovalMode,
    LocalToolBroker,
    ToolRejected,
    routine_action,
)
from copper_pilot_cli.deepagents_tools import FILE_CHANGED_CODE, file_sha256


def _workspace(tmp_path: Path) -> Path:
    (tmp_path / "board.kicad_sch").write_bytes(b"(kicad_sch (version 20250114))\n")
    (tmp_path / "board.kicad_pcb").write_bytes(b"(kicad_pcb)\n")
    (tmp_path / "notes.txt").write_bytes(b"hello\n")
    return tmp_path


def _client(fake: Any, workspace: Path, broker: LocalToolBroker) -> ToolSessionClient:
    return ToolSessionClient(
        fake.credential,
        workspace=workspace,
        conversation_id="conv-1",
        executor=broker.execute,
        agent={"provider": "claude", "model": "claude-haiku-5-5"},
        reconnect_window=10,
    )


# ------------------------------------------------------------------ broker


def test_kicad_writes_are_routine_only_for_subagent_turns(tmp_path: Path) -> None:
    arguments = {"file_path": "board.kicad_sch"}
    assert routine_action("write_file", arguments, tmp_path) is False
    assert routine_action("write_file", arguments, tmp_path, kicad_writes=True) is True
    for suffix in (".kicad_pcb", ".kicad_sym", ".kicad_pro"):
        assert routine_action("edit_file", {"file_path": f"x{suffix}"}, tmp_path, kicad_writes=True)
    # Still never outside the workspace or into sensitive folders.
    assert not routine_action(
        "write_file", {"file_path": "/etc/x.kicad_sch"}, tmp_path, kicad_writes=True
    )
    assert not routine_action(
        "write_file", {"file_path": ".git/x.kicad_sch"}, tmp_path, kicad_writes=True
    )


async def test_broker_auto_subagent_writes_kicad_without_asking(tmp_path: Path) -> None:
    asked: list[Any] = []

    async def approve(request: Any) -> bool:
        asked.append(request)
        return False

    plain = LocalToolBroker(tmp_path, mode=ApprovalMode.AUTO, approve=approve)
    with pytest.raises(ToolRejected):
        await plain.execute(
            ToolRequest(
                tool_call_id="w1",
                tool_name="write_file",
                arguments={"file_path": "a.kicad_sch", "content": "(kicad_sch)"},
            )
        )
    assert asked
    asked.clear()
    subagent = LocalToolBroker(
        tmp_path, mode=ApprovalMode.AUTO, approve=approve, routine_kicad_writes=True
    )
    result = await subagent.execute(
        ToolRequest(
            tool_call_id="w2",
            tool_name="write_file",
            arguments={"file_path": "b.kicad_sch", "content": "(kicad_sch)"},
        )
    )
    assert not asked
    assert (tmp_path / "b.kicad_sch").read_text() == "(kicad_sch)"
    assert "error" not in result


async def test_cas_reads_return_sha_and_writes_honour_if_match(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    broker = LocalToolBroker(workspace, mode=ApprovalMode.YOLO, cas_writes=True)
    read = await broker.execute(
        ToolRequest(
            tool_call_id="r1",
            tool_name="read_file",
            arguments={"file_path": "notes.txt", "purpose": "server_internal_text"},
        )
    )
    digest = hashlib.sha256(b"hello\n").hexdigest()
    assert read["content_sha256"] == digest
    (workspace / "notes.txt").write_bytes(b"changed by the user\n")
    refused = await broker.execute(
        ToolRequest(
            tool_call_id="w1",
            tool_name="edit_file",
            arguments={"file_path": "notes.txt", "old_string": "hello", "new_string": "hi"},
            if_match=digest,
        )
    )
    assert refused["code"] == FILE_CHANGED_CODE
    assert "changed since it was read" in refused["error"]
    assert (workspace / "notes.txt").read_text() == "changed by the user\n"
    current = file_sha256(workspace / "notes.txt")
    ok = await broker.execute(
        ToolRequest(
            tool_call_id="w2",
            tool_name="edit_file",
            arguments={"file_path": "notes.txt", "old_string": "changed", "new_string": "edited"},
            if_match=current,
        )
    )
    assert "error" not in ok
    assert (workspace / "notes.txt").read_text() == "edited by the user\n"
    assert ok["content_sha256"] == hashlib.sha256(b"edited by the user\n").hexdigest()


async def test_cas_can_be_turned_off(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    broker = LocalToolBroker(workspace, mode=ApprovalMode.YOLO, cas_writes=False)
    read = await broker.execute(
        ToolRequest(
            tool_call_id="r1",
            tool_name="read_file",
            arguments={"file_path": "notes.txt", "purpose": "server_internal_text"},
        )
    )
    assert "content_sha256" not in read


# ------------------------------------------------------------ tool socket


def test_tools_ws_url_and_eda(tmp_path: Path) -> None:
    assert tools_ws_url("https://copperpilot.ai") == "wss://copperpilot.ai/api/copperpilot/tools/ws"
    assert tools_ws_url("http://localhost:80") == "ws://localhost:80/api/copperpilot/tools/ws"
    assert detect_eda(tmp_path, "a.kicad_sch", None) == "kicad"
    assert detect_eda(tmp_path, None, "b.PcbDoc") == "altium"
    assert detect_eda(tmp_path, None, None) is None


async def test_tool_session_handshake_call_and_bridge(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    broker = LocalToolBroker(workspace, mode=ApprovalMode.AUTO, cas_writes=True)
    async with tools_server() as fake:
        fake.bridge_read = "notes.txt"
        client = _client(fake, workspace, broker)
        progress: list[str] = []
        client.on_progress = lambda _call, message: progress.append(message)
        try:
            info = await client.start()
            assert [tool["name"] for tool in info.tools] == [
                "query_design",
                "run_erc",
                "place_symbol",
            ]
            assert client.guide == GUIDE
            result = await client.call_tool("query_design", {"query": "U1"})
            await client.send_usage(
                provider="claude", model="m", input_tokens=10, output_tokens=2, turn_id="t1"
            )
            await asyncio.sleep(0.1)
        finally:
            await client.aclose()
        start = fake.starts[0]
        assert start["type"] == "tool_session_start"
        assert start["protocol_version"] == 1
        assert start["conversation_id"] == "conv-1"
        assert start["workspace_path"] == str(workspace.resolve())
        assert start["eda"] == "kicad"
        assert start["schematic_path"].endswith("board.kicad_sch")
        assert "notes.txt" in start["initial_file_tree"]
        assert start["agent"]["provider"] == "claude" and start["agent"]["client"] == "cli"
        assert (
            set(CAPABILITIES)
            == set(start["capabilities"])
            == {
                "parallel_tool_requests",
                "cas_writes_v1",
            }
        )
        assert fake.headers[0]["authorization"] == "Bearer device-key"
        assert fake.headers[0]["x-copperpilot-fingerprint"] == "fp"
        assert result["ok"] is True
        bridge = fake.bridge_results[0]
        assert bridge["conversation_id"] == "conv-1"
        assert "hello" in bridge["result"]["result"]
        assert bridge["content_sha256"] == hashlib.sha256(b"hello\n").hexdigest()
        assert "content_sha256" not in bridge["result"]
        assert progress == ["50%"]
        usage = fake.of_type("agent_usage")[0]
        assert usage["provider"] == "claude" and usage["input_tokens"] == 10


async def test_tool_session_bridge_cas_refusal(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    broker = LocalToolBroker(workspace, mode=ApprovalMode.YOLO, cas_writes=True)
    async with tools_server() as fake:
        fake.bridge_write = {
            "tool_name": "write",
            "arguments": {"file_path": "notes.txt", "content": "new"},
            "if_match": "0" * 64,
        }
        client = _client(fake, workspace, broker)
        try:
            await client.call_tool("query_design", {})
        finally:
            await client.aclose()
    assert fake.bridge_results[0]["result"]["code"] == "FILE_CHANGED"
    assert (workspace / "notes.txt").read_text() == "hello\n"


async def test_tool_session_refused_start(tmp_path: Path) -> None:
    broker = LocalToolBroker(tmp_path)
    async with tools_server() as fake:
        fake.refuse_with = {"code": "unauthorized", "status": 401, "public_message": "Log in"}
        client = _client(fake, tmp_path, broker)
        with pytest.raises(ToolSessionError) as caught:
            await client.start()
    assert caught.value.status == 401
    assert str(caught.value) == "Log in"


async def test_tool_session_reconnects_and_resends_after_drain(tmp_path: Path) -> None:
    broker = LocalToolBroker(tmp_path)
    async with tools_server() as fake:
        fake.restart_once = True
        client = _client(fake, tmp_path, broker)
        try:
            result = await asyncio.wait_for(client.call_tool("run_erc", {}), 15)
        finally:
            await client.aclose()
    assert result["ok"] is True
    assert fake.connections == 2
    assert len(fake.starts) == 2


async def test_edit_in_flight_at_a_drop_is_not_replayed(tmp_path: Path) -> None:
    broker = LocalToolBroker(tmp_path)
    async with tools_server() as fake:
        fake.restart_once = True
        client = _client(fake, tmp_path, broker)
        try:
            result = await asyncio.wait_for(client.call_tool("place_symbol", {}), 15)
        finally:
            await client.aclose()
    assert result["ok"] is False
    assert result["error"]["code"] == "connection_lost"
    assert result["error"]["retryable"] is True
    assert "Re-read the design" in result["error"]["message"]
    assert [f["name"] for f in fake.of_type("tool_call")] == ["place_symbol"]


async def test_turn_end_not_sent_to_a_server_without_the_capability(tmp_path: Path) -> None:
    broker = LocalToolBroker(tmp_path)
    async with tools_server() as fake:
        fake.capabilities = None
        client = _client(fake, tmp_path, broker)
        try:
            await client.start()
            assert await client.send_turn_end("t1") is None
        finally:
            await client.aclose()
    assert fake.of_type("turn_end") == []


# ------------------------------------------------------------- MCP server


class StubBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> list[dict[str, Any]]:
        return [
            {"name": "query_design", "description": "q", "inputSchema": {"type": "object"}},
            {"name": SUBAGENT_TOOL, "description": "s", "inputSchema": {"type": "object"}},
        ]

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, progress: Any = None
    ) -> ToolOutcome:
        self.calls.append((name, arguments))
        if progress is not None:
            await progress("working")
        return ToolOutcome.text(f"{name} ok")

    async def read_guide(self) -> str:
        return GUIDE


async def test_mcp_server_lists_tools_and_guide() -> None:
    backend = StubBackend()
    server = build_mcp_server(backend)
    async with create_connected_server_and_client_session(server) as client:
        tools = await client.list_tools()
        assert [tool.name for tool in tools.tools] == ["query_design", SUBAGENT_TOOL]
        result = await client.call_tool("query_design", {"query": "U1"})
        assert result.content[0].text == "query_design ok"  # ty: ignore[missing-attribute]
        resources = await client.list_resources()
        assert str(resources.resources[0].uri) == GUIDE_URI
        guide = await client.read_resource(GUIDE_URI)  # ty: ignore[invalid-argument-type]
        assert guide.contents[0].text == GUIDE  # ty: ignore[missing-attribute]
    assert backend.calls == [("query_design", {"query": "U1"})]


async def test_stdio_relay_reaches_the_parent_backend(tmp_path: Path) -> None:
    """``copper-pilot mcp --tools-session --relay`` as Codex would start it."""
    backend = StubBackend()
    relay = RelayServer(backend)
    address = await relay.start()
    env = {**os.environ, RELAY_TOKEN_ENV: relay.token, "COPPER_PILOT_CLI_HOME": str(tmp_path)}
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "copper_pilot_cli", "mcp", "--tools-session", "--relay", address],
        env=env,
    )
    try:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert SUBAGENT_TOOL in [tool.name for tool in tools.tools]
            result = await session.call_tool(SUBAGENT_TOOL, {"task": "Add C9"})
            assert result.content[0].text == f"{SUBAGENT_TOOL} ok"  # ty: ignore[missing-attribute]
            guide = await session.read_resource(GUIDE_URI)  # ty: ignore[invalid-argument-type]
            assert CANARY in guide.contents[0].text  # ty: ignore[missing-attribute]
    finally:
        await relay.aclose()
    assert backend.calls == [(SUBAGENT_TOOL, {"task": "Add C9"})]


async def test_relay_refuses_a_wrong_token() -> None:
    from copper_pilot_cli.agents.mcp_server import RelayBackend

    relay = RelayServer(StubBackend())
    address = await relay.start()
    try:
        backend = RelayBackend(address, "wrong")
        with pytest.raises(ConnectionError):
            await backend.list_tools()
    finally:
        await relay.aclose()


# --------------------------------------------------------------- subagent


async def test_subagent_runs_a_fresh_hosted_turn_with_routine_kicad_writes(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, Any]] = []

    async def runner(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        await kwargs["progress"]("writing board.kicad_sch")
        return {"ok": True, "text": "Added C9 near U1.", "changed_files": ["board.kicad_sch"]}

    async def approve(_request: Any) -> bool:
        return True

    parent = LocalToolBroker(tmp_path, mode=ApprovalMode.AUTO, approve=approve)
    events: list[CopperEvent] = []
    proxy = ToolProxy(
        ToolSessionClient.__new__(ToolSessionClient),
        workspace=tmp_path,
        broker=parent,
        emit=events.append,
        subagent_runner=runner,
    )
    first = await proxy.call_tool(SUBAGENT_TOOL, {"task": "Add C9", "effort": "intelligence"})
    await proxy.call_tool(SUBAGENT_TOOL, {"task": "Check ERC", "mode": "ask"})
    assert not first.is_error
    assert "Added C9 near U1." in first.plain() and "- board.kicad_sch" in first.plain()
    assert calls[0]["thread_id"] != calls[1]["thread_id"]
    assert calls[0]["effort"].value == "intelligence"
    assert calls[1]["mode"].value == "ask"
    assert calls[0]["subagent"] is True
    broker = calls[0]["broker"]
    assert broker.routine_kicad_writes is True and broker.mode is ApprovalMode.AUTO
    assert broker.approve is approve
    kinds = [
        (event.kind, (event.data or {}).get("status"))
        for event in events
        if event.kind is EventKind.STATUS_EVENT
    ]
    assert kinds[0] == (EventKind.STATUS_EVENT, "running")
    assert kinds[1] == (EventKind.STATUS_EVENT, "success")
    assert any(event.kind is EventKind.STATUS for event in events)


async def test_subagent_reports_login_and_failures(tmp_path: Path) -> None:
    async def runner(**_kwargs: Any) -> dict[str, Any]:
        return {"ok": False, "error": "Upgrade to a paid plan to use Intelligence."}

    proxy = ToolProxy(
        ToolSessionClient.__new__(ToolSessionClient),
        workspace=tmp_path,
        broker=LocalToolBroker(tmp_path),
        subagent_runner=runner,
    )
    outcome = await proxy.call_tool(SUBAGENT_TOOL, {"task": "x", "effort": "intelligence"})
    assert outcome.is_error and "paid plan" in outcome.plain()
    empty = await proxy.call_tool(SUBAGENT_TOOL, {})
    assert empty.is_error
    assert "login" in subagent_text({"ok": False, "error": "login_required", "message": "login"})


def test_changed_files_from_events() -> None:
    events = [
        {
            "type": "copper.tool_request",
            "data": {"tool_call_id": "a", "tool_name": "write", "arguments": {"file_path": "x"}},
        },
        {"type": "copper.tool_result", "data": {"tool_call_id": "a", "result": {"result": "ok"}}},
        {
            "type": "copper.tool_request",
            "data": {"tool_call_id": "b", "tool_name": "edit", "arguments": {"file_path": "y"}},
        },
        {"type": "copper.tool_result", "data": {"tool_call_id": "b", "result": {"error": "no"}}},
        {
            "type": "copper.tool_request",
            "data": {"tool_call_id": "c", "tool_name": "read", "arguments": {"file_path": "z"}},
        },
        {"type": "copper.tool_result", "data": {"tool_call_id": "c", "result": {"result": "ok"}}},
    ]
    assert changed_files(events) == ["x"]


async def test_proxy_lists_server_tools_plus_subagent(tmp_path: Path) -> None:
    broker = LocalToolBroker(tmp_path)
    async with tools_server() as fake:
        client = _client(fake, tmp_path, broker)
        proxy = ToolProxy(client, workspace=tmp_path, broker=broker)
        try:
            tools = await proxy.list_tools()
            outcome = await ProxyBackend(proxy).call_tool("run_erc", {})
            guide = await ProxyBackend(proxy).read_guide()
        finally:
            await client.aclose()
    assert [tool["name"] for tool in tools] == [
        "query_design",
        "run_erc",
        "place_symbol",
        SUBAGENT_TOOL,
    ]
    assert tools[0]["inputSchema"]["properties"]["query"]["type"] == "string"
    assert outcome.plain() == "result of run_erc"
    assert guide == GUIDE


def test_chat_socket_advertises_cas_and_lifts_the_hash() -> None:
    from copper_pilot_cli.copper_protocol import client_capabilities, tool_result_frame

    assert "cas_writes_v1" in client_capabilities()
    frame = tool_result_frame("c", "t", {"result": "x", "content_sha256": "ab"})
    assert frame["content_sha256"] == "ab" and frame["result"] == {"result": "x"}


# ------------------------------------------------------------ BYOA v2


async def test_mcp_annotations_and_core_tools_follow_read_only_flags(tmp_path: Path) -> None:
    broker = LocalToolBroker(tmp_path)
    async with tools_server() as fake:
        client = _client(fake, tmp_path, broker)
        proxy = ToolProxy(client, workspace=tmp_path, broker=broker)
        server = build_mcp_server(ProxyBackend(proxy))
        try:
            async with create_connected_server_and_client_session(server) as session:
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        finally:
            await client.aclose()
    assert tools["query_design"].annotations.readOnlyHint is True  # ty: ignore[unresolved-attribute]
    assert tools["place_symbol"].annotations.readOnlyHint is False  # ty: ignore[unresolved-attribute]
    assert tools[SUBAGENT_TOOL].annotations.readOnlyHint is False  # ty: ignore[unresolved-attribute]
    # Core tools stay loaded in Claude Code; the rest may be deferred to tool search.
    assert tools["query_design"].meta == {"anthropic/alwaysLoad": True}
    assert tools["place_symbol"].meta == {"anthropic/alwaysLoad": False}
    assert tools[SUBAGENT_TOOL].meta == {"anthropic/alwaysLoad": True}
    assert {"read_copperpilot_playbook", "apply_kicad_schematic_batch", SUBAGENT_TOOL} <= CORE_TOOLS


async def test_missing_read_only_flag_means_an_old_server_and_read_only() -> None:
    backend = StubBackend()  # its specs carry no readOnly key
    server = build_mcp_server(backend)
    async with create_connected_server_and_client_session(server) as client:
        tools = (await client.list_tools()).tools
    assert all(tool.annotations.readOnlyHint is True for tool in tools)  # ty: ignore[unresolved-attribute]


def test_subagent_description_is_optional_and_billed() -> None:
    assert SUBAGENT_DESCRIPTION.startswith("Optional:")
    assert "CopperPilot plan" in SUBAGENT_DESCRIPTION and "second opinion" in SUBAGENT_DESCRIPTION
    assert "edit the design yourself" in SUBAGENT_DESCRIPTION


async def test_ask_and_plan_modes_limit_copperpilot_edit_tools(tmp_path: Path) -> None:
    broker = LocalToolBroker(tmp_path)
    ran: list[str] = []

    async def runner(**kwargs: Any) -> dict[str, Any]:
        ran.append(kwargs["mode"].value)
        return {"ok": True, "text": "done", "changed_files": []}

    async with tools_server() as fake:
        client = _client(fake, tmp_path, broker)
        proxy = ToolProxy(client, workspace=tmp_path, broker=broker, subagent_runner=runner)
        try:
            await proxy.start()
            for mode, subagent_modes in (
                (CopperMode.ASK, {"ask"}),
                (CopperMode.PLAN, {"ask", "plan"}),
            ):
                proxy.mode = mode
                edit = await proxy.call_tool("place_symbol", {})
                assert edit.is_error and "can change the design" in edit.plain()
                assert not (await proxy.call_tool("run_erc", {})).is_error
                for requested in ("agent", "plan", "ask"):
                    outcome = await proxy.call_tool(SUBAGENT_TOOL, {"task": "x", "mode": requested})
                    assert outcome.is_error is (requested not in subagent_modes)
            ran.clear()
            proxy.mode = CopperMode.AGENT
            assert not (await proxy.call_tool("place_symbol", {})).is_error
            assert not (await proxy.call_tool(SUBAGENT_TOOL, {"task": "x"})).is_error
            assert ran == ["agent"]
        finally:
            await client.aclose()
    # Refused calls never reached the server; only the Agent-mode one did.
    assert [f["name"] for f in fake.of_type("tool_call") if f["name"] == "place_symbol"] == [
        "place_symbol"
    ]


async def test_bridge_design_writes_are_routine_under_auto_inside_the_workspace(
    tmp_path: Path,
) -> None:
    asked: list[Any] = []

    async def approve(request: Any) -> bool:
        asked.append(request)
        return False

    parent = LocalToolBroker(tmp_path, mode=ApprovalMode.AUTO, approve=approve)
    bridge = BridgeBroker(lambda: parent)
    for name in (
        "a.kicad_sch",
        "a.kicad_pcb",
        "a.kicad_sym",
        "a.kicad_mod",
        "a.kicad_pro",
        "a.SchDoc",
    ):
        result = await bridge.execute(
            ToolRequest(
                tool_call_id=f"w-{name}",
                tool_name="write_file",
                arguments={"file_path": name, "content": "(x)"},
            )
        )
        assert "error" not in result
        assert (tmp_path / name).read_text() == "(x)"
    assert not asked
    # Outside the workspace is unchanged: refused, never a routine write.
    outside = tmp_path.parent / f"{tmp_path.name}-outside.kicad_sch"
    with pytest.raises(ToolRejected):
        await bridge.execute(
            ToolRequest(
                tool_call_id="w-out",
                tool_name="write_file",
                arguments={"file_path": str(outside), "content": "(x)"},
            )
        )
    assert not outside.exists()
    # Manual still asks.
    manual = BridgeBroker(
        lambda: LocalToolBroker(tmp_path, mode=ApprovalMode.MANUAL, approve=approve)
    )
    with pytest.raises(ToolRejected):
        await manual.execute(
            ToolRequest(
                tool_call_id="w-m",
                tool_name="write_file",
                arguments={"file_path": "m.kicad_sch", "content": "(x)"},
            )
        )
    assert asked


async def test_turn_end_is_advertised_only_by_owners_that_send_it(tmp_path: Path) -> None:
    broker = LocalToolBroker(tmp_path)
    async with tools_server() as fake:
        plain = _client(fake, tmp_path, broker)
        owner = ToolSessionClient(
            fake.credential,
            workspace=tmp_path,
            conversation_id="c2",
            executor=broker.execute,
            agent={"provider": "codex", "model": ""},
            turn_end=True,
        )
        try:
            await plain.start()
            await owner.start()
        finally:
            await plain.aclose()
            await owner.aclose()
    assert TURN_END_CAPABILITY not in fake.starts[0]["capabilities"]
    assert TURN_END_CAPABILITY in fake.starts[1]["capabilities"]
    assert TURN_END_CAPABILITY == "turn_end_v1"


async def test_send_turn_end_returns_the_note_and_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = LocalToolBroker(tmp_path)
    async with tools_server() as fake:
        client = _client(fake, tmp_path, broker)
        assert await client.send_turn_end("t0") is None  # not connected: no frame, no error
        try:
            await client.start()
            fake.turn_end_note = "Applied 3 staged Altium edits."
            assert await client.send_turn_end("t1") == "Applied 3 staged Altium edits."
            fake.turn_end_note = None
            assert await client.send_turn_end("t2") is None
            fake.turn_end_silent = True
            monkeypatch.setattr(
                "copper_pilot_cli.agents.tools_session.TURN_END_TIMEOUT_SECONDS", 0.2
            )
            assert await client.send_turn_end("t3") is None
        finally:
            await client.aclose()
        assert [f["turn_id"] for f in fake.of_type("turn_end")] == ["t1", "t2", "t3"]
