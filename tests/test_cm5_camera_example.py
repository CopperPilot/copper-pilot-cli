from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from copper_pilot_cli.copper_auth import DeviceCredential
from copper_pilot_cli.copper_protocol import CopperEvent, CopperMode, EventKind, ProtocolError
from copper_pilot_cli.langchain import CopperPilotAgent

HELPER_PATH = Path(__file__).resolve().parents[1] / "examples" / "002_cm5_camera" / "helper.py"


def load_helper() -> Any:
    spec = importlib.util.spec_from_file_location("cm5_camera_example_helper", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


helper = load_helper()

PLAN_FRONTMATTER = """---
name: CM5 Camera Carrier
overview: >-
  55.0 x 40.0 mm Raspberry Pi Compute Module 5 nano camera carrier cloned from
  Waveshare CM4-NANO-C, with 3.0 mm corners, 4x M2.5 holes, and a 6-layer
  SIG-GND-SIG-PWR-GND-SIG stackup. Closest KiCad DF40 reference: xerxes.
  Also study KiCad cm5_minima.
todos:
  - id: setup-libraries
    content: Register symbols and footprints for DF40, IMX219, Mini-HDMI.
    status: open
  - id: schematic-capture
    content: Capture the schematic including camera, USB, and HDMI.
    status: open
  - id: schematic-erc
    content: Run kicad-cli sch erc until clean.
    status: open
  - id: pcb-outline-placement
    content: Draw Edge.Cuts outline and place footprints.
    status: open
  - id: pcb-zones-netclasses
    content: Pour In1.Cu/In4.Cu GND and In3.Cu power zones; set netclasses.
    status: open
  - id: pcb-routing
    content: Route with kicad_autorouter in headless FreeRouting mode; lock diffs.
    status: open
  - id: pcb-drc-3d
    content: Run DRC and assign 3D models then render.
    status: open
  - id: pcb-3d-pose-audit
    content: Audit 3D model offsets and rotations against the pose table, then render.
    status: open
overall_status: open
---

"""
VALID_PLAN = PLAN_FRONTMATTER + helper.hosted_plan_prompt()


def _netlist_xml_from_contract() -> str:
    nets: list[str] = []
    for index, (name, refs) in enumerate(helper.REQUIRED_NETS.items(), start=1):
        nodes = "".join(f'<node ref="{ref}" pin="1"/>' for ref in refs)
        nets.append(f'<net code="{index}" name="/{name}">{nodes}</net>')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<export version="D">\n  <nets>\n    '
        + "\n    ".join(nets)
        + "\n  </nets>\n</export>\n"
    )


NETLIST_XML = _netlist_xml_from_contract()

