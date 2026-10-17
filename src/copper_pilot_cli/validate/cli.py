"""`copper-pilot validate` argument parsing and handlers."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from copper_pilot_cli.copper_protocol import CopperEffort
from copper_pilot_cli.copper_workspace import canonical_workspace
from copper_pilot_cli.validate.config import (
    CheckSpec,
    ConfigError,
    GateMode,
    ValidateConfig,
    load_config,
)
from copper_pilot_cli.validate.design import input_hashes
from copper_pilot_cli.validate.runner import (
    EXIT_ENVIRONMENT,
    EXIT_INTERRUPTED,
    EXIT_USAGE,
    EnvironmentError_,
    Runner,
    RunOptions,
    RunOutcome,
)

SUBCOMMANDS = ("list", "graph", "explain")
DESCRIPTION = (
    "Run a stage-gated graph of read-only reviewers over a KiCad workspace. "
    "Exit codes: 0 pass, 1 fail, 2 usage/config error, 3 incomplete, 4 environment."
)


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def build_parser() -> argparse.ArgumentParser:
    """The `copper-pilot validate [workspace] [flags]` run parser."""
    parser = argparse.ArgumentParser(
        prog="copper-pilot validate",
        description=DESCRIPTION,
        epilog="Subcommands: copper-pilot validate list|graph [workspace]; "
        "copper-pilot validate explain CHECK_ID [workspace].",
    )
    parser.set_defaults(command="validate", action="run", message=None)
    parser.add_argument("workspace", nargs="?", default=".", help="Workspace directory")
    parser.add_argument("--config", type=Path, help="Graph file layered on the default graph")
    parser.add_argument(
        "--context",
        type=Path,
        nargs="+",
        action="extend",
        default=[],
        help="Extra reviewer context files (requirements, fab checklist, review prompt)",
    )
    parser.add_argument("--profile", help="Severity policy profile (default: dev)")
    parser.add_argument(
        "--gate",
        choices=[item.value for item in GateMode],
        default=GateMode.STAGE.value,
        help="stage: stop after a failing stage; full: run everything unblocked; "
        "strict: stop after the first failing wave (default: stage)",
    )
    parser.add_argument("--until", metavar="STAGE", help="Last stage to run, e.g. S3")
    parser.add_argument(
        "--only",
        type=_csv,
        action="extend",
        default=[],
        metavar="ID[,ID...]",
        help="Run these checks and their hard prerequisites",
    )
    parser.add_argument(
        "--skip", type=_csv, action="extend", default=[], metavar="ID[,ID...]", help="Skip checks"
    )
    parser.add_argument(
        "--effort",
        choices=[item.value for item in CopperEffort],
        help="Effort for agent checks (a per-check value wins)",
    )
    parser.add_argument(
        "--concurrency", type=int, default=3, help="Concurrent hosted turns (default: 3)"
    )
    parser.add_argument("--no-cache", action="store_true", help="Ignore cached results")
    parser.add_argument(
        "--repeat", type=int, help="Consensus runs for agent checks (overrides the graph)"
    )
    parser.add_argument("--json", action="store_true", help="Stream JSON events to stdout")
    parser.add_argument("--junit", type=Path, metavar="PATH", help="Also write JUnit XML")
    parser.add_argument(
        "--out", type=Path, metavar="DIR", help="Output directory (default: .copperpilot/validate)"
    )
    parser.add_argument(
        "--emit-fix-prompt",
        action="store_true",
        help="Also print a ready prompt for `copper-pilot --mode agent`",
    )
    return parser


def build_subcommand_parser(action: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"copper-pilot validate {action}")
    parser.set_defaults(command="validate", action=action, message=None)
    if action == "explain":
        parser.add_argument("check_id", help="Check id, e.g. sch.erc")
    parser.add_argument("workspace", nargs="?", default=".", help="Workspace directory")
    parser.add_argument("--config", type=Path, help="Graph file layered on the default graph")
    return parser


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse `validate ...` arguments; the first token may name a subcommand."""
    if argv and argv[0] in SUBCOMMANDS:
        return build_subcommand_parser(argv[0]).parse_args(argv[1:])
    return build_parser().parse_args(argv)


# ---------------------------------------------------------------- handlers


def _config(args: argparse.Namespace) -> tuple[Path, ValidateConfig]:
    workspace = canonical_workspace(args.workspace)
    return workspace, load_config(workspace, args.config)


def render_list(config: ValidateConfig) -> str:
    lines = ["Stages:"]
    for stage in config.stages:
        lines.append(f"  {stage.id}  {stage.title}: {stage.question}")
    lines.append("")
    lines.append("Checks:")
    width = max(len(check.id) for check in config.checks) if config.checks else 10
    for check in config.checks:
        flags = [check.kind, check.gate]
        if check.scope != "project":
            flags.append(check.scope)
        if check.repeat > 1:
            flags.append(f"repeat={check.repeat}")
        if not check.enabled:
            flags.append("disabled")
        edges = []
        if check.requires:
            edges.append("requires " + ", ".join(check.requires))
        if check.after:
            edges.append("after " + ", ".join(check.after))
        lines.append(
            f"  {check.stage}  {check.id:<{width}}  [{', '.join(flags)}]  {check.title}"
            + (f"  ({'; '.join(edges)})" if edges else "")
        )
    return "\n".join(lines)


