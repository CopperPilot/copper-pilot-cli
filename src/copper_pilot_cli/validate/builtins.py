"""Deterministic checks: kicad-cli reports and file inspections, no hosted turn."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from copper_pilot_cli.validate.config import (
    SEVERITY_RANK,
    CheckSpec,
    Confidence,
    Finding,
    Locator,
    Severity,
)
from copper_pilot_cli.validate.design import (
    DesignContext,
    build_index,
    iter_design_files,
    load_intent,
)
from copper_pilot_cli.validate.kicad import SUPPORTED_MAJORS, KicadError

MAX_REFS_PER_FINDING = 50
_IGNORED_SEVERITIES = {"excluded", "ignore", "ignored", "exclusion"}
_REF_RE = re.compile(r"\b([A-Z]{1,4}\d+)\b")
_NET_RE = re.compile(r"\[([^\]]+)\]")
_ABSOLUTE_USER_PATH_RE = re.compile(
    r"(?:/home/[^/\s\"']+|/Users/[^/\s\"']+|[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s\"']+)"
)
_LCSC_RE = re.compile(r"^C\d{2,}$")
_COPPER_GERBER_RE = re.compile(r"(?:_Cu\.[^.]+|\.(?:gtl|gbl|g\d+))$", re.IGNORECASE)


@dataclass(slots=True)
class CheckOutcome:
    findings: list[Finding] = field(default_factory=list)
    summary: str = ""
    error: str | None = None


BuiltinFn = Callable[[DesignContext, CheckSpec, dict[str, Any]], CheckOutcome]


@dataclass(frozen=True, slots=True)
class Builtin:
    fn: BuiltinFn
    cacheable: bool = True
    description: str = ""


def _finding(
    severity: Severity,
    title: str,
    detail: str = "",
    *,
    refs: list[str] | None = None,
    nets: list[str] | None = None,
    sheet: str | None = None,
    layer: str | None = None,
    x_mm: float | None = None,
    y_mm: float | None = None,
    evidence: str = "",
    rule: str = "",
    action: str = "",
) -> Finding:
    return Finding(
        severity=severity,
        title=title,
        detail=detail,
        locator=Locator(
            refs=refs or [], nets=nets or [], sheet=sheet, layer=layer, x_mm=x_mm, y_mm=y_mm
        ),
        evidence=evidence,
        rule=rule,
        suggested_action=action,
        confidence=Confidence.HIGH,
        grounded=True,
    )


# ------------------------------------------------------------------- S0


def check_tools(design: DesignContext, check: CheckSpec, limits: dict[str, Any]) -> CheckOutcome:
    del check, limits
    cli = design.kicad
    if cli is None:
        return CheckOutcome(
            findings=[
                _finding(
                    Severity.P0,
                    "kicad-cli was not found",
                    "kicad-cli is required for ERC, DRC, netlist and export checks.",
                    rule="validate requires KiCad on PATH",
                    action="Install KiCad 10 or set KICAD_CLI to the kicad-cli executable.",
                )
            ],
            summary="kicad-cli missing",
        )
    try:
        version = cli.version()
        major = cli.major()
    except KicadError as exc:
        return CheckOutcome(error=str(exc))
    design.kicad_version = version
    if major not in SUPPORTED_MAJORS:
        return CheckOutcome(
            findings=[
                _finding(
                    Severity.P0,
                    f"kicad-cli {version} is not supported",
                    f"Supported major versions: {', '.join(map(str, SUPPORTED_MAJORS))}.",
                    rule="validate requires KiCad 9 or 10",
                    action="Install KiCad 10.",
                )
            ],
            summary=f"kicad-cli {version} unsupported",
        )
    return CheckOutcome(summary=f"kicad-cli {version} at {cli.executable}")


def check_project(design: DesignContext, check: CheckSpec, limits: dict[str, Any]) -> CheckOutcome:
    del limits
    findings: list[Finding] = []
    schematic = design.schematic_path
    pcb = design.pcb_path
    if design.project_path is None:
        findings.append(
            _finding(
                Severity.P2,
                "No .kicad_pro project file",
                "Net classes and board rules are read from the project file.",
                action="Save the project from KiCad so a .kicad_pro file exists.",
            )
        )
    if schematic is None or not schematic.is_file():
        findings.append(
            _finding(
                Severity.P0,
                "No KiCad schematic found",
                "validate needs a root .kicad_sch to export the netlist.",
                action="Point validate at the folder that contains the KiCad project.",
            )
        )
        return CheckOutcome(findings=findings, summary="no schematic")
    if pcb is None or not pcb.is_file():
        findings.append(
            _finding(
                Severity.P2,
                "No KiCad PCB found",
                "PCB stages will report ERROR until a .kicad_pcb exists.",
                action="Create the PCB from the schematic (Update PCB from Schematic).",
            )
        )
    if design.kicad is None:
        return CheckOutcome(error="kicad-cli is unavailable; intake.tools must pass first")
    artifacts = design.artifacts_dir(check.id)
    try:
        netlist = design.kicad.netlist(schematic, artifacts / "netlist.xml")
    except KicadError as exc:
        return CheckOutcome(error=str(exc))
    project_text = (
        design.project_path.read_text(encoding="utf-8", errors="replace")
        if design.project_path is not None and design.project_path.is_file()
        else None
    )
    pcb_text = pcb.read_text(encoding="utf-8", errors="replace") if pcb and pcb.is_file() else None
    try:
        design.index = build_index(netlist, project_text, pcb_text)
    except Exception as exc:  # noqa: BLE001 - any parse failure is a check error
        return CheckOutcome(error=f"could not index the netlist: {exc}")
    design.intent = load_intent(design.workspace)
    summary = design.index.summary()
    if not design.index.parts:
        findings.append(
            _finding(
                Severity.P1,
                "The netlist has no components",
                "The schematic exported an empty component list.",
                action="Check that the root sheet is the one validate discovered.",
            )
        )
    return CheckOutcome(
        findings=findings,
        summary=(
            f"{summary['parts']} parts, {summary['nets']} nets, {summary['sheets']} sheets, "
            f"{summary['copper_layers']} copper layers"
        ),
    )


def check_intent(design: DesignContext, check: CheckSpec, limits: dict[str, Any]) -> CheckOutcome:
    del check, limits
    intent = load_intent(design.workspace)
    design.intent = intent
    findings: list[Finding] = []
    if ".copperpilot/README.md" not in intent:
        findings.append(
            _finding(
                Severity.P3,
                "No project index (.copperpilot/README.md)",
                "Architecture reviewers use the index to learn what the board is for.",
                action="Describe the board, its interfaces and its power budget in the index.",
            )
        )
    if not any(key.endswith(".plan.md") for key in intent):
        findings.append(
            _finding(
                Severity.P3,
                "No plan in .copperpilot/plans/",
                "Reviewers compare the design with the plan it was built from.",
                action="Run a Plan-mode turn or write a short plan with the requirements.",
            )
        )
    return CheckOutcome(findings=findings, summary=f"{len(intent)} intent files")


# ------------------------------------------------------------------- S2


def check_bom_fields(
    design: DesignContext, check: CheckSpec, limits: dict[str, Any]
) -> CheckOutcome:
    del check, limits
    index = design.index
    if index is None:
        return CheckOutcome(error="the design index is missing; intake.project must pass first")
    missing: dict[str, list[str]] = {
        "Value": [],
        "Footprint": [],
        "MPN": [],
        "LCSC": [],
        "LCSC format": [],
        "Datasheet": [],
    }
    reviewed = 0
    for part in index.parts:
        if part.dnp or part.exclude_from_bom:
            continue
        reviewed += 1
        if not part.value:
            missing["Value"].append(part.ref)
        if not part.footprint:
            missing["Footprint"].append(part.ref)
        if part.mechanical:
            continue
        if not part.mpn:
            missing["MPN"].append(part.ref)
        if not part.lcsc:
            missing["LCSC"].append(part.ref)
        elif not _LCSC_RE.match(part.lcsc.upper()):
            missing["LCSC format"].append(part.ref)
        if not part.datasheet or part.datasheet == "~":
            missing["Datasheet"].append(part.ref)
    severity = {
        "Value": Severity.P1,
        "Footprint": Severity.P1,
        "MPN": Severity.P2,
        "LCSC": Severity.P2,
        "LCSC format": Severity.P1,
        "Datasheet": Severity.P3,
    }
    findings = [
        _finding(
            severity[name],
            f"{len(refs)} part(s) missing {name}"
            if name != "LCSC format"
            else f"{len(refs)} part(s) with a malformed LCSC number",
            f"{name} is required for ordering and assembly. Affected: {', '.join(refs[:50])}",
            refs=refs[:MAX_REFS_PER_FINDING],
            rule="BOM fields: Value, Footprint, MPN, LCSC (C#####), Datasheet",
            action=f"Fill the {name} field on each listed part (DNP parts are exempt).",
        )
        for name, refs in missing.items()
        if refs
    ]
    return CheckOutcome(findings=findings, summary=f"{reviewed} parts reviewed")


# ---------------------------------------------------------------- ERC/DRC


def _items_locator(
    items: list[dict[str, Any]],
) -> tuple[list[str], list[str], float | None, float | None]:
    refs: list[str] = []
    nets: list[str] = []
    x = y = None
    for item in items:
        description = str(item.get("description") or "")
        for ref in _REF_RE.findall(description):
            if ref not in refs:
                refs.append(ref)
        for net in _NET_RE.findall(description):
            if net not in nets:
                nets.append(net)
        pos = item.get("pos")
        if x is None and isinstance(pos, dict):
            x, y = pos.get("x"), pos.get("y")
    return refs, nets, x, y


def group_violations(
    violations: list[dict[str, Any]],
    *,
    severity_for: Callable[[str], Severity | None],
    rule: str,
    sheet: str | None = None,
) -> list[Finding]:
    """One finding per violation type, with a count and up to 50 refs."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for violation in violations:
        if str(violation.get("severity") or "").lower() in _IGNORED_SEVERITIES:
            continue
        kind = str(violation.get("type") or "unknown")
        groups.setdefault(kind, []).append(violation)
    findings: list[Finding] = []
    for kind, members in sorted(groups.items()):
        worst: Severity | None = None
        for member in members:
            severity = severity_for(str(member.get("severity") or "warning").lower())
            if severity is not None and (
                worst is None or SEVERITY_RANK[severity] < SEVERITY_RANK[worst]
            ):
                worst = severity
        if worst is None:
            continue
        refs: list[str] = []
        nets: list[str] = []
        x = y = None
        for member in members:
            member_refs, member_nets, mx, my = _items_locator(member.get("items") or [])
            refs.extend(ref for ref in member_refs if ref not in refs)
            nets.extend(net for net in member_nets if net not in nets)
            if x is None:
                x, y = mx, my
        sample = str(members[0].get("description") or kind)
        findings.append(
            _finding(
                worst,
                f"{kind}: {len(members)} violation(s)",
                sample,
                refs=refs[:MAX_REFS_PER_FINDING],
                nets=nets[:MAX_REFS_PER_FINDING],
                sheet=sheet,
                x_mm=x,
                y_mm=y,
                evidence=f"{len(members)} report entries of type {kind}; first: {sample}",
                rule=rule,
                action=f"Resolve every {kind} entry in the report; do not exclude violations.",
            )
        )
    return findings


