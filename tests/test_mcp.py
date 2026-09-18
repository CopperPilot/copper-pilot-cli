from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from copper_pilot_cli.copper_auth import DeviceCredential
from copper_pilot_cli.copper_mcp import (
    LOGIN_MESSAGE,
    collect_turn,
    create_server,
    handle_ask,
    handle_plan,
    handle_resume,
    handle_run,
    handle_status,
    handle_threads_list,
    registered_tool_names,
    resolve_workspace,
)
from copper_pilot_cli.copper_protocol import CopperMode
from copper_pilot_cli.copper_tools import ApprovalMode

CREDENTIAL = DeviceCredential(
    api_key="cf_live_test",
    fingerprint="fingerprint",
    base_url="https://example.test",
)


class FakeAgent:
    last: FakeAgent | None = None

    def __init__(self, workspace, *, credential, conversation_id, mode, tool_broker) -> None:
        self.workspace = workspace
        self.credential = credential
        self.conversation_id = conversation_id
        self.mode = mode
        self.tool_broker = tool_broker
        type(self).last = self


class FakeRuntime:
    def __init__(self, text: str = "Looks good.", success: bool = True) -> None:
        self.text = text
        self.success = success
        self.values: dict[str, object] | None = None

    def config(self, thread_id, workspace):
        return {"thread_id": thread_id, "workspace": workspace}

    async def astream(self, values, _config):
        self.values = values
        yield (
            (),
            "custom",
            {"type": "copper.status", "data": "Inspecting", "title": "status"},
        )
        yield ((), "custom", {"type": "copper.text", "data": self.text})
        yield (
            (),
            "custom",
            {"type": "copper.final", "data": {"success": self.success}},
        )

    async def aget_state(self, _config):
        return SimpleNamespace(values={"messages": [AIMessage(content=self.text)]})


def _patch_turn(monkeypatch, tmp_path, runtime: FakeRuntime | None = None) -> FakeRuntime:
    fake_runtime = runtime or FakeRuntime()

    @asynccontextmanager
    async def fake_graph(_agent):
        yield fake_runtime

    monkeypatch.setattr("copper_pilot_cli.copper_mcp.load_credential", lambda: CREDENTIAL)
    monkeypatch.setattr("copper_pilot_cli.copper_mcp.CopperPilotAgent", FakeAgent)
    monkeypatch.setattr("copper_pilot_cli.copper_mcp.create_copper_graph", fake_graph)
    monkeypatch.chdir(tmp_path)
    return fake_runtime


