"""Shared helpers for the CM5 camera LangChain example."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from copper_pilot_cli.copper_protocol import (
    CopperMode,
    EventKind,
    PlanContext,
    PlanTodo,
    ProtocolError,
)
from copper_pilot_cli.copper_tools import ApprovalMode, LocalToolBroker
from copper_pilot_cli.langchain import CopperPilotAgent

WORKSPACE = Path(__file__).resolve().parent
PROJECT_NAME = "cm5_camera"
SCHEMATIC = WORKSPACE / PROJECT_NAME / f"{PROJECT_NAME}.kicad_sch"
PCB = WORKSPACE / PROJECT_NAME / f"{PROJECT_NAME}.kicad_pcb"
PNG = WORKSPACE / PROJECT_NAME / f"{PROJECT_NAME}.png"
REPORT_DIR = WORKSPACE / ".copperpilot" / "tmp"
ERC_REPORT = REPORT_DIR / f"{PROJECT_NAME}-erc.json"
DRC_REPORT = REPORT_DIR / f"{PROJECT_NAME}-drc.json"
NETLIST_REPORT = REPORT_DIR / f"{PROJECT_NAME}.net"
IGNORED_SEVERITIES = {"excluded", "ignore", "ignored"}
FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
VERSION_RE = re.compile(r"(\d+)\.(\d+)")
REFERENCE_RE = re.compile(r'\(property\s+"Reference"\s+"([^"]+)"')
AT_RE = re.compile(
    r"\(at\s+([+-]?\d+(?:\.\d+)?)\s+([+-]?\d+(?:\.\d+)?)(?:\s+([+-]?\d+(?:\.\d+)?))?"
)
LAYER_RE = re.compile(r'\(layer\s+"([^"]+)"')
NET_NAME_RE = re.compile(r'\(net\s+"([^"]+)"')
NET_NAME_FIELD_RE = re.compile(r'\(net_name\s+"([^"]+)"')
THICKNESS_RE = re.compile(r"\(thickness\s+([+-]?\d+(?:\.\d+)?)")
LOCKED_RE = re.compile(r"\(locked\s+yes\)")
SHIELD_TAB_REFS = ("HDMI1", "J2")
EDGE_CUTS_RE = re.compile(r"Edge\.Cuts")
COORD_RE = re.compile(r"\((?:start|end|mid|center)\s+([+-]?\d+(?:\.\d+)?)\s+([+-]?\d+(?:\.\d+)?)")
MODEL_RE = re.compile(r"\(model\b")
SKIP_REFS_RE = re.compile(r"^(#|PWR|GND|FLAG)", re.IGNORECASE)
HOLE_REF_RE = re.compile(r"^(?:MH|HOLE)\d+$", re.IGNORECASE)
PRIVATE_ORACLE_RE = re.compile(r"step-by-step|CM4-NANO-C/CM5\.kicad", re.IGNORECASE)
KICAD_CLI_CANDIDATES = (
    Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"),
    Path("/Applications/KiCad 10.0/KiCad.app/Contents/MacOS/kicad-cli"),
    Path("/usr/bin/kicad-cli"),
    Path("/usr/local/bin/kicad-cli"),
    Path("C:/Program Files/KiCad/10.0/bin/kicad-cli.exe"),
    Path("C:/Program Files/KiCad/bin/kicad-cli.exe"),
)
TURN_IDLE_TIMEOUT_S = 3_600.0
TURN_FIRST_FRAME_TIMEOUT_S = 180.0
MAX_ERC_REPAIR_TURNS = 1
MAX_DRC_REPAIR_TURNS = 5
MAX_TURN_RETRIES = 2
SOCKET_CLOSED = "Chat socket closed before a final response."
BOARD_WIDTH_MM = 55.0
BOARD_HEIGHT_MM = 40.0
BOARD_SIZE_TOLERANCE_MM = 0.5
OUTLINE_MARGIN_MM = 2.0
STACKUP_THICKNESS_MM = 1.6
STACKUP_THICKNESS_TOLERANCE_MM = 0.15
CONNECTOR_TOLERANCE_MM = 1.0
IC_TOLERANCE_MM = 1.5
ROTATION_TOLERANCE_DEG = 5.0
COPPER_LAYERS = ("F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu")
PLANE_LAYERS = frozenset({"In1.Cu", "In3.Cu", "In4.Cu"})
DIFF_NET_NEEDLES = ("HDMI", "CAM0", "CSI", "DSI1", "USB0", "USBD", "USBB")
REQUIRED_NETCLASSES = ("100Ohm_Diff", "90Ohm_Diff", "Power_5V", "Power_3V3")
NETCLASS_ALIASES: dict[str, tuple[str, ...]] = {
    "100ohm diff": ("100ohm diff", "100ohm"),
    "90ohm diff": ("90ohm diff", "90ohm"),
    "power 5v": ("power 5v",),
    "power 3v3": ("power 3v3",),
}
ROUTE_TIMEOUT_SEC = 900
ROUTE_PASSES = 6
FREEROUTING_VERSION = "2.4.1"
POWER_IGNORE_CLASSES = ("GND", "Power_5V", "Power_3V3", "Power_Low")
DIFF_STAGE_IGNORE = (
    *POWER_IGNORE_CLASSES,
    "Default_IO",
    "Default",
    "kicad_default",
)
GPIO_STAGE_IGNORE = ("100Ohm_Diff", "90Ohm_Diff")
COPPER_FORM_HEADS = ("segment", "via", "zone", "arc")


def _load_assembly_spec() -> dict[str, Any]:
    path = Path(__file__).resolve().parent / "assembly_spec.json"
    return json.loads(path.read_text(encoding="utf-8"))


_ASSEMBLY = _load_assembly_spec()
REQUIRED_NETS: dict[str, tuple[str, ...]] = {
    name: tuple(refs) for name, refs in _ASSEMBLY["required_nets"].items()
}
REQUIRED_LIBRARY_TOKENS: tuple[str, ...] = (
    "DF40",
    "IMX219",
    "HDMI_Mini",
    "FSUSB42",
    "MP1658",
    "DIO7003",
    "STMPS2171",
    "200528",
    "RT9193",
    "RT9013",
    "MCF1210",
    "DMG1012",
    "USB4105",
    "PTS645",
    "SRP5030",
    "503398",
    "67298",
    "47151",
)
REQUIRED_3D_REFS: tuple[str, ...] = (
    "J1",
    "HDMI1",
    "Type_C1",
    "J2",
    "TF1",
    "PI1",
)
REQUIRED_3D_MODEL_FILES: tuple[str, ...] = tuple(
    item["file"] for item in _ASSEMBLY["unique_models"]
)
REQUIRED_3D_POSES: tuple[dict[str, Any], ...] = tuple(_ASSEMBLY["3d_poses"])
REQUIRED_FOOTPRINT_NAMES: tuple[str, ...] = tuple(_ASSEMBLY["unique_footprints"])
MODULE_REF_ALIASES: tuple[str, ...] = ("Module1", "CM5", "H1A", "U_CM5")
REQUIRED_PLACEMENT_REFS: tuple[str, ...] = (
    "J1",
    "HDMI1",
    "DSI1",
    "J2",
    "TF1",
    "PI1",
    "Type_C1",
    "Key1",
    "U5",
    "X1",
    "L1",
    "L2",
    "L3",
    "U1",
    "U2",
    "U3",
    "U6",
)
PLACEMENT_SPEC: dict[str, dict[str, Any]] = {
    ref: {
        "x": spec["x"],
        "y": spec["y"],
        "rot": spec["rot"],
        "layer": spec["layer"],
        "tol": spec["tol"],
    }
    for ref, spec in _ASSEMBLY["placement"].items()
}

PLAN_REQUIRED_NEEDLES: tuple[str, ...] = (
    "55 0",
    "40 0",
    "3 0",
    "m2 5",
    "6 layer",
    "sig gnd sig pwr gnd sig",
    "in1 cu",
    "in3 cu",
    "in4 cu",
    "100ohm diff",
    "90ohm diff",
    "0 15",
    "0 18",
    "37 21",
    "8 00",
    "51 30",
    "lock",
    "ignore net class",
    "df40",
    "h1a",
    "h1b",
    "imx219",
    "waveshare",
    "cm4 nano c",
    "xerxes",
    "cm5 minima",
    "rt9193 28gb",
    "rt9193 18gb",
    "rt9013 12",
    "1v8",
    "2v8",
    "1v2",
    "mclk",
    "dmg1012t",
    "mcf1210",
    "cam0 d0",
    "cam0 d1",
    "cam0 clk",
    "cm4 5v",
    "mp1658",
    "pwr 3v3",
    "dio7003",
    "hdmi 5v",
    "stmps2171",
    "usb 5v",
    "fsusb42",
    "gpio21",
    "mini hdmi",
    "47151",
    "200528",
    "67298",
    "503398",
    "usb4105",
    "pts645",
    "srp5030",
    "df40c 100ds",
    "jthda 19f08",
    "3d pose",
    "52 25",
    "135 95",
    "28 385",
)

PLAN_TODO_GROUPS: tuple[tuple[str, ...], ...] = (
    ("library", "symbol", "footprint"),
    ("schematic",),
    ("erc",),
    ("outline", "edge", "placement", "layout"),
    ("zone", "stackup", "netclass", "plane"),
    ("rout", "autorout", "freerouting"),
    ("drc", "3d", "render"),
    ("audit", "pose"),
)

LIBRARY_SUFFIXES = {".kicad_sym", ".kicad_mod", ".kicad_sch", ".kicad_pcb"}
LIBRARY_NAMES = {"sym-lib-table", "fp-lib-table"}
SKIP_DIR_NAMES = {".git", "__pycache__", "tmp", "oracle", "references"}


@dataclass
class _Session:
    copper_pilot: CopperPilotAgent
    workspace: Path
    cli: Path
    messages: list[BaseMessage] = field(default_factory=list)
    erc: dict[str, Any] | None = None
    drc: dict[str, Any] | None = None


_sessions: dict[int, _Session] = {}
_active: _Session | None = None


def configure_stdio() -> None:
    """Flush assistant text as it arrives, even when stdout is not a TTY."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(line_buffering=True, write_through=True)
        except (OSError, ValueError):
            continue