def _erc_severity(value: str) -> Severity | None:
    if value == "error":
        return Severity.P1
    if value == "warning":
        return Severity.P2
    return None


def check_erc(design: DesignContext, check: CheckSpec, limits: dict[str, Any]) -> CheckOutcome:
    del limits
    schematic = design.schematic_path
    if design.kicad is None or schematic is None or not schematic.is_file():
        return CheckOutcome(error="ERC needs kicad-cli and a schematic")
    try:
        report = design.kicad.erc(schematic, design.artifacts_dir(check.id) / "erc.json")
    except KicadError as exc:
        return CheckOutcome(error=str(exc))
    findings: list[Finding] = []
    total = 0
    for sheet in report.get("sheets") or []:
        if not isinstance(sheet, dict):
            continue
        violations = [v for v in (sheet.get("violations") or []) if isinstance(v, dict)]
        total += len(violations)
        findings.extend(
            group_violations(
                violations,
                severity_for=_erc_severity,
                rule="KiCad ERC",
                sheet=str(sheet.get("path") or "/"),
            )
        )
    return CheckOutcome(findings=findings, summary=f"{total} ERC entries")


def _drc_severity(value: str) -> Severity | None:
    if value == "error":
        return Severity.P1
    if value == "warning":
        return Severity.P2
    return None


