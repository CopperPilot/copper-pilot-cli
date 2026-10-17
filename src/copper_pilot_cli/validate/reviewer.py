"""Hosted reviewer adapter: read-only broker, prompts, reply parsing, consensus."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from copper_pilot_cli.copper_auth import DeviceCredential
from copper_pilot_cli.copper_graph import RETRY_EVENT, create_copper_graph
from copper_pilot_cli.copper_protocol import CopperEffort, CopperMode, EventKind
from copper_pilot_cli.copper_tools import (
    ApprovalMode,
    ApprovalRequest,
    LocalToolBroker,
    ToolRejected,
    dangerous_shell_command,
)
from copper_pilot_cli.validate.config import SEVERITY_RANK, CheckSpec, Finding, ValidateConfig
from copper_pilot_cli.validate.design import SCRATCH_DIR, DesignContext, DesignIndex

MAX_REPAIR_TURNS = 2
REPAIR_MESSAGE = (
    "Your reply did not end with a valid fenced ```json block matching the output contract "
    "({error}). Reply with only the corrected json block: "
    '{{"findings": [...], "summary": "..."}}.'
)
QUESTION_REJECTION = (
    "Nobody can answer questions during a validate run. State the assumption you are making "
    "in your reply and continue the review."
)
OUTPUT_CONTRACT = """## Output contract

End your reply with exactly one fenced ```json block of this shape and nothing after it:

```json
{"findings": [
  {"severity": "P0|P1|P2|P3", "title": "one line", "detail": "what and why",
   "locator": {"sheet": "/power/", "refs": ["U3"], "nets": ["+3V3"], "layer": "F.Cu",
               "x_mm": 41.2, "y_mm": 18.0},
   "evidence": "datasheet section, report line, or calculation",
   "rule": "standard, skill rule or fab limit", "suggested_action": "the fix, not applied",
   "confidence": "high|medium|low"}
 ],
 "summary": "one or two sentences"}