def render_graph(config: ValidateConfig) -> str:
    def node(check_id: str) -> str:
        return check_id.replace(".", "_")

    lines = ["flowchart TD"]
    for stage in config.stages:
        members = [check for check in config.enabled_checks() if check.stage == stage.id]
        if not members:
            continue
        lines.append(f'  subgraph {stage.id}["{stage.id} {stage.title}"]')
        for check in members:
            shape = "[[" if check.kind == "builtin" else "("
            close = "]]" if check.kind == "builtin" else ")"
            lines.append(f'    {node(check.id)}{shape}"{check.id}"{close}')
        lines.append("  end")
    for check in config.enabled_checks():
        for dep in check.requires:
            lines.append(f"  {node(dep)} --> {node(check.id)}")
        for dep in check.after:
            lines.append(f"  {node(dep)} -.-> {node(check.id)}")
    return "\n".join(lines)


def render_explain(workspace: Path, config: ValidateConfig, check: CheckSpec) -> str:
    from copper_pilot_cli.validate.reviewer import OUTPUT_CONTRACT

    sections = [
        f"# {check.id}: {check.title}",
        "",
        "## Config",
        yaml.safe_dump(check.model_dump(mode="json", exclude_none=True), sort_keys=False).rstrip(),
    ]
    if check.kind == "agent":
        sections.extend(
            [
                "",
                "## Prompt (preamble + reference material + design paths + body + contract)",
                config.reviewer.preamble.strip(),
                "",
                f"Your single responsibility: {check.title or check.id}",
                "",
                (check.prompt or "").strip(),
                "",
                OUTPUT_CONTRACT,
            ]
        )
    else:
        from copper_pilot_cli.validate.builtins import BUILTINS

        sections.extend(
            ["", f"## Builtin: {check.builtin}", BUILTINS[check.builtin or ""].description]
        )
    hashes = input_hashes(workspace, list(check.inputs))
    sections.extend(["", f"## Inputs ({', '.join(check.inputs)}): {len(hashes)} files"])
    sections.extend(f"- {name}  {digest[:12]}" for name, digest in sorted(hashes.items())[:40])
    latest = workspace / ".copperpilot" / "validate" / "latest" / "report.json"
    if not latest.is_file():
        pointer = workspace / ".copperpilot" / "validate" / "latest"
        if pointer.is_file():
            latest = Path(pointer.read_text(encoding="utf-8").strip()) / "report.json"
    sections.append("")
    sections.append("## Last result")
    if latest.is_file():
        try:
            report = json.loads(latest.read_text(encoding="utf-8"))
            match = next(
                (row for row in report.get("results", []) if row.get("check_id") == check.id),
                None,
            )
        except (OSError, json.JSONDecodeError):
            match = None
        if match is None:
            sections.append("not in the latest run")
        else:
            sections.append(json.dumps(match, indent=2))
    else:
        sections.append("no run yet")
    return "\n".join(sections)


def options_from_args(args: argparse.Namespace) -> RunOptions:
    return RunOptions(
        profile=args.profile,
        gate=GateMode(args.gate),
        until=args.until,
        only=list(args.only),
        skip=list(args.skip),
        effort=args.effort,
        concurrency=args.concurrency,
        no_cache=args.no_cache,
        repeat=args.repeat,
        context_files=[Path(item) for item in args.context],
        out_dir=args.out,
        junit_path=args.junit,
        emit_fix_prompt=args.emit_fix_prompt,
    )


def _print_outcome(outcome: RunOutcome, *, as_json: bool) -> None:
    if as_json:
        return
    counts = {"FAIL": 0, "ERROR": 0, "WARN": 0, "PASS": 0, "SKIPPED": 0}
    for result in outcome.results:
        counts[result.status.value] += 1
    print(
        f"{outcome.verdict}: {counts['PASS']} pass, {counts['WARN']} warn, {counts['FAIL']} fail, "
        f"{counts['ERROR']} error, {counts['SKIPPED']} skipped"
        + ("  (interrupted)" if outcome.interrupted else "")
    )
    print(f"report: {outcome.paths['markdown']}")
    print(f"json: {outcome.paths['report']}")
    print(f"next actions: {outcome.paths['next_actions']}")
    if "junit" in outcome.paths:
        print(f"junit: {outcome.paths['junit']}")
    if outcome.fix_prompt is not None:
        print()
        print(outcome.fix_prompt)


def run_command(args: argparse.Namespace, **runner_kwargs: Any) -> int:
    """Dispatch list/graph/explain/run; returns the process exit code."""
    try:
        workspace, config = _config(args)
        if args.action == "list":
            print(render_list(config))
            return 0
        if args.action == "graph":
            print(render_graph(config))
            return 0
        if args.action == "explain":
            try:
                check = config.check(args.check_id)
            except KeyError:
                raise ConfigError(f"unknown check {args.check_id!r}") from None
            print(render_explain(workspace, config, check))
            return 0
        options = options_from_args(args)
        if args.json:
            options.on_event = lambda event: print(json.dumps(event), flush=True)
        else:
            options.on_progress = lambda line: print(line, file=sys.stderr, flush=True)
        runner = Runner(workspace, config, options, **runner_kwargs)
        try:
            outcome = asyncio.run(runner.run())
        except KeyboardInterrupt:
            outcome = runner.last_outcome
            if outcome is None:
                return EXIT_INTERRUPTED
            _print_outcome(outcome, as_json=args.json)
            return EXIT_INTERRUPTED
        _print_outcome(outcome, as_json=args.json)
        return outcome.exit_code
    except (ConfigError, FileNotFoundError, NotADirectoryError) as exc:
        print(f"copper-pilot validate: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except EnvironmentError_ as exc:
        print(f"copper-pilot validate: {exc}", file=sys.stderr)
        return EXIT_ENVIRONMENT
