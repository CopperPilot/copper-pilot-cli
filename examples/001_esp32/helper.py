"""Shared helpers for the ESP32 LangChain example."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
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
SCHEMATIC = WORKSPACE / "esp32" / "esp32.kicad_sch"
PCB = WORKSPACE / "esp32" / "esp32.kicad_pcb"
PNG = WORKSPACE / "esp32" / "esp32.png"
REPORT_DIR = WORKSPACE / ".copperpilot" / "tmp"
ERC_REPORT = REPORT_DIR / "esp32-erc.json"
DRC_REPORT = REPORT_DIR / "esp32-drc.json"
IGNORED_SEVERITIES = {"excluded", "ignore", "ignored"}
FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
VERSION_RE = re.compile(r"(\d+)\.(\d+)")
KICAD_CLI_CANDIDATES = (
    Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"),
    Path("/Applications/KiCad 10.0/KiCad.app/Contents/MacOS/kicad-cli"),
    Path("/usr/bin/kicad-cli"),
    Path("/usr/local/bin/kicad-cli"),
    Path("C:/Program Files/KiCad/10.0/bin/kicad-cli.exe"),
    Path("C:/Program Files/KiCad/bin/kicad-cli.exe"),
)
TURN_IDLE_TIMEOUT_S = 1_800.0
TURN_FIRST_FRAME_TIMEOUT_S = 180.0
MAX_REPAIR_TURNS = 2


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
    if (resolved / "esp32" / "esp32.kicad_sch").is_file():
        return resolved
    if (resolved / "esp32.kicad_sch").is_file() and resolved.name == "esp32":
        return resolved.parent
    return resolved


def project_paths(workspace: Path) -> tuple[Path, Path, Path, Path, Path]:
    root = example_root(workspace)
    schematic = root / "esp32" / "esp32.kicad_sch"
    pcb = root / "esp32" / "esp32.kicad_pcb"
    png = root / "esp32" / "esp32.png"
    report_dir = root / ".copperpilot" / "tmp"
    return root, schematic, pcb, png, report_dir


def resolve_png_path(workspace: Path, path: str = "esp32.png") -> Path:
    root, _, _, png, _ = project_paths(workspace)
    if path in {"esp32.png", "esp32/esp32.png"}:
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
    return {
        "violations": _active_violations(violations),
        "unconnected_items": unconnected,
        "kicad_version": report.get("kicad_version", ""),
    }


def run_electrical_checks(
    cli: Path, *, workspace: Path | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    root, schematic, pcb, _, report_dir = project_paths(workspace or WORKSPACE)
    erc_report = report_dir / "esp32-erc.json"
    drc_report = report_dir / "esp32-drc.json"
    report_dir.mkdir(parents=True, exist_ok=True)
    erc_proc = _run_kicad(
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
    drc_proc = _run_kicad(
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
    if not erc_report.is_file():
        raise RuntimeError(
            "kicad-cli sch erc did not write a JSON report.\n"
            f"stdout: {erc_proc.stdout}\nstderr: {erc_proc.stderr}"
        )
    if not drc_report.is_file():
        raise RuntimeError(
            "kicad-cli pcb drc did not write a JSON report.\n"
            f"stdout: {drc_proc.stdout}\nstderr: {drc_proc.stderr}"
        )
    erc = summarize_erc(json.loads(erc_report.read_text(encoding="utf-8")))
    drc = summarize_drc(json.loads(drc_report.read_text(encoding="utf-8")))
    return erc, drc


def reports_are_clean(erc: dict[str, Any], drc: dict[str, Any]) -> bool:
    return not erc["errors"] and not drc["violations"] and not drc["unconnected_items"]


def format_check_summary(erc: dict[str, Any], drc: dict[str, Any]) -> str:
    return (
        f"ERC errors={len(erc['errors'])} warnings={len(erc['warnings'])}; "
        f"DRC violations={len(drc['violations'])} "
        f"unconnected={len(drc['unconnected_items'])}"
    )


def require_project(workspace: Path | None = None) -> None:
    _, schematic, pcb, _, _ = project_paths(workspace or WORKSPACE)
    if not schematic.is_file() or not pcb.is_file():
        raise FileNotFoundError(
            f"Expected a KiCad project under {(workspace or WORKSPACE) / 'esp32'}"
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


def repair_prompt(erc: dict[str, Any], drc: dict[str, Any]) -> str:
    payload = {
        "erc_errors": erc["errors"][:40],
        "erc_warnings": erc["warnings"][:20],
        "drc_violations": drc["violations"][:40],
        "unconnected_items": drc["unconnected_items"][:40],
    }
    return (
        "Fix these ERC/DRC violations so both reports are clean.\n"
        "Keep the existing ESP32 schematic and PCB; do not start over.\n"
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


def _refresh_reports(session: _Session | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    current = session or _require_session()
    current.erc, current.drc = run_electrical_checks(current.cli, workspace=current.workspace)
    return current.erc, current.drc


async def _repair_until_clean(session: _Session) -> None:
    copper_pilot = session.copper_pilot
    for attempt in range(MAX_REPAIR_TURNS + 1):
        erc, drc = _refresh_reports(session)
        summary = format_check_summary(erc, drc)
        print_line(summary)
        if reports_are_clean(erc, drc):
            return
        if attempt == MAX_REPAIR_TURNS:
            raise AssertionError(f"Board is not ERC/DRC clean after repairs: {summary}")
        prompt = repair_prompt(erc, drc)
        announce_phase(f"Repair turn {attempt + 1}")
        session.messages.append(HumanMessage(content=prompt))
        session.messages = await run_turn(copper_pilot, session.messages)


async def _arun(copper_pilot: CopperPilotAgent, prompt: str, mode: CopperMode) -> None:
    session = _prepare(copper_pilot)
    copper_pilot.mode = mode
    title = "Plan" if mode is CopperMode.PLAN else "Agent"
    announce_phase(title, prompt)
    if mode is CopperMode.PLAN:
        copper_pilot.plan_context = None
    elif copper_pilot.plan_context is None and session.messages:
        copper_pilot.plan_context = load_latest_plan(session.workspace).model_copy(
            update={"overall_status": "running"}
        )
    session.messages.append(HumanMessage(content=prompt))
    session.messages = await run_turn(copper_pilot, session.messages)
    if mode is CopperMode.PLAN:
        plan = load_latest_plan(session.workspace).model_copy(update={"overall_status": "running"})
        copper_pilot.plan_context = plan
        print_line(f"Loaded plan {plan.file_path} with {len(plan.todos)} todos.")
        return
    await _repair_until_clean(session)


def run(
    copper_pilot: CopperPilotAgent,
    prompt: str,
    mode: CopperMode = CopperMode.AGENT,
) -> None:
    """Run one hosted turn and stream assistant text."""

    asyncio.run(_arun(copper_pilot, prompt, mode))


def erc_errors() -> int:
    erc, _drc = _refresh_reports()
    return len(erc["errors"])


def drc_violations() -> int:
    _erc, drc = _refresh_reports()
    return len(drc["violations"]) + len(drc["unconnected_items"])


def render_pcb(path: str = "esp32.png") -> None:
    session = _require_session()
    root, _, pcb, _, _ = project_paths(session.workspace)
    png = resolve_png_path(session.workspace, path)
    png.parent.mkdir(parents=True, exist_ok=True)
    relative_png = str(png.relative_to(root))
    relative_pcb = str(pcb.relative_to(root))
    rendered = _run_kicad(
        session.cli,
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