def _p0(_value: str) -> Severity | None:
    return Severity.P0


def _parity_severity(value: str) -> Severity | None:
    """Missing or wrong footprints are errors (P0); field and value drift is P1."""
    return Severity.P0 if value == "error" else Severity.P1


def _drc_report(design: DesignContext, check: CheckSpec, *, parity: bool) -> dict[str, Any]:
    pcb = design.pcb_path
    if design.kicad is None or pcb is None or not pcb.is_file():
        raise KicadError("DRC needs kicad-cli and a PCB")
    return design.kicad.drc(
        pcb, design.artifacts_dir(check.id) / "drc.json", schematic_parity=parity
    )


def check_parity(design: DesignContext, check: CheckSpec, limits: dict[str, Any]) -> CheckOutcome:
    del limits
    try:
        report = _drc_report(design, check, parity=True)
    except KicadError as exc:
        return CheckOutcome(error=str(exc))
    parity = [v for v in (report.get("schematic_parity") or []) if isinstance(v, dict)]
    unconnected = [v for v in (report.get("unconnected_items") or []) if isinstance(v, dict)]
    findings = group_violations(
        parity, severity_for=_parity_severity, rule="Schematic parity (DRC)"
    )
    findings.extend(group_violations(unconnected, severity_for=_p0, rule="Unconnected items (DRC)"))
    return CheckOutcome(
        findings=findings,
        summary=f"{len(parity)} parity, {len(unconnected)} unconnected",
    )


