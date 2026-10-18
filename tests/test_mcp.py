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
    handle_models,
    handle_plan,
    handle_resume,
    handle_run,
    handle_status,
    handle_threads_list,
    registered_tool_names,
    resolve_workspace,
)
from copper_pilot_cli.copper_models import (
    BALANCED_MODEL,
    DEFAULT_INTELLIGENCE_MODEL,
    FALLBACK_CATALOG,
)
from copper_pilot_cli.copper_protocol import CopperEffort, CopperMode
from copper_pilot_cli.copper_tools import ApprovalMode

CREDENTIAL = DeviceCredential(
    api_key="cf_live_test",
    fingerprint="fingerprint",
    base_url="https://example.test",
)


class FakeAgent:
    last: FakeAgent | None = None

    def __init__(
        self,
        workspace,
        *,
        credential,
        conversation_id,
        mode,
        effort,
        tool_broker,
        model=None,
    ) -> None:
        self.workspace = workspace
        self.credential = credential
        self.conversation_id = conversation_id
        self.mode = mode
        self.effort = effort
        self.model = model
        self.tool_broker = tool_broker
        type(self).last = self


class FakeRuntime:
    def __init__(
        self,
        text: str = "Looks good.",
        success: bool = True,
        extra_events: list[dict[str, object]] | None = None,
        final_model: str | None = None,
    ) -> None:
        self.text = text
        self.success = success
        self.extra_events = list(extra_events or [])
        self.final_model = final_model
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
        for event in self.extra_events:
            yield ((), "custom", event)
        yield ((), "custom", {"type": "copper.text", "data": self.text})
        final: dict[str, object] = {"success": self.success}
        if self.final_model:
            final["model"] = self.final_model
        yield ((), "custom", {"type": "copper.final", "data": final})

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
    assert payload["effort"] == CopperEffort.BALANCED.value
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
async def test_plan_can_use_intelligence_effort(tmp_path, monkeypatch) -> None:
    runtime = _patch_turn(monkeypatch, tmp_path)
    payload = await handle_plan(
        message="Propose a power tree",
        workspace=str(tmp_path),
        effort="intelligence",
    )
    assert payload["effort"] == CopperEffort.INTELLIGENCE.value
    assert FakeAgent.last is not None
    assert FakeAgent.last.effort is CopperEffort.INTELLIGENCE
    assert runtime.values is not None
    assert runtime.values["copper_effort"] == "intelligence"
    assert payload["model"] == DEFAULT_INTELLIGENCE_MODEL
    assert payload["answered_model"] == DEFAULT_INTELLIGENCE_MODEL


@pytest.mark.asyncio
async def test_model_argument_selects_intelligence_on_that_model(tmp_path, monkeypatch) -> None:
    runtime = _patch_turn(monkeypatch, tmp_path, FakeRuntime(final_model="gpt-6.1-sol"))
    payload = await handle_ask(message="Review U3", workspace=str(tmp_path), model="GPT-6.1-Sol")
    assert payload["effort"] == CopperEffort.INTELLIGENCE.value
    assert payload["model"] == "gpt-6.1-sol"
    assert payload["answered_model"] == "gpt-6.1-sol"
    assert FakeAgent.last is not None
    assert FakeAgent.last.effort is CopperEffort.INTELLIGENCE
    assert FakeAgent.last.model == "gpt-6.1-sol"
    assert runtime.values == {
        "messages": runtime.values["messages"],
        "copper_mode": "ask",
        "copper_effort": "intelligence",
        "copper_model": "gpt-6.1-sol",
    }


@pytest.mark.asyncio
async def test_balanced_turn_reports_the_balanced_model(tmp_path, monkeypatch) -> None:
    runtime = _patch_turn(monkeypatch, tmp_path)
    payload = await handle_run(message="Fix U3", workspace=str(tmp_path))
    assert payload["effort"] == "balanced"
    assert payload["model"] == BALANCED_MODEL
    assert payload["answered_model"] == BALANCED_MODEL
    assert runtime.values is not None
    assert "copper_model" not in runtime.values


