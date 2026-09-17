from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from copper_pilot_cli.copper_auth import DeviceCredential
from copper_pilot_cli.copper_protocol import CopperEvent, CopperMode, EventKind
from copper_pilot_cli.langchain import CopperPilotAgent

HELPER_PATH = Path(__file__).resolve().parents[1] / "examples" / "001_esp32" / "helper.py"


def load_helper() -> Any:
    spec = importlib.util.spec_from_file_location("esp32_example_helper", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


helper = load_helper()


class FakeClient:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.modes: list[str] = []
        self.first_frame_timeout = 60.0
        self.idle_frame_timeout = 300.0

    async def stream(self, request, tool_handler):
        del tool_handler
        self.queries.append(request.query)
        self.modes.append(request.mode)
        yield CopperEvent(EventKind.REASONING, "hidden")
        yield CopperEvent(EventKind.TEXT, "Hello")
        yield CopperEvent(EventKind.TEXT, "Hello world")
        yield CopperEvent(EventKind.FINAL, {"is_final": True})


def _blank_project(workspace: Path) -> None:
    project = workspace / "esp32"
    project.mkdir(parents=True)
    (project / "esp32.kicad_sch").write_text("(kicad_sch)\n", encoding="utf-8")
    (project / "esp32.kicad_pcb").write_text("(kicad_pcb)\n", encoding="utf-8")
    plans = workspace / ".copperpilot" / "plans"
    plans.mkdir(parents=True)
    (plans / "esp32-dev-board.plan.md").write_text(
        "---\nname: ESP32 Dev Board\noverview: A board\ntodos:\n"
        "  - id: schematic\n    content: Draw the schematic\n    status: open\n---\n\n"
        "# Plan\n\nBuild it.\n",
        encoding="utf-8",
    )


def _stub_copper_pilot(tmp_path: Path) -> tuple[CopperPilotAgent, FakeClient]:
    _blank_project(tmp_path)
    credential = DeviceCredential(
        api_key="cf_live_test",
        fingerprint="fingerprint",
        base_url="https://example.test",
    )
    copper_pilot = CopperPilotAgent(tmp_path, credential=credential)
    client = FakeClient()
    copper_pilot._client = client
    return copper_pilot, client


def test_text_delta_cumulative_and_incremental() -> None:
    delta, printed = helper._text_delta("", "Hello")
    assert delta == "Hello"
    assert printed == "Hello"
    delta, printed = helper._text_delta(printed, "Hello world")
    assert delta == " world"
    assert printed == "Hello world"
    delta, printed = helper._text_delta(printed, "!")
    assert delta == "!"
    assert printed == "Hello world!"


def test_erc_errors_ignore_excluded_and_warnings() -> None:
    erc = helper.summarize_erc(
        {
            "sheets": [
                {
                    "path": "/",
                    "violations": [
                        {"severity": "error", "type": "pin_not_connected"},
                        {"severity": "excluded", "type": "legacy"},
                        {"severity": "warning", "type": "sim"},
                    ],
                }
            ]
        }
    )
    assert len(erc["errors"]) == 1
    assert len(erc["warnings"]) == 1


def test_drc_violations_count_includes_unconnected() -> None:
    drc = helper.summarize_drc(
        {
            "violations": [
                {"severity": "error", "type": "clearance"},
                {"severity": "ignore", "type": "silk"},
            ],
            "unconnected_items": [{"severity": "error", "type": "unconnected_items"}],
        }
    )
    assert len(drc["violations"]) == 1
    assert len(drc["unconnected_items"]) == 1
    assert len(drc["violations"]) + len(drc["unconnected_items"]) == 2


def test_render_pcb_maps_short_name(tmp_path: Path) -> None:
    _blank_project(tmp_path)
    assert helper.resolve_png_path(tmp_path, "esp32.png") == tmp_path / "esp32" / "esp32.png"
    assert helper.resolve_png_path(tmp_path, "esp32/esp32.png") == tmp_path / "esp32" / "esp32.png"


def test_example_root_keeps_copperpilot_on_example_folder(tmp_path: Path) -> None:
    _blank_project(tmp_path)
    inner = tmp_path / "esp32"
    assert helper.example_root(tmp_path) == tmp_path.resolve()
    assert helper.example_root(inner) == tmp_path.resolve()


def test_run_streams_plain_text_and_loads_plan(tmp_path: Path, monkeypatch, capsys) -> None:
    helper._sessions.clear()
    helper._active = None
    copper_pilot, client = _stub_copper_pilot(tmp_path)
    monkeypatch.setattr(helper, "require_kicad_10", lambda: Path("/usr/bin/kicad-cli"))
    monkeypatch.setattr(
        helper,
        "run_electrical_checks",
        lambda cli, *, workspace=None: (
            {"errors": [], "warnings": []},
            {"violations": [], "unconnected_items": []},
        ),
    )

    helper.run(copper_pilot, "Build me an ESP32 dev board.", CopperMode.PLAN)
    out = capsys.readouterr().out
    assert "Hello world" in out
    assert "hidden" not in out
    assert client.queries == ["Build me an ESP32 dev board."]
    assert client.modes == [CopperMode.PLAN]
    assert copper_pilot.plan_context is not None
    assert copper_pilot.plan_context.name == "ESP32 Dev Board"
    assert helper.erc_errors() == 0
    assert helper.drc_violations() == 0
    helper._sessions.clear()
    helper._active = None