def check_drc(design: DesignContext, check: CheckSpec, limits: dict[str, Any]) -> CheckOutcome:
    del limits
    try:
        report = _drc_report(design, check, parity=False)
    except KicadError as exc:
        return CheckOutcome(error=str(exc))
    violations = [v for v in (report.get("violations") or []) if isinstance(v, dict)]
    findings = group_violations(violations, severity_for=_drc_severity, rule="KiCad DRC")
    return CheckOutcome(findings=findings, summary=f"{len(violations)} DRC entries")


# ------------------------------------------------------------------- S4


_RULE_LIMITS = (
    ("min_clearance", "min_clearance_mm", "minimum clearance"),
    ("min_track_width", "min_track_mm", "minimum track width"),
    ("min_via_diameter", "min_via_diameter_mm", "minimum via diameter"),
    ("min_through_hole_diameter", "min_drill_mm", "minimum through-hole drill"),
    ("min_copper_edge_clearance", "min_copper_edge_mm", "copper to edge clearance"),
    ("min_text_height", "min_silk_text_height_mm", "minimum text height"),
)
_CLASS_LIMITS = (
    ("clearance", "min_clearance_mm", "clearance"),
    ("track_width", "min_track_mm", "track width"),
    ("via_diameter", "min_via_diameter_mm", "via diameter"),
    ("via_drill", "min_drill_mm", "via drill"),
)


