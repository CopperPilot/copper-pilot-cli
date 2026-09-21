"""Build a CM5 camera carrier with CopperPilot."""

from __future__ import annotations

import sys
from pathlib import Path

from copper_pilot_cli.copper_auth import AuthenticationError
from copper_pilot_cli.copper_protocol import CopperMode
from copper_pilot_cli.langchain import CopperPilotAgent

from helper import (
    already_done,
    discard_stale_plan,
    drc_violations,
    ensure_outer_gnd_zones,
    erc_errors,
    footprint_list_matches,
    footprints_have_3d_models,
    format_netlist_contract,
    format_placement_spec,
    high_speed_not_on_planes,
    hosted_plan_prompt,
    inject_feedback,
    libraries_ready,
    lock_diff_tracks,
    lock_placement,
    models_3d_list_matches,
    netlist_matches,
    pcb_matches_schematic,
    pcb_segment_count,
    placement_feedback,
    placement_matches_spec,
    plan_has_spec,
    refill_zones,
    render_pcb,
    repair_drc,
    repair_erc,
    reset_pcb_copper,
    restore_pcb_if_wiped,
    route_headless,
    run,
    snapshot_pcb,
    solidify_plane_pad_connections,
    start,
    strip_high_speed_from_planes,
    telemetry_prompt,
    unconnected_count,
    zones_and_rules_ready,
)

PLAN = CopperMode.PLAN
WORKSPACE_PATH = Path(__file__).resolve().parent

PLAN_PROMPT = hosted_plan_prompt()
LIBS_PROMPT = (
    "Set up symbols and footprints for this CM5 camera project, reverse-engineered "
    "from Waveshare CM4-NANO-C (https://www.waveshare.com/wiki/CM4-NANO-C) and "
    "aligned with https://github.com/rapidanalysis/xerxes DF40 practice. "
    "Goal: project libraries must include every BOM row from the plan: dual DF40 "
    "Raspberry-Pi-4-Compute-Module / DF40C-100DS, Camera_Module_IMX219-D160, "
    "HDMI_Mini-C_Molex_47151-0001, Molex_200528-0150 DSI, MOLEX_USB_67298-4090 USB-A, "
    "USB_C GCT USB4105, SDCARD_MOLEX_503398-1892, PTS645 Key1, SRP5030CC L7, "
    "FSUSB42UMX UQFN-10, MP1658GTF-Z TSOT-23-6, DIO7003/STMPS2171/RT9193/RT9013 "
    "SOT-23-5, MCF1210 ACM1210 chokes, Texas_DRT-3, 24 MHz 3225 crystal, 0402/0805 "
    "passives, PinHeader_2x20 and 1x03. Assign the 3D models listed in the plan."
)
SCHEMATIC_PROMPT = (
    "Implement schematic capture in cm5_camera/cm5_camera.kicad_sch from the plan "
    "BOM and netlist. Reverse-derive connectivity from the Waveshare CM4-NANO-C "
    "wiki schematic; keep those net names (`CM4_5V`, `1V8`, `2V8`, `1V2`, `CSI_*`, "
    "`CAM0_*`, `HDMI0_*`). Goal: kicad-cli `sch export netlist --format kicadxml` "
    "must contain these nets and connections:\n"
    f"{format_netlist_contract()}\n"
    "Populate every BOM ref from the plan, including H3/H4/H5, L5/L6, Q1/Q2, FB1, L7."
)
ERC_PROMPT = (
    "Run kicad-cli sch erc on cm5_camera/cm5_camera.kicad_sch and fix pin types, "
    "power flags, and floating inputs. Goal: zero ERC errors."
)
UPDATE_PCB_PROMPT = (
    "Update PCB footprints from the schematic. Goal: every schematic symbol with a "
    "footprint is on cm5_camera/cm5_camera.kicad_pcb using the exact footprint names "
    "from the plan, extra footprints are removed, and the netlist is imported."
)
LAYOUT_PROMPT = (
    "Lay out cm5_camera/cm5_camera.kicad_pcb from a wiped copper state to match the "
    "Waveshare CM4-NANO-C photos and the plan placement table. "
    "Goal: Edge.Cuts 55.0 x 40.0 mm with 3.0 mm corner radii and 4x M2.5 holes at "
    "board-local 3.5,3.5 / 3.5,36.5 / 51.5,3.5 / 51.5,36.5. Place and lock "
    "footprints to this board-local table (origin = SW of Edge.Cuts). J1 is east "
    "of center, not the board middle. HDMI1/DSI1 north, J2 east, TF1 west, "
    "Type_C1 south-west, PI1 west, Key1 south-east, DF40 on B.Cu. Silk: CM5 CAMERA.\n"
    f"{format_placement_spec()}"
)
ZONES_PROMPT = (
    "Configure the 6-layer SIG-GND-SIG-PWR-GND-SIG stackup and pour planes. "
    "Goal: F.Cu, In1.Cu, In2.Cu, In3.Cu, In4.Cu, B.Cu present; solid GND zone on "
    "In1.Cu and In4.Cu; split CM4_5V and PWR_3V3 zones on In3.Cu; netclasses "
    "100Ohm_Diff (0.15 mm / 0.18 mm gap), 90Ohm_Diff (0.18 mm / 0.20 mm), "
    "Power_5V, Power_3V3 in cm5_camera.kicad_pro; refill zones. Do not autoroute."
)
ACK_PROMPT = (
    "Reply with the single word OK. Do not call tools. Do not edit files. "
    "Do not run kicad_autorouter or FreeRouting. Local headless routing already "
    "imported copper."
)
MODELS_3D_PROMPT = (
    "Inspect kicad-cli pcb render of cm5_camera/cm5_camera.kicad_pcb and assign "
    "every 3D model listed in the plan (DF40C-100DS, Camera_IMX219-D160, "
    "JTHDA-19F08, USB4105, USB-A wrl, 5033981892, SRP5030CC, PTS645, ACM1210, "
    "and KiCad SMD passives). Goal: the unique 3D model list matches the plan; "
    "re-render the top side."
)