@pytest.mark.asyncio
async def test_resume_keeps_thread_effort_and_model_unless_given(tmp_path, monkeypatch) -> None:
    runtime = _patch_turn(monkeypatch, tmp_path)
    await handle_resume(thread_id="t1", message="More", workspace=str(tmp_path))
    assert runtime.values is not None
    assert "copper_effort" not in runtime.values
    assert "copper_model" not in runtime.values
    await handle_resume(
        thread_id="t1", message="More", workspace=str(tmp_path), model="claude-sonnet-5-5"
    )
    assert runtime.values["copper_effort"] == "intelligence"
    assert runtime.values["copper_model"] == "claude-sonnet-5-5"


@pytest.mark.asyncio
async def test_model_fallback_is_reported_in_progress_digest_and_payload(
    tmp_path, monkeypatch
) -> None:
    fallback = {
        "kind": "model_fallback",
        "from_model": "claude-opus-5-5",
        "to_model": "gemini-4-argon",
        "message": "Claude Opus 5.5 is unavailable right now. Switched this chat to Argon.",
    }
    _patch_turn(
        monkeypatch,
        tmp_path,
        FakeRuntime(extra_events=[{"type": "copper.status_event", "data": fallback}]),
    )
    lines: list[str] = []

    async def progress(message: str) -> None:
        lines.append(message)

    payload = await handle_plan(
        message="Plan", workspace=str(tmp_path), model="claude-opus-5-5", progress=progress
    )
    assert fallback["message"] in lines
    assert payload["model"] == "claude-opus-5-5"
    assert payload["answered_model"] == "gemini-4-argon"
    digest = [item for item in payload["events"] if item["kind"] == "status_event"]
    assert digest[0]["data"] == fallback


@pytest.mark.asyncio
async def test_models_tool_lists_catalog(monkeypatch) -> None:
    async def fake_catalog(_credential, *, force=False):
        return FALLBACK_CATALOG

    monkeypatch.setattr("copper_pilot_cli.copper_mcp.load_credential", lambda: CREDENTIAL)
    monkeypatch.setattr("copper_pilot_cli.copper_mcp.load_chat_model_catalog", fake_catalog)
    payload = await handle_models()
    assert payload["ok"] is True
    assert [model["id"] for model in payload["models"]] == list(FALLBACK_CATALOG.ids)
    assert payload["models"][0]["default"] is True
    assert payload["default_intelligence_model"] == DEFAULT_INTELLIGENCE_MODEL
    assert payload["balanced_model"] == BALANCED_MODEL
    assert payload["from_server"] is False
    json.dumps(payload)


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


@pytest.mark.asyncio
async def test_collect_turn_reports_bounded_subagent_progress(tmp_path, monkeypatch) -> None:
    class SubagentRuntime(FakeRuntime):
        async def astream(self, values, _config):
            self.values = values
            yield (
                (),
                "custom",
                {
                    "type": "copper.status_event",
                    "data": {
                        "kind": "subagent",
                        "id": "worker",
                        "task": "Audit power",
                        "status": "running",
                    },
                },
            )
            yield (
                (),
                "custom",
                {
                    "type": "copper.status_event",
                    "data": {
                        "kind": "subagent",
                        "id": "worker",
                        "status": "success",
                        "output": "x" * 20_000,
                    },
                },
            )
            yield (
                (),
                "custom",
                {"type": "copper.final", "data": {"success": True}},
            )

    _patch_turn(monkeypatch, tmp_path, SubagentRuntime())
    notes: list[str] = []

    async def progress(message: str) -> None:
        notes.append(message)

    payload = await handle_plan(
        message="Plan",
        workspace=str(tmp_path),
        effort="intelligence",
        progress=progress,
    )
    assert "Subagent: Audit power — Running" in notes
    assert "Subagent: Audit power — Done" in notes
    status_events = [event for event in payload["events"] if event["kind"] == "status_event"]
    assert status_events[-1]["data"]["status"] == "success"
    assert "output" not in status_events[-1]["data"]