PCB_LAYOUT = """(kicad_pcb
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (4 "In1.Cu" signal)
    (6 "In2.Cu" signal)
    (8 "In3.Cu" signal)
    (10 "In4.Cu" signal)
    (2 "B.Cu" signal)
  )
  (gr_rect
    (start 0 0)
    (end 55 40)
    (stroke (width 0.05) (type default))
    (layer "Edge.Cuts")
  )
  (footprint "Camera:IMX219"
    (layer "F.Cu")
    (at 37.21 18)
    (property "Reference" "J1")
    (model "imx219.step")
  )
  (footprint "Connector:HDMI_Mini"
    (layer "F.Cu")
    (at 8.00 5.52 180)
    (property "Reference" "HDMI1")
    (model "hdmi.step")
  )
  (footprint "Connector:DSI_15P"
    (layer "F.Cu")
    (at 27.00 4.39 180)
    (property "Reference" "DSI1")
  )
  (footprint "Connector:USB_A"
    (layer "F.Cu")
    (at 43.50 6.50)
    (property "Reference" "J2")
    (model "usba.step")
  )
  (footprint "Connector:MicroSD"
    (layer "F.Cu")
    (at 7.50 18.50 270)
    (property "Reference" "TF1")
    (model "sd.step")
  )
  (footprint "Connector:GPIO_40"
    (layer "F.Cu")
    (at 3.50 30.78 90)
    (property "Reference" "PI1")
    (model "gpio.step")
  )
  (footprint "Connector:USB_C"
    (layer "F.Cu")
    (at 9.50 37.50)
    (property "Reference" "Type_C1")
    (model "usbc.step")
  )
  (footprint "Button:SW"
    (layer "F.Cu")
    (at 45.52 36.25)
    (property "Reference" "Key1")
  )
  (footprint "Package:TSOT-23-6"
    (layer "F.Cu")
    (at 44.64 15.50)
    (property "Reference" "U5")
  )
  (footprint "Crystal:X3225"
    (layer "F.Cu")
    (at 41.65 19.90 90)
    (property "Reference" "X1")
  )
  (footprint "Choke:MCF1210"
    (layer "F.Cu")
    (at 34.50 8.00)
    (property "Reference" "L1")
  )
  (footprint "Choke:MCF1210"
    (layer "F.Cu")
    (at 28.50 8.00)
    (property "Reference" "L2")
  )
  (footprint "Choke:MCF1210"
    (layer "F.Cu")
    (at 31.50 8.00)
    (property "Reference" "L3")
  )
  (footprint "Package:SOT-23-5"
    (layer "B.Cu")
    (at 22.00 18.50)
    (property "Reference" "U1")
  )
  (footprint "Package:SOT-23-5"
    (layer "B.Cu")
    (at 17.00 18.50)
    (property "Reference" "U2")
  )
  (footprint "Package:SOT-23-5"
    (layer "B.Cu")
    (at 27.00 18.50)
    (property "Reference" "U3")
  )
  (footprint "Package:UQFN-10"
    (layer "B.Cu")
    (at 8.00 10.00)
    (property "Reference" "U6")
  )
  (footprint "Module:CM5_DF40"
    (layer "B.Cu")
    (at 51.30 36.42 90)
    (property "Reference" "Module1")
    (model "cm5.step")
  )
  (footprint "MountingHole:MountingHole_2.5mm"
    (layer "F.Cu")
    (at 3.5 3.5)
    (property "Reference" "MH1")
  )
  (footprint "MountingHole:MountingHole_2.5mm"
    (layer "F.Cu")
    (at 51.5 3.5)
    (property "Reference" "MH2")
  )
  (footprint "MountingHole:MountingHole_2.5mm"
    (layer "F.Cu")
    (at 3.5 36.5)
    (property "Reference" "MH3")
  )
  (footprint "MountingHole:MountingHole_2.5mm"
    (layer "F.Cu")
    (at 51.5 36.5)
    (property "Reference" "MH4")
  )
  (zone
    (net 1)
    (net_name "GND")
    (layer "In1.Cu")
  )
  (zone
    (net 1)
    (net_name "GND")
    (layer "In4.Cu")
  )
  (zone
    (net 2)
    (net_name "CM5_5V")
    (layer "In3.Cu")
  )
  (zone
    (net 3)
    (net_name "PWR_3V3")
    (layer "In3.Cu")
  )
)
"""

PROJECT_NETCLASSES = {
    "net_settings": {
        "classes": [
            {"name": "Default", "track_width": 0.2},
            {"name": "100Ohm_Diff", "track_width": 0.15, "diff_pair_gap": 0.18},
            {"name": "90Ohm_Diff", "track_width": 0.18, "diff_pair_gap": 0.20},
            {"name": "Power_5V", "track_width": 0.80},
            {"name": "Power_3V3", "track_width": 0.60},
        ]
    }
}

SCHEMATIC_REFS = """(kicad_sch
  (symbol
    (property "Reference" "J1")
  )
  (symbol
    (property "Reference" "HDMI1")
  )
  (symbol
    (property "Reference" "Type_C1")
  )
  (symbol
    (property "Reference" "#PWR01")
  )
)
"""

PCB_REFS = """(kicad_pcb
  (footprint "X"
    (layer "F.Cu")
    (at 1 1)
    (property "Reference" "J1")
  )
  (footprint "X"
    (layer "F.Cu")
    (at 2 2)
    (property "Reference" "HDMI1")
  )
  (footprint "X"
    (layer "F.Cu")
    (at 3 3)
    (property "Reference" "Type_C1")
  )
)
"""


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
    project = workspace / "cm5_camera"
    project.mkdir(parents=True)
    (project / "cm5_camera.kicad_sch").write_text("(kicad_sch)\n", encoding="utf-8")
    (project / "cm5_camera.kicad_pcb").write_text("(kicad_pcb)\n", encoding="utf-8")
    plans = workspace / ".copperpilot" / "plans"
    plans.mkdir(parents=True)
    (plans / "cm5-camera.plan.md").write_text(VALID_PLAN, encoding="utf-8")


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


def _reset_helper() -> None:
    helper._sessions.clear()
    helper._active = None