def main() -> None:
    copper_pilot = CopperPilotAgent(workspace=WORKSPACE_PATH)
    start(copper_pilot)
    discard_stale_plan()
    if not already_done(plan_has_spec):
        run(copper_pilot, PLAN_PROMPT, PLAN)
    if not already_done(plan_has_spec):
        run(
            copper_pilot,
            "Write `.copperpilot/plans/cm5-camera-carrier.plan.md` now as your first "
            "tool call. Copy every table, footprint name, 3D model, coordinate, and "
            "net name from the previous PLAN prompt verbatim. Include YAML todos for "
            "libraries, schematic, ERC, outline/placement, zones/stackup/netclass, "
            "routing, DRC and 3D. Cite Waveshare CM4-NANO-C, xerxes, cm5_minima, "
            "Raspberry Pi CM5IO, Makerforge, ShawnHymel CM4 template. Do not web "
            "search. Do not finish without that file.",
            PLAN,
        )
    assert plan_has_spec()
    if "--plan-only" in sys.argv:
        print("Plan-only: hosted plan aligned.", flush=True)
        return
    if not already_done(libraries_ready):
        run(copper_pilot, LIBS_PROMPT)
    assert libraries_ready()
    if not already_done(netlist_matches):
        run(copper_pilot, SCHEMATIC_PROMPT)
    assert netlist_matches()
    if not already_done(lambda: erc_errors() == 0):
        run(copper_pilot, ERC_PROMPT)
        repair_erc()
    assert erc_errors() == 0
    if not already_done(pcb_matches_schematic):
        run(copper_pilot, UPDATE_PCB_PROMPT)
    assert pcb_matches_schematic()
    assert footprint_list_matches()

    has_routes = pcb_segment_count() >= 50
    keep_zones = already_done(placement_matches_spec) and already_done(zones_and_rules_ready)
    if not has_routes:
        reset_pcb_copper(keep_zones=keep_zones)
        inject_feedback(
            "Tracks and vias were wiped"
            + ("." if keep_zones else ", along with zones.")
            + " Rebuild layout, stackup, and routing from the CM5 camera spec in this "
            "same conversation."
        )
    if not already_done(placement_matches_spec):
        run(copper_pilot, LAYOUT_PROMPT)
        if not already_done(placement_matches_spec):
            run(copper_pilot, placement_feedback())
        if not already_done(placement_matches_spec):
            locked = lock_placement()
            inject_feedback(
                "Locked these footprints to the board-local assembly table: "
                + ", ".join(locked)
                + ". Continue from this placement; do not scatter parts."
            )
    assert placement_matches_spec()

    if not already_done(zones_and_rules_ready):
        run(copper_pilot, ZONES_PROMPT)
        if not already_done(zones_and_rules_ready):
            run(
                copper_pilot,
                "Finish the 6-layer stackup, 100Ohm_Diff / 90Ohm_Diff / Power_5V / "
                "Power_3V3 netclasses, GND pours on In1.Cu and In4.Cu, and CM4_5V plus "
                "PWR_3V3 pours on In3.Cu. Do not autoroute.",
            )
    assert zones_and_rules_ready()

    if not has_routes:
        diffs = route_headless(stage="diffs")
        if strip_high_speed_from_planes():
            diffs = route_headless(stage="diffs", passes=3)
            strip_high_speed_from_planes()
        lock_diff_tracks()
        leftover = unconnected_count()
        copper = snapshot_pcb()
        inject_feedback(telemetry_prompt(diffs, leftover=leftover))
        run(copper_pilot, ACK_PROMPT)
        restore_pcb_if_wiped(copper)
        assert high_speed_not_on_planes()

    ensure_outer_gnd_zones()
    solidify_plane_pad_connections()
    if pcb_segment_count() < 700:
        gpio = route_headless(stage="gpio")
        if unconnected_count() > 0:
            gpio = route_headless(stage="gpio", passes=12)
        leftover = unconnected_count()
        copper = snapshot_pcb()
        inject_feedback(telemetry_prompt(gpio, leftover=leftover))
        run(copper_pilot, ACK_PROMPT)
        restore_pcb_if_wiped(copper)
    refill_zones()
    if drc_violations():
        repair_drc()
    if not already_done(footprints_have_3d_models) or not already_done(models_3d_list_matches):
        run(copper_pilot, MODELS_3D_PROMPT)
    assert footprints_have_3d_models()
    assert models_3d_list_matches()
    render_pcb("cm5_camera.png")
    assert high_speed_not_on_planes()
    assert drc_violations() == 0
    leftover = unconnected_count()
    assert leftover == 0, f"{leftover} unconnected items remain"


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (AssertionError, AuthenticationError, FileNotFoundError, RuntimeError) as exc:
        sys.stderr.write(f"cm5 camera example: {exc}\n")
        raise SystemExit(1) from exc
