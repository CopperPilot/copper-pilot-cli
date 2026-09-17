"""Folder workspace discovery and recent-project persistence."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from copper_pilot_cli.copper_config import paths

IGNORED_NAMES = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
}


@dataclass(frozen=True, slots=True)
class WorkspaceContext:
    root: Path
    schematic_path: str | None
    pcb_path: str | None
    project_files: tuple[str, ...]


def canonical_workspace(value: str | Path | None = None) -> Path:
    """Resolve an existing directory, defaulting to the current directory."""
    candidate = Path(value or Path.cwd()).expanduser().resolve()
    if not candidate.exists():
        raise FileNotFoundError(candidate)
    if not candidate.is_dir():
        raise NotADirectoryError(candidate)
    return candidate


def _walk(root: Path, max_depth: int = 8, max_files: int = 2_000) -> list[Path]:
    found: list[Path] = []
    for current, dirs, files in os.walk(root):
        relative = Path(current).relative_to(root)
        if len(relative.parts) >= max_depth:
            dirs[:] = []
        dirs[:] = sorted(name for name in dirs if name not in IGNORED_NAMES)
        for name in sorted(files):
            found.append(Path(current) / name)
            if len(found) >= max_files:
                return found
    return found


def _relative(root: Path, value: Path | None) -> str | None:
    return value.relative_to(root).as_posix() if value else None


def discover_workspace(root: Path) -> WorkspaceContext:
    """Discover KiCad/Altium artifacts without requiring them."""
    files = _walk(root)
    projects = sorted(
        (path for path in files if path.suffix.lower() in {".kicad_pro", ".prjpcb"}),
        key=lambda item: (len(item.relative_to(root).parts), item.as_posix().lower()),
    )
    schematics = sorted(
        (path for path in files if path.suffix.lower() in {".kicad_sch", ".schdoc"}),
        key=lambda item: item.as_posix().lower(),
    )
    pcbs = sorted(
        (path for path in files if path.suffix.lower() in {".kicad_pcb", ".pcbdoc"}),
        key=lambda item: item.as_posix().lower(),
    )
    preferred_schematic: Path | None = None
    preferred_pcb: Path | None = None
    if projects:
        project = projects[0]
        matching_schematic = project.with_suffix(".kicad_sch")
        matching_pcb = project.with_suffix(".kicad_pcb")
        preferred_schematic = (
            matching_schematic
            if matching_schematic in schematics
            else next(
                (item for item in schematics if item.parent == project.parent),
                schematics[0] if schematics else None,
            )
        )
        preferred_pcb = (
            matching_pcb
            if matching_pcb in pcbs
            else next(
                (item for item in pcbs if item.parent == project.parent),
                pcbs[0] if pcbs else None,
            )
        )
    else:
        preferred_schematic = schematics[0] if schematics else None
        preferred_pcb = pcbs[0] if pcbs else None
    return WorkspaceContext(
        root=root,
        schematic_path=_relative(root, preferred_schematic),
        pcb_path=_relative(root, preferred_pcb),
        project_files=tuple(_relative(root, item) or "" for item in projects),
    )


def build_initial_file_tree(root: Path, limit: int = 2_000) -> str:
    """Build a bounded relative file list for the hosted agent."""
    paths = [
        path.relative_to(root).as_posix()
        for path in _walk(root, max_files=limit)
        if ".copperpilot" not in path.relative_to(root).parts
    ]
    return "\n".join(paths)


def remember_workspace(context: WorkspaceContext, limit: int = 20) -> None:
    """Persist most-recently-used folders atomically."""
    destination = paths().recent_projects
    destination.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, object]] = []
    try:
        loaded = json.loads(destination.read_text())
        if isinstance(loaded, list):
            existing = [row for row in loaded if isinstance(row, dict)]
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        pass
    root = str(context.root)
    rows = [{"root": root, **asdict(context)}]
    rows.extend(row for row in existing if row.get("root") != root)
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(rows[:limit], default=str, indent=2))
    temporary.replace(destination)


def recent_workspaces() -> list[Path]:
    """Return existing recent workspace folders."""
    try:
        rows = json.loads(paths().recent_projects.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    result: list[Path] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        candidate = Path(str(row.get("root") or "")).expanduser()
        if candidate.is_dir():
            result.append(candidate.resolve())
    return result
