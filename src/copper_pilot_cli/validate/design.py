"""Design snapshot, hashing, and the index used to ground reviewer findings."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import xml.etree.ElementTree as ET  # noqa: S405 - KiCad's own netlist export, local file
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from copper_pilot_cli.copper_workspace import IGNORED_NAMES, WorkspaceContext

SCRATCH_DIR = ".validate-scratch"
INTENT_FILES = (
    Path(".copperpilot") / "README.md",
    Path(".copperpilot") / "PLANS.md",
)
_SNAPSHOT_EXCLUDED_DIRS = IGNORED_NAMES | {
    ".copperpilot",
    ".ruff_cache",
    ".pytest_cache",
    SCRATCH_DIR,
}
_SNAPSHOT_EXCLUDED_SUFFIXES = {".lck", ".kicad_prl", ".pyc"}
_SNAPSHOT_EXCLUDED_NAMES = {"fp-info-cache", ".DS_Store"}
_LIB_SUFFIXES = {".kicad_sym", ".kicad_mod"}
_LIB_TABLES = {"fp-lib-table", "sym-lib-table"}
_LAYER_BLOCK_RE = re.compile(r"\(layers\s*\n((?:\s*\(\d+\s+\"[^\"]+\"[^\n]*\n)+)", re.MULTILINE)
_LAYER_LINE_RE = re.compile(r"\(\d+\s+\"([^\"]+)\"\s+(\w+)")
_MECHANICAL_PREFIXES = ("H", "TP", "FID", "LOGO", "MH", "G")


def _is_backup_dir(name: str) -> bool:
    return name.endswith("-backups")


def _skip_dir(name: str) -> bool:
    return name in _SNAPSHOT_EXCLUDED_DIRS or _is_backup_dir(name)


def _skip_file(name: str) -> bool:
    return (
        name in _SNAPSHOT_EXCLUDED_NAMES
        or name.startswith("~")
        or Path(name).suffix.lower() in _SNAPSHOT_EXCLUDED_SUFFIXES
    )


def iter_design_files(root: Path) -> list[Path]:
    """Walk the workspace the way the snapshot does, sorted for stable hashes."""
    found: list[Path] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs if not _skip_dir(name))
        for name in sorted(files):
            if _skip_file(name):
                continue
            found.append(Path(current) / name)
    return found


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_hashes(root: Path) -> dict[str, str]:
    """Per-file hashes keyed by POSIX relative path."""
    return {
        path.relative_to(root).as_posix(): file_sha256(path)
        for path in iter_design_files(root)
        if path.is_file()
    }


def combined_hash(hashes: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for key in sorted(hashes):
        digest.update(key.encode())
        digest.update(b"\0")
        digest.update(hashes[key].encode())
        digest.update(b"\n")
    return digest.hexdigest()


def design_hash(root: Path) -> str:
    return combined_hash(tree_hashes(root))


def _matches_input(relative: str, kind: str) -> bool:
    path = Path(relative)
    suffix = path.suffix.lower()
    if kind == "all":
        return True
    if kind == "sch":
        return suffix == ".kicad_sch"
    if kind == "pcb":
        return suffix == ".kicad_pcb"
    if kind == "pro":
        return suffix == ".kicad_pro"
    if kind == "libs":
        return suffix in _LIB_SUFFIXES or path.name in _LIB_TABLES
    if kind == "intent":
        parts = path.parts
        return (
            len(parts) >= 2
            and parts[0] == ".copperpilot"
            and (
                path.name in {"README.md", "PLANS.md"} or (parts[1] == "plans" and suffix == ".md")
            )
        )
    return False


def input_hashes(root: Path, inputs: list[str]) -> dict[str, str]:
    """Hashes of the files a check depends on, by `inputs:` kind."""
    hashes = tree_hashes(root)
    if "intent" in inputs:
        for relative in INTENT_FILES:
            candidate = root / relative
            if candidate.is_file():
                hashes[relative.as_posix()] = file_sha256(candidate)
        plans = root / ".copperpilot" / "plans"
        if plans.is_dir():
            for plan in sorted(plans.glob("*.md")):
                hashes[plan.relative_to(root).as_posix()] = file_sha256(plan)
    return {
        relative: value
        for relative, value in hashes.items()
        if any(_matches_input(relative, kind) for kind in inputs)
    }


# ----------------------------------------------------------------- snapshot


def _make_read_only(path: Path) -> None:
    try:
        mode = path.stat().st_mode
        path.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    except OSError:
        pass


def create_snapshot(root: Path, destination: Path) -> Path:
    """Copy the design into `destination`, read-only, with a writable scratch folder."""
    destination.mkdir(parents=True, exist_ok=True)
    for source in iter_design_files(root):
        relative = source.relative_to(root)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        _make_read_only(target)
    scratch = destination / SCRATCH_DIR
    scratch.mkdir(exist_ok=True)
    return destination


def snapshot_hashes(snapshot: Path) -> dict[str, str]:
    """Hash every file in the snapshot except the scratch folder."""
    return tree_hashes(snapshot)


def snapshot_changes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Return relative paths added, removed, or modified outside scratch."""
    changed = sorted(
        {
            key
            for key in set(before) | set(after)
            if before.get(key) != after.get(key) and not key.startswith(f"{SCRATCH_DIR}/")
        }
    )
    return changed


