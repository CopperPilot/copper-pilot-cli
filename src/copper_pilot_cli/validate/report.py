"""Run outputs: report.json, REPORT.md, next_actions.json."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from copper_pilot_cli.validate.config import (
    REPORT_SCHEMA,
    SEVERITY_RANK,
    CheckResult,
    Finding,
    Status,
    ValidateConfig,
)

MAX_NEXT_ACTIONS = 15


def verdict_for(results: list[CheckResult]) -> str:
    statuses = {result.status for result in results}
    blocking_bad = any(
        result.gate == "blocking" and result.status in {Status.FAIL, Status.ERROR}
        for result in results
    )
    if blocking_bad:
        return "FAIL"
    if Status.SKIPPED in statuses:
        return "INCOMPLETE"
    return "PASS"


def next_actions(results: list[CheckResult], config: ValidateConfig) -> dict[str, Any]:
    """Only the earliest failing stage's grounded, blocking findings, worst first."""
    stage_order = config.stage_index()
    failing = [
        result
        for result in results
        if result.gate == "blocking" and result.status in {Status.FAIL, Status.ERROR}
    ]
    if not failing:
        return {
            "stage": None,
            "stage_title": None,
            "actions": [],
            "more": 0,
            "checks_blocked_downstream": [],
            "rerun": "copper-pilot validate",
        }
    earliest = min(failing, key=lambda result: stage_order.get(result.stage, 1 << 30))
    stage = earliest.stage
    in_stage = [result for result in failing if result.stage == stage]
    actions: list[dict[str, Any]] = []
    for result in in_stage:
        if result.status is Status.ERROR and not result.findings:
            actions.append(
                {
                    "id": f"{result.check_id}/error",
                    "check": result.check_id,
                    "severity": "P0",
                    "title": f"{result.check_id} could not run",
                    "locator": {},
                    "evidence": result.error or "",
                    "rule": "every check must produce a trustworthy answer",
                    "action": "Fix the environment or input the error names, then re-run.",
                }
            )
            continue
        for finding in result.findings:
            if not finding.grounded:
                continue
            actions.append(_action(result.check_id, finding))
    actions.sort(key=lambda item: SEVERITY_RANK[item["severity"]])
    blocked = sorted(
        result.check_id
        for result in results
        if result.status is Status.SKIPPED
        and any(
            (blocker.split(" ")[0] in {item.check_id for item in in_stage})
            or f"({earliest.check_id})" in blocker
            or blocker.startswith("stage gate")
            for blocker in result.blocked_by
        )
    )
    stage_title = ""
    try:
        stage_title = config.stage(stage).title
    except KeyError:
        pass
    return {
        "stage": stage,
        "stage_title": stage_title,
        "actions": actions[:MAX_NEXT_ACTIONS],
        "more": max(0, len(actions) - MAX_NEXT_ACTIONS),
        "checks_blocked_downstream": blocked,
        "rerun": f"copper-pilot validate --until {stage}",
    }


def _action(check_id: str, finding: Finding) -> dict[str, Any]:
    return {
        "id": finding.id,
        "check": check_id,
        "severity": finding.severity.value,
        "title": finding.title,
        "locator": finding.locator.model_dump(exclude_none=True, exclude_defaults=True),
        "evidence": finding.evidence,
        "rule": finding.rule,
        "action": finding.suggested_action,
    }