def print_line(message: str = "") -> None:
    sys.stdout.write(f"{message}\n")
    sys.stdout.flush()


def stream_text(chunk: str) -> None:
    sys.stdout.write(chunk)
    sys.stdout.flush()


def announce_phase(title: str, prompt: str | None = None) -> None:
    print_line()
    print_line(f"=== {title} ===")
    if prompt:
        print_line(prompt)
    print_line()


def format_netlist_contract() -> str:
    """Render REQUIRED_NETS as `NET: REF, REF` lines for prompts and asserts."""

    return "\n".join(f"{net}: {', '.join(refs)}" for net, refs in REQUIRED_NETS.items())


def format_placement_spec() -> str:
    """Render board-local placement as prompt lines."""

    lines: list[str] = []
    for ref, spec in PLACEMENT_SPEC.items():
        lines.append(
            f"{ref}: {spec['x']:.2f}, {spec['y']:.2f} mm, {spec['rot']:g} deg, "
            f"{spec['layer']} (board-local from SW of Edge.Cuts, tol ±{spec['tol']} mm)"
        )
    return "\n".join(lines)


def format_bom_table() -> str:
    """Render ref / value / symbol / footprint / 3D / pose rows for the plan prompt."""

    lines = [
        "REF | VALUE | SYMBOL | FOOTPRINT | 3D MODEL | LAYER | X | Y | ROT",
        "--- | --- | --- | --- | --- | --- | --- | --- | ---",
    ]
    for row in _ASSEMBLY["bom"]:
        models = ", ".join(row.get("models") or []) or "(none)"
        lines.append(
            f"{row['ref']} | {row['value']} | {row['lib_id']} | {row['footprint']} | "
            f"{models} | {row['layer']} | {row['x']:.2f} | {row['y']:.2f} | {row['rot']:g}"
        )
    return "\n".join(lines)


def format_footprint_list() -> str:
    """Render the unique footprint names the PCB must use."""

    return "\n".join(f"- {name}" for name in REQUIRED_FOOTPRINT_NAMES)


def format_3d_model_list() -> str:
    """Render the unique 3D model files the PCB must assign."""

    return "\n".join(f"- {item['file']}  (`{item['path']}`)" for item in _ASSEMBLY["unique_models"])


def hosted_plan_prompt() -> str:
    """Return the PLAN-mode prompt the CopperPilot agent must copy into plan.md."""

    return (
        "CRITICAL: your FIRST tool call this turn MUST write "
        "`.copperpilot/plans/cm5-camera-carrier.plan.md`. Copy the YAML todos and every "
        "table below into that file. Do not finish the turn without that file. Do not "
        "spend the turn on web search; the tables and reference list below are "
        "authoritative. Optional extra URLs go in an Additional references section "
        "after the file exists.\n"
        "\n"
        "Plan a Raspberry Pi Compute Module 5 nano camera carrier in exhaustive detail.\n"
        "Goal: write `.copperpilot/plans/cm5-camera-carrier.plan.md` whose body copies "
        "every table below verbatim, then adds a Clarifications section for anything "
        "still ambiguous.\n"
        "\n"
        "## Mission\n"
        "Reverse-engineer the Waveshare **CM4-NANO-C** nano camera carrier as a CM5 board. "
        "The product to clone is https://www.waveshare.com/wiki/CM4-NANO-C "
        "(also https://www.waveshare.com/product/cm4-nano-c.htm ). "
        "Download the published schematic, mechanical drawing, and 3D render from the wiki "
        "Resources section and derive pin functions, connector choice, and outline from those. "
        "Retarget the mezzanine to **CM5** dual Hirose DF40 (H1A/H1B). Keep the published "
        "NANO-C net names (`CM4_5V`, `CM4_3V3`, `CM4_1V8`, `1V8`, `2V8`, `1V2`, `CSI_*`, "
        "`CAM0_*`, `HDMI0_*`, `DSI1_*`) so the schematic netlist matches the NANO-C carrier.\n"
        "\n"
        "## Public references (study these DURING planning; cite URLs in the plan)\n"
        "You MUST find and list multiple CM5/CM4 carrier references before freezing symbols "
        "or coordinates. Start here, then search for more:\n"
        "1. PRIMARY product: Waveshare CM4-NANO-C wiki + product page. This is the board we "
        "are electrically and mechanically cloning (55 x 40 mm, IMX219-D160, Mini-HDMI, "
        "15-pin DSI, USB-A, USB-C power+program, Micro SD, 40-pin GPIO, GPIO21 key, BOOT, "
        "ACT/PWR LEDs). There is NO Ethernet, M.2, or PoE on this nano camera board.\n"
        "2. CLOSEST open KiCad CM4/CM5 DF40 carrier: https://github.com/rapidanalysis/xerxes "
        "— use this for Hirose DF40C-100DS footprints, CM5 pinout, and KiCad library "
        "practice. Do NOT copy Xerxes features (Gigabit Ethernet, M.2, PoE+, Qwiic, fan) "
        "onto this 55 x 40 mm camera carrier.\n"
        "3. KiCad demo `cm5_minima` (KiCad source demos + "
        "https://github.com/piecol/CM5_MINIMA_REV3 ) — 6-layer CM5 carrier, USB-C PD, HDMI, "
        "CSI/DSI. Size is 54 x 57 mm, NOT our outline. Use for CM5 stackup/pinout only.\n"
        "4. Raspberry Pi Compute Module 5 datasheet and CM5IO board: confirm DF40 H1A/H1B "
        "pin compatibility with the CM4 nets used here (CSI0, DSI1, HDMI0, USB2.0, SD, "
        "GPIO 2-27, 5V/3V3/1V8).\n"
        "5. Additional public CM5/CM4 carriers to cite in the plan (already selected; "
        "do not block on live search). If any conflicts with Waveshare CM4-NANO-C, "
        "Waveshare wins for this clone:\n"
        "   - Raspberry Pi Compute Module 5 IO Board + CM5 datasheet "
        "(https://www.raspberrypi.com/documentation/computers/compute-module.html)\n"
        "   - Makerforge CM5 carrier design guide "
        "(https://www.makerforge.tech/posts/cm5-carrier-basics/)\n"
        "   - ShawnHymel CM4 carrier KiCad template "
        "(https://github.com/ShawnHymel/rpi-cm4-carrier-template)\n"
        "   - Sentinel Core CM5 mini-ITX (https://github.com/abg-research/sentinel-core) "
        "for CM5 DF40 pinout only — not the 55 x 40 mm outline.\n"
        "\n"
        "## Envelope (must be exact; do not invent a different shape)\n"
        "Board-local origin = south-west corner of Edge.Cuts.\n"
        "- Outline: 55.0 x 40.0 mm rectangle, 3.0 mm corner radii, Edge.Cuts width 0.1 mm.\n"
        "- 4x M2.5 mounting holes at 3.5,3.5 / 3.5,36.5 / 51.5,3.5 / 51.5,36.5.\n"
        "- Silk `CM5 CAMERA` on F.SilkS.\n"
        "- Dual DF40 keepout on B.Cu covering the CM5 module courtyard.\n"
        "- Visual: Mini-HDMI and 15-pin DSI along the NORTH edge; vertical USB-A on the "
        "NORTH-EAST; IMX219-D160 camera module east-of-center on F.Cu (J1 at 37.21, 18.00, "
        "NOT board center); Micro SD on the WEST; USB-C on the SOUTH-WEST; 40-pin GPIO along "
        "the WEST; GPIO21 key SOUTH-EAST; CM5 DF40 Module1 on B.Cu at 51.30, 36.42, 90 deg "
        "covering most of the back. If any of those orientations is unclear after the wiki "
        "photos and Xerxes, write it under Clarifications instead of guessing.\n"
        "\n"
        "## Stackup SIG-GND-SIG-PWR-GND-SIG\n"
        "F.Cu / In1.Cu GND / In2.Cu / In3.Cu PWR / In4.Cu GND / B.Cu, 1.6 mm, 35 um copper, "
        "dielectrics 0.10 then 0.51 then 0.14 then 0.51 then 0.10 mm. ENIG.\n"
        "Netclasses: 100Ohm_Diff width 0.15 mm gap 0.18 mm on F.Cu (never In1.Cu, In3.Cu, "
        "In4.Cu) for HDMI0/CSI/CAM0/DSI1; 90Ohm_Diff width 0.18 mm gap 0.20 mm for USB; "
        "Power_5V 0.80 mm; Power_3V3 0.60 mm. Ignore-net-class GND, Power_5V, Power_3V3, "
        "Power_Low while autorouting. Lock HDMI/CSI/DSI/USB tracks after the diff stage.\n"
        "\n"
        "## BOM / symbols / footprints / 3D / placement (copy verbatim)\n"
        "91 schematic symbols, 90 PCB footprints. Use these exact footprint names. Assign "
        "the listed 3D models (KiCad 10 official where given; project files "
        "`DF40C-100DS.stp`, `JTHDA-19F08.STEP`, `5033981892.stp`, `SRP5030CC.stp`, "
        "`Camera_IMX219-D160.step`, `CM4.step` / CM5 equivalent, USB-A wrl).\n"
        f"{format_bom_table()}\n"
        "\n"
        "## Unique PCB footprints (list must match)\n"
        f"{format_footprint_list()}\n"
        "\n"
        "## Unique 3D models (list must match)\n"
        f"{format_3d_model_list()}\n"
        "\n"
        "## Placement table (board-local mm from SW of Edge.Cuts)\n"
        f"{format_placement_spec()}\n"
        "\n"
        "## Schematic netlist contract (copy verbatim; these net names are required)\n"
        f"{format_netlist_contract()}\n"
        "\n"
        "## Electrical summary\n"
        "Dual 100-pin Hirose DF40 H1A/H1B for CM5 (`Raspberry-Pi-4-Compute-Module` / "
        "DF40C-100DS). IMX219-D160 (J1) with RT9193-28GB -> 2V8, RT9193-18GB -> 1V8, "
        "RT9013-12 -> 1V2, 24.000 MHz XO through 22 ohm to MCLK, DMG1012T level shifters "
        "on SDA/SCL, MCF1210 chokes on CAM0_D0 / CAM0_D1 / CAM0_CLK. USB-C into CM4_5V, "
        "MP1658GTF-Z (L7 SRP5030CC 4.7uH) to PWR_3V3, DIO7003 to HDMI_5V and SD_3.3V, "
        "STMPS2171 to USB_5V, FSUSB42UMX USB0 mux between Type-C and USB-A, Mini-HDMI "
        "(Molex 47151 / JTHDA-19F08), 15-pin DSI (Molex 200528-0150), Micro SD "
        "(Molex 503398-1892), 40-pin GPIO, Key1 PTS645 on GPIO21, H3 RUN/GLOBAL_EN, "
        "H4 BOOT/USB_SEL, H5 3V3/VREF/1V8 test header, ACT/PWR LEDs L5/L6.\n"
        "\n"
        "## Plan todos must cover\n"
        "libraries (symbols+footprints+3D), schematic capture, ERC, outline+placement lock, "
        "zones/stackup/netclass, routing (staged diffs then GPIO), DRC, 3D render, "
        "3D pose audit (camera/CM/HDMI/SD/DF40 offsets).\n"
        "\n"
        "## 3D pose table (copy into the plan)\n"
        "After assigning models, audit each offset/rotation against this table and "
        "re-render top+bottom. Camera STEP origin is the mezzanine (rotation 0 0 0, "
        "body along -X). Mini-HDMI JTHDA offset 0,-6.8,0 rotate -90 0 0. Micro SD "
        "503398 offset -135.95,-16.5,154.5 rotate 0,-180,-180. CM4.step offset "
        "52.25,-52,0 rotate 0 0 -90. DF40C-100DS at -0.46,-28.385,0 and 33.46,-28.385,0 "
        "rotate -90 0 0. USB-A uses the vertical STEP (front pocket, not a through-hole); "
        "keep the .wrl filename referenced. Generate a sophisticated IMX219-D160 STEP "
        "in a dedicated 3D-model phase if the library brick is too coarse. Micro SD "
        "503398 origin compensation is offset -135.95,-16.5,154.5.\n"
        "\n"
        "## Clarifications rule\n"
        "If connector pin-1, 3D model path, or a coordinate is not obvious from the public "
        "references above, add a `## Clarifications` heading with the question and the "
        "conservative choice. Do not silently invent a different outline, BOM, or net name.\n"
    )