def test_server_registers_seven_tools() -> None:
    names = registered_tool_names(create_server())
    assert names == {
        "status",
        "models",
        "ask",
        "plan",
        "run",
        "resume",
        "threads_list",
        "validate",
    }


def test_status_payload_is_json_serializable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("copper_pilot_cli.copper_mcp.load_credential", lambda: None)
    json.dumps(handle_status(str(tmp_path)))


def test_validate_tool_is_registered() -> None:
    assert "validate" in registered_tool_names()


@pytest.mark.asyncio
async def test_validate_tool_returns_verdict_and_next_actions(tmp_path, monkeypatch) -> None:
    from copper_pilot_cli.copper_mcp import handle_validate
    from copper_pilot_cli.validate import ValidationReport
    from copper_pilot_cli.validate.config import CheckResult, Status

    seen: dict[str, object] = {}

    async def fake_avalidate(workspace, **kwargs):
        seen["workspace"] = workspace
        seen.update(kwargs)
        kwargs["on_progress"]("running  sch.erc")
        return ValidationReport(
            verdict="FAIL",
            exit_code=1,
            run_dir=tmp_path,
            report={"schema": "copper-pilot.validate/v1"},
            next_actions={"stage": "S3", "actions": [{"id": "sch.erc/001"}]},
            results=[
                CheckResult(
                    check_id="sch.erc", stage="S3", title="ERC", status=Status.FAIL, gate="blocking"
                )
            ],
            paths={"report": tmp_path / "report.json", "markdown": tmp_path / "REPORT.md"},
        )

    monkeypatch.setattr("copper_pilot_cli.validate.avalidate", fake_avalidate)
    progress: list[str] = []

    async def report(message: str) -> None:
        progress.append(message)

    payload = await handle_validate(
        workspace=str(tmp_path),
        profile="release",
        gate="full",
        until="S3",
        only=["sch.erc"],
        effort="balanced",
        no_cache=True,
        progress=report,
    )
    assert payload["ok"] is False
    assert payload["verdict"] == "FAIL"
    assert payload["exit_code"] == 1
    assert payload["next_actions"]["actions"][0]["id"] == "sch.erc/001"
    assert payload["report_path"] == str(tmp_path / "report.json")
    assert seen["workspace"] == tmp_path.resolve()
    assert seen["profile"] == "release" and seen["gate"] == "full" and seen["until"] == "S3"
    assert seen["only"] == ["sch.erc"] and seen["no_cache"] is True
    assert progress == ["running  sch.erc"]


@pytest.mark.asyncio
async def test_validate_tool_reports_config_and_login_errors(tmp_path, monkeypatch) -> None:
    from copper_pilot_cli.copper_mcp import handle_validate
    from copper_pilot_cli.validate.config import ConfigError
    from copper_pilot_cli.validate.runner import EnvironmentError_

    async def bad_config(workspace, **kwargs):
        raise ConfigError("check 'a': dependency cycle")

    monkeypatch.setattr("copper_pilot_cli.validate.avalidate", bad_config)
    payload = await handle_validate(workspace=str(tmp_path))
    assert payload == {
        "ok": False,
        "error": "config_error",
        "message": "check 'a': dependency cycle",
        "exit_code": 2,
    }

    async def not_logged_in(workspace, **kwargs):
        raise EnvironmentError_("Not logged in. Run `copper-pilot auth login`.")

    monkeypatch.setattr("copper_pilot_cli.validate.avalidate", not_logged_in)
    payload = await handle_validate(workspace=str(tmp_path))
    assert payload["error"] == "login_required" and payload["exit_code"] == 4
    assert (await handle_validate(workspace="/no/such/dir"))["error"] == "invalid_workspace"
