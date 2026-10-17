"""Stage-gated execution: waves, gating, fan-out, grounding, status, cache."""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from copper_pilot_cli._version import __version__
from copper_pilot_cli.copper_auth import AuthenticationError, DeviceCredential, load_credential
from copper_pilot_cli.copper_protocol import CopperEffort
from copper_pilot_cli.copper_workspace import discover_workspace
from copper_pilot_cli.validate.builtins import BUILTINS, CheckOutcome
from copper_pilot_cli.validate.config import (
    CONFIDENCE_RANK,
    SEVERITY_RANK,
    CheckResult,
    CheckSpec,
    ConfigError,
    Finding,
    GateMode,
    ProfileSpec,
    Status,
    ValidateConfig,
    hard_prerequisites,
)
from copper_pilot_cli.validate.design import (
    DesignContext,
    create_snapshot,
    design_hash,
    input_hashes,
    remove_snapshot,
    snapshot_changes,
    snapshot_hashes,
)
from copper_pilot_cli.validate.kicad import KicadCli, find_kicad_cli
from copper_pilot_cli.validate.report import build_report, next_actions, verdict_for, write_outputs
from copper_pilot_cli.validate.reviewer import (
    HostedTurnRunner,
    ReviewTurn,
    SliceResult,
    TurnFn,
    batches,
    build_prompt,
    consensus,
    fan_out_items,
    ground_findings,
    make_broker,
    review_slice,
)

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_INCOMPLETE = 3
EXIT_ENVIRONMENT = 4
EXIT_INTERRUPTED = 130
LOGIN_HINT = "Run `copper-pilot auth login`"

EventFn = Callable[[dict[str, Any]], None]
ProgressFn = Callable[[str], None]


class EnvironmentError_(RuntimeError):
    """Not logged in or another environment failure; exit code 4."""


@dataclass(slots=True)
class RunOptions:
    profile: str | None = None
    gate: GateMode = GateMode.STAGE
    until: str | None = None
    only: list[str] = field(default_factory=list)
    skip: list[str] = field(default_factory=list)
    effort: str | None = None
    concurrency: int = 3
    no_cache: bool = False
    repeat: int | None = None
    out_dir: Path | None = None
    on_event: EventFn | None = None
    on_progress: ProgressFn | None = None


@dataclass(slots=True)
class RunOutcome:
    verdict: str
    exit_code: int
    run_dir: Path
    report: dict[str, Any]
    next_actions: dict[str, Any]
    paths: dict[str, Path]
    results: list[CheckResult]
    interrupted: bool = False


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def compute_status(
    check: CheckSpec,
    findings: list[Finding],
    profile: ProfileSpec,
    *,
    error: str | None = None,
) -> Status:
    if error:
        return Status.ERROR
    if not findings:
        return Status.PASS
    if check.gate == "blocking":
        threshold = CONFIDENCE_RANK[profile.min_confidence_to_fail]
        for finding in findings:
            if (
                finding.grounded
                and finding.severity in profile.fail_on
                and CONFIDENCE_RANK[finding.confidence] >= threshold
            ):
                return Status.FAIL
    return Status.WARN


def stage_waves(checks: list[CheckSpec]) -> list[list[CheckSpec]]:
    """Dependency waves inside one stage (Kahn's algorithm over requires + after)."""
    ids = {check.id for check in checks}
    remaining = {check.id: check for check in checks}
    done: set[str] = set()
    waves: list[list[CheckSpec]] = []
    while remaining:
        ready = [
            check
            for check in remaining.values()
            if all(dep in done or dep not in ids for dep in (*check.requires, *check.after))
        ]
        if not ready:
            raise ConfigError(f"dependency cycle among {', '.join(sorted(remaining))}")
        ready.sort(key=lambda check: (check.kind != "builtin", check.id))
        waves.append(ready)
        for check in ready:
            done.add(check.id)
            del remaining[check.id]
    return waves