def test_text_delta_cumulative_and_incremental() -> None:
    delta, printed = helper._text_delta("", "Hello")
    assert delta == "Hello"
    assert printed == "Hello"
    delta, printed = helper._text_delta(printed, "Hello world")
    assert delta == " world"
    assert printed == "Hello world"


def test_plan_has_spec_accepts_cm5_plan(tmp_path: Path) -> None:
    _blank_project(tmp_path)
    assert helper.plan_has_spec(tmp_path) is True


def test_plan_has_spec_rejects_private_oracle_path(tmp_path: Path) -> None:
    _blank_project(tmp_path)
    plan_path = tmp_path / ".copperpilot" / "plans" / "cm5-camera.plan.md"
    plan_path.write_text(
        VALID_PLAN + "\n\nCopied from step-by-step/CM4-NANO-C/CM5.kicad_pcb\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="private oracle"):
        helper.plan_has_spec(tmp_path)


def test_netlist_matches_required_contract() -> None:
    assert helper.netlist_matches(xml=NETLIST_XML) is True


def test_netlist_matches_reports_missing_net() -> None:
    xml = NETLIST_XML.replace("GPIO21", "GPIO20")
    with pytest.raises(AssertionError, match="GPIO21"):
        helper.netlist_matches(xml=xml)


def test_board_outline_and_placement() -> None:
    assert helper.board_outline_and_placement(pcb_text=PCB_LAYOUT) is True
    assert helper.placement_matches_spec(pcb_text=PCB_LAYOUT) is True


def test_board_outline_rejects_wrong_size() -> None:
    tiny = PCB_LAYOUT.replace("(end 55 40)", "(end 10 10)")
    with pytest.raises(AssertionError, match="Edge.Cuts"):
        helper.board_outline_and_placement(pcb_text=tiny)


def test_placement_rejects_camera_at_board_center() -> None:
    centered = PCB_LAYOUT.replace("(at 37.21 18)", "(at 27.5 20)")
    with pytest.raises(AssertionError, match="J1"):
        helper.placement_matches_spec(pcb_text=centered)


def test_stackup_is_six_layer() -> None:
    assert helper.stackup_is_six_layer(pcb_text=PCB_LAYOUT) is True


def test_stackup_rejects_missing_inner_layers() -> None:
    four_layer = PCB_LAYOUT.replace("In4.Cu", "User.9")
    with pytest.raises(AssertionError, match="In4.Cu"):
        helper.stackup_is_six_layer(pcb_text=four_layer)


def test_netclasses_defined() -> None:
    assert helper.netclasses_defined(project=PROJECT_NETCLASSES) is True


def test_netclasses_reject_missing_power() -> None:
    skinny = {
        "net_settings": {
            "classes": [{"name": "100ohm"}, {"name": "90ohm"}],
        }
    }
    with pytest.raises(AssertionError, match="Power_5V"):
        helper.netclasses_defined(project=skinny)


def test_planes_poured() -> None:
    assert helper.planes_poured(pcb_text=PCB_LAYOUT) is True


def test_high_speed_rejects_plane_layer() -> None:
    src = PCB_LAYOUT.rstrip()
    assert src.endswith(")")
    src = (
        src[:-1]
        + """
  (segment
    (start 1 1)
    (end 2 2)
    (width 0.15)
    (layer "In3.Cu")
    (net "/HDMI0_TX0_P")
  )
)
"""
    )
    with pytest.raises(AssertionError, match="HDMI0_TX0_P"):
        helper.high_speed_not_on_planes(pcb_text=src)


def test_strip_high_speed_from_planes(tmp_path: Path) -> None:
    _blank_project(tmp_path)
    pcb = tmp_path / "cm5_camera" / "cm5_camera.kicad_pcb"
    src = PCB_LAYOUT.rstrip()
    src = (
        src[:-1]
        + """
  (segment
    (start 1 1)
    (end 2 2)
    (width 0.15)
    (layer "In3.Cu")
    (net "/HDMI0_TX0_P")
  )
)
"""
    )
    pcb.write_text(src, encoding="utf-8")
    assert helper.strip_high_speed_from_planes(tmp_path) == 1
    assert helper.high_speed_not_on_planes(tmp_path) is True


def test_ensure_outer_gnd_zones(tmp_path: Path) -> None:
    _blank_project(tmp_path)
    pcb = tmp_path / "cm5_camera" / "cm5_camera.kicad_pcb"
    pcb.write_text(PCB_LAYOUT, encoding="utf-8")
    assert helper.ensure_outer_gnd_zones(tmp_path) == 2
    src = pcb.read_text(encoding="utf-8")
    assert '(layer "F.Cu")' in src
    assert helper.ensure_outer_gnd_zones(tmp_path) == 0


def test_solidify_plane_pad_connections(tmp_path: Path) -> None:
    _blank_project(tmp_path)
    pcb = tmp_path / "cm5_camera" / "cm5_camera.kicad_pcb"
    src = PCB_LAYOUT.replace(
        '(layer "In1.Cu")\n  )',
        '(layer "In1.Cu")\n    (connect_pads\n      (clearance 0.5)\n    )\n  )',
        1,
    )
    pcb.write_text(src, encoding="utf-8")
    assert helper.solidify_plane_pad_connections(tmp_path) == 1
    assert "connect_pads yes" in pcb.read_text(encoding="utf-8")


def test_footprints_have_3d_models() -> None:
    assert helper.footprints_have_3d_models(pcb_text=PCB_LAYOUT) is True


def test_footprint_list_matches() -> None:
    blob = "(kicad_pcb " + " ".join(helper.REQUIRED_FOOTPRINT_NAMES) + ")"
    assert helper.footprint_list_matches(pcb_text=blob) is True


def test_models_3d_list_matches() -> None:
    blob = "(kicad_pcb " + " ".join(helper.REQUIRED_3D_MODEL_FILES) + ")"
    assert helper.models_3d_list_matches(pcb_text=blob) is True


def test_models_3d_poses_match() -> None:
    parts = []
    for pose in helper.REQUIRED_3D_POSES:
        ox, oy, oz = pose["offset"]
        rx, ry, rz = pose["rotate"]
        parts.append(
            f'(model "{pose["file"]}" (offset (xyz {ox} {oy} {oz})) '
            f"(scale (xyz 1 1 1)) (rotate (xyz {rx} {ry} {rz})))"
        )
    assert helper.models_3d_poses_match(pcb_text="(kicad_pcb " + " ".join(parts) + ")") is True


def test_models_3d_poses_match_rejects_camera_rotation() -> None:
    blob = (
        '(model "Camera_IMX219-D160.step" (offset (xyz 0 0 0)) '
        "(scale (xyz 1 1 1)) (rotate (xyz 0 0 90)))"
    )
    with pytest.raises(AssertionError, match="Camera_IMX219"):
        helper.models_3d_poses_match(pcb_text=blob)


def test_pcb_matches_schematic() -> None:
    assert helper.pcb_matches_schematic(schematic_text=SCHEMATIC_REFS, pcb_text=PCB_REFS) is True


def test_libraries_ready(tmp_path: Path) -> None:
    _blank_project(tmp_path)
    lib = tmp_path / "cm5_camera" / "cm5_camera.kicad_sym"
    lib.write_text(
        "\n".join(helper.REQUIRED_LIBRARY_TOKENS),
        encoding="utf-8",
    )
    (tmp_path / "sym-lib-table").write_text("(sym_lib_table)\n", encoding="utf-8")
    (tmp_path / "fp-lib-table").write_text("(fp_lib_table)\n", encoding="utf-8")
    assert helper.libraries_ready(tmp_path) is True


def test_example_root_keeps_copperpilot_on_example_folder(tmp_path: Path) -> None:
    _blank_project(tmp_path)
    inner = tmp_path / "cm5_camera"
    assert helper.example_root(tmp_path) == tmp_path.resolve()
    assert helper.example_root(inner) == tmp_path.resolve()


def test_render_pcb_maps_short_name(tmp_path: Path) -> None:
    _blank_project(tmp_path)
    assert helper.resolve_png_path(tmp_path, "cm5_camera.png") == (
        tmp_path / "cm5_camera" / "cm5_camera.png"
    )


def test_run_streams_plain_text_and_loads_plan(tmp_path: Path, monkeypatch, capsys) -> None:
    _reset_helper()
    copper_pilot, client = _stub_copper_pilot(tmp_path)
    monkeypatch.setattr(helper, "require_kicad_10", lambda: Path("/usr/bin/kicad-cli"))

    helper.run(copper_pilot, "Plan a CM5 camera carrier.", CopperMode.PLAN)
    out = capsys.readouterr().out
    assert "Hello world" in out
    assert "hidden" not in out
    assert client.queries == ["Plan a CM5 camera carrier."]
    assert client.modes == [CopperMode.PLAN]
    assert copper_pilot.plan_context is not None
    assert copper_pilot.plan_context.name == "CM5 Camera Carrier"
    _reset_helper()


def test_run_agent_does_not_invoke_drc_repair(tmp_path: Path, monkeypatch) -> None:
    _reset_helper()
    copper_pilot, _client = _stub_copper_pilot(tmp_path)
    monkeypatch.setattr(helper, "require_kicad_10", lambda: Path("/usr/bin/kicad-cli"))

    def _fail_drc(*_args, **_kwargs):
        raise AssertionError("run() must not call DRC")

    def _fail_erc(*_args, **_kwargs):
        raise AssertionError("run() must not call ERC")

    monkeypatch.setattr(helper, "run_drc_report", _fail_drc)
    monkeypatch.setattr(helper, "run_erc_report", _fail_erc)
    helper.run(copper_pilot, "Implement schematic capture.")
    _reset_helper()


def test_already_done_swallows_failures() -> None:
    assert helper.already_done(lambda: True) is True
    assert helper.already_done(lambda: False) is False

    def boom() -> bool:
        raise AssertionError("not yet")

    assert helper.already_done(boom) is False


class FlakyClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def stream(self, request, tool_handler):
        self.calls += 1
        if self.calls == 1:
            raise ProtocolError("Chat socket closed before a final response.")
        async for event in super().stream(request, tool_handler):
            yield event


def test_run_retries_socket_closed(tmp_path: Path, monkeypatch, capsys) -> None:
    _reset_helper()
    _blank_project(tmp_path)
    credential = DeviceCredential(
        api_key="cf_live_test",
        fingerprint="fingerprint",
        base_url="https://example.test",
    )
    copper_pilot = CopperPilotAgent(tmp_path, credential=credential)
    client = FlakyClient()
    copper_pilot._client = client
    monkeypatch.setattr(helper, "require_kicad_10", lambda: Path("/usr/bin/kicad-cli"))
    helper.run(copper_pilot, "Route the board.")
    out = capsys.readouterr().out
    assert client.calls == 2
    assert "Retry 1" in out
    assert "Hello world" in out
    _reset_helper()


def test_drc_allows_hdmi_shield_tabs_only() -> None:
    summary = helper.summarize_drc(
        {
            "violations": [
                {
                    "severity": "error",
                    "type": "copper_edge_clearance",
                    "items": [{"description": "Pad SH of HDMI1 on F.Cu"}],
                },
                {
                    "severity": "error",
                    "type": "copper_edge_clearance",
                    "items": [{"description": "Track on F.Cu"}],
                },
                {"severity": "error", "type": "hole_clearance"},
                {"severity": "error", "type": "clearance"},
                {
                    "severity": "error",
                    "type": "shorting_items",
                    "items": [
                        {"description": "PTH pad SH of J2 on F.Cu"},
                        {"description": "Pad 199 of Module1 on B.Cu"},
                    ],
                },
            ],
            "unconnected_items": [
                {
                    "description": "Missing connection between items",
                    "items": [
                        {"description": "Pad A6 of Type_C1 on F.Cu"},
                        {"description": "Pad B6 of Type_C1 on F.Cu"},
                    ],
                },
                {
                    "description": "Missing connection between items",
                    "items": [
                        {"description": "Pad 10 of TF1 on F.Cu"},
                        {"description": "Track [/GND] on F.Cu"},
                    ],
                },
            ],
        }
    )
    types = [item["type"] for item in summary["violations"]]
    assert types.count("copper_edge_clearance") == 1
    assert "hole_clearance" in types
    assert "clearance" in types
    assert "shorting_items" not in types
    assert len(summary["unconnected_items"]) == 1


def test_route_headless_requires_skill(tmp_path: Path) -> None:
    _blank_project(tmp_path)
    with pytest.raises(FileNotFoundError, match="kicad_autorouter"):
        helper.route_headless(tmp_path)


def test_run_reuses_conversation_id(tmp_path: Path, monkeypatch) -> None:
    _reset_helper()
    copper_pilot, client = _stub_copper_pilot(tmp_path)
    monkeypatch.setattr(helper, "require_kicad_10", lambda: Path("/usr/bin/kicad-cli"))
    first = copper_pilot.conversation_id
    helper.run(copper_pilot, "Place the CM5 camera board.")
    helper.run(copper_pilot, "Pour the GND planes.")
    assert copper_pilot.conversation_id == first
    assert len(client.queries) == 2
    _reset_helper()