```

Omit locator fields you cannot fill. Use an empty findings list when nothing is wrong.
Severity: P0 = will not work, unsafe, or the fab will rejects it; P1 = reliability, yield or
assembly risk; P2 = an expert reviewer would flag it; P3 = polish."""

_FENCE_RE = re.compile(r"```(?:json)?[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
_USAGE_CODES = {"http_402"}
_INVALID_ESCAPE_RE = re.compile(r'\\(?!["\\/bfnrtu])')


# ------------------------------------------------------------------ broker


def review_policy(snapshot: Path) -> Callable[[ApprovalRequest], Awaitable[bool]]:
    """Approve reads, scratch writes, and non-dangerous shell; reject the rest."""
    root = snapshot.resolve()
    scratch = (root / SCRATCH_DIR).resolve()

    def _within_scratch(raw: object) -> bool:
        if not isinstance(raw, str) or not raw:
            return False
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = root / candidate
        else:
            try:
                candidate.resolve(strict=False).relative_to(root)
            except ValueError:
                # Hosted tools use virtual paths rooted at the snapshot ("/board/x").
                candidate = root / candidate.relative_to(candidate.anchor)
        try:
            candidate.resolve(strict=False).relative_to(scratch)
        except ValueError:
            return False
        return True

    async def approve(request: ApprovalRequest) -> bool:
        name = request.canonical_name or request.tool_name
        if name in {"write_file", "edit_file", "write_binary_file"}:
            return _within_scratch(request.arguments.get("file_path"))
        if name == "execute":
            return not dangerous_shell_command(request.arguments.get("command"))
        return False

    return approve


async def reject_question(_question: Any) -> Any:
    raise ToolRejected(QUESTION_REJECTION)


def make_broker(snapshot: Path) -> LocalToolBroker:
    return LocalToolBroker(
        snapshot,
        mode=ApprovalMode.MANUAL,
        approve=review_policy(snapshot),
        ask_user=reject_question,
        always_allow_shell_commands=False,
        always_allow_dangerous_shell_commands=False,
    )


# ------------------------------------------------------------------- turns


@dataclass(slots=True)
class ReviewTurn:
    """One hosted message sent to one reviewer thread against the snapshot."""

    snapshot: Path
    prompt: str
    thread_id: str
    effort: CopperEffort
    broker: LocalToolBroker
    check_id: str
    on_event: Callable[[dict[str, Any]], None] | None = None


@dataclass(slots=True)
class TurnResult:
    text: str = ""
    error: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


TurnFn = Callable[[ReviewTurn], Awaitable[TurnResult]]


class HostedTurnRunner:
    """Default TurnFn: one checkpointed hosted Ask turn through the durability graph."""

    def __init__(
        self,
        credential: DeviceCredential,
        *,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.credential = credential
        self.client_factory = client_factory

    async def __call__(self, turn: ReviewTurn) -> TurnResult:
        from copper_pilot_cli.langchain import CopperPilotAgent

        agent = CopperPilotAgent(
            turn.snapshot,
            credential=self.credential,
            conversation_id=turn.thread_id,
            mode=CopperMode.ASK,
            effort=turn.effort,
            tool_broker=turn.broker,
        )
        if self.client_factory is not None:
            agent._client = self.client_factory()
        result = TurnResult()
        async with create_copper_graph(agent) as runtime:
            config = runtime.config(turn.thread_id, turn.snapshot)
            values = {
                "messages": [HumanMessage(content=turn.prompt)],
                "copper_mode": CopperMode.ASK.value,
                "copper_effort": turn.effort.value,
            }
            try:
                async for item in runtime.astream(values, config):
                    _namespace, stream_mode, data = item
                    if stream_mode != "custom" or not isinstance(data, dict):
                        continue
                    kind = str(data.get("type") or "").removeprefix("copper.")
                    payload = data.get("data")
                    if data.get("type") == RETRY_EVENT:
                        result.events.append(data)
                        if turn.on_event is not None:
                            turn.on_event(data)
                    elif kind == EventKind.ERROR.value:
                        result.error = (
                            dict(payload)
                            if isinstance(payload, dict)
                            else {"message": str(payload)}
                        )
                    elif kind == EventKind.FINAL.value and isinstance(payload, dict):
                        if payload.get("success") is False or payload.get("error"):
                            result.error = result.error or {
                                "message": str(payload.get("error") or "hosted turn failed")
                            }
            except BaseException:
                await runtime.acancel()
                raise
            state = await runtime.aget_state(config)
        messages = (getattr(state, "values", {}) or {}).get("messages", [])
        result.text = str(messages[-1].content) if messages else ""
        return result


def is_usage_limit(error: dict[str, Any] | None) -> bool:
    if not error:
        return False
    code = str(error.get("code") or "")
    status = error.get("status")
    return (
        code in _USAGE_CODES or status == 402 or (status == 429 and bool(error.get("block_reason")))
    )


# ------------------------------------------------------------------ prompts


def fan_out_items(check: CheckSpec, index: DesignIndex | None) -> list[str]:
    """Item lines for per_part, per_sheet and per_netclass checks."""
    if check.scope == "project" or index is None:
        return []
    if check.scope == "per_sheet":
        return [f"- sheet {sheet}" for sheet in index.sheets]
    if check.scope == "per_netclass":
        lines = []
        for name, settings in index.netclasses.items():
            nets = index.nets_in_class(name)
            geometry = ", ".join(
                f"{key}={settings[key]}"
                for key in (
                    "track_width",
                    "clearance",
                    "via_diameter",
                    "via_drill",
                    "diff_pair_width",
                    "diff_pair_gap",
                )
                if key in settings
            )
            lines.append(
                f"- net class {name} ({geometry or 'defaults'}): nets "
                f"{', '.join(nets[:40]) or 'none'}{' ...' if len(nets) > 40 else ''}"
            )
        return lines
    groups: dict[str, list[Any]] = {}
    for part in index.parts:
        if part.dnp:
            continue
        groups.setdefault(part.group_key(), []).append(part)
    lines = []
    for members in groups.values():
        first = members[0]
        refs = ", ".join(part.ref for part in members)
        lines.append(
            f"- {refs}: value={first.value or '?'} footprint={first.footprint or '?'} "
            f"LCSC={first.lcsc or '-'} MPN={first.mpn or '-'} "
            f"datasheet={first.datasheet or '-'} sheet={first.sheet}"
        )
    return lines


def batches(items: list[str], size: int) -> list[list[str]]:
    size = max(1, size)
    return [items[index : index + size] for index in range(0, len(items), size)]


def design_paths_block(design: DesignContext, snapshot: Path) -> str:
    lines = [
        "## Design paths (read-only snapshot of the workspace)",
        f"- workspace: {snapshot}",
        f"- writable scratch folder: {snapshot / SCRATCH_DIR}",
    ]
    if design.context.project_files:
        lines.append(f"- project: {design.context.project_files[0]}")
    if design.context.schematic_path:
        lines.append(f"- root schematic: {design.context.schematic_path}")
    if design.context.pcb_path:
        lines.append(f"- pcb: {design.context.pcb_path}")
    if design.index is not None:
        summary = design.index.summary()
        lines.append(
            f"- index: {summary['parts']} parts, {summary['nets']} nets, "
            f"{summary['sheets']} sheets, layers {', '.join(design.index.copper_layers)}"
        )
    if design.kicad is not None:
        lines.append(f"- kicad-cli: {design.kicad.executable}")
    return "\n".join(lines)


def build_prompt(
    config: ValidateConfig,
    check: CheckSpec,
    design: DesignContext,
    snapshot: Path,
    *,
    items: list[str] | None = None,
    reference_material: list[tuple[str, str]] | None = None,
) -> str:
    sections = [config.reviewer.preamble.strip()]
    if reference_material:
        blocks = [f"### {name}\n{text.strip()}" for name, text in reference_material]
        sections.append("## Reference material\n\n" + "\n\n".join(blocks))
    if design.intent:
        blocks = [f"### {name}\n{text.strip()[:12000]}" for name, text in design.intent.items()]
        sections.append("## Design intent\n\n" + "\n\n".join(blocks))
    sections.append(design_paths_block(design, snapshot))
    limits = config.fab_limits()
    if limits:
        sections.append(
            f"## Fab limits ({config.limits.get('fab')})\n"
            + "\n".join(f"- {key}: {value}" for key, value in limits.items())
        )
    sections.append(f"## Your single responsibility: {check.title or check.id}")
    body = (check.prompt or "").strip()
    if items is not None:
        body = body.replace("{items}", "\n".join(items))
    sections.append(body)
    sections.append(OUTPUT_CONTRACT)
    return "\n\n".join(section for section in sections if section)


# ------------------------------------------------------------------ parsing


def extract_json_block(text: str) -> str | None:
    """Return the body of the last fenced json block."""
    matches = _FENCE_RE.findall(text or "")
    return matches[-1].strip() if matches else None


def parse_reply(text: str) -> tuple[list[Finding], str]:
    block = extract_json_block(text)
    if block is None:
        stripped = (text or "").strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            block = stripped
        else:
            raise ValueError("no fenced json block")
    try:
        payload = json.loads(block)
    except json.JSONDecodeError as exc:
        # Models often markdown-escape characters inside strings ("\*", "\_");
        # drop escapes JSON does not define before asking for a repair turn.
        try:
            payload = json.loads(_INVALID_ESCAPE_RE.sub("", block))
        except json.JSONDecodeError:
            raise ValueError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("findings"), list):
        raise ValueError("expected an object with a findings list")
    findings: list[Finding] = []
    for position, item in enumerate(payload["findings"]):
        if not isinstance(item, dict):
            raise ValueError(f"findings[{position}] is not an object")
        try:
            findings.append(Finding.model_validate(item))
        except ValidationError as exc:
            first = exc.errors()[0]
            location = ".".join(str(part) for part in first.get("loc", ()))
            raise ValueError(f"findings[{position}].{location}: {first.get('msg')}") from exc
    return findings, str(payload.get("summary") or "")


@dataclass(slots=True)
class SliceResult:
    thread_id: str
    findings: list[Finding] = field(default_factory=list)
    summary: str = ""
    error: str | None = None
    usage_limit: dict[str, Any] | None = None
    prompts: list[str] = field(default_factory=list)
    replies: list[str] = field(default_factory=list)


async def review_slice(
    turn_fn: TurnFn,
    turn: ReviewTurn,
    *,
    max_repairs: int = MAX_REPAIR_TURNS,
) -> SliceResult:
    """Send the prompt, then up to `max_repairs` repair turns in the same thread."""
    result = SliceResult(thread_id=turn.thread_id)
    prompt = turn.prompt
    last_error = "empty reply"
    for attempt in range(max_repairs + 1):
        turn.prompt = prompt
        result.prompts.append(prompt)
        outcome = await turn_fn(turn)
        result.replies.append(outcome.text)
        if outcome.error is not None:
            result.error = str(outcome.error.get("message") or "hosted turn failed")
            if is_usage_limit(outcome.error):
                result.usage_limit = outcome.error
            return result
        try:
            result.findings, result.summary = parse_reply(outcome.text)
            result.error = None
            return result
        except ValueError as exc:
            last_error = str(exc)
            if attempt < max_repairs:
                prompt = REPAIR_MESSAGE.format(error=last_error)
    result.error = f"reviewer reply was not valid after {max_repairs} repair turns: {last_error}"
    return result


# ---------------------------------------------------------------- consensus


def consensus(runs: list[list[Finding]]) -> list[Finding]:
    """Keep findings reported by a majority of runs, matched by locator key."""
    if len(runs) <= 1:
        return list(runs[0]) if runs else []
    needed = len(runs) // 2 + 1
    votes: dict[str, list[Finding]] = {}
    order: list[str] = []
    for run in runs:
        seen: set[str] = set()
        for finding in run:
            key = finding.locator_key()
            if key in seen:
                continue
            seen.add(key)
            if key not in votes:
                order.append(key)
            votes.setdefault(key, []).append(finding)
    kept: list[Finding] = []
    for key in order:
        group = votes[key]
        if len(group) >= needed:
            worst = min(group, key=lambda item: SEVERITY_RANK[item.severity])
            kept.append(worst)
    return kept


# ---------------------------------------------------------------- grounding


def ground_findings(findings: list[Finding], index: DesignIndex | None) -> list[Finding]:
    """Mark findings whose refs, nets, layer or sheet are not in the design."""
    for finding in findings:
        problems: list[str] = []
        loc = finding.locator
        if index is None:
            if loc.refs or loc.nets or loc.layer:
                problems.append("design index unavailable")
        else:
            missing_refs = [ref for ref in loc.refs if ref.upper() not in index.refs]
            missing_nets = [net for net in loc.nets if net.upper() not in index.net_names]
            if missing_refs:
                problems.append(f"unknown refs {', '.join(missing_refs)}")
            if missing_nets:
                problems.append(f"unknown nets {', '.join(missing_nets)}")
            if loc.layer and index.layers and loc.layer.lower() not in index.layer_names:
                problems.append(f"unknown layer {loc.layer}")
        if problems:
            finding.grounded = False
            prefix = f"[UNGROUNDED: {'; '.join(problems)}] "
            if not finding.title.startswith("[UNGROUNDED"):
                finding.title = prefix + finding.title
        else:
            finding.grounded = True
    return findings