class Runner:
    """One validate run over one workspace."""

    def __init__(
        self,
        workspace: Path,
        config: ValidateConfig,
        options: RunOptions,
        *,
        turn: TurnFn | None = None,
        kicad_finder: Callable[[], KicadCli | None] = find_kicad_cli,
        credential_loader: Callable[[], DeviceCredential | None] = load_credential,
        snapshot_root: Path | None = None,
    ) -> None:
        self.workspace = workspace
        self.config = config
        self.options = options
        self.turn = turn
        self.kicad_finder = kicad_finder
        self.credential_loader = credential_loader
        self.snapshot_root = snapshot_root
        self.profile_name, self.profile = config.profile(options.profile)
        self.results: dict[str, CheckResult] = {}
        self.out_dir = options.out_dir or workspace / ".copperpilot" / "validate"
        self.cache_dir = self.out_dir / "cache"
        self.run_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        self.run_dir = self.out_dir / "runs" / self.run_id
        self.snapshot: Path | None = None
        self._snapshot_tmp: tempfile.TemporaryDirectory[str] | None = None
        self.semaphore = asyncio.Semaphore(max(1, options.concurrency))
        self.usage_limit: dict[str, Any] | None = None
        self.halt: str | None = None
        self.interrupted = False
        self.design: DesignContext | None = None
        self.started = _now()
        self.last_outcome: RunOutcome | None = None

    # --------------------------------------------------------------- events

    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        if self.options.on_event is not None:
            self.options.on_event({"type": kind, "data": data})

    def _progress(self, line: str) -> None:
        if self.options.on_progress is not None:
            self.options.on_progress(line)

    # ------------------------------------------------------------ selection

    def selected_checks(self) -> tuple[list[CheckSpec], dict[str, str]]:
        """Return the checks to run, and the reason each unselected check is skipped."""
        config = self.config
        enabled = config.enabled_checks()
        ids = {check.id for check in enabled}
        stage_order = config.stage_index()
        for check_id in (*self.options.only, *self.options.skip):
            if check_id not in {check.id for check in config.checks}:
                raise ConfigError(f"unknown check {check_id!r}")
        if self.options.until is not None and self.options.until not in stage_order:
            raise ConfigError(
                f"unknown stage {self.options.until!r}; stages: {', '.join(stage_order)}"
            )
        skipped: dict[str, str] = {}
        chosen = set(ids)
        if self.options.only:
            chosen = hard_prerequisites(config, set(self.options.only)) & ids
            for check_id in ids - chosen:
                skipped[check_id] = "--only"
        if self.options.until is not None:
            limit = stage_order[self.options.until]
            for check in enabled:
                if check.id in chosen and stage_order[check.stage] > limit:
                    chosen.discard(check.id)
                    skipped[check.id] = f"--until {self.options.until}"
        for check_id in self.options.skip:
            if check_id in chosen:
                chosen.discard(check_id)
                skipped[check_id] = "--skip"
        selected = [check for check in enabled if check.id in chosen]
        return selected, skipped

    # ----------------------------------------------------------------- run

    async def run(self) -> RunOutcome:
        selected, skipped = self.selected_checks()
        self._emit(
            "validate.start",
            {
                "run_id": self.run_id,
                "checks": [check.id for check in selected],
                "stages": [stage.id for stage in self.config.stages],
                "profile": self.profile_name,
                "gate": self.options.gate.value,
            },
        )
        for check in self.config.checks:
            if check.id in skipped:
                self._record_skip(check, [skipped[check.id]])
        self.run_dir.mkdir(parents=True, exist_ok=True)
        workspace_context = discover_workspace(self.workspace)
        self.design = DesignContext(
            workspace=self.workspace,
            context=workspace_context,
            run_dir=self.run_dir,
            kicad=self.kicad_finder(),
        )
        needs_agent = any(check.kind == "agent" for check in selected)
        if needs_agent:
            self._prepare_hosted()
        try:
            await self._run_stages(selected)
        except asyncio.CancelledError:
            self.interrupted = True
            for check in selected:
                if check.id not in self.results:
                    self._record_skip(check, ["interrupted"])
            outcome = self._finish()
            raise _Interrupted(outcome) from None
        finally:
            self._cleanup_snapshot()
        return self._finish()

    def _prepare_hosted(self) -> None:
        try:
            credential = self.credential_loader()
        except AuthenticationError:
            credential = None
        if self.turn is None:
            if credential is None:
                raise EnvironmentError_(f"Not logged in. {LOGIN_HINT}.")
            self.turn = HostedTurnRunner(credential)
        parent = self.snapshot_root
        if parent is not None:
            parent.mkdir(parents=True, exist_ok=True)
        self._snapshot_tmp = tempfile.TemporaryDirectory(
            prefix="copper-pilot-validate-", dir=str(parent) if parent else None
        )
        self.snapshot = create_snapshot(self.workspace, Path(self._snapshot_tmp.name) / "design")
        self._progress(f"snapshot: {self.snapshot}")

    def _cleanup_snapshot(self) -> None:
        if self.snapshot is not None:
            remove_snapshot(self.snapshot)
            self.snapshot = None
        if self._snapshot_tmp is not None:
            self._snapshot_tmp.cleanup()
            self._snapshot_tmp = None

    async def _run_stages(self, selected: list[CheckSpec]) -> None:
        by_stage: dict[str, list[CheckSpec]] = {}
        for check in selected:
            by_stage.setdefault(check.stage, []).append(check)
        for stage in self.config.stages:
            checks = by_stage.get(stage.id, [])
            if not checks:
                continue
            if self.halt is not None:
                for check in checks:
                    self._record_skip(check, self._failed_prerequisites(check) or [self.halt])
                continue
            for wave in stage_waves(checks):
                if self.halt is not None:
                    for check in wave:
                        self._record_skip(check, self._failed_prerequisites(check) or [self.halt])
                    continue
                await self._run_wave(wave)
                if self.options.gate is GateMode.STRICT:
                    failed = self._blocking_failures(wave)
                    if failed:
                        self.halt = f"wave gate: {stage.id} ({failed[0]})"
            if self.options.gate is GateMode.STAGE and self.halt is None:
                failed = self._blocking_failures(checks)
                if failed:
                    self.halt = f"stage gate: {stage.id} ({failed[0]})"

    def _blocking_failures(self, checks: list[CheckSpec]) -> list[str]:
        failed = []
        for check in checks:
            result = self.results.get(check.id)
            if result is None or check.gate != "blocking":
                continue
            if result.status is Status.FAIL or (
                result.status is Status.ERROR and self.config.policy.error_blocks_dependents
            ):
                failed.append(check.id)
        return failed

    async def _run_wave(self, wave: list[CheckSpec]) -> None:
        agents: list[CheckSpec] = []
        for check in wave:
            blockers = self._blockers(check)
            if blockers:
                self._record_skip(check, blockers)
                continue
            if check.kind == "builtin":
                await self._run_check(check)
            elif self.usage_limit is not None:
                self._record_skip(check, [self._usage_label()])
            else:
                agents.append(check)
        if agents:
            await asyncio.gather(*(self._run_check(check) for check in agents))

    def _usage_label(self) -> str:
        reason = (self.usage_limit or {}).get("block_reason") or "blocked"
        return f"usage limit: {reason}"

    def _failed_prerequisites(self, check: CheckSpec) -> list[str]:
        """Prerequisites that failed outright; a gate label is used otherwise."""
        return [item for item in self._blockers(check) if not item.endswith("(skipped)")]

    def _blockers(self, check: CheckSpec) -> list[str]:
        blockers: list[str] = []
        for dep in check.requires:
            result = self.results.get(dep)
            if result is None:
                if dep in {c.id for c in self.config.enabled_checks()}:
                    blockers.append(f"{dep} (not run)")
                continue
            if result.status is Status.FAIL:
                blockers.append(dep)
            elif result.status is Status.ERROR and self.config.policy.error_blocks_dependents:
                blockers.append(f"{dep} (error)")
            elif result.status is Status.SKIPPED and result.blocked_by != ["--skip"]:
                blockers.append(f"{dep} (skipped)")
        return blockers

    def _record_skip(self, check: CheckSpec, blocked_by: list[str]) -> CheckResult:
        result = CheckResult(
            check_id=check.id,
            stage=check.stage,
            title=check.title or check.id,
            status=Status.SKIPPED,
            gate=check.gate,
            blocked_by=blocked_by,
        )
        self.results[check.id] = result
        self._emit("validate.result", result.model_dump(mode="json"))
        self._progress(f"SKIPPED  {check.id}  ({', '.join(blocked_by)})")
        return result

    # --------------------------------------------------------------- checks

    def cache_key(self, check: CheckSpec) -> str:
        payload = {
            "check": check.model_dump(mode="json"),
            "preamble": self.config.reviewer.preamble,
            "limits": self.config.limits,
            "profile": {self.profile_name: self.profile.model_dump(mode="json")},
            "inputs": input_hashes(self.workspace, list(check.inputs)),
            "repeat": self.options.repeat,
            "effort": self.options.effort,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def _cacheable(self, check: CheckSpec) -> bool:
        if check.kind == "builtin":
            return BUILTINS[check.builtin or ""].cacheable
        return True

    def _load_cached(self, key: str) -> CheckResult | None:
        path = self.cache_dir / f"{key}.json"
        if self.options.no_cache or not path.is_file():
            return None
        try:
            result = CheckResult.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        result.cached = True
        return result

    def _store_cached(self, key: str, result: CheckResult) -> None:
        if result.status is Status.ERROR:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / f"{key}.json").write_text(
            result.model_dump_json(indent=2), encoding="utf-8"
        )

    async def _run_check(self, check: CheckSpec) -> CheckResult:
        self._emit(
            "validate.check", {"check_id": check.id, "stage": check.stage, "status": "running"}
        )
        self._progress(f"running  {check.id}")
        key = self.cache_key(check)
        if self._cacheable(check):
            # Builtins that feed later checks (intake.*) are never cacheable.
            cached = self._load_cached(key)
            if cached is not None:
                cached.cache_key = key
                self._finish_check(check, cached)
                return cached
        started = time.monotonic()
        if check.kind == "builtin":
            result = self._run_builtin(check)
        else:
            result = await self._run_agent(check)
        result.seconds = round(time.monotonic() - started, 3)
        result.cache_key = key
        if self._cacheable(check):
            self._store_cached(key, result)
        self._finish_check(check, result)
        return result

    def _finish_check(self, check: CheckSpec, result: CheckResult) -> None:
        self.results[check.id] = result
        self._emit("validate.result", result.model_dump(mode="json"))
        counts = result.counts()
        extra = result.error or result.summary or ""
        self._progress(
            f"{result.status.value:<8} {check.id}  "
            f"P0={counts['P0']} P1={counts['P1']} P2={counts['P2']} P3={counts['P3']}"
            f"{'  (cached)' if result.cached else ''}{'  ' + extra if extra else ''}"
        )

    def _run_builtin(self, check: CheckSpec) -> CheckResult:
        assert self.design is not None
        builtin = BUILTINS[check.builtin or ""]
        limits = self.config.fab_limits()
        limits["fab"] = self.config.limits.get("fab")
        try:
            outcome = builtin.fn(self.design, check, limits)
        except Exception as exc:  # noqa: BLE001 - a crashing builtin is an ERROR result
            outcome = CheckOutcome(error=f"{type(exc).__name__}: {exc}")
        findings = self._number(check, outcome.findings)
        return CheckResult(
            check_id=check.id,
            stage=check.stage,
            title=check.title or check.id,
            status=compute_status(check, findings, self.profile, error=outcome.error),
            gate=check.gate,
            findings=findings,
            summary=outcome.summary,
            error=outcome.error,
        )

    def _number(self, check: CheckSpec, findings: list[Finding]) -> list[Finding]:
        findings.sort(key=lambda item: (SEVERITY_RANK[item.severity], item.title.lower()))
        for position, finding in enumerate(findings, start=1):
            finding.id = f"{check.id}/{position:03d}"
        return findings

    async def _run_agent(self, check: CheckSpec) -> CheckResult:
        assert self.design is not None and self.snapshot is not None and self.turn is not None
        design = self.design
        index = design.index
        items = fan_out_items(check, index)
        if check.scope != "project" and not items:
            return CheckResult(
                check_id=check.id,
                stage=check.stage,
                title=check.title or check.id,
                status=Status.PASS,
                gate=check.gate,
                summary="nothing to review for this scope",
            )
        slices = batches(items, check.batch) if items else [None]
        repeat = max(1, self.options.repeat or check.repeat)
        effort = CopperEffort(self.options.effort or check.effort or self.config.reviewer.effort)
        before = snapshot_hashes(self.snapshot)
        artifacts = design.artifacts_dir(check.id)
        tasks = []
        for slice_index, slice_items in enumerate(slices):
            prompt = build_prompt(
                self.config,
                check,
                design,
                self.snapshot,
                items=slice_items,
            )
            for run_index in range(repeat):
                thread_id = f"validate-{self.run_id}-{check.id}-{slice_index}-{run_index}"
                tasks.append(
                    self._review(
                        check, prompt, thread_id, effort, slice_index, run_index, artifacts
                    )
                )
        slice_results: list[SliceResult] = await asyncio.gather(*tasks)
        after = snapshot_hashes(self.snapshot)
        changed = snapshot_changes(before, after)
        threads = [item.thread_id for item in slice_results]
        errors = [item.error for item in slice_results if item.error]
        for item in slice_results:
            if item.usage_limit is not None and self.usage_limit is None:
                retryable = item.usage_limit.get("retryable")
                if retryable is not True:
                    self.usage_limit = item.usage_limit
        findings: list[Finding] = []
        summaries: list[str] = []
        for slice_index in range(len(slices)):
            runs = [
                item.findings
                for item in slice_results[slice_index * repeat : (slice_index + 1) * repeat]
                if item.error is None
            ]
            if runs:
                findings.extend(consensus(runs))
            summary = next(
                (
                    item.summary
                    for item in slice_results[slice_index * repeat : (slice_index + 1) * repeat]
                    if item.summary
                ),
                "",
            )
            if summary:
                summaries.append(summary)
        error: str | None = None
        if changed:
            error = f"reviewer modified the snapshot: {', '.join(changed[:10])}"
        elif errors:
            error = errors[0] if len(errors) == 1 else f"{len(errors)} slices failed: {errors[0]}"
        ground_findings(findings, index)
        findings = self._number(check, findings)
        return CheckResult(
            check_id=check.id,
            stage=check.stage,
            title=check.title or check.id,
            status=compute_status(check, findings, self.profile, error=error),
            gate=check.gate,
            findings=findings,
            summary=" ".join(summaries)[:2000],
            threads=threads,
            error=error,
        )

    async def _review(
        self,
        check: CheckSpec,
        prompt: str,
        thread_id: str,
        effort: CopperEffort,
        slice_index: int,
        run_index: int,
        artifacts: Path,
    ) -> SliceResult:
        assert self.snapshot is not None and self.turn is not None
        turn = ReviewTurn(
            snapshot=self.snapshot,
            prompt=prompt,
            thread_id=thread_id,
            effort=effort,
            broker=make_broker(self.snapshot),
            check_id=check.id,
            on_event=lambda event: self._emit(
                str(event.get("type") or "copper.retry"),
                {"check_id": check.id, **(event.get("data") or {}), "title": event.get("title")},
            ),
        )
        async with self.semaphore:
            if self.usage_limit is not None:
                return SliceResult(thread_id=thread_id, error=self._usage_label())
            result = await review_slice(self.turn, turn)
        suffix = f"{slice_index}-{run_index}"
        for number, (sent, reply) in enumerate(
            zip(result.prompts, result.replies, strict=False), start=1
        ):
            (artifacts / f"prompt-{suffix}-{number}.md").write_text(sent, encoding="utf-8")
            (artifacts / f"reply-{suffix}-{number}.md").write_text(reply, encoding="utf-8")
        return result

    # -------------------------------------------------------------- finish

    def _ordered_results(self) -> list[CheckResult]:
        order = {check.id: position for position, check in enumerate(self.config.checks)}
        stage_order = self.config.stage_index()
        return sorted(
            self.results.values(),
            key=lambda result: (
                stage_order.get(result.stage, 1 << 30),
                order.get(result.check_id, 0),
            ),
        )

    def exit_code(self, results: list[CheckResult]) -> int:
        if self.usage_limit is not None:
            return EXIT_ENVIRONMENT
        if any(
            result.gate == "blocking" and result.status in {Status.FAIL, Status.ERROR}
            for result in results
        ):
            return EXIT_FAIL
        if any(
            result.status is Status.SKIPPED and result.blocked_by != ["--skip"]
            for result in results
        ):
            return EXIT_INCOMPLETE
        return EXIT_PASS

    def _finish(self) -> RunOutcome:
        results = self._ordered_results()
        verdict = verdict_for(results)
        code = EXIT_INTERRUPTED if self.interrupted else self.exit_code(results)
        report = build_report(
            verdict=verdict,
            profile=self.profile_name,
            gate=self.options.gate.value,
            design_hash=design_hash(self.workspace),
            cli_version=__version__,
            kicad_version=self.design.kicad_version if self.design else None,
            started=self.started,
            finished=_now(),
            results=results,
            run_id=self.run_id,
            interrupted=self.interrupted,
            exit_code=code,
        )
        if self.usage_limit is not None:
            report["usage_limit"] = self.usage_limit
        actions = next_actions(results, self.config)
        paths = write_outputs(self.run_dir, report, actions)
        self._emit(
            "validate.end",
            {
                "verdict": verdict,
                "report": str(paths["report"]),
                "next_actions": str(paths["next_actions"]),
                "exit_code": code,
                "run_dir": str(self.run_dir),
            },
        )
        self.last_outcome = RunOutcome(
            verdict=verdict,
            exit_code=code,
            run_dir=self.run_dir,
            report=report,
            next_actions=actions,
            paths=paths,
            results=results,
            interrupted=self.interrupted,
        )
        return self.last_outcome


class _Interrupted(BaseException):
    """Carries the partial outcome out of a cancelled run."""

    def __init__(self, outcome: RunOutcome) -> None:
        super().__init__("validate interrupted")
        self.outcome = outcome


async def run_validation(
    workspace: Path,
    config: ValidateConfig,
    options: RunOptions,
    **kwargs: Any,
) -> RunOutcome:
    """Run and return the outcome; a cancelled run returns its partial outcome."""
    runner = Runner(workspace, config, options, **kwargs)
    try:
        return await runner.run()
    except _Interrupted as exc:
        return exc.outcome
