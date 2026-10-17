"""`copper-pilot validate`: a stage-gated graph of read-only reviewers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from copper_pilot_cli.copper_workspace import canonical_workspace
from copper_pilot_cli.validate.config import GateMode, load_config
from copper_pilot_cli.validate.runner import RunOptions, RunOutcome, run_validation


async def avalidate(
    workspace: str | Path = ".",
    *,
    profile: str | None = None,
    gate: str = GateMode.STAGE.value,
    until: str | None = None,
    only: list[str] | None = None,
    effort: str | None = None,
    no_cache: bool = False,
    **runner_kwargs: Any,
) -> RunOutcome:
    """Run validate over `workspace`; used by the MCP `validate` tool."""
    root = canonical_workspace(workspace)
    options = RunOptions(
        profile=profile,
        gate=GateMode(gate),
        until=until,
        only=list(only or []),
        effort=effort,
        no_cache=no_cache,
    )
    return await run_validation(root, load_config(root), options, **runner_kwargs)