def remove_snapshot(snapshot: Path) -> None:
    def _onerror(_func: Any, path: str, _exc: Any) -> None:
        try:
            os.chmod(path, stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)  # noqa: S103
            if Path(path).is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.unlink(path)
        except OSError:
            pass

    for current, dirs, files in os.walk(snapshot):
        for name in dirs:
            with_mode = Path(current) / name
            try:
                with_mode.chmod(with_mode.stat().st_mode | stat.S_IWUSR)
            except OSError:
                pass
        for name in files:
            _make_writable(Path(current) / name)
    shutil.rmtree(snapshot, onerror=_onerror)  # noqa: B028


def _make_writable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | stat.S_IWUSR)
    except OSError:
        pass


# -------------------------------------------------------------------- index


@dataclass(slots=True)
class Part:
    ref: str
    value: str = ""
    footprint: str = ""
    lib: str = ""
    datasheet: str = ""
    description: str = ""
    sheet: str = "/"
    dnp: bool = False
    exclude_from_bom: bool = False
    fields: dict[str, str] = field(default_factory=dict)

    def field_any(self, *names: str) -> str:
        lowered = {key.lower(): value for key, value in self.fields.items()}
        for name in names:
            value = lowered.get(name.lower())
            if value:
                return value.strip()
        return ""

    @property
    def lcsc(self) -> str:
        return self.field_any("LCSC", "LCSC Part", "LCSC Part #", "LCSC#", "JLCPCB Part")

    @property
    def mpn(self) -> str:
        return self.field_any(
            "MPN", "MP", "Manufacturer Part Number", "Mfr. Part #", "Part Number", "PN"
        )

    @property
    def mechanical(self) -> bool:
        prefix = re.match(r"[A-Za-z]+", self.ref)
        return bool(prefix) and prefix.group(0).upper() in _MECHANICAL_PREFIXES

    def group_key(self) -> str:
        if self.lcsc:
            return f"lcsc:{self.lcsc.upper()}"
        if self.mpn:
            return f"mpn:{self.mpn.upper()}"
        return f"vf:{self.value.strip().lower()}|{self.footprint.strip().lower()}"


