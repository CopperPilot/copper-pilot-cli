"""`copper-pilot validate`: a stage-gated graph of read-only reviewers.

from copper_pilot_cli.validate import validate
assert validate("esp32", profile="release").verdict == "PASS"
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from copper_pilot_cli.copper_workspace import canonical_workspace
from copper_pilot_cli.validate.config import CheckResult, Finding, GateMode, load_config
from copper_pilot_cli.validate.runner import RunOptions, RunOutcome, run_validation

__all__ = ["ValidationReport", "avalidate", "validate"]


@dataclass(slots=True)
class ValidationReport:
    """What a script needs from one run."""

    verdict: str
    exit_code: int
    run_dir: Path
    report: dict[str, Any]
    next_actions: dict[str, Any]
    results: list[CheckResult]
    paths: dict[str, Path]
    interrupted: bool = False

    @property
    def passed(self) -> bool:
        return self.verdict == "PASS"

    @property
    def findings(self) -> list[Finding]:
        return [finding for result in self.results for finding in result.findings]

    @classmethod
    def from_outcome(cls, outcome: RunOutcome) -> ValidationReport:
        return cls(
            verdict=outcome.verdict,
            exit_code=outcome.exit_code,
            run_dir=outcome.run_dir,
            report=outcome.report,
            next_actions=outcome.next_actions,
            results=outcome.results,
            paths=outcome.paths,
            interrupted=outcome.interrupted,
        )


async def avalidate(
    workspace: str | Path = ".",
    *,
    profile: str | None = None,
    gate: str = GateMode.STAGE.value,
    until: str | None = None,
    only: Iterable[str] | None = None,
    skip: Iterable[str] | None = None,
    effort: str | None = None,
    concurrency: int = 3,
    no_cache: bool = False,
    repeat: int | None = None,
    context: Iterable[str | Path] | None = None,
    config: str | Path | None = None,
    out: str | Path | None = None,
    junit: str | Path | None = None,
    **runner_kwargs: Any,
) -> ValidationReport:
    """Async form of :func:`validate`. Extra keyword arguments reach the Runner."""
    root = canonical_workspace(workspace)
    graph = load_config(root, Path(config) if config else None)
    options = RunOptions(
        profile=profile,
        gate=GateMode(gate),
        until=until,
        only=list(only or []),
        skip=list(skip or []),
        effort=effort,
        concurrency=concurrency,
        no_cache=no_cache,
        repeat=repeat,
        context_files=[Path(item) for item in (context or [])],
        out_dir=Path(out) if out else None,
        junit_path=Path(junit) if junit else None,
    )
    outcome = await run_validation(root, graph, options, **runner_kwargs)
    return ValidationReport.from_outcome(outcome)


def validate(workspace: str | Path = ".", **kwargs: Any) -> ValidationReport:
    """Run validate over `workspace` and return a :class:`ValidationReport`."""
    return asyncio.run(avalidate(workspace, **kwargs))