# ---------------------------------------------------------------- renderers


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Validate report: {report['verdict']}",
        "",
        f"- profile: {report['profile']}  gate: {report['gate']}",
        f"- design: {report['design_hash'][:12]}  kicad-cli: {report.get('kicad_version') or '-'}"
        f"  cli: {report['cli_version']}",
        f"- started: {report['started']}  finished: {report['finished']}",
    ]
    if report.get("interrupted"):
        lines.append("- interrupted: yes")
    lines.extend(
        [
            "",
            "| Check | Stage | Status | P0 | P1 | P2 | P3 | Notes |",
            "|---|---|---|---|---|---|---|---|",
        ]
    )
    for raw in report["results"]:
        result = CheckResult.model_validate(raw)
        counts = result.counts()
        note = result.error or result.summary or ""
        if result.blocked_by:
            note = "blocked by " + ", ".join(result.blocked_by)
        if result.cached:
            note = f"(cached) {note}"
        lines.append(
            f"| {result.check_id} | {result.stage} | {result.status.value} | "
            f"{counts['P0']} | {counts['P1']} | {counts['P2']} | {counts['P3']} | "
            f"{note.replace('|', '/')} |"
        )
    for raw in report["results"]:
        result = CheckResult.model_validate(raw)
        if not result.findings:
            continue
        lines.extend(["", f"## {result.check_id}: {result.title}", ""])
        if result.threads:
            lines.append(f"Threads: {', '.join(result.threads)}")
            lines.append("")
        for finding in result.findings:
            loc = finding.locator
            where = ", ".join(
                part
                for part in (
                    f"sheet {loc.sheet}" if loc.sheet else "",
                    f"refs {', '.join(loc.refs)}" if loc.refs else "",
                    f"nets {', '.join(loc.nets)}" if loc.nets else "",
                    f"layer {loc.layer}" if loc.layer else "",
                    f"at ({loc.x_mm}, {loc.y_mm}) mm" if loc.x_mm is not None else "",
                )
                if part
            )
            lines.append(
                f"### {finding.id} {finding.severity.value} {finding.title} "
                f"({finding.confidence.value} confidence)"
            )
            if where:
                lines.append(f"- Where: {where}")
            if finding.detail:
                lines.append(f"- Why: {finding.detail}")
            if finding.evidence:
                lines.append(f"- Evidence: {finding.evidence}")
            if finding.rule:
                lines.append(f"- Rule: {finding.rule}")
            if finding.suggested_action:
                lines.append(f"- Action: {finding.suggested_action}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ------------------------------------------------------------------ writing


def build_report(
    *,
    verdict: str,
    profile: str,
    gate: str,
    design_hash: str,
    cli_version: str,
    kicad_version: str | None,
    started: str,
    finished: str,
    results: list[CheckResult],
    run_id: str,
    interrupted: bool = False,
    exit_code: int = 0,
) -> dict[str, Any]:
    return {
        "schema": REPORT_SCHEMA,
        "run_id": run_id,
        "verdict": verdict,
        "profile": profile,
        "gate": gate,
        "design_hash": design_hash,
        "cli_version": cli_version,
        "kicad_version": kicad_version,
        "started": started,
        "finished": finished,
        "interrupted": interrupted,
        "exit_code": exit_code,
        "results": [result.model_dump(mode="json") for result in results],
    }


def write_outputs(
    run_dir: Path,
    report: dict[str, Any],
    actions: dict[str, Any],
) -> dict[str, Path]:
    run_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "report": run_dir / "report.json",
        "markdown": run_dir / "REPORT.md",
        "next_actions": run_dir / "next_actions.json",
    }
    paths["report"].write_text(json.dumps(report, indent=2), encoding="utf-8")
    paths["markdown"].write_text(render_markdown(report), encoding="utf-8")
    paths["next_actions"].write_text(json.dumps(actions, indent=2), encoding="utf-8")
    _point_latest(run_dir)
    return paths


def _point_latest(run_dir: Path) -> None:
    latest = run_dir.parent.parent / "latest"
    try:
        if latest.is_symlink() or latest.is_file():
            latest.unlink()
        elif latest.is_dir():
            return
        os.symlink(run_dir.name, latest, target_is_directory=True)
        # Validate the link resolves; runs/ is a sibling so point through it.
        if not latest.exists():
            latest.unlink()
            os.symlink(Path("runs") / run_dir.name, latest, target_is_directory=True)
    except (OSError, NotImplementedError):
        try:
            latest.write_text(str(run_dir), encoding="utf-8")
        except OSError:
            pass