@dataclass(slots=True)
class Net:
    name: str
    net_class: str = "Default"
    nodes: list[tuple[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class DesignIndex:
    parts: list[Part] = field(default_factory=list)
    nets: dict[str, Net] = field(default_factory=dict)
    sheets: list[str] = field(default_factory=list)
    layers: list[str] = field(default_factory=list)
    copper_layers: list[str] = field(default_factory=list)
    netclasses: dict[str, dict[str, Any]] = field(default_factory=dict)
    rules: dict[str, Any] = field(default_factory=dict)

    @property
    def refs(self) -> set[str]:
        return {part.ref.upper() for part in self.parts}

    @property
    def net_names(self) -> set[str]:
        return {name.upper() for name in self.nets}

    @property
    def layer_names(self) -> set[str]:
        return {name.lower() for name in self.layers}

    @property
    def sheet_paths(self) -> set[str]:
        return {normalize_sheet(sheet) for sheet in self.sheets}

    def nets_in_class(self, name: str) -> list[str]:
        return sorted(net.name for net in self.nets.values() if net.net_class == name)

    def summary(self) -> dict[str, Any]:
        return {
            "parts": len(self.parts),
            "nets": len(self.nets),
            "sheets": len(self.sheets),
            "layers": len(self.layers),
            "copper_layers": len(self.copper_layers),
            "netclasses": sorted(self.netclasses),
        }


def normalize_sheet(value: str) -> str:
    text = value.strip()
    if not text:
        return "/"
    if not text.startswith("/"):
        text = "/" + text
    if not text.endswith("/"):
        text += "/"
    return text.lower()


def parse_netlist_xml(text: str) -> tuple[list[Part], dict[str, Net], list[str]]:
    """Parse `kicad-cli sch export netlist --format kicadxml` output."""
    root = ET.fromstring(text)  # noqa: S314 - local KiCad export
    parts: list[Part] = []
    for comp in root.iter("comp"):
        ref = comp.get("ref") or ""
        if not ref:
            continue
        fields: dict[str, str] = {}
        for item in comp.iter("field"):
            name = item.get("name")
            if name:
                fields[name] = (item.text or "").strip()
        properties = {item.get("name"): item.get("value") for item in comp.iter("property")}
        libsource = comp.find("libsource")
        sheetpath = comp.find("sheetpath")
        parts.append(
            Part(
                ref=ref,
                value=(comp.findtext("value") or "").strip(),
                footprint=(comp.findtext("footprint") or "").strip(),
                lib=(libsource.get("lib") or "") if libsource is not None else "",
                datasheet=(comp.findtext("datasheet") or fields.get("Datasheet") or "").strip(),
                description=(comp.findtext("description") or "").strip(),
                sheet=(sheetpath.get("names") or "/") if sheetpath is not None else "/",
                dnp="dnp" in properties,
                exclude_from_bom="exclude_from_bom" in properties,
                fields=fields,
            )
        )
    nets: dict[str, Net] = {}
    for item in root.iter("net"):
        name = item.get("name") or ""
        if not name:
            continue
        classes = (item.get("class") or "Default").split(",")
        net = Net(name=name, net_class=classes[0].strip() or "Default")
        for node in item.iter("node"):
            net.nodes.append((node.get("ref") or "", node.get("pin") or ""))
        nets[name] = net
    sheets: list[str] = []
    design = root.find("design")
    if design is not None:
        for sheet in design.iter("sheet"):
            name = sheet.get("name") or "/"
            if name not in sheets:
                sheets.append(name)
    if not sheets:
        sheets.append("/")
    return parts, nets, sheets


def parse_pcb_layers(text: str) -> tuple[list[str], list[str]]:
    """Return (all layer names, copper layer names) from a `.kicad_pcb` file."""
    match = _LAYER_BLOCK_RE.search(text)
    if match is None:
        return [], []
    layers: list[str] = []
    copper: list[str] = []
    for line in match.group(1).splitlines():
        found = _LAYER_LINE_RE.search(line)
        if found is None:
            continue
        name, kind = found.group(1), found.group(2)
        layers.append(name)
        if kind in {"signal", "power", "mixed", "jumper"} and name.endswith(".Cu"):
            copper.append(name)
    return layers, copper


def parse_project(text: str) -> tuple[dict[str, dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Return (net classes, board rules, net class assignment info) from `.kicad_pro`."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}, {}, {}
    settings = data.get("net_settings") or {}
    classes: dict[str, dict[str, Any]] = {}
    for item in settings.get("classes") or []:
        if isinstance(item, dict) and item.get("name"):
            classes[str(item["name"])] = item
    rules = ((data.get("board") or {}).get("design_settings") or {}).get("rules") or {}
    assignment = {
        "patterns": settings.get("netclass_patterns") or [],
        "assignments": settings.get("netclass_assignments") or {},
    }
    return classes, rules, assignment


def apply_netclass_assignment(nets: dict[str, Net], assignment: dict[str, Any]) -> None:
    """Resolve project-level net class patterns onto nets that only carry Default."""
    explicit = assignment.get("assignments") or {}
    patterns = assignment.get("patterns") or []
    for net in nets.values():
        if net.net_class != "Default":
            continue
        chosen = explicit.get(net.name) if isinstance(explicit, dict) else None
        if isinstance(chosen, str) and chosen:
            net.net_class = chosen
            continue
        for pattern in patterns:
            if not isinstance(pattern, dict):
                continue
            glob = str(pattern.get("pattern") or "")
            target = str(pattern.get("netclass") or "")
            if glob and target and fnmatch.fnmatchcase(net.name, glob):
                net.net_class = target
                break


def build_index(
    netlist_xml: str | None,
    project_text: str | None,
    pcb_text: str | None,
) -> DesignIndex:
    index = DesignIndex()
    if netlist_xml:
        index.parts, index.nets, index.sheets = parse_netlist_xml(netlist_xml)
    if project_text:
        index.netclasses, index.rules, assignment = parse_project(project_text)
        apply_netclass_assignment(index.nets, assignment)
    if pcb_text:
        index.layers, index.copper_layers = parse_pcb_layers(pcb_text)
    if not index.netclasses:
        index.netclasses = {"Default": {"name": "Default"}}
    return index


# ------------------------------------------------------------------ context


@dataclass(slots=True)
class DesignContext:
    """Everything a builtin or reviewer may read about the design under review."""

    workspace: Path
    context: WorkspaceContext
    run_dir: Path
    kicad: Any | None = None
    kicad_version: str | None = None
    index: DesignIndex | None = None
    intent: dict[str, str] = field(default_factory=dict)

    @property
    def project_path(self) -> Path | None:
        if self.context.project_files:
            return self.workspace / self.context.project_files[0]
        return None

    @property
    def schematic_path(self) -> Path | None:
        if self.context.schematic_path:
            return self.workspace / self.context.schematic_path
        return None

    @property
    def pcb_path(self) -> Path | None:
        if self.context.pcb_path:
            return self.workspace / self.context.pcb_path
        return None

    def artifacts_dir(self, check_id: str) -> Path:
        directory = self.run_dir / "checks" / check_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def schematic_files(self) -> list[Path]:
        return [path for path in iter_design_files(self.workspace) if path.suffix == ".kicad_sch"]


def load_intent(workspace: Path) -> dict[str, str]:
    """Read the project index, plans index and newest plan, when present."""
    result: dict[str, str] = {}
    for relative in INTENT_FILES:
        candidate = workspace / relative
        if candidate.is_file():
            result[relative.as_posix()] = candidate.read_text(encoding="utf-8", errors="replace")
    plans = workspace / ".copperpilot" / "plans"
    if plans.is_dir():
        newest = sorted(plans.glob("*.plan.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        if newest:
            result[newest[0].relative_to(workspace).as_posix()] = newest[0].read_text(
                encoding="utf-8", errors="replace"
            )
    return result