def check_rules(design: DesignContext, check: CheckSpec, limits: dict[str, Any]) -> CheckOutcome:
    del check
    index = design.index
    if index is None:
        return CheckOutcome(error="the design index is missing; intake.project must pass first")
    if not limits:
        return CheckOutcome(summary="no fab limits configured")
    fab = str(limits.get("fab") or "fab")
    findings: list[Finding] = []
    for rule_key, limit_key, label in _RULE_LIMITS:
        limit = limits.get(limit_key)
        if not isinstance(limit, (int, float)):
            continue
        value = index.rules.get(rule_key)
        if not isinstance(value, (int, float)):
            continue
        if value <= 0:
            findings.append(
                _finding(
                    Severity.P2,
                    f"Board {label} is not set",
                    f"design_settings.rules.{rule_key} is 0; only net classes constrain it.",
                    rule=f"{fab} {limit_key} = {limit} mm",
                    evidence=f".kicad_pro rules.{rule_key} = {value}",
                    action=f"Set the board {label} to at least {limit} mm.",
                )
            )
        elif value < limit:
            findings.append(
                _finding(
                    Severity.P1,
                    f"Board {label} {value} mm is below the fab limit {limit} mm",
                    f"design_settings.rules.{rule_key} = {value}.",
                    rule=f"{fab} {limit_key} = {limit} mm",
                    evidence=f".kicad_pro rules.{rule_key} = {value}",
                    action=f"Raise the board {label} to at least {limit} mm.",
                )
            )
    for name, settings in index.netclasses.items():
        for class_key, limit_key, label in _CLASS_LIMITS:
            limit = limits.get(limit_key)
            value = settings.get(class_key)
            if not isinstance(limit, (int, float)) or not isinstance(value, (int, float)):
                continue
            if value < limit:
                findings.append(
                    _finding(
                        Severity.P1,
                        f"Net class {name} {label} {value} mm is below the fab limit {limit} mm",
                        f"Nets in {name}: {', '.join(index.nets_in_class(name)[:20]) or 'none'}",
                        nets=index.nets_in_class(name)[:MAX_REFS_PER_FINDING],
                        rule=f"{fab} {limit_key} = {limit} mm",
                        evidence=f".kicad_pro net class {name}.{class_key} = {value}",
                        action=f"Raise {name} {label} to at least {limit} mm.",
                    )
                )
    return CheckOutcome(
        findings=findings,
        summary=f"{len(index.netclasses)} net classes checked against {fab}",
    )


# ------------------------------------------------------------------- S8


def check_outputs(design: DesignContext, check: CheckSpec, limits: dict[str, Any]) -> CheckOutcome:
    del limits
    pcb = design.pcb_path
    index = design.index
    if design.kicad is None or pcb is None or not pcb.is_file():
        return CheckOutcome(error="Gerber export needs kicad-cli and a PCB")
    artifacts = design.artifacts_dir(check.id)
    try:
        gerbers = design.kicad.gerbers(pcb, artifacts / "gerbers")
        drills = design.kicad.drill(pcb, artifacts / "drill")
    except KicadError as exc:
        return CheckOutcome(error=str(exc))
    findings: list[Finding] = []
    names = [path.name for path in gerbers]
    if not any("Edge_Cuts" in name or "Edge.Cuts" in name for name in names):
        findings.append(
            _finding(
                Severity.P0,
                "No board outline Gerber (Edge.Cuts)",
                "The fab cannot cut a board without an outline layer.",
                layer="Edge.Cuts",
                evidence=f"Gerber files: {', '.join(names[:20])}",
                rule="Gerber set must include Edge.Cuts",
                action="Draw a closed outline on Edge.Cuts and enable it in the plot settings.",
            )
        )
    if not any(path.suffix.lower() in {".drl", ".xln", ".txt"} for path in drills):
        findings.append(
            _finding(
                Severity.P0,
                "No drill file generated",
                "The fab needs an Excellon drill file.",
                evidence=f"Drill outputs: {', '.join(p.name for p in drills[:10])}",
                rule="Drill export must produce an Excellon file",
                action="Check the board has drilled holes and the drill export succeeds.",
            )
        )
    copper = [name for name in names if _COPPER_GERBER_RE.search(name)]
    expected = len(index.copper_layers) if index else 0
    if expected and len(copper) != expected:
        findings.append(
            _finding(
                Severity.P1,
                f"{len(copper)} copper Gerbers for {expected} copper layers",
                "Plot settings exclude or duplicate a copper layer.",
                evidence=f"Copper Gerbers: {', '.join(copper)}",
                rule="One Gerber per enabled copper layer",
                action="Enable every copper layer in the plot layer selection.",
            )
        )
    return CheckOutcome(
        findings=findings,
        summary=f"{len(gerbers)} Gerber files, {len(drills)} drill files",
    )


