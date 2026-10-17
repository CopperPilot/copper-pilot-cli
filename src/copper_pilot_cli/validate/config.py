"""Validate graph models: checks, stages, policy, findings, and YAML loading."""

from __future__ import annotations

import copy
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

PROJECT_CONFIG = Path(".copperpilot") / "validate.yaml"
DEFAULT_GRAPH_NAME = "default"
REPORT_SCHEMA = "copper-pilot.validate/v1"


class ConfigError(ValueError):
    """The graph file is invalid; the CLI maps this to exit code 2."""


class Severity(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Status(StrEnum):
    PASS = "PASS"  # noqa: S105 - a check status, not a secret
    WARN = "WARN"
    FAIL = "FAIL"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"


class GateMode(StrEnum):
    STAGE = "stage"
    FULL = "full"
    STRICT = "strict"


SEVERITY_RANK = {Severity.P0: 0, Severity.P1: 1, Severity.P2: 2, Severity.P3: 3}
CONFIDENCE_RANK = {Confidence.HIGH: 3, Confidence.MEDIUM: 2, Confidence.LOW: 1}
INPUT_KINDS = ("sch", "pcb", "pro", "libs", "intent", "all")
InputKind = Literal["sch", "pcb", "pro", "libs", "intent", "all"]


class Locator(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sheet: str | None = None
    refs: list[str] = Field(default_factory=list)
    nets: list[str] = Field(default_factory=list)
    layer: str | None = None
    x_mm: float | None = None
    y_mm: float | None = None


class Finding(BaseModel):
    """One reviewer finding; the output contract every agent reviewer must meet."""

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    severity: Severity = Severity.P2
    title: str
    detail: str = ""
    locator: Locator = Field(default_factory=Locator)
    evidence: str = ""
    rule: str = ""
    suggested_action: str = ""
    confidence: Confidence = Confidence.MEDIUM
    grounded: bool = True

    def locator_key(self) -> str:
        """Match findings across consensus runs by what they point at."""
        loc = self.locator
        parts = sorted(r.upper() for r in loc.refs) + sorted(n.upper() for n in loc.nets)
        if loc.sheet:
            parts.append(loc.sheet.strip().lower())
        if parts:
            return "|".join(parts)
        return " ".join(self.title.lower().split())


class CheckResult(BaseModel):
    check_id: str
    stage: str
    title: str
    status: Status
    gate: Literal["blocking", "advisory"]
    findings: list[Finding] = Field(default_factory=list)
    summary: str = ""
    blocked_by: list[str] = Field(default_factory=list)
    threads: list[str] = Field(default_factory=list)
    cache_key: str | None = None
    cached: bool = False
    seconds: float = 0.0
    error: str | None = None

    def counts(self) -> dict[str, int]:
        counts = {severity.value: 0 for severity in Severity}
        for finding in self.findings:
            counts[finding.severity.value] += 1
        return counts


class StageSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str = ""
    question: str = ""


class ProfileSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fail_on: list[Severity] = Field(default_factory=lambda: [Severity.P0, Severity.P1])
    min_confidence_to_fail: Confidence = Confidence.MEDIUM


class PolicySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_profile: str = "dev"
    profiles: dict[str, ProfileSpec] = Field(default_factory=lambda: {"dev": ProfileSpec()})
    error_blocks_dependents: bool = True


class ReviewerSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preamble: str = ""
    effort: Literal["balanced", "intelligence"] = "balanced"
    timeout_s: int = 1800


class CheckSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    stage: str
    title: str = ""
    kind: Literal["agent", "builtin"] = "agent"
    builtin: str | None = None
    scope: Literal["project", "per_part", "per_sheet", "per_netclass"] = "project"
    batch: int = 4
    requires: list[str] = Field(default_factory=list)
    after: list[str] = Field(default_factory=list)
    gate: Literal["blocking", "advisory"] = "blocking"
    inputs: list[InputKind] = Field(default_factory=lambda: ["all"])  # ty: ignore[invalid-assignment]
    repeat: int = 1
    prompt: str | None = None
    effort: Literal["balanced", "intelligence"] | None = None
    enabled: bool = True


class ValidateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    extends: str | None = None
    stages: list[StageSpec] = Field(default_factory=list)
    policy: PolicySpec = Field(default_factory=PolicySpec)
    limits: dict[str, Any] = Field(default_factory=dict)
    reviewer: ReviewerSpec = Field(default_factory=ReviewerSpec)
    checks: list[CheckSpec] = Field(default_factory=list)

    # ------------------------------------------------------------ lookups

    def stage_index(self) -> dict[str, int]:
        return {stage.id: position for position, stage in enumerate(self.stages)}

    def stage(self, stage_id: str) -> StageSpec:
        for stage in self.stages:
            if stage.id == stage_id:
                return stage
        raise KeyError(stage_id)

    def check(self, check_id: str) -> CheckSpec:
        for check in self.checks:
            if check.id == check_id:
                return check
        raise KeyError(check_id)

    def enabled_checks(self) -> list[CheckSpec]:
        return [check for check in self.checks if check.enabled]

    def profile(self, name: str | None) -> tuple[str, ProfileSpec]:
        chosen = name or self.policy.default_profile
        try:
            return chosen, self.policy.profiles[chosen]
        except KeyError as exc:
            raise ConfigError(
                f"unknown profile {chosen!r}; profiles: {', '.join(self.policy.profiles)}"
            ) from exc

    def fab_limits(self) -> dict[str, Any]:
        fab = self.limits.get("fab")
        if isinstance(fab, str) and isinstance(self.limits.get(fab), dict):
            return dict(self.limits[fab])
        return {}


# ------------------------------------------------------------------ loading


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path}: the graph must be a mapping at the top level")
    return loaded


def packaged_default_raw() -> dict[str, Any]:
    """Return the packaged default graph as plain data."""
    text = (
        resources.files("copper_pilot_cli.validate")
        .joinpath("default.yaml")
        .read_text(encoding="utf-8")
    )
    loaded = yaml.safe_load(text)
    assert isinstance(loaded, dict)
    return loaded


def _merge_by_id(base: list[Any], overrides: list[Any], label: str) -> list[Any]:
    result = [copy.deepcopy(item) for item in base]
    positions = {
        item.get("id"): index for index, item in enumerate(result) if isinstance(item, dict)
    }
    for item in overrides:
        if not isinstance(item, dict) or not item.get("id"):
            raise ConfigError(f"every {label} entry needs an id")
        if item["id"] in positions:
            result[positions[item["id"]]].update(item)
        else:
            positions[item["id"]] = len(result)
            result.append(copy.deepcopy(item))
    return result


def _deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def merge_raw(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Merge a project or `--config` graph onto `base` (`extends: default`)."""
    result = copy.deepcopy(base)
    for key, value in overrides.items():
        if key == "extends":
            continue
        if key in {"checks", "stages"}:
            if not isinstance(value, list):
                raise ConfigError(f"{key} must be a list")
            result[key] = _merge_by_id(result.get(key) or [], value, key[:-1])
        elif key in {"policy", "limits", "reviewer"} and isinstance(value, dict):
            result[key] = _deep_merge(result.get(key) or {}, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _apply_layer(current: dict[str, Any], layer: dict[str, Any], source: Path) -> dict[str, Any]:
    extends = layer.get("extends")
    if extends is None:
        return copy.deepcopy(layer)
    if extends != DEFAULT_GRAPH_NAME:
        raise ConfigError(f"{source}: extends must be {DEFAULT_GRAPH_NAME!r}, got {extends!r}")
    return merge_raw(current, layer)


def load_raw_graph(workspace: Path, config_path: Path | None = None) -> dict[str, Any]:
    """Layer packaged default, `.copperpilot/validate.yaml`, then `--config`."""
    raw = packaged_default_raw()
    project = workspace / PROJECT_CONFIG
    if project.is_file():
        raw = _apply_layer(raw, _load_yaml(project), project)
    if config_path is not None:
        if not config_path.is_file():
            raise ConfigError(f"config file not found: {config_path}")
        raw = _apply_layer(raw, _load_yaml(config_path), config_path)
    return raw


def build_config(raw: dict[str, Any]) -> ValidateConfig:
    try:
        config = ValidateConfig.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first.get("loc", ()))
        raise ConfigError(f"invalid graph at {location or '<root>'}: {first.get('msg')}") from exc
    validate_graph(config)
    return config


def load_config(workspace: Path, config_path: Path | None = None) -> ValidateConfig:
    return build_config(load_raw_graph(workspace, config_path))


# --------------------------------------------------------------- validation


def validate_graph(config: ValidateConfig) -> None:
    """Enforce the structural rules; each violation names the offending check."""
    from copper_pilot_cli.validate.builtins import BUILTINS

    stages = config.stage_index()
    if not stages:
        raise ConfigError("the graph defines no stages")
    seen: set[str] = set()
    for check in config.checks:
        if check.id in seen:
            raise ConfigError(f"check {check.id!r}: duplicate id")
        seen.add(check.id)
    checks = {check.id: check for check in config.checks}
    for check in config.checks:
        if check.stage not in stages:
            raise ConfigError(f"check {check.id!r}: unknown stage {check.stage!r}")
        for label, references in (("requires", check.requires), ("after", check.after)):
            for other in references:
                if other not in checks:
                    raise ConfigError(f"check {check.id!r}: {label} unknown check {other!r}")
                if stages[checks[other].stage] > stages[check.stage]:
                    raise ConfigError(
                        f"check {check.id!r}: {label} {other!r} belongs to the later stage "
                        f"{checks[other].stage}"
                    )
                if other == check.id:
                    raise ConfigError(f"check {check.id!r}: depends on itself")
        if check.kind == "agent":
            if not check.prompt or not check.prompt.strip():
                raise ConfigError(f"check {check.id!r}: agent checks need a prompt")
            if check.scope != "project" and "{items}" not in check.prompt:
                raise ConfigError(
                    f"check {check.id!r}: scope {check.scope} needs an {{items}} placeholder"
                )
        else:
            if not check.builtin:
                raise ConfigError(f"check {check.id!r}: builtin checks need a builtin name")
            if check.builtin not in BUILTINS:
                raise ConfigError(
                    f"check {check.id!r}: unknown builtin {check.builtin!r}; "
                    f"known: {', '.join(sorted(BUILTINS))}"
                )
        if check.repeat < 1:
            raise ConfigError(f"check {check.id!r}: repeat must be at least 1")
        if check.batch < 1:
            raise ConfigError(f"check {check.id!r}: batch must be at least 1")
    _reject_cycles(config)


def _reject_cycles(config: ValidateConfig) -> None:
    edges = {check.id: [*check.requires, *check.after] for check in config.checks}
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for other in edges.get(node, []):
            if state.get(other) == 1:
                cycle = stack[stack.index(other) :] + [other]
                raise ConfigError(f"check {node!r}: dependency cycle {' -> '.join(cycle)}")
            if state.get(other) is None:
                visit(other)
        stack.pop()
        state[node] = 2

    for check_id in edges:
        if state.get(check_id) is None:
            visit(check_id)


def hard_prerequisites(config: ValidateConfig, check_ids: set[str]) -> set[str]:
    """Close `check_ids` over `requires` (transitively)."""
    result = set(check_ids)
    queue = list(check_ids)
    while queue:
        current = config.check(queue.pop())
        for other in current.requires:
            if other not in result:
                result.add(other)
                queue.append(other)
    return result
