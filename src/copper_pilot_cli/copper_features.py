"""CopperPilot project skills, plans, attachments, and KiCad paste handling."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

from copper_pilot_cli.copper_auth import DeviceCredential
from copper_pilot_cli.copper_protocol import PlanContext, PlanTodo
from copper_pilot_cli.copper_widgets import CompletionEntry

MAX_LOCAL_SKILLS = 32
MAX_SKILL_BYTES = 64 * 1024
MAX_INSTRUCTIONS = 8_000
MAX_DESCRIPTION = 500
MAX_METADATA = 300
MAX_SNIPPET_CHARS = 2_000_000
SKILL_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
FRONTMATTER = re.compile(r"^---\r?\n([\s\S]*?)\r?\n---\r?\n?")
KICAD_PREFIXES = (
    "(lib_symbols",
    "(kicad_pcb",
    "(footprint",
    "(symbol",
    "(text",
    "(version",
    "(generator",
    "(wire",
    "(junction",
    "(global_label",
    "(net ",
    "(net\t",
    "(net\n",
)


@dataclass(frozen=True, slots=True)
class LocalSkill:
    id: str
    label: str
    description: str
    required_inputs: str
    artifact_outputs: str
    instructions: str
    path: str

    def manifest_entry(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "slash": f"/{self.id}",
            "category": "local",
            "description": self.description,
            "required_inputs": self.required_inputs,
            "artifact_outputs": self.artifact_outputs,
            "instructions": self.instructions,
            "source": {
                "type": "local",
                "path": self.path,
                "folder": str(Path(self.path).parent),
            },
        }

    def command_entry(self) -> CompletionEntry:
        return CompletionEntry(
            name=f"/{self.id}",
            description=f"Skill · {self.description}",
            display_name=f"/{self.id}  [skill]",
        )


def _paragraph(body: str) -> str:
    objective = re.search(
        r"^##\s+Objective\s*\n+([\s\S]*?)(?:\n##\s|\n#+\s|$)",
        body,
        re.IGNORECASE | re.MULTILINE,
    )
    value = objective.group(1) if objective else body
    paragraphs = [
        re.sub(r"\s+", " ", chunk).strip()
        for chunk in re.split(r"\n\s*\n", value)
        if chunk.strip() and not chunk.lstrip().startswith("#")
    ]
    return (paragraphs[0] if paragraphs else "Local CopperPilot skill")[:MAX_DESCRIPTION]


def scan_local_skills(workspace: Path) -> tuple[list[LocalSkill], list[str]]:
    """Read bounded `.copperpilot/skills/<id>/SKILL.md` entries."""
    root = workspace / ".copperpilot" / "skills"
    skills: list[LocalSkill] = []
    diagnostics: list[str] = []
    if not root.is_dir():
        return skills, diagnostics
    for folder in sorted(root.iterdir())[:MAX_LOCAL_SKILLS]:
        identifier = folder.name.lower()
        file = folder / "SKILL.md"
        if not folder.is_dir() or not SKILL_ID.fullmatch(identifier) or not file.is_file():
            diagnostics.append(f"Ignored invalid local skill folder: {folder.name}")
            continue
        if file.stat().st_size > MAX_SKILL_BYTES:
            diagnostics.append(f"Ignored oversized local skill: {identifier}")
            continue
        text = file.read_text(encoding="utf-8")
        match = FRONTMATTER.match(text)
        if not match:
            diagnostics.append(f"Missing YAML frontmatter: {identifier}")
            continue
        try:
            metadata = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            diagnostics.append(f"Invalid YAML frontmatter: {identifier}")
            continue
        if not isinstance(metadata, dict):
            diagnostics.append(f"Invalid skill metadata: {identifier}")
            continue
        required = str(metadata.get("required_inputs") or "").strip()[:MAX_METADATA]
        outputs = str(metadata.get("artifact_outputs") or "").strip()[:MAX_METADATA]
        body = text[match.end() :].strip()
        if not required or not outputs or not body:
            diagnostics.append(f"Incomplete local skill: {identifier}")
            continue
        heading = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        label = (
            re.sub(r"^skill:\s*", "", heading.group(1), flags=re.IGNORECASE).strip()
            if heading
            else identifier.replace("-", " ").title()
        )
        skills.append(
            LocalSkill(
                id=identifier,
                label=label,
                description=_paragraph(body),
                required_inputs=required,
                artifact_outputs=outputs,
                instructions=(
                    body + "\n\n## Local skill helper scripts\nHelper scripts must remain inside "
                    "this skill folder and use the normal file and shell tools."
                )[:MAX_INSTRUCTIONS],
                path=file.relative_to(workspace).as_posix(),
            )
        )
    return skills, diagnostics


async def resolve_skill_manifest(
    credential: DeviceCredential,
    skills: list[LocalSkill],
    *,
    cache_key: str | None = None,
) -> dict[str, Any]:
    """Merge local skill metadata through the hosted manifest endpoint."""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{credential.base_url}/api/copperpilot/skills/manifest",
            headers={
                "Authorization": f"Bearer {credential.api_key}",
                "X-CopperPilot-Fingerprint": credential.fingerprint,
            },
            json={
                "local_manifest": {
                    "version": 1,
                    "skills": [skill.manifest_entry() for skill in skills],
                },
                "client_cache_key": cache_key,
            },
        )
        response.raise_for_status()
        return response.json()


def parse_plan(path: Path, workspace: Path) -> PlanContext:
    """Parse a `.copperpilot/plans/*.plan.md` artifact."""
    resolved = path.resolve()
    plans = (workspace / ".copperpilot" / "plans").resolve()
    if plans not in resolved.parents or not resolved.name.endswith(".plan.md"):
        raise ValueError("Plan must be a .copperpilot/plans/*.plan.md file.")
    text = resolved.read_text(encoding="utf-8")
    match = FRONTMATTER.match(text)
    metadata: dict[str, Any] = {}
    if match:
        loaded = yaml.safe_load(match.group(1)) or {}
        metadata = loaded if isinstance(loaded, dict) else {}
    body = text[match.end() :].strip() if match else text.strip()
    todos: list[PlanTodo] = []
    for row in metadata.get("todos") or []:
        if isinstance(row, dict) and row.get("id") and row.get("content"):
            status = str(row.get("status") or "open").lower()
            todos.append(
                PlanTodo(
                    id=str(row["id"]),
                    content=str(row["content"]),
                    status=status if status in {"open", "running", "done"} else "open",
                )
            )
    overall = str(metadata.get("overall_status") or "open").lower()
    return PlanContext(
        file_path=resolved.relative_to(workspace).as_posix(),
        name=str(metadata.get("name") or resolved.name.removesuffix(".plan.md")),
        overview=str(metadata.get("overview") or ""),
        overall_status=overall if overall in {"open", "running", "done"} else "open",
        todos=todos,
        body=body,
    )


def looks_like_kicad_snippet(text: str) -> bool:
    """Validate a bounded KiCad S-expression in one pass."""
    if not text or len(text) > MAX_SNIPPET_CHARS:
        return False
    stripped = text.strip()
    if not stripped.startswith(KICAD_PREFIXES):
        return False
    depth = 0
    quoted = False
    escaped = False
    for char in stripped:
        if escaped:
            escaped = False
        elif char == "\\" and quoted:
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif not quoted and char == "(":
            depth += 1
        elif not quoted and char == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not quoted


def path_first_attachment(path: Path, workspace: Path) -> dict[str, Any]:
    """Describe a workspace attachment without embedding file bytes."""
    resolved = path.expanduser().resolve()
    root = workspace.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("Attachment must be inside the workspace.")
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    return {
        "type": "file",
        "path": resolved.relative_to(root).as_posix(),
        "name": resolved.name,
        "size": resolved.stat().st_size,
    }


def encode_runtime_context(
    *,
    attachments: list[dict[str, Any]] | None = None,
    snippet: str | None = None,
) -> dict[str, Any] | None:
    payload: dict[str, Any] = {}
    if attachments:
        payload["attachments"] = attachments
    if snippet and looks_like_kicad_snippet(snippet):
        payload["kicad_snippet"] = {"format": "sexpr", "content": snippet}
    return payload or None