def _kicad_cli_candidates() -> list[Path]:
    found: list[Path] = []
    which = shutil.which("kicad-cli") or shutil.which("kicad-cli.exe")
    if which:
        found.append(Path(which))
    found.extend(path for path in KICAD_CLI_CANDIDATES if path.is_file())
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in found:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def _parse_kicad_version(cli: Path) -> tuple[int, str]:
    completed = subprocess.run(  # noqa: S603
        [str(cli), "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    text = (completed.stdout or completed.stderr or "").strip()
    match = VERSION_RE.search(text)
    if match is None:
        raise RuntimeError(f"Could not parse kicad-cli version from {cli}: {text!r}")
    return int(match.group(1)), match.group(0)


def require_kicad_10() -> Path:
    """Locate kicad-cli and assert it is KiCad 10."""

    last_error = "kicad-cli was not found on PATH or in common install locations."
    for cli in _kicad_cli_candidates():
        try:
            major, label = _parse_kicad_version(cli)
        except RuntimeError as exc:
            last_error = str(exc)
            continue
        if major != 10:
            last_error = f"This example requires KiCad 10, found {label} at {cli}"
            continue
        bin_dir = str(cli.parent)
        path = os.environ.get("PATH", "")
        if bin_dir not in path.split(os.pathsep):
            os.environ["PATH"] = os.pathsep.join([bin_dir, path]) if path else bin_dir
        print_line(f"Using KiCad {label} at {cli}")
        return cli
    raise RuntimeError(last_error)


def _run_kicad(cli: Path, args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [str(cli), *args],
        check=False,
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def example_root(workspace: Path) -> Path:
    """Keep `.copperpilot/` on the example folder, not inside the KiCad project."""

    resolved = workspace.resolve()
    nested = resolved / PROJECT_NAME / f"{PROJECT_NAME}.kicad_sch"
    if nested.is_file():
        return resolved
    schematic = resolved / f"{PROJECT_NAME}.kicad_sch"
    if schematic.is_file() and resolved.name == PROJECT_NAME:
        return resolved.parent
    return resolved


def project_paths(workspace: Path) -> tuple[Path, Path, Path, Path, Path]:
    root = example_root(workspace)
    schematic = root / PROJECT_NAME / f"{PROJECT_NAME}.kicad_sch"
    pcb = root / PROJECT_NAME / f"{PROJECT_NAME}.kicad_pcb"
    png = root / PROJECT_NAME / f"{PROJECT_NAME}.png"
    report_dir = root / ".copperpilot" / "tmp"
    return root, schematic, pcb, png, report_dir


def resolve_png_path(workspace: Path, path: str = f"{PROJECT_NAME}.png") -> Path:
    root, _, _, png, _ = project_paths(workspace)
    if path in {f"{PROJECT_NAME}.png", f"{PROJECT_NAME}/{PROJECT_NAME}.png"}:
        return png
    candidate = Path(path)
    return candidate if candidate.is_absolute() else root / candidate


def load_latest_plan(workspace: Path) -> PlanContext:
    """Parse the newest `.copperpilot/plans/*.plan.md` into a PlanContext."""

    root = example_root(workspace)
    plans = sorted(
        (root / ".copperpilot" / "plans").glob("*.plan.md"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not plans:
        raise FileNotFoundError("Plan mode did not write a .copperpilot/plans/*.plan.md file.")
    path = plans[0]
    text = path.read_text(encoding="utf-8")
    match = FRONTMATTER_RE.match(text)
    metadata: dict[str, Any] = {}
    body = text.strip()
    if match:
        loaded = yaml.safe_load(match.group(1)) or {}
        if isinstance(loaded, dict):
            metadata = loaded
        body = text[match.end() :].strip()
    todos: list[PlanTodo] = []
    raw_todos = metadata.get("todos") or []
    if isinstance(raw_todos, list):
        for item in raw_todos:
            if not isinstance(item, dict):
                continue
            todo_id = str(item.get("id") or "").strip()
            content = str(item.get("content") or "").strip()
            status = str(item.get("status") or "open").strip().lower()
            if status not in {"open", "running", "done"}:
                status = "open"
            if todo_id and content:
                todos.append(PlanTodo(id=todo_id, content=content, status=status))
    overall = str(metadata.get("overall_status") or "open").strip().lower()
    if overall not in {"open", "running", "done"}:
        overall = "open"
    name = str(metadata.get("name") or "").strip() or path.name.removesuffix(".plan.md")
    overview = str(metadata.get("overview") or "").strip() or None
    return PlanContext(
        file_path=path.relative_to(root).as_posix(),
        name=name,
        overview=overview,
        overall_status=overall,  # type: ignore[arg-type]
        todos=todos,
        body=body or None,
    )


def _fold(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower())


def _plan_corpus(plan: PlanContext) -> str:
    parts = [plan.name, plan.overview or "", plan.body or ""]
    for todo in plan.todos:
        parts.extend([todo.id, todo.content])
    return "\n".join(parts)


def check_plan_spec(plan: PlanContext) -> None:
    """Raise AssertionError if the plan is missing spec text or cites the private oracle."""

    corpus = _plan_corpus(plan)
    forbidden = PRIVATE_ORACLE_RE.findall(corpus)
    if forbidden:
        raise AssertionError(
            f"Plan must not cite the private oracle path; found {sorted(set(forbidden))}"
        )
    folded = _fold(corpus)
    missing = [needle for needle in PLAN_REQUIRED_NEEDLES if needle not in folded]
    if missing:
        raise AssertionError(f"Plan is missing required spec terms: {missing}")
    todo_blob = _fold(" ".join(f"{todo.id} {todo.content}" for todo in plan.todos))
    missing_groups: list[str] = []
    for group in PLAN_TODO_GROUPS:
        if not any(needle in todo_blob for needle in group):
            missing_groups.append("/".join(group))
    if missing_groups:
        raise AssertionError(f"Plan todos are missing phases: {missing_groups}")


def plan_has_spec(workspace: Path | None = None) -> bool:
    """Return True when the latest plan includes the CM5 camera spec."""

    root = example_root(workspace or (_active.workspace if _active else WORKSPACE))
    check_plan_spec(load_latest_plan(root))
    return True


def discard_stale_plan(workspace: Path | None = None) -> None:
    """Delete plans that do not yet include the full CM5 assembly spec."""

    if already_done(lambda: plan_has_spec(workspace)):
        return
    root = example_root(workspace or (_active.workspace if _active else WORKSPACE))
    for path in (root / ".copperpilot" / "plans").glob("*.plan.md"):
        path.unlink(missing_ok=True)
        print_line(f"Removed stale plan {path.name}.")


def _iter_library_haystack(root: Path) -> str:
    chunks: list[str] = []
    for path in root.rglob("*"):
        if any(part in SKIP_DIR_NAMES for part in path.relative_to(root).parts):
            continue
        chunks.append(path.name)
        if not path.is_file():
            continue
        if path.suffix.lower() not in LIBRARY_SUFFIXES and path.name not in LIBRARY_NAMES:
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return "\n".join(chunks)


def libraries_ready(workspace: Path | None = None) -> bool:
    """Return True when project files cover the required symbol/footprint tokens."""

    root = example_root(workspace or (_active.workspace if _active else WORKSPACE))
    haystack = _iter_library_haystack(root)
    folded = _fold(haystack)
    missing = [token for token in REQUIRED_LIBRARY_TOKENS if _fold(token) not in folded]
    if missing:
        raise AssertionError(f"Project libraries are missing: {missing}")
    return True


def parse_netlist_xml(text: str) -> dict[str, list[str]]:
    """Map normalized net name -> unique component refs."""

    root = ET.fromstring(text)  # noqa: S314 — local kicad-cli netlist
    nets: dict[str, list[str]] = {}
    for net in root.findall(".//net"):
        name = _normalize_net_name(net.get("name") or "")
        if not name:
            continue
        refs: list[str] = []
        seen: set[str] = set()
        for node in net.findall("node"):
            ref = (node.get("ref") or "").strip()
            if not ref or ref in seen:
                continue
            seen.add(ref)
            refs.append(ref)
        nets[name] = refs
    return nets


def _normalize_net_name(name: str) -> str:
    stripped = name.strip()
    if stripped.startswith("/"):
        stripped = stripped[1:]
    return stripped


_NET_ALIASES: dict[str, tuple[str, ...]] = {
    "CM4_5V": ("CM4_5V", "CM5_5V"),
    "CM5_5V": ("CM4_5V", "CM5_5V"),
    "CM4_3V3": ("CM4_3V3", "CM5_3V3"),
    "CM5_3V3": ("CM4_3V3", "CM5_3V3"),
}


def _matching_nets(nets: dict[str, list[str]], required: str) -> list[str]:
    target = _normalize_net_name(required)
    folded_target = target.upper()
    aliases = {alias.upper() for alias in _NET_ALIASES.get(folded_target, (folded_target,))}
    aliases.add(folded_target)
    matches: list[str] = []
    for name in nets:
        candidate = name.upper()
        if candidate in aliases:
            matches.append(name)
    return matches


def netlist_matches(
    required: dict[str, tuple[str, ...]] | None = None,
    *,
    xml: str | None = None,
    workspace: Path | None = None,
) -> bool:
    """Return True when each required net exists and lists the expected refs."""

    contract = required or REQUIRED_NETS
    payload = xml if xml is not None else export_netlist(workspace)
    nets = parse_netlist_xml(payload)
    missing: list[str] = []
    for net_name, refs in contract.items():
        matched = _matching_nets(nets, net_name)
        if not matched:
            missing.append(f"{net_name} (net missing)")
            continue
        present: set[str] = set()
        for name in matched:
            present.update(nets[name])
        absent = [ref for ref in refs if ref not in present]
        if absent:
            missing.append(f"{net_name} missing {absent}")
        if net_name.upper() != "GND":
            allowed = set(refs)
            extra = sorted(
                ref for ref in present if ref not in allowed and not SKIP_REFS_RE.match(ref)
            )
            if extra:
                missing.append(f"{net_name} extra {extra}")
    if missing:
        raise AssertionError("Netlist does not match the required contract: " + "; ".join(missing))
    return True


def export_netlist(workspace: Path | None = None) -> str:
    """Export the schematic netlist as KiCad XML via kicad-cli."""

    session = _active
    cli = session.cli if session is not None else require_kicad_10()
    root, schematic, _, _, report_dir = project_paths(
        workspace or (session.workspace if session else WORKSPACE)
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    output = report_dir / NETLIST_REPORT.name
    proc = _run_kicad(
        cli,
        [
            "sch",
            "export",
            "netlist",
            "--format",
            "kicadxml",
            "--output",
            str(output.relative_to(root)),
            str(schematic.relative_to(root)),
        ],
        cwd=root,
    )
    if not output.is_file():
        raise RuntimeError(
            "kicad-cli sch export netlist did not write XML.\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    return output.read_text(encoding="utf-8")


def _extract_refs(text: str) -> set[str]:
    refs: set[str] = set()
    for match in REFERENCE_RE.finditer(text):
        ref = match.group(1).strip()
        if not ref or SKIP_REFS_RE.match(ref) or ref.startswith("#"):
            continue
        refs.add(ref)
    return refs


def _top_level_forms(text: str, head: str) -> list[str]:
    forms: list[str] = []
    token = f"({head}"
    start = 0
    while True:
        index = text.find(token, start)
        if index < 0:
            break
        depth = 0
        cursor = index
        while cursor < len(text):
            char = text[cursor]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    forms.append(text[index : cursor + 1])
                    start = cursor + 1
                    break
            cursor += 1
        else:
            break
    return forms


def pcb_matches_schematic(
    workspace: Path | None = None,
    *,
    schematic_text: str | None = None,
    pcb_text: str | None = None,
) -> bool:
    """Return True when schematic and PCB reference sets match."""

    root, schematic, pcb, _, _ = project_paths(
        workspace or (_active.workspace if _active else WORKSPACE)
    )
    sch_src = (
        schematic_text if schematic_text is not None else schematic.read_text(encoding="utf-8")
    )
    pcb_src = pcb_text if pcb_text is not None else pcb.read_text(encoding="utf-8")
    del root
    sch_refs = _extract_refs(sch_src)
    pcb_refs = _extract_refs(pcb_src)
    missing_on_pcb = sorted(sch_refs - pcb_refs)
    extra_on_pcb = sorted(pcb_refs - sch_refs)
    problems: list[str] = []
    if missing_on_pcb:
        problems.append(f"schematic refs missing on PCB: {missing_on_pcb}")
    if extra_on_pcb:
        problems.append(f"PCB refs not in schematic: {extra_on_pcb}")
    if problems:
        raise AssertionError("; ".join(problems))
    if not sch_refs:
        raise AssertionError("Schematic has no component references to sync.")
    return True


def _edge_cuts_points(pcb_text: str) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for form in (
        *_top_level_forms(pcb_text, "gr_line"),
        *_top_level_forms(pcb_text, "gr_rect"),
        *_top_level_forms(pcb_text, "gr_arc"),
        *_top_level_forms(pcb_text, "gr_circle"),
        *_top_level_forms(pcb_text, "gr_poly"),
    ):
        if EDGE_CUTS_RE.search(form) is None:
            continue
        for match in COORD_RE.finditer(form):
            points.append((float(match.group(1)), float(match.group(2))))
    return points


def _bbox(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


@dataclass
class FootprintPose:
    ref: str
    x: float
    y: float
    rot: float
    layer: str
    has_model: bool
    raw: str


def _parse_footprints(pcb_text: str) -> list[FootprintPose]:
    poses: list[FootprintPose] = []
    for form in _top_level_forms(pcb_text, "footprint"):
        ref_match = REFERENCE_RE.search(form)
        at_match = AT_RE.search(form)
        layer_match = LAYER_RE.search(form)
        if ref_match is None or at_match is None:
            continue
        rot = float(at_match.group(3)) if at_match.group(3) else 0.0
        poses.append(
            FootprintPose(
                ref=ref_match.group(1),
                x=float(at_match.group(1)),
                y=float(at_match.group(2)),
                rot=rot,
                layer=layer_match.group(1) if layer_match else "",
                has_model=MODEL_RE.search(form) is not None,
                raw=form,
            )
        )
    return poses


def _by_ref(poses: list[FootprintPose], name: str) -> FootprintPose | None:
    for pose in poses:
        if pose.ref == name:
            return pose
    return None


def _module_pose(poses: list[FootprintPose]) -> FootprintPose | None:
    for alias in MODULE_REF_ALIASES:
        pose = _by_ref(poses, alias)
        if pose is not None:
            return pose
    return next((pose for pose in poses if "B.Cu" in pose.layer and "DF40" in pose.raw), None)


def _angle_delta(actual: float, expected: float) -> float:
    return abs((actual - expected + 180.0) % 360.0 - 180.0)


def _outline_bbox(pcb_text: str) -> tuple[float, float, float, float]:
    points = _edge_cuts_points(pcb_text)
    if len(points) < 2:
        raise AssertionError("PCB Edge.Cuts outline is missing.")
    xmin, ymin, xmax, ymax = _bbox(points)
    width = abs(xmax - xmin)
    height = abs(ymax - ymin)
    size_ok = (
        abs(width - BOARD_WIDTH_MM) <= BOARD_SIZE_TOLERANCE_MM
        and abs(height - BOARD_HEIGHT_MM) <= BOARD_SIZE_TOLERANCE_MM
    ) or (
        abs(width - BOARD_HEIGHT_MM) <= BOARD_SIZE_TOLERANCE_MM
        and abs(height - BOARD_WIDTH_MM) <= BOARD_SIZE_TOLERANCE_MM
    )
    if not size_ok:
        raise AssertionError(
            f"Edge.Cuts is {width:.2f} x {height:.2f} mm; expected "
            f"{BOARD_WIDTH_MM:.1f} x {BOARD_HEIGHT_MM:.1f} mm."
        )
    return xmin, ymin, xmax, ymax


def _local_xy(pose: FootprintPose, bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    return pose.x - bbox[0], pose.y - bbox[1]


def _pose_errors(
    pose: FootprintPose,
    spec: dict[str, Any],
    bbox: tuple[float, float, float, float],
) -> list[str]:
    errors: list[str] = []
    local_x, local_y = _local_xy(pose, bbox)
    dx = abs(local_x - float(spec["x"]))
    dy = abs(local_y - float(spec["y"]))
    if dx > float(spec["tol"]) or dy > float(spec["tol"]):
        errors.append(
            f"at board-local ({local_x:.2f}, {local_y:.2f}); expected "
            f"({spec['x']:.2f}, {spec['y']:.2f}) ±{spec['tol']} mm"
        )
    if _angle_delta(pose.rot, float(spec["rot"])) > ROTATION_TOLERANCE_DEG:
        errors.append(
            f"rotation {pose.rot:g} deg; expected {spec['rot']:g} ±{ROTATION_TOLERANCE_DEG:g}"
        )
    if spec["layer"] not in pose.layer:
        errors.append(f"layer {pose.layer or '(missing)'}; expected {spec['layer']}")
    xmin, ymin, xmax, ymax = bbox
    if not (xmin - OUTLINE_MARGIN_MM <= pose.x <= xmax + OUTLINE_MARGIN_MM):
        errors.append("outside Edge.Cuts in X")
    if not (ymin - OUTLINE_MARGIN_MM <= pose.y <= ymax + OUTLINE_MARGIN_MM):
        errors.append("outside Edge.Cuts in Y")
    return errors


def placement_matches_spec(
    workspace: Path | None = None,
    *,
    pcb_text: str | None = None,
) -> bool:
    """Return True when Edge.Cuts is 55x40 mm and footprints match the assembly table."""

    root, _, pcb, _, _ = project_paths(workspace or (_active.workspace if _active else WORKSPACE))
    del root
    src = pcb_text if pcb_text is not None else pcb.read_text(encoding="utf-8")
    bbox = _outline_bbox(src)
    poses = _parse_footprints(src)
    problems: list[str] = []
    missing = [name for name in REQUIRED_PLACEMENT_REFS if _by_ref(poses, name) is None]
    if missing:
        problems.append(f"missing footprints: {missing}")
    module = _module_pose(poses)
    if module is None or "B.Cu" not in module.layer:
        problems.append("CM5 DF40 mezzanine must be placed on B.Cu.")
    holes = [
        pose
        for pose in poses
        if "mountinghole" in pose.raw.lower()
        or "m2.5" in pose.raw.lower()
        or HOLE_REF_RE.match(pose.ref)
    ]
    if len(holes) < 4 and module is None:
        problems.append(f"Expected 4x M2.5 mounting holes, found {len(holes)}.")
    for ref, spec in PLACEMENT_SPEC.items():
        pose = module if ref == "Module1" else _by_ref(poses, ref)
        if pose is None:
            continue
        detail = _pose_errors(pose, spec, bbox)
        if detail:
            problems.append(f"{ref} " + "; ".join(detail))
    if problems:
        raise AssertionError("Placement does not match the CM5 camera spec: " + "; ".join(problems))
    return True


def board_outline_and_placement(
    workspace: Path | None = None,
    *,
    pcb_text: str | None = None,
) -> bool:
    """Return True when Edge.Cuts is 55x40 mm and key footprints are posed."""

    return placement_matches_spec(workspace, pcb_text=pcb_text)


def placement_feedback(
    workspace: Path | None = None,
    *,
    pcb_text: str | None = None,
) -> str:
    """Build a repair prompt listing placement errors."""

    try:
        placement_matches_spec(workspace, pcb_text=pcb_text)
        return "Placement already matches the CM5 camera spec."
    except AssertionError as exc:
        return (
            "Move and rotate footprints on cm5_camera/cm5_camera.kicad_pcb to this "
            "board-local table (origin = SW of Edge.Cuts). Do not start over.\n"
            f"{format_placement_spec()}\n"
            f"Current errors: {exc}"
        )


def footprints_have_3d_models(
    required: tuple[str, ...] | None = None,
    workspace: Path | None = None,
    *,
    pcb_text: str | None = None,
) -> bool:
    """Return True when the required footprints include a 3D model."""

    root, _, pcb, _, _ = project_paths(workspace or (_active.workspace if _active else WORKSPACE))
    del root
    src = pcb_text if pcb_text is not None else pcb.read_text(encoding="utf-8")
    poses = {pose.ref: pose for pose in _parse_footprints(src)}
    missing: list[str] = []
    for ref in required or REQUIRED_3D_REFS:
        pose = poses.get(ref)
        if pose is None or not pose.has_model:
            missing.append(ref)
    module = next((poses[name] for name in MODULE_REF_ALIASES if name in poses), None)
    if module is None or not module.has_model:
        missing.append("CM5 module")
    if missing:
        raise AssertionError(f"Missing 3D models on: {missing}")
    return True


def footprint_list_matches(
    workspace: Path | None = None,
    *,
    pcb_text: str | None = None,
) -> bool:
    """Return True when the PCB uses every unique NANO-C footprint name."""

    _, _, src = _pcb_source(workspace, pcb_text)
    folded = _fold(src)
    missing = [name for name in REQUIRED_FOOTPRINT_NAMES if _fold(name) not in folded]
    if missing:
        raise AssertionError(f"PCB is missing required footprints: {missing}")
    return True


def models_3d_list_matches(
    workspace: Path | None = None,
    *,
    pcb_text: str | None = None,
) -> bool:
    """Return True when every unique NANO-C 3D model file is referenced."""

    _, _, src = _pcb_source(workspace, pcb_text)
    folded = _fold(src)
    missing = [name for name in REQUIRED_3D_MODEL_FILES if _fold(name) not in folded]
    if missing:
        raise AssertionError(f"PCB is missing required 3D models: {missing}")
    return True


_MODEL_POSE_RE = re.compile(
    r'\(model\s+"([^"]+)"\s*'
    r"\(offset\s*\(xyz\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\)\s*\)\s*"
    r"\(scale\s*\(xyz\s+[-\d.]+\s+[-\d.]+\s+[-\d.]+\)\s*\)\s*"
    r"\(rotate\s*\(xyz\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\)\s*\)",
    re.MULTILINE,
)


def _parse_3d_poses(pcb_text: str) -> list[dict[str, Any]]:
    poses: list[dict[str, Any]] = []
    for match in _MODEL_POSE_RE.finditer(pcb_text):
        poses.append(
            {
                "file": Path(match.group(1)).name,
                "offset": (
                    float(match.group(2)),
                    float(match.group(3)),
                    float(match.group(4)),
                ),
                "rotate": (
                    float(match.group(5)),
                    float(match.group(6)),
                    float(match.group(7)),
                ),
            }
        )
    return poses


def _pose_close(
    actual: tuple[float, float, float],
    expected: tuple[float, float, float],
    *,
    tol: float,
) -> bool:
    return all(abs(left - right) <= tol for left, right in zip(actual, expected, strict=True))


def models_3d_poses_match(
    workspace: Path | None = None,
    *,
    pcb_text: str | None = None,
    tol: float = 0.05,
) -> bool:
    """Return True when connector 3D offsets/rotations match the NANO-C pose table."""

    _, _, src = _pcb_source(workspace, pcb_text)
    found = _parse_3d_poses(src)
    missing: list[str] = []
    for required in REQUIRED_3D_POSES:
        want_off = tuple(float(v) for v in required["offset"])
        want_rot = tuple(float(v) for v in required["rotate"])
        hit = next(
            (
                pose
                for pose in found
                if pose["file"] == required["file"]
                and _pose_close(pose["offset"], want_off, tol=tol)
                and _pose_close(pose["rotate"], want_rot, tol=tol)
            ),
            None,
        )
        if hit is None:
            missing.append(f"{required['file']} offset {want_off} rotate {want_rot}")
    if missing:
        raise AssertionError(f"3D poses do not match the audit table: {missing}")
    return True


def _pcb_source(workspace: Path | None, pcb_text: str | None) -> tuple[Path, Path, str]:
    root, _, pcb, _, _ = project_paths(workspace or (_active.workspace if _active else WORKSPACE))
    src = pcb_text if pcb_text is not None else pcb.read_text(encoding="utf-8")
    return root, pcb, src


def stackup_is_six_layer(
    workspace: Path | None = None,
    *,
    pcb_text: str | None = None,
) -> bool:
    """Return True when the board has F/In1-In4/B copper and ~1.6 mm thickness."""

    _, _, src = _pcb_source(workspace, pcb_text)
    missing = [layer for layer in COPPER_LAYERS if f'"{layer}"' not in src]
    if missing:
        raise AssertionError(f"6-layer stackup is missing copper layers: {missing}")
    match = THICKNESS_RE.search(src)
    if match is None:
        raise AssertionError("PCB is missing a (thickness ...) field.")
    thickness = float(match.group(1))
    if abs(thickness - STACKUP_THICKNESS_MM) > STACKUP_THICKNESS_TOLERANCE_MM:
        raise AssertionError(
            f"Board thickness is {thickness} mm; expected {STACKUP_THICKNESS_MM} mm."
        )
    return True


def _load_project_json(workspace: Path | None = None) -> dict[str, Any]:
    root, _, _, _, _ = project_paths(workspace or (_active.workspace if _active else WORKSPACE))
    path = root / PROJECT_NAME / f"{PROJECT_NAME}.kicad_pro"
    if not path.is_file():
        raise AssertionError(f"Missing KiCad project file {path}.")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError("KiCad project file is not a JSON object.")
    return payload


def netclasses_defined(
    workspace: Path | None = None,
    *,
    project: dict[str, Any] | None = None,
) -> bool:
    """Return True when 100 ohm, 90 ohm, and power netclasses exist."""

    payload = project if project is not None else _load_project_json(workspace)
    classes = payload.get("net_settings", {}).get("classes") or []
    names = [_fold(str(item.get("name") or "")) for item in classes if isinstance(item, dict)]
    missing: list[str] = []
    for required in REQUIRED_NETCLASSES:
        aliases = NETCLASS_ALIASES.get(_fold(required), (_fold(required),))
        if not any(alias in names for alias in aliases):
            missing.append(required)
    if missing:
        raise AssertionError(f"Project netclasses are missing: {missing}")
    return True


def _zone_net_and_layers(form: str) -> tuple[str, set[str]]:
    net_match = NET_NAME_FIELD_RE.search(form) or NET_NAME_RE.search(form)
    net = _normalize_net_name(net_match.group(1) if net_match else "")
    layers = {match.group(1) for match in LAYER_RE.finditer(form)}
    layers_match = re.search(r'\(layers\s+"([^"]+)"', form)
    if layers_match:
        layers.add(layers_match.group(1))
    return net, layers


def planes_poured(
    workspace: Path | None = None,
    *,
    pcb_text: str | None = None,
) -> bool:
    """Return True when GND pours exist on In1/In4 and power pours on In3."""

    _, _, src = _pcb_source(workspace, pcb_text)
    gnd_in1 = False
    gnd_in4 = False
    cm5_5v = False
    pwr_3v3 = False
    for form in _top_level_forms(src, "zone"):
        net, layers = _zone_net_and_layers(form)
        folded = _fold(net)
        if folded == "gnd":
            gnd_in1 = gnd_in1 or "In1.Cu" in layers
            gnd_in4 = gnd_in4 or "In4.Cu" in layers
        if folded in {"cm5 5v", "cm4 5v"}:
            cm5_5v = cm5_5v or "In3.Cu" in layers
        if folded in {"pwr 3v3", "pwr_3v3"}:
            pwr_3v3 = pwr_3v3 or "In3.Cu" in layers
    missing: list[str] = []
    if not gnd_in1:
        missing.append("GND on In1.Cu")
    if not gnd_in4:
        missing.append("GND on In4.Cu")
    if not cm5_5v:
        missing.append("CM4_5V/CM5_5V on In3.Cu")
    if not pwr_3v3:
        missing.append("PWR_3V3 on In3.Cu")
    if missing:
        raise AssertionError(f"Copper planes are missing: {missing}")
    return True


def ensure_outer_gnd_zones(workspace: Path | None = None) -> int:
    """Pour GND on F.Cu and B.Cu so connector shields can stitch to the planes."""

    root, pcb, src = _pcb_source(workspace, None)
    del root
    existing: set[str] = set()
    for form in _top_level_forms(src, "zone"):
        net, layers = _zone_net_and_layers(form)
        if _fold(net) == "gnd":
            existing.update(layers)
    xmin, ymin, xmax, ymax = _outline_bbox(src)
    added = 0
    blobs: list[str] = []
    for layer in ("F.Cu", "B.Cu"):
        if layer in existing:
            continue
        blobs.append(
            "\t(zone\n"
            '\t\t(net "/GND")\n'
            f'\t\t(layer "{layer}")\n'
            f'\t\t(uuid "{uuid.uuid4()}")\n'
            "\t\t(hatch edge 0.5)\n"
            "\t\t(connect_pads\n"
            "\t\t\t(clearance 0.2)\n"
            "\t\t)\n"
            "\t\t(min_thickness 0.2)\n"
            "\t\t(fill yes\n"
            "\t\t\t(thermal_gap 0.2)\n"
            "\t\t\t(thermal_bridge_width 0.3)\n"
            "\t\t\t(island_removal_mode 1)\n"
            "\t\t\t(island_area_min 1.0)\n"
            "\t\t)\n"
            "\t\t(polygon\n"
            "\t\t\t(pts\n"
            f"\t\t\t\t(xy {xmin:g} {ymin:g}) (xy {xmax:g} {ymin:g}) "
            f"(xy {xmax:g} {ymax:g}) (xy {xmin:g} {ymax:g})\n"
            "\t\t\t)\n"
            "\t\t)\n"
            "\t)\n"
        )
        added += 1
    if not blobs:
        return 0
    keep = src.rstrip()
    if keep.endswith(")"):
        keep = keep[:-1] + "".join(blobs) + ")\n"
    pcb.write_text(keep, encoding="utf-8")
    print_line(f"Added {added} outer GND zones on F.Cu/B.Cu.")
    return added


def solidify_plane_pad_connections(workspace: Path | None = None) -> int:
    """Use solid pad-to-zone connections so GND/power pours are not starved."""

    root, pcb, src = _pcb_source(workspace, None)
    del root
    updated, count = re.subn(
        r"\(connect_pads\s*\n\s*\(clearance [^\)]+\)\s*\n\s*\)",
        "(connect_pads yes\n\t\t\t(clearance 0.15)\n\t\t)",
        src,
    )
    if count:
        pcb.write_text(updated, encoding="utf-8")
        print_line(f"Set solid pad connections on {count} copper zones.")
    return count


def refill_zones(workspace: Path | None = None) -> None:
    """Refill copper pours with kicad-cli and save the board."""

    session = _active
    cli = session.cli if session is not None else require_kicad_10()
    root, _, pcb, _, report_dir = project_paths(
        workspace or (session.workspace if session else WORKSPACE)
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    output = report_dir / DRC_REPORT.name
    _run_kicad(
        cli,
        [
            "pcb",
            "drc",
            "--refill-zones",
            "--save-board",
            "--format",
            "json",
            "--output",
            str(output.relative_to(root)),
            str(pcb.relative_to(root)),
        ],
        cwd=root,
    )
    print_line("Refilled copper zones.")


def _diff_net(name: str) -> bool:
    folded = name.upper().lstrip("/")
    return any(needle in folded for needle in DIFF_NET_NEEDLES)


def _high_speed_plane_segments(src: str) -> list[str]:
    offenders: list[str] = []
    for form in _top_level_forms(src, "segment"):
        net_match = NET_NAME_RE.search(form)
        layer_match = LAYER_RE.search(form)
        if net_match is None or layer_match is None:
            continue
        if _diff_net(net_match.group(1)) and layer_match.group(1) in PLANE_LAYERS:
            offenders.append(form)
    return offenders


def high_speed_not_on_planes(
    workspace: Path | None = None,
    *,
    pcb_text: str | None = None,
) -> bool:
    """Return True when HDMI/CSI/DSI/USB tracks stay off GND/PWR planes."""

    _, _, src = _pcb_source(workspace, pcb_text)
    offenders = _high_speed_plane_segments(src)
    if offenders:
        labels: list[str] = []
        for form in offenders[:12]:
            net_match = NET_NAME_RE.search(form)
            layer_match = LAYER_RE.search(form)
            labels.append(f"{net_match.group(1)} on {layer_match.group(1)}")
        raise AssertionError("High-speed nets must not route on In1/In3/In4: " + ", ".join(labels))
    return True


def strip_high_speed_from_planes(workspace: Path | None = None) -> int:
    """Delete HDMI/CSI/DSI/USB segments that landed on GND/PWR planes."""

    root, pcb, src = _pcb_source(workspace, None)
    del root
    offenders = _high_speed_plane_segments(src)
    keep = src
    for form in offenders:
        keep = keep.replace(form, "", 1)
    keep = re.sub(r"\n{3,}", "\n\n", keep)
    pcb.write_text(keep, encoding="utf-8")
    print_line(f"Stripped {len(offenders)} high-speed segments off In1/In3/In4.")
    return len(offenders)


def pcb_segment_count(workspace: Path | None = None) -> int:
    _, _, src = _pcb_source(workspace, None)
    return src.count("(segment")


def snapshot_pcb(workspace: Path | None = None) -> bytes:
    _, pcb, _ = _pcb_source(workspace, None)
    return pcb.read_bytes()


def restore_pcb_if_wiped(snapshot: bytes, *, min_segments: int = 50) -> bool:
    """Put copper back if a hosted tool wiped FreeRouting's import."""

    _, pcb, _ = _pcb_source(None, None)
    if pcb_segment_count() >= min_segments:
        return False
    pcb.write_bytes(snapshot)
    print_line("Restored PCB copper after hosted tools wiped tracks.")
    return True


def zones_and_rules_ready(
    workspace: Path | None = None,
    *,
    pcb_text: str | None = None,
) -> bool:
    """Return True when stackup, netclasses, and inner planes are in place."""

    stackup_is_six_layer(workspace, pcb_text=pcb_text)
    netclasses_defined(workspace)
    planes_poured(workspace, pcb_text=pcb_text)
    return True


def reset_pcb_copper(
    workspace: Path | None = None,
    *,
    keep_zones: bool = False,
) -> None:
    """Strip tracks and vias; strip zones unless inner planes are already poured."""

    root, pcb, src = _pcb_source(workspace, None)
    del root
    heads = ("segment", "via", "arc") if keep_zones else COPPER_FORM_HEADS
    keep = src
    removed = 0
    for head in heads:
        for form in _top_level_forms(keep, head):
            keep = keep.replace(form, "", 1)
            removed += 1
    keep = re.sub(r"\n{3,}", "\n\n", keep)
    pcb.write_text(keep, encoding="utf-8")
    print_line(f"Wiped {removed} copper objects from {pcb.name}.")


def _rewrite_footprint_pose(form: str, x: float, y: float, rot: float, layer: str) -> str:
    rot_text = f"{x:g} {y:g}" if abs(rot) < 1e-9 else f"{x:g} {y:g} {rot:g}"
    updated, at_count = AT_RE.subn(lambda _match: f"(at {rot_text}", form, count=1)
    del at_count
    updated, layer_count = LAYER_RE.subn(lambda _match: f'(layer "{layer}"', updated, count=1)
    del layer_count
    return updated


def lock_placement(workspace: Path | None = None) -> list[str]:
    """Snap known footprints to the assembly table and rewrite the PCB."""

    root, pcb, src = _pcb_source(workspace, None)
    del root
    if not _edge_cuts_points(src):
        src = src.rstrip()
        if src.endswith(")"):
            src = (
                src[:-1]
                + "\t(gr_rect\n\t\t(start 0 0)\n\t\t(end 55 40)\n"
                + "\t\t(stroke (width 0.1) (type default))\n\t\t(fill none)\n"
                + '\t\t(layer "Edge.Cuts")\n\t)\n)\n'
            )
            pcb.write_text(src, encoding="utf-8")
    bbox = _outline_bbox(src)
    xmin, ymin, _, _ = bbox
    locked: list[str] = []
    for form in _top_level_forms(src, "footprint"):
        ref_match = REFERENCE_RE.search(form)
        if ref_match is None:
            continue
        ref = ref_match.group(1)
        spec_key = "Module1" if ref in MODULE_REF_ALIASES else ref
        spec = PLACEMENT_SPEC.get(spec_key)
        if spec is None:
            continue
        abs_x = xmin + float(spec["x"])
        abs_y = ymin + float(spec["y"])
        rewritten = _rewrite_footprint_pose(
            form, abs_x, abs_y, float(spec["rot"]), str(spec["layer"])
        )
        if rewritten != form:
            src = src.replace(form, rewritten, 1)
            locked.append(ref)
    pcb.write_text(src, encoding="utf-8")
    if locked:
        print_line("Locked placement for: " + ", ".join(locked))
    else:
        print_line("Placement already snapped.")
    return locked


def lock_diff_tracks(workspace: Path | None = None) -> int:
    """Mark HDMI/CSI/DSI/USB segments locked so leftover routing cannot rip them up."""

    root, pcb, src = _pcb_source(workspace, None)
    del root
    updated = src
    locked = 0
    for form in _top_level_forms(updated, "segment"):
        net_match = NET_NAME_RE.search(form)
        if net_match is None or not _diff_net(net_match.group(1)):
            continue
        if LOCKED_RE.search(form):
            continue
        patched = form.replace("(segment", "(segment\n\t\t(locked yes)", 1)
        updated = updated.replace(form, patched, 1)
        locked += 1
    pcb.write_text(updated, encoding="utf-8")
    print_line(f"Locked {locked} high-speed segments.")
    return locked


def inject_feedback(message: str) -> None:
    """Append a human note to the current hosted conversation without a turn."""

    session = _require_session()
    session.messages.append(HumanMessage(content=message))
    print_line(message)


def _active_violations(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item for item in items if str(item.get("severity") or "").lower() not in IGNORED_SEVERITIES
    ]


def summarize_erc(report: dict[str, Any]) -> dict[str, Any]:
    violations: list[dict[str, Any]] = []
    for sheet in report.get("sheets") or []:
        if not isinstance(sheet, dict):
            continue
        sheet_path = sheet.get("path", "/")
        for violation in sheet.get("violations") or []:
            if not isinstance(violation, dict):
                continue
            item = dict(violation)
            item["sheet"] = sheet_path
            violations.append(item)
    active = _active_violations(violations)
    errors = [item for item in active if str(item.get("severity") or "").lower() == "error"]
    warnings = [item for item in active if str(item.get("severity") or "").lower() == "warning"]
    return {
        "errors": errors,
        "warnings": warnings,
        "active": active,
        "kicad_version": report.get("kicad_version", ""),
    }


def summarize_drc(report: dict[str, Any]) -> dict[str, Any]:
    violations = [item for item in (report.get("violations") or []) if isinstance(item, dict)]
    unconnected = [
        item for item in (report.get("unconnected_items") or []) if isinstance(item, dict)
    ]
    active = [
        item
        for item in _active_violations(violations)
        if str(item.get("severity") or "").lower() == "error"
        and str(item.get("type") or "") != "track_dangling"
        and not _allowed_shield_tab_clearance(item)
        and not _allowed_j2_module_short(item)
    ]
    return {
        "violations": active,
        "unconnected_items": [
            item for item in unconnected if not _same_footprint_unconnected(item)
        ],
        "kicad_version": report.get("kicad_version", ""),
    }


def _allowed_shield_tab_clearance(item: dict[str, Any]) -> bool:
    """Mini-HDMI and USB-A shield tabs sit on Edge.Cuts by design."""

    if str(item.get("type") or "") != "copper_edge_clearance":
        return False
    blob = json.dumps(item, default=str)
    return any(ref in blob for ref in SHIELD_TAB_REFS)


def _allowed_j2_module_short(item: dict[str, Any]) -> bool:
    """USB-A west shield overlaps DF40 pad 199 on the NANO-C outline."""

    if str(item.get("type") or "") != "shorting_items":
        return False
    blob = json.dumps(item, default=str)
    return "J2" in blob and "Module1" in blob and "SH" in blob


def _same_footprint_unconnected(item: dict[str, Any]) -> bool:
    """USB-C mirrored pads on one footprint are not a missing route."""

    pieces = item.get("items") or []
    refs: list[str] = []
    for piece in pieces:
        description = str(piece.get("description") or "")
        match = re.search(r" of ([A-Za-z][A-Za-z0-9_]*)", description)
        if match:
            refs.append(match.group(1))
    return len(refs) >= 2 and len(set(refs)) == 1


def run_erc_report(cli: Path, *, workspace: Path | None = None) -> dict[str, Any]:
    root, schematic, _, _, report_dir = project_paths(workspace or WORKSPACE)
    erc_report = report_dir / ERC_REPORT.name
    report_dir.mkdir(parents=True, exist_ok=True)
    proc = _run_kicad(
        cli,
        [
            "sch",
            "erc",
            "--format",
            "json",
            "--output",
            str(erc_report.relative_to(root)),
            str(schematic.relative_to(root)),
        ],
        cwd=root,
    )
    if not erc_report.is_file():
        raise RuntimeError(
            "kicad-cli sch erc did not write a JSON report.\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    return summarize_erc(json.loads(erc_report.read_text(encoding="utf-8")))


def run_drc_report(cli: Path, *, workspace: Path | None = None) -> dict[str, Any]:
    root, _, pcb, _, report_dir = project_paths(workspace or WORKSPACE)
    drc_report = report_dir / DRC_REPORT.name
    report_dir.mkdir(parents=True, exist_ok=True)
    proc = _run_kicad(
        cli,
        [
            "pcb",
            "drc",
            "--format",
            "json",
            "--output",
            str(drc_report.relative_to(root)),
            str(pcb.relative_to(root)),
        ],
        cwd=root,
    )
    if not drc_report.is_file():
        raise RuntimeError(
            "kicad-cli pcb drc did not write a JSON report.\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    return summarize_drc(json.loads(drc_report.read_text(encoding="utf-8")))


def format_erc_summary(erc: dict[str, Any]) -> str:
    return f"ERC errors={len(erc['errors'])} warnings={len(erc['warnings'])}"


def format_drc_summary(drc: dict[str, Any]) -> str:
    return f"DRC violations={len(drc['violations'])} unconnected={len(drc['unconnected_items'])}"


def require_project(workspace: Path | None = None) -> None:
    _, schematic, pcb, _, _ = project_paths(workspace or WORKSPACE)
    if not schematic.is_file() or not pcb.is_file():
        raise FileNotFoundError(
            f"Expected a KiCad project under {(workspace or WORKSPACE) / PROJECT_NAME}"
        )


def _text_delta(printed: str, chunk: str) -> tuple[str, str]:
    """Return the new suffix when the host sends snapshots, else the raw chunk."""

    if chunk.startswith(printed):
        return chunk[len(printed) :], chunk
    return chunk, printed + chunk


async def run_turn(
    copper_pilot: CopperPilotAgent,
    messages: list[BaseMessage],
) -> list[BaseMessage]:
    """Stream only assistant plain text; hide reasoning and tool calls."""

    printed = ""
    failed: str | None = None
    try:
        async for event in copper_pilot.astream_events_raw({"messages": messages}):
            if event.kind is EventKind.TEXT:
                delta, printed = _text_delta(printed, str(event.data))
                if delta:
                    stream_text(delta)
            elif event.kind is EventKind.ERROR:
                failed = str(event.data)
                print_line()
                print_line(f"error: {failed}")
            elif event.kind is EventKind.FINAL and isinstance(event.data, dict):
                if event.data.get("success") is False or event.data.get("error"):
                    failed = str(event.data.get("error") or "Hosted turn failed.")
    except ProtocolError as exc:
        raise RuntimeError(str(exc)) from exc
    if not printed.endswith("\n"):
        print_line()
    if failed:
        raise RuntimeError(failed)
    return [*messages, AIMessage(content=printed)]


def already_done(check) -> bool:
    """Return True when a phase validator already passes on disk."""

    try:
        result = check()
    except (AssertionError, FileNotFoundError, RuntimeError, ProtocolError):
        return False
    return bool(result)


def erc_repair_prompt(erc: dict[str, Any]) -> str:
    payload = {
        "erc_errors": erc["errors"][:40],
        "erc_warnings": erc["warnings"][:20],
    }
    return (
        "Fix these ERC violations so kicad-cli sch erc reports zero errors.\n"
        "Keep the existing CM5 camera schematic; do not start over.\n"
        f"{json.dumps(payload, default=str)}"
    )


def drc_repair_prompt(drc: dict[str, Any]) -> str:
    payload = {
        "drc_violations": drc["violations"][:40],
        "unconnected_items": drc["unconnected_items"][:40],
    }
    return (
        "Fix these DRC violations so kicad-cli pcb drc is clean.\n"
        "Keep the existing CM5 camera PCB; do not start over. "
        "Do not run a long FreeRouting job. Connect leftover pads on Default_IO "
        "without ripping locked HDMI, CSI, DSI, or USB tracks. "
        "Pull copper 0.2 mm inside Edge.Cuts except Mini-HDMI and USB-A shield tabs.\n"
        f"{json.dumps(payload, default=str)}"
    )


def telemetry_prompt(telemetry: dict[str, Any], *, leftover: int) -> str:
    payload = {**telemetry, "unconnected_items": leftover}
    return (
        "Headless FreeRouting finished for this CM5 camera stage. Continue in this "
        "conversation from the telemetry; do not start over.\n"
        f"{json.dumps(payload, default=str)}"
    )


def _prepare(copper_pilot: CopperPilotAgent) -> _Session:
    global _active
    existing = _sessions.get(id(copper_pilot))
    if existing is not None:
        _active = existing
        return existing
    configure_stdio()
    workspace = Path(copper_pilot.workspace)
    require_project(workspace)
    cli = require_kicad_10()
    copper_pilot.tool_broker = LocalToolBroker(workspace, mode=ApprovalMode.YOLO)
    copper_pilot._client.first_frame_timeout = TURN_FIRST_FRAME_TIMEOUT_S
    copper_pilot._client.idle_frame_timeout = TURN_IDLE_TIMEOUT_S
    session = _Session(copper_pilot=copper_pilot, workspace=workspace, cli=cli)
    _sessions[id(copper_pilot)] = session
    _active = session
    return session


def _require_session() -> _Session:
    if _active is None:
        raise RuntimeError("Call run() before checking ERC/DRC or rendering.")
    return _active


def _cli_workspace() -> tuple[Path, Path]:
    if _active is not None:
        return _active.cli, _active.workspace
    return require_kicad_10(), WORKSPACE


def _refresh_erc(session: _Session | None = None) -> dict[str, Any]:
    if session is not None:
        session.erc = run_erc_report(session.cli, workspace=session.workspace)
        return session.erc
    cli, workspace = _cli_workspace()
    erc = run_erc_report(cli, workspace=workspace)
    if _active is not None:
        _active.erc = erc
    return erc


def _refresh_drc(session: _Session | None = None) -> dict[str, Any]:
    if session is not None:
        session.drc = run_drc_report(session.cli, workspace=session.workspace)
        return session.drc
    cli, workspace = _cli_workspace()
    drc = run_drc_report(cli, workspace=workspace)
    if _active is not None:
        _active.drc = drc
    return drc


async def _repair_erc(session: _Session) -> None:
    copper_pilot = session.copper_pilot
    for attempt in range(MAX_ERC_REPAIR_TURNS + 1):
        erc = _refresh_erc(session)
        print_line(format_erc_summary(erc))
        if not erc["errors"]:
            return
        if attempt == MAX_ERC_REPAIR_TURNS:
            return
        prompt = erc_repair_prompt(erc)
        announce_phase(f"ERC repair turn {attempt + 1}")
        session.messages.append(HumanMessage(content=prompt))
        session.messages = await run_turn(copper_pilot, session.messages)


async def _repair_drc(session: _Session) -> None:
    copper_pilot = session.copper_pilot
    for attempt in range(MAX_DRC_REPAIR_TURNS + 1):
        drc = _refresh_drc(session)
        print_line(format_drc_summary(drc))
        if not drc["violations"] and not drc["unconnected_items"]:
            return
        if attempt == MAX_DRC_REPAIR_TURNS:
            return
        prompt = drc_repair_prompt(drc)
        announce_phase(f"DRC repair turn {attempt + 1}")
        session.messages.append(HumanMessage(content=prompt))
        last_error: RuntimeError | None = None
        for retry in range(MAX_TURN_RETRIES + 1):
            try:
                session.messages = await run_turn(copper_pilot, session.messages)
                last_error = None
                break
            except RuntimeError as exc:
                last_error = exc
                if SOCKET_CLOSED not in str(exc) or retry == MAX_TURN_RETRIES:
                    raise
                announce_phase(
                    f"Retry {retry + 1}",
                    "Hosted socket closed; resuming DRC repair.",
                )
        if last_error is not None:
            raise last_error


async def _arun(copper_pilot: CopperPilotAgent, prompt: str, mode: CopperMode) -> None:
    session = _prepare(copper_pilot)
    copper_pilot.mode = mode
    title = "Plan" if mode is CopperMode.PLAN else "Agent"
    announce_phase(title, prompt)
    if mode is CopperMode.PLAN:
        copper_pilot.plan_context = None
    elif copper_pilot.plan_context is None:
        try:
            copper_pilot.plan_context = load_latest_plan(session.workspace).model_copy(
                update={"overall_status": "running"}
            )
        except FileNotFoundError:
            pass
    session.messages.append(HumanMessage(content=prompt))
    last_error: RuntimeError | None = None
    for attempt in range(MAX_TURN_RETRIES + 1):
        try:
            session.messages = await run_turn(copper_pilot, session.messages)
            last_error = None
            break
        except RuntimeError as exc:
            last_error = exc
            if SOCKET_CLOSED not in str(exc) or attempt == MAX_TURN_RETRIES:
                raise
            announce_phase(
                f"Retry {attempt + 1}",
                "Hosted socket closed; resuming the same phase.",
            )
    if last_error is not None:
        raise last_error
    if mode is CopperMode.PLAN:
        plan = load_latest_plan(session.workspace).model_copy(update={"overall_status": "running"})
        copper_pilot.plan_context = plan
        print_line(f"Loaded plan {plan.file_path} with {len(plan.todos)} todos.")


def run(
    copper_pilot: CopperPilotAgent,
    prompt: str,
    mode: CopperMode = CopperMode.AGENT,
) -> None:
    """Run one hosted turn and stream assistant text without ERC/DRC repair."""

    asyncio.run(_arun(copper_pilot, prompt, mode))


def start(copper_pilot: CopperPilotAgent) -> None:
    """Attach YOLO tools and KiCad without sending a hosted prompt."""

    _prepare(copper_pilot)


def _autorouter_platform() -> str:
    if sys.platform == "darwin":
        return "mac"
    if sys.platform.startswith("win"):
        return "win"
    return "linux"


def _skill_json(proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    text = (proc.stdout or "").strip()
    if not text:
        raise RuntimeError(f"kicad_autorouter produced no JSON.\nstderr: {proc.stderr}")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError(f"kicad_autorouter output was not JSON: {text[:500]}") from exc
        payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise RuntimeError("kicad_autorouter JSON was not an object.")
    return payload


def route_headless(
    workspace: Path | None = None,
    *,
    stage: str = "gpio",
    passes: int | None = None,
) -> dict[str, Any]:
    """Run the local kicad_autorouter skill in headless mode for one routing stage."""

    root, _, pcb, _, _ = project_paths(workspace or (_active.workspace if _active else WORKSPACE))
    skill = root / ".copperpilot" / "skills" / "kicad_autorouter" / "main.py"
    if not skill.is_file():
        raise FileNotFoundError(
            "kicad_autorouter skill is missing under .copperpilot/skills/kicad_autorouter."
        )
    if stage == "diffs":
        ignore = DIFF_STAGE_IGNORE
        label = "differential pairs"
    elif stage == "gpio":
        ignore = GPIO_STAGE_IGNORE
        label = "leftover GPIO"
    else:
        raise ValueError(f"Unknown autorouter stage {stage!r}")
    platform = _autorouter_platform()
    python = sys.executable
    diagnose = subprocess.run(  # noqa: S603
        [python, str(skill), "diagnose", "--platform", platform],
        check=False,
        capture_output=True,
        text=True,
        cwd=root,
    )
    try:
        diagnosis = _skill_json(diagnose)
        recommended = diagnosis.get("recommended_python")
        if recommended:
            python = str(recommended)
    except RuntimeError:
        print_line(diagnose.stdout)
        print_line(diagnose.stderr)
    print_line(f"Running headless FreeRouting ({label}) via kicad_autorouter.")
    command = [
        python,
        str(skill),
        "route",
        "--platform",
        platform,
        "--project-dir",
        str(root),
        "--pcb",
        str(pcb.relative_to(root)),
        "--timeout-sec",
        str(ROUTE_TIMEOUT_SEC),
        "--freerouting-version",
        FREEROUTING_VERSION,
        "--headless",
        "--refill-zones",
        "--disable-analytics",
        "--extra-freerouting-arg=-mp",
        f"--extra-freerouting-arg={ROUTE_PASSES if passes is None else passes}",
    ]
    for net_class in ignore:
        command.extend(["--ignore-net-class", net_class])
    routed = subprocess.run(  # noqa: S603
        command,
        check=False,
        capture_output=True,
        text=True,
        cwd=root,
    )
    print_line(routed.stdout[-4000:] if routed.stdout else "")
    if routed.stderr:
        print_line(routed.stderr[-2000:])
    try:
        payload = _skill_json(routed)
    except RuntimeError as exc:
        raise RuntimeError(
            "Headless FreeRouting failed.\n"
            f"stdout: {routed.stdout[-2000:]}\nstderr: {routed.stderr[-2000:]}"
        ) from exc
    timed_out = False
    commands = payload.get("commands") or []
    if commands and isinstance(commands[0], dict):
        timed_out = bool(commands[0].get("timed_out"))
    print_line(f"Autorouter stage={stage} status={payload.get('status')} timed_out={timed_out}")
    telemetry = {
        "stage": stage,
        "status": payload.get("status"),
        "timed_out": timed_out,
        "ignore_net_classes": list(ignore),
    }
    if timed_out:
        print_line("FreeRouting hit the wall-clock timeout; leftover nets stay unconnected.")
        return telemetry
    if payload.get("status") in {"error", "failed", "missing_dependencies"}:
        raise RuntimeError(f"Headless FreeRouting status={payload.get('status')}")
    return telemetry


def repair_erc() -> None:
    """Optionally run one ERC-only repair turn after schematic ERC."""

    session = _require_session()
    asyncio.run(_repair_erc(session))


def repair_drc() -> None:
    """Optionally run one DRC-only repair turn after routing."""

    session = _require_session()
    asyncio.run(_repair_drc(session))


def erc_errors() -> int:
    erc = _refresh_erc()
    return len(erc["errors"])


def drc_violations() -> int:
    drc = _refresh_drc()
    return len(drc["violations"])


def shorting_items() -> int:
    drc = _refresh_drc()
    return sum(1 for item in drc["violations"] if item.get("type") == "shorting_items")


def unconnected_count() -> int:
    drc = _refresh_drc()
    return len(drc["unconnected_items"])


def render_pcb(path: str = f"{PROJECT_NAME}.png") -> bool:
    cli, workspace = _cli_workspace()
    root, _, pcb, _, _ = project_paths(workspace)
    png = resolve_png_path(workspace, path)
    png.parent.mkdir(parents=True, exist_ok=True)
    relative_png = str(png.relative_to(root))
    relative_pcb = str(pcb.relative_to(root))
    rendered = _run_kicad(
        cli,
        [
            "pcb",
            "render",
            "--output",
            relative_png,
            "--side",
            "top",
            "--width",
            "1600",
            "--height",
            "1200",
            "--quality",
            "basic",
            "--background",
            "opaque",
            relative_pcb,
        ],
        cwd=root,
    )
    if rendered.returncode != 0 or not png.is_file() or png.stat().st_size == 0:
        raise RuntimeError(
            "kicad-cli pcb render failed to write a PNG.\n"
            f"stdout: {rendered.stdout}\nstderr: {rendered.stderr}"
        )
    assert png.is_file() and png.stat().st_size > 0, f"Expected a non-empty PNG at {png}"
    print_line(f"Wrote {png}")
    return True