def check_repo(design: DesignContext, check: CheckSpec, limits: dict[str, Any]) -> CheckOutcome:
    del check, limits
    findings: list[Finding] = []
    absolute: dict[str, list[str]] = {}
    external_libs: list[str] = []
    for path in iter_design_files(design.workspace):
        if path.suffix.lower() not in {
            ".kicad_pro",
            ".kicad_sch",
            ".kicad_pcb",
        } and path.name not in {"fp-lib-table", "sym-lib-table"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        relative = path.relative_to(design.workspace).as_posix()
        for match in _ABSOLUTE_USER_PATH_RE.finditer(text):
            absolute.setdefault(relative, []).append(match.group(0))
        if path.name in {"fp-lib-table", "sym-lib-table"}:
            for uri in re.findall(r"\(uri\s+\"?([^\")]+)\"?\)", text):
                if not uri.startswith("${KIPRJMOD}") and (
                    Path(uri).is_absolute() or uri.startswith(("~", "../"))
                ):
                    external_libs.append(f"{relative}: {uri}")
    if absolute:
        files = sorted(absolute)
        sample = absolute[files[0]][0]
        findings.append(
            _finding(
                Severity.P1,
                f"Absolute user paths in {len(files)} file(s)",
                f"{', '.join(files[:10])}; for example {sample}",
                evidence=f"{sum(len(v) for v in absolute.values())} matches",
                rule="Project files must not embed user home paths",
                action="Use ${KIPRJMOD}-relative paths and project-local libraries.",
            )
        )
    if external_libs:
        findings.append(
            _finding(
                Severity.P2,
                f"{len(external_libs)} library table entries outside the project",
                "; ".join(external_libs[:10]),
                rule="Libraries should live in the project (${KIPRJMOD})",
                action="Copy symbol and footprint libraries into the project folder.",
            )
        )
    return CheckOutcome(findings=findings, summary=f"{len(absolute)} files with user paths")


BUILTINS: dict[str, Builtin] = {
    "tools": Builtin(check_tools, cacheable=False, description="kicad-cli presence and version"),
    "project": Builtin(check_project, cacheable=False, description="project discovery and index"),
    "intent": Builtin(check_intent, cacheable=False, description="design intent files"),
    "bom_fields": Builtin(check_bom_fields, description="BOM field completeness"),
    "erc": Builtin(check_erc, description="kicad-cli sch erc, grouped by type"),
    "parity": Builtin(check_parity, description="schematic parity and unconnected items"),
    "rules": Builtin(check_rules, description="board rules and net classes vs fab limits"),
    "drc": Builtin(check_drc, description="kicad-cli pcb drc, grouped by type"),
    "outputs": Builtin(check_outputs, description="Gerber and drill generation"),
    "repo": Builtin(check_repo, description="portable paths and project-local libraries"),
}