def test_status_reports_logged_out(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("copper_pilot_cli.copper_mcp.load_credential", lambda: None)
    payload = handle_status(str(tmp_path))
    assert payload["ok"] is True
    assert payload["logged_in"] is False
    assert payload["login_message"] == LOGIN_MESSAGE
    assert payload["workspace"] == str(tmp_path.resolve())


def test_status_reports_logged_in_and_paths(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("copper_pilot_cli.copper_mcp.load_credential", lambda: CREDENTIAL)
    (tmp_path / "board.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    (tmp_path / "board.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
    payload = handle_status(str(tmp_path))
    assert payload["logged_in"] is True
    assert payload["base_url"] == "https://example.test"
    assert payload["schematic_path"] == "board.kicad_sch"
    assert payload["pcb_path"] == "board.kicad_pcb"


def test_resolve_workspace_prefers_explicit_then_claude_project_dir(tmp_path, monkeypatch) -> None:
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(other))
    monkeypatch.chdir(tmp_path)
    assert resolve_workspace(str(tmp_path)) == tmp_path.resolve()
    assert resolve_workspace(None) == other.resolve()


def test_status_invalid_workspace() -> None:
    payload = handle_status("/no/such/copper-pilot-workspace")
    assert payload["ok"] is False
    assert payload["error"] == "invalid_workspace"


@pytest.mark.asyncio
async def test_ask_uses_ask_mode_and_auto(tmp_path, monkeypatch) -> None:
    _patch_turn(monkeypatch, tmp_path)
    payload = await handle_ask(message="Review U3", workspace=str(tmp_path))
    assert payload["ok"] is True
    assert payload["text"] == "Looks good."
    assert payload["mode"] == CopperMode.ASK.value
    assert FakeAgent.last is not None
    assert FakeAgent.last.mode is CopperMode.ASK
    assert FakeAgent.last.tool_broker.mode is ApprovalMode.AUTO
    assert payload["thread_id"]


@pytest.mark.asyncio
async def test_plan_uses_plan_mode(tmp_path, monkeypatch) -> None:
    _patch_turn(monkeypatch, tmp_path)
    payload = await handle_plan(message="Propose a power tree", workspace=str(tmp_path))
    assert payload["mode"] == CopperMode.PLAN.value
    assert FakeAgent.last is not None
    assert FakeAgent.last.mode is CopperMode.PLAN


@pytest.mark.asyncio
async def test_run_yolo_sets_agent_mode(tmp_path, monkeypatch) -> None:
    runtime = _patch_turn(monkeypatch, tmp_path)
    payload = await handle_run(
        message="Apply the 3D models",
        workspace=str(tmp_path),
        approval="yolo",
    )
    assert payload["mode"] == CopperMode.AGENT.value
    assert FakeAgent.last is not None
    assert FakeAgent.last.mode is CopperMode.AGENT
    assert FakeAgent.last.tool_broker.mode is ApprovalMode.YOLO
    assert runtime.values is not None
    assert runtime.values["messages"][0] == HumanMessage(content="Apply the 3D models")


@pytest.mark.asyncio
async def test_resume_requires_thread_id(tmp_path, monkeypatch) -> None:
    _patch_turn(monkeypatch, tmp_path)
    payload = await handle_resume(thread_id="  ", message="Continue", workspace=str(tmp_path))
    assert payload["ok"] is False
    assert payload["error"] == "thread_required"


@pytest.mark.asyncio
async def test_resume_reuses_thread_id(tmp_path, monkeypatch) -> None:
    _patch_turn(monkeypatch, tmp_path)
    payload = await handle_resume(
        thread_id="thread-123",
        message="Continue",
        workspace=str(tmp_path),
    )
    assert payload["thread_id"] == "thread-123"
    assert FakeAgent.last is not None
    assert FakeAgent.last.conversation_id == "thread-123"


@pytest.mark.asyncio
async def test_resume_defaults_to_agent_mode(tmp_path, monkeypatch) -> None:
    _patch_turn(monkeypatch, tmp_path)
    payload = await handle_resume(
        thread_id="thread-123",
        message="Continue",
        workspace=str(tmp_path),
    )
    assert payload["mode"] == CopperMode.AGENT.value
    assert FakeAgent.last is not None
    assert FakeAgent.last.mode is CopperMode.AGENT


@pytest.mark.asyncio
async def test_resume_can_stay_in_ask_mode(tmp_path, monkeypatch) -> None:
    _patch_turn(monkeypatch, tmp_path)
    payload = await handle_resume(
        thread_id="thread-123",
        message="Continue",
        workspace=str(tmp_path),
        mode="ask",
    )
    assert payload["mode"] == CopperMode.ASK.value
    assert FakeAgent.last is not None
    assert FakeAgent.last.mode is CopperMode.ASK


@pytest.mark.asyncio
async def test_ask_without_login_returns_structured_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("copper_pilot_cli.copper_mcp.load_credential", lambda: None)
    payload = await handle_ask(message="Review", workspace=str(tmp_path))
    assert payload == {
        "ok": False,
        "error": "login_required",
        "message": LOGIN_MESSAGE,
    }


@pytest.mark.asyncio
async def test_threads_list_returns_stub_rows(tmp_path, monkeypatch) -> None:
    async def fake_list_threads(**kwargs):
        assert kwargs["cwd"] == str(tmp_path.resolve())
        return [
            {
                "thread_id": "abc",
                "updated_at": "2026-01-01",
                "message_count": 2,
                "initial_prompt": "Review",
                "cwd": str(tmp_path),
            }
        ]

    monkeypatch.setattr("copper_pilot_cli.copper_mcp.list_threads", fake_list_threads)
    payload = await handle_threads_list(workspace=str(tmp_path))
    assert payload["ok"] is True
    assert payload["threads"][0]["thread_id"] == "abc"


@pytest.mark.asyncio
async def test_collect_turn_reports_progress(tmp_path, monkeypatch) -> None:
    _patch_turn(monkeypatch, tmp_path)
    notes: list[str] = []

    async def progress(message: str) -> None:
        notes.append(message)

    payload = await collect_turn(
        message="Review",
        mode=CopperMode.ASK,
        workspace=tmp_path,
        thread_id="thread",
        approval=ApprovalMode.AUTO,
        progress=progress,
    )
    assert payload["ok"] is True
    assert any("Starting" in note for note in notes)
    assert any("Inspecting" in note for note in notes)
    assert notes[-1] == "CopperPilot turn complete"


def test_server_registers_six_tools() -> None:
    names = registered_tool_names(create_server())
    assert names == {"status", "ask", "plan", "run", "resume", "threads_list"}


def test_status_payload_is_json_serializable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("copper_pilot_cli.copper_mcp.load_credential", lambda: None)
    json.dumps(handle_status(str(tmp_path)))
