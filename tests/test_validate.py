"""`copper-pilot validate` engine tests; no credential and no KiCad required."""

from __future__ import annotations

import json
import os
import stat
import sys
import xml.etree.ElementTree as ET  # noqa: S405 - our own JUnit output
from pathlib import Path
from typing import Any

import pytest
import yaml

from copper_pilot_cli.copper_auth import DeviceCredential
from copper_pilot_cli.copper_protocol import CopperEvent, CopperMode, EventKind, ToolRequest
from copper_pilot_cli.copper_tools import ApprovalRequest, ToolRejected
from copper_pilot_cli.validate import avalidate
from copper_pilot_cli.validate.builtins import _library_uri_outside_project
from copper_pilot_cli.validate.cli import parse_args, render_graph, render_list, run_command
from copper_pilot_cli.validate.config import (
    CheckResult,
    ConfigError,
    Finding,
    GateMode,
    Locator,
    Severity,
    Status,
    ValidateConfig,
    build_config,
    load_config,
    merge_raw,
    packaged_default_raw,
)
from copper_pilot_cli.validate.design import (
    SCRATCH_DIR,
    build_index,
    create_snapshot,
    input_hashes,
    snapshot_changes,
    snapshot_hashes,
)
from copper_pilot_cli.validate.kicad import KicadError
from copper_pilot_cli.validate.report import fix_prompt, next_actions, render_junit
from copper_pilot_cli.validate.reviewer import (
    QUESTION_REJECTION,
    HostedTurnRunner,
    ReviewTurn,
    TurnResult,
    consensus,
    extract_json_block,
    fan_out_items,
    make_broker,
    parse_reply,
    review_policy,
)
from copper_pilot_cli.validate.runner import (
    EXIT_ENVIRONMENT,
    EXIT_FAIL,
    EXIT_INCOMPLETE,
    EXIT_PASS,
    EXIT_USAGE,
    Runner,
    RunOptions,
)

CREDENTIAL = DeviceCredential(
    api_key="cf_live_test",
    fingerprint="fingerprint",
    base_url="https://example.test",
)

NETLIST = """<?xml version="1.0" encoding="UTF-8"?>
<export version="E">
  <design>
    <source>board.kicad_sch</source>
    <sheet number="1" name="/" tstamps="/"/>
    <sheet number="2" name="/power/" tstamps="/abc/"/>
  </design>
  <components>
    <comp ref="R1"><value>10k</value><footprint>Resistor_SMD:R_0603_1608Metric</footprint>
      <fields><field name="LCSC">C25804</field><field name="MPN">0603WAF1002T5E</field>
      <field name="Datasheet">https://x/r.pdf</field></fields>
      <sheetpath names="/" tstamps="/"/></comp>
    <comp ref="R2"><value>10k</value><footprint>Resistor_SMD:R_0603_1608Metric</footprint>
      <fields><field name="LCSC">C25804</field><field name="MPN">0603WAF1002T5E</field>
      <field name="Datasheet">https://x/r.pdf</field></fields>
      <sheetpath names="/" tstamps="/"/></comp>
    <comp ref="C1"><value>100n</value><footprint>Capacitor_SMD:C_0603_1608Metric</footprint>
      <fields><field name="Datasheet"/></fields>
      <sheetpath names="/power/" tstamps="/abc/"/></comp>
    <comp ref="U1"><value>ESP32-S3-WROOM-1</value><footprint>RF_Module:ESP32-S3-WROOM-1</footprint>
      <fields><field name="LCSC">C2913202</field><field name="MPN">ESP32-S3-WROOM-1-N8</field>
      <field name="Datasheet">https://x/esp.pdf</field></fields>
      <sheetpath names="/" tstamps="/"/></comp>
    <comp ref="H1"><value>MountingHole</value><footprint>MountingHole:MountingHole_3.2mm</footprint>
      <fields/><sheetpath names="/" tstamps="/"/></comp>
    <comp ref="R9"><value>DNP</value><footprint>Resistor_SMD:R_0603_1608Metric</footprint>
      <fields/><property name="dnp"/><sheetpath names="/" tstamps="/"/></comp>
  </components>
  <nets>
    <net code="1" name="+3V3" class="Power,Default">
      <node ref="U1" pin="2"/><node ref="C1" pin="1"/></net>
    <net code="2" name="GND" class="Default"><node ref="U1" pin="1"/><node ref="C1" pin="2"/></net>
    <net code="3" name="SDA" class="Default"><node ref="U1" pin="5"/><node ref="R1" pin="1"/></net>
  </nets>
</export>
"""

PROJECT = {
    "board": {
        "design_settings": {
            "rules": {
                "min_clearance": 0.0,
                "min_track_width": 0.15,
                "min_via_diameter": 0.5,
                "min_through_hole_diameter": 0.3,
                "min_copper_edge_clearance": 0.5,
                "min_text_height": 1.0,
            }
        }
    },
    "net_settings": {
        "classes": [
            {
                "name": "Default",
                "clearance": 0.2,
                "track_width": 0.2,
                "via_diameter": 0.6,
                "via_drill": 0.3,
            },
            {
                "name": "Power",
                "clearance": 0.2,
                "track_width": 0.3,
                "via_diameter": 0.6,
                "via_drill": 0.3,
            },
        ],
        "netclass_patterns": [{"netclass": "Power", "pattern": "+3V3"}],
    },
}

PCB = """(kicad_pcb (version 20240108)
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(2 "B.Cu" signal)
\t\t(5 "F.SilkS" user "F.Silkscreen")
\t\t(44 "Edge.Cuts" user)
\t)
\t(footprint "Resistor_SMD:R_0603_1608Metric" (property "Reference" "R1"))
)
"""


def make_project(root: Path, *, pcb: bool = True, power_track: float = 0.3) -> Path:
    board = root / "board"
    board.mkdir(parents=True, exist_ok=True)
    project = json.loads(json.dumps(PROJECT))
    project["net_settings"]["classes"][1]["track_width"] = power_track
    (board / "board.kicad_pro").write_text(json.dumps(project), encoding="utf-8")
    (board / "board.kicad_sch").write_text("(kicad_sch (version 20231120))\n", encoding="utf-8")
    if pcb:
        (board / "board.kicad_pcb").write_text(PCB, encoding="utf-8")
    intent = root / ".copperpilot"
    intent.mkdir(exist_ok=True)
    (intent / "README.md").write_text("# Project index\nA test board.\n", encoding="utf-8")
    plans = intent / "plans"
    plans.mkdir(exist_ok=True)
    (plans / "board.plan.md").write_text("---\nname: Board\n---\nBuild it.\n", encoding="utf-8")
    return root


class FakeKicad:
    """Writes canned kicad-cli outputs; configurable per test."""

    executable = Path("/usr/bin/kicad-cli")

    def __init__(
        self,
        *,
        version: str = "10.0.4",
        erc: list[dict[str, Any]] | None = None,
        drc: list[dict[str, Any]] | None = None,
        parity: list[dict[str, Any]] | None = None,
        unconnected: list[dict[str, Any]] | None = None,
        netlist: str = NETLIST,
    ) -> None:
        self._version = version
        self.erc_violations = erc or []
        self.drc_violations = drc or []
        self.parity_violations = parity or []
        self.unconnected = unconnected or []
        self.netlist_xml = netlist
        self.calls: list[str] = []

    def version(self) -> str:
        self.calls.append("version")
        return self._version

    def major(self) -> int:
        return int(self._version.split(".")[0])

    def erc(self, schematic: Path, output: Path) -> dict[str, Any]:
        self.calls.append("erc")
        report = {"sheets": [{"path": "/", "violations": self.erc_violations}]}
        output.write_text(json.dumps(report), encoding="utf-8")
        return report

    def drc(self, pcb: Path, output: Path, *, schematic_parity: bool = False) -> dict[str, Any]:
        self.calls.append("drc" if not schematic_parity else "drc+parity")
        report = {
            "violations": self.drc_violations,
            "schematic_parity": self.parity_violations if schematic_parity else [],
            "unconnected_items": self.unconnected,
        }
        output.write_text(json.dumps(report), encoding="utf-8")
        return report

    def netlist(self, schematic: Path, output: Path) -> str:
        self.calls.append("netlist")
        output.write_text(self.netlist_xml, encoding="utf-8")
        return self.netlist_xml

    def gerbers(self, pcb: Path, output_dir: Path) -> list[Path]:
        self.calls.append("gerbers")
        output_dir.mkdir(parents=True, exist_ok=True)
        names = ["board-F_Cu.gtl", "board-B_Cu.gbl", "board-Edge_Cuts.gm1", "board-F_Mask.gts"]
        for name in names:
            (output_dir / name).write_text("G04*\n", encoding="utf-8")
        return sorted(output_dir.iterdir())

    def drill(self, pcb: Path, output_dir: Path) -> list[Path]:
        self.calls.append("drill")
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "board.drl").write_text("M48\n", encoding="utf-8")
        return sorted(output_dir.iterdir())


def reply(findings: list[dict[str, Any]] | None = None, summary: str = "reviewed") -> str:
    payload = {"findings": findings or [], "summary": summary}
    return f"Here is my review.\n\n```json\n{json.dumps(payload)}\n```\n"


class FakeTurn:
    """TurnFn returning canned replies keyed by check id."""

    def __init__(self, replies: dict[str, Any] | None = None, default: str | None = None) -> None:
        self.replies = replies or {}
        self.default = default if default is not None else reply()
        self.turns: list[ReviewTurn] = []
        self.calls: dict[str, int] = {}

    async def __call__(self, turn: ReviewTurn) -> TurnResult:
        self.turns.append(turn)
        self.calls[turn.check_id] = self.calls.get(turn.check_id, 0) + 1
        scripted = self.replies.get(turn.check_id, self.default)
        if callable(scripted):
            scripted = scripted(turn, self.calls[turn.check_id])
        if isinstance(scripted, TurnResult):
            return scripted
        if isinstance(scripted, list):
            index = min(self.calls[turn.check_id], len(scripted)) - 1
            scripted = scripted[index]
        return TurnResult(text=str(scripted))


def finding(
    severity: str = "P1",
    title: str = "Something is wrong",
    *,
    refs: list[str] | None = None,
    nets: list[str] | None = None,
    confidence: str = "high",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "severity": severity,
        "title": title,
        "detail": "because",
        "locator": {"refs": refs or [], "nets": nets or []},
        "evidence": "datasheet §1",
        "rule": "rule",
        "suggested_action": "fix it",
        "confidence": confidence,
        **extra,
    }


def runner_for(
    workspace: Path,
    *,
    options: RunOptions | None = None,
    kicad: FakeKicad | None = None,
    turn: FakeTurn | None = None,
    config: ValidateConfig | None = None,
    credential: DeviceCredential | None = CREDENTIAL,
) -> Runner:
    kicad = kicad if kicad is not None else FakeKicad()
    return Runner(
        workspace,
        config or load_config(workspace),
        options or RunOptions(),
        turn=turn if turn is not None else FakeTurn(),
        kicad_finder=lambda: kicad,
        credential_loader=lambda: credential,
        snapshot_root=workspace.parent / "snapshots",
    )


def statuses(results: list[CheckResult]) -> dict[str, str]:
    return {result.check_id: result.status.value for result in results}


def by_id(results: list[CheckResult]) -> dict[str, CheckResult]:
    return {result.check_id: result for result in results}


# ----------------------------------------------------------------- config


def test_packaged_graph_lists_and_graphs(tmp_path, capsys) -> None:
    make_project(tmp_path)
    assert run_command(parse_args(["list", str(tmp_path)])) == 0
    out = capsys.readouterr().out
    assert "intake.project" in out and "S8" in out and "requires intake.tools" in out
    assert run_command(parse_args(["graph", str(tmp_path)])) == 0
    out = capsys.readouterr().out
    assert out.startswith("flowchart TD")
    assert "intake_tools --> intake_project" in out
    assert "intake_intent -.-> arch_review" in out


def test_default_graph_is_valid_and_covers_every_stage() -> None:
    config = build_config(packaged_default_raw())
    assert [stage.id for stage in config.stages] == [f"S{n}" for n in range(9)]
    stages_with_checks = {check.stage for check in config.checks}
    assert stages_with_checks == {stage.id for stage in config.stages}
    assert render_list(config) and render_graph(config)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            [
                {"id": "a", "stage": "S0", "kind": "builtin", "builtin": "erc", "requires": ["b"]},
                {"id": "b", "stage": "S0", "kind": "builtin", "builtin": "erc", "requires": ["a"]},
            ],
            "dependency cycle",
        ),
        (
            [{"id": "a", "stage": "S0", "kind": "builtin", "builtin": "erc", "requires": ["nope"]}],
            "unknown check 'nope'",
        ),
        (
            [
                {
                    "id": "a",
                    "stage": "S0",
                    "kind": "builtin",
                    "builtin": "erc",
                    "requires": ["sch.erc"],
                }
            ],
            "later stage S3",
        ),
        ([{"id": "a", "stage": "S1", "kind": "agent"}], "need a prompt"),
        (
            [{"id": "a", "stage": "S1", "kind": "agent", "scope": "per_part", "prompt": "x"}],
            "{items} placeholder",
        ),
        ([{"id": "a", "stage": "S1", "kind": "builtin", "builtin": "nope"}], "unknown builtin"),
        ([{"id": "a", "stage": "S9", "kind": "builtin", "builtin": "erc"}], "unknown stage"),
    ],
)
def test_invalid_graphs_name_the_offending_check(overrides, message) -> None:
    raw = merge_raw(packaged_default_raw(), {"checks": overrides})
    with pytest.raises(ConfigError, match=message) as excinfo:
        build_config(raw)
    assert "'a'" in str(excinfo.value) or "'b'" in str(excinfo.value)


def test_duplicate_check_ids_are_rejected() -> None:
    raw = packaged_default_raw()
    raw["checks"].append(dict(raw["checks"][0]))
    with pytest.raises(ConfigError, match="duplicate id"):
        build_config(raw)


def test_project_overrides_extend_default(tmp_path) -> None:
    make_project(tmp_path)
    (tmp_path / ".copperpilot" / "validate.yaml").write_text(
        yaml.safe_dump(
            {
                "extends": "default",
                "checks": [
                    {"id": "pcb.silkscreen", "enabled": False},
                    {"id": "sch.ratings", "repeat": 3, "effort": "intelligence"},
                    {
                        "id": "custom.note",
                        "stage": "S3",
                        "kind": "agent",
                        "prompt": "Check the notes",
                        "requires": ["sch.erc"],
                    },
                ],
                "limits": {"jlcpcb": {"min_track_mm": 0.2}},
            }
        ),
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config.check("pcb.silkscreen").enabled is False
    assert config.check("sch.ratings").repeat == 3
    assert config.check("sch.ratings").effort == "intelligence"
    assert config.check("custom.note").requires == ["sch.erc"]
    assert config.fab_limits()["min_track_mm"] == 0.2
    assert config.fab_limits()["min_clearance_mm"] == 0.127


def test_config_without_extends_replaces_the_graph(tmp_path) -> None:
    make_project(tmp_path)
    own = tmp_path / "own.yaml"
    own.write_text(
        yaml.safe_dump(
            {
                "stages": [{"id": "A", "title": "Only"}],
                "checks": [{"id": "x.erc", "stage": "A", "kind": "builtin", "builtin": "erc"}],
            }
        ),
        encoding="utf-8",
    )
    config = load_config(tmp_path, own)
    assert [check.id for check in config.checks] == ["x.erc"]


def test_invalid_yaml_and_bad_extends_exit_2(tmp_path, capsys) -> None:
    make_project(tmp_path)
    bad = tmp_path / "bad.yaml"
    bad.write_text("checks: [\n", encoding="utf-8")
    assert run_command(parse_args(["list", str(tmp_path), "--config", str(bad)])) == EXIT_USAGE
    assert "invalid YAML" in capsys.readouterr().err
    bad.write_text("extends: other\n", encoding="utf-8")
    assert run_command(parse_args(["list", str(tmp_path), "--config", str(bad)])) == EXIT_USAGE
    assert run_command(parse_args(["list", str(tmp_path), "--config", "/no/file.yaml"])) == 2


# ----------------------------------------------------------------- design


def test_index_parses_netlist_project_and_pcb() -> None:
    index = build_index(NETLIST, json.dumps(PROJECT), PCB)
    assert index.refs == {"R1", "R2", "C1", "U1", "H1", "R9"}
    assert index.net_names == {"+3V3", "GND", "SDA"}
    assert index.nets["+3V3"].net_class == "Power"
    assert index.layers == ["F.Cu", "B.Cu", "F.SilkS", "Edge.Cuts"]
    assert index.copper_layers == ["F.Cu", "B.Cu"]
    assert index.sheets == ["/", "/power/"]
    assert index.rules["min_track_width"] == 0.15
    dnp = next(part for part in index.parts if part.ref == "R9")
    assert dnp.dnp is True


def test_input_hashes_select_by_kind(tmp_path) -> None:
    make_project(tmp_path)
    assert set(input_hashes(tmp_path, ["sch"])) == {"board/board.kicad_sch"}
    assert set(input_hashes(tmp_path, ["pcb", "pro"])) == {
        "board/board.kicad_pcb",
        "board/board.kicad_pro",
    }
    assert ".copperpilot/README.md" in input_hashes(tmp_path, ["intent"])
    assert "board/board.kicad_sch" in input_hashes(tmp_path, ["all"])
    assert not input_hashes(tmp_path, ["libs"])


def test_snapshot_is_read_only_and_excludes_git_and_copperpilot(tmp_path) -> None:
    make_project(tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    (tmp_path / "board" / "board-backups").mkdir()
    (tmp_path / "board" / "board-backups" / "x.zip").write_bytes(b"zip")
    (tmp_path / "board" / "~board.kicad_sch.lck").write_text("", encoding="utf-8")
    snapshot = create_snapshot(tmp_path, tmp_path.parent / "snap" / "design")
    assert (snapshot / "board" / "board.kicad_sch").is_file()
    assert not (snapshot / ".git").exists()
    assert not (snapshot / ".copperpilot").exists()
    assert not (snapshot / "board" / "board-backups").exists()
    assert not (snapshot / "board" / "~board.kicad_sch.lck").exists()
    assert (snapshot / SCRATCH_DIR).is_dir()
    mode = (snapshot / "board" / "board.kicad_sch").stat().st_mode
    assert not mode & stat.S_IWUSR
    before = snapshot_hashes(snapshot)
    (snapshot / SCRATCH_DIR / "render.png").write_bytes(b"png")
    assert snapshot_changes(before, snapshot_hashes(snapshot)) == []
    (snapshot / "board" / "new.kicad_sch").write_text("x", encoding="utf-8")
    assert snapshot_changes(before, snapshot_hashes(snapshot)) == ["board/new.kicad_sch"]


# --------------------------------------------------------------- reviewer


def test_review_policy_only_allows_scratch_writes_and_safe_shell(tmp_path) -> None:
    make_project(tmp_path)
    snapshot = create_snapshot(tmp_path, tmp_path.parent / "snap2" / "design")
    policy = review_policy(snapshot)

    async def ask(name: str, **arguments: Any) -> bool:
        return await policy(
            ApprovalRequest("1", name, arguments, "Local side effect", canonical_name=name)
        )

    import asyncio

    assert asyncio.run(ask("write_file", file_path=f"/{SCRATCH_DIR}/render.png"))
    assert asyncio.run(ask("write_file", file_path=str(snapshot / SCRATCH_DIR / "a.txt")))
    assert not asyncio.run(ask("write_file", file_path="/board/board.kicad_sch"))
    assert not asyncio.run(ask("edit_file", file_path=f"/{SCRATCH_DIR}/../board/board.kicad_pcb"))
    assert not asyncio.run(ask("delete", file_path=f"/{SCRATCH_DIR}/a.txt"))
    assert asyncio.run(ask("execute", command="kicad-cli pcb render board/board.kicad_pcb"))
    assert not asyncio.run(ask("execute", command="rm -rf board"))
    assert not asyncio.run(ask("unknown_tool"))


@pytest.mark.asyncio
async def test_broker_rejects_design_writes_and_questions(tmp_path) -> None:
    make_project(tmp_path)
    snapshot = create_snapshot(tmp_path, tmp_path.parent / "snap3" / "design")
    broker = make_broker(snapshot)
    broker.begin_turn()
    with pytest.raises(ToolRejected, match="rejected"):
        await broker.execute(
            ToolRequest(
                tool_call_id="w",
                tool_name="write_file",
                arguments={"file_path": "board/board.kicad_sch", "content": "(kicad_sch)"},
            )
        )
    assert (
        (snapshot / "board" / "board.kicad_sch")
        .read_text(encoding="utf-8")
        .startswith("(kicad_sch (version")
    )
    with pytest.raises(ToolRejected, match=QUESTION_REJECTION[:20]):
        await broker.execute(
            ToolRequest(tool_call_id="q", tool_name="ask_user_question", arguments={})
        )
    result = await broker.execute(
        ToolRequest(
            tool_call_id="s",
            tool_name="write_file",
            arguments={"file_path": f"{SCRATCH_DIR}/notes.md", "content": "ok"},
        )
    )
    assert "error" not in result
    assert (snapshot / SCRATCH_DIR / "notes.md").read_text(encoding="utf-8") == "ok"


def test_extract_last_json_block_and_parse() -> None:
    text = (
        '```json\n{"findings": []}\n```\nthen\n```json\n'
        + json.dumps({"findings": [finding("P0", refs=["U1"])], "summary": "s"})
        + "\n```"
    )
    assert json.loads(extract_json_block(text) or "")["summary"] == "s"
    findings, summary = parse_reply(text)
    assert summary == "s" and findings[0].severity is Severity.P0
    with pytest.raises(ValueError, match="no fenced json block"):
        parse_reply("no json here")
    with pytest.raises(ValueError, match="invalid JSON"):
        parse_reply("```json\n{oops}\n```")
    with pytest.raises(ValueError, match="findings list"):
        parse_reply('```json\n{"summary": 1}\n```')
    # Markdown-escaped characters inside strings are tolerated.
    lenient = (
        '```json\n{"findings": [{"title": "C = 22 uF \\* 0.8", "detail": "a\\_b"}],'
        ' "summary": "ok"}\n```'
    )
    findings, _ = parse_reply(lenient)
    assert findings[0].title == "C = 22 uF * 0.8" and findings[0].detail == "a_b"


def test_fan_out_collapses_identical_parts_and_skips_dnp() -> None:
    index = build_index(NETLIST, json.dumps(PROJECT), PCB)
    config = build_config(packaged_default_raw())
    items = fan_out_items(config.check("parts.footprints"), index)
    assert len(items) == 4  # R1+R2 collapsed, C1, U1, H1; R9 is DNP
    assert any(item.startswith("- R1, R2:") and "C25804" in item for item in items)
    sheets = fan_out_items(config.check("sch.conventions"), index)
    assert sheets == ["- sheet /", "- sheet /power/"]
    classes = fan_out_items(config.check("pcb.signal_integrity"), index)
    assert len(classes) == 2 and any("Power" in item and "+3V3" in item for item in classes)


def test_consensus_keeps_majority_by_locator() -> None:
    shared = Finding(title="Pin swap", severity=Severity.P0, locator=Locator(refs=["U1"]))
    runs = [
        [shared, Finding(title="Only once", locator=Locator(nets=["SDA"]))],
        [Finding(title="pin swap seen again", severity=Severity.P1, locator=Locator(refs=["U1"]))],
        [Finding(title="Pin Swap", severity=Severity.P2, locator=Locator(refs=["u1"]))],
    ]
    kept = consensus(runs)
    assert [item.title for item in kept] == ["Pin swap"]
    assert kept[0].severity is Severity.P0
    assert consensus([[shared]]) == [shared]
    assert consensus([]) == []


# ----------------------------------------------------------------- runner


@pytest.mark.asyncio
async def test_clean_design_passes_with_exit_0(tmp_path) -> None:
    make_project(tmp_path)
    turn = FakeTurn()
    runner = runner_for(tmp_path, options=RunOptions(gate=GateMode.FULL), turn=turn)
    outcome = await runner.run()
    assert outcome.verdict == "PASS"
    assert outcome.exit_code == EXIT_PASS
    results = by_id(outcome.results)
    assert results["intake.project"].status is Status.PASS
    assert results["parts.bom_fields"].status is Status.WARN  # C1 has no LCSC/MPN
    assert results["pcb.rules"].status is Status.WARN  # min_clearance unset is a P2
    assert (outcome.run_dir / "REPORT.md").is_file()
    assert (outcome.run_dir / "next_actions.json").is_file()
    assert json.loads((outcome.run_dir / "report.json").read_text())["schema"] == (
        "copper-pilot.validate/v1"
    )


@pytest.mark.asyncio
async def test_stage_gate_halts_after_failing_stage_but_runs_siblings(tmp_path) -> None:
    make_project(tmp_path)
    kicad = FakeKicad(
        erc=[
            {
                "severity": "error",
                "type": "pin_not_connected",
                "description": "Pin 3 of U1 unconnected",
                "items": [{"description": "Symbol U1"}],
            }
        ]
    )
    turn = FakeTurn()
    runner = runner_for(
        tmp_path,
        options=RunOptions(skip=["pcb.rules"]),
        kicad=kicad,
        turn=turn,
    )
    outcome = await runner.run()
    results = by_id(outcome.results)
    assert results["sch.erc"].status is Status.FAIL
    assert results["sch.erc"].findings[0].locator.refs == ["U1"]
    # Siblings in S3 that do not require sch.erc still ran.
    assert results["sch.conventions"].status is Status.PASS
    assert results["sch.ratings"].status is Status.PASS
    assert results["sch.circuits"].status is Status.SKIPPED
    assert results["sch.circuits"].blocked_by == ["sch.erc"]
    for later in ("pcb.parity", "pcb.placement", "pcb.drc", "release.outputs"):
        assert results[later].status is Status.SKIPPED
        assert results[later].blocked_by == ["stage gate: S3 (sch.erc)"]
    assert results["pcb.rules"].blocked_by == ["--skip"]
    assert outcome.exit_code == EXIT_FAIL
    assert outcome.next_actions["stage"] == "S3"
    assert "drc" not in kicad.calls


@pytest.mark.asyncio
async def test_full_gate_skips_only_transitive_dependents(tmp_path) -> None:
    make_project(tmp_path)
    kicad = FakeKicad(
        parity=[
            {
                "severity": "error",
                "type": "missing_footprint",
                "description": "Missing footprint for U1",
                "items": [],
            }
        ]
    )
    runner = runner_for(tmp_path, options=RunOptions(gate=GateMode.FULL), kicad=kicad)
    outcome = await runner.run()
    results = by_id(outcome.results)
    assert results["pcb.parity"].status is Status.FAIL
    assert results["pcb.placement"].status is Status.SKIPPED  # requires pcb.parity
    assert results["pcb.placement"].blocked_by == ["pcb.parity"]
    assert results["pcb.power_routing"].status is Status.SKIPPED
    assert results["pcb.drc"].status is not Status.SKIPPED
    assert results["dfm.fab"].status is Status.PASS
    assert results["release.cpl"].status is Status.PASS
    assert outcome.exit_code == EXIT_FAIL


@pytest.mark.asyncio
async def test_strict_gate_stops_after_first_failing_wave(tmp_path) -> None:
    make_project(tmp_path)
    kicad = FakeKicad(
        erc=[{"severity": "error", "type": "pin_not_connected", "description": "x", "items": []}]
    )
    runner = runner_for(tmp_path, options=RunOptions(gate=GateMode.STRICT), kicad=kicad)
    outcome = await runner.run()
    results = by_id(outcome.results)
    assert results["sch.erc"].status is Status.FAIL
    assert results["sch.conventions"].status is Status.PASS  # same wave as sch.erc
    assert results["sch.ratings"].blocked_by == ["wave gate: S3 (sch.erc)"]  # next wave
    assert results["sch.circuits"].blocked_by == ["sch.erc"]  # hard prerequisite failed
    assert results["pcb.parity"].blocked_by == ["wave gate: S3 (sch.erc)"]
    assert outcome.exit_code == EXIT_FAIL


@pytest.mark.asyncio
async def test_unsupported_kicad_version_fails_intake(tmp_path) -> None:
    make_project(tmp_path)
    runner = runner_for(tmp_path, options=RunOptions(until="S0"), kicad=FakeKicad(version="8.0.1"))
    outcome = await runner.run()
    results = by_id(outcome.results)
    assert results["intake.tools"].status is Status.FAIL
    assert results["intake.project"].blocked_by == ["intake.tools"]
    assert results["intake.intent"].status is Status.PASS  # advisory sibling still ran
    assert outcome.next_actions["actions"][0]["title"].startswith("kicad-cli 8.0.1")


@pytest.mark.asyncio
async def test_missing_kicad_fails_intake_and_skips_everything(tmp_path) -> None:
    make_project(tmp_path)
    runner = Runner(
        tmp_path,
        load_config(tmp_path),
        RunOptions(),
        turn=FakeTurn(),
        kicad_finder=lambda: None,
        credential_loader=lambda: CREDENTIAL,
    )
    outcome = await runner.run()
    results = by_id(outcome.results)
    assert results["intake.tools"].status is Status.FAIL
    assert results["sch.erc"].status is Status.SKIPPED
    assert outcome.exit_code == EXIT_FAIL
    assert outcome.next_actions["actions"][0]["title"] == "kicad-cli was not found"


@pytest.mark.asyncio
async def test_ungrounded_finding_never_fails_a_check(tmp_path) -> None:
    make_project(tmp_path)
    turn = FakeTurn(
        {
            "arch.review": reply(
                [finding("P0", "Ghost part", refs=["U99"]), finding("P2", "Odd net", nets=["NOPE"])]
            )
        }
    )
    runner = runner_for(tmp_path, options=RunOptions(until="S1"), turn=turn)
    outcome = await runner.run()
    result = by_id(outcome.results)["arch.review"]
    assert result.status is Status.WARN
    assert all(not item.grounded for item in result.findings)
    assert result.findings[0].title.startswith("[UNGROUNDED: unknown refs U99] Ghost part")
    assert result.findings[0].id == "arch.review/001"
    assert outcome.next_actions["actions"] == []


# KiCad names local and hierarchical label nets with their sheet path.
HIERARCHICAL_NETLIST = (
    NETLIST.replace('name="+3V3"', 'name="/power/+3V3"')
    .replace('name="GND"', 'name="/GND"')
    .replace('name="SDA"', 'name="/RAIL_IO_3"')
)


@pytest.mark.asyncio
async def test_findings_citing_bare_label_names_stay_grounded_on_hierarchical_nets(
    tmp_path,
) -> None:
    make_project(tmp_path)
    turn = FakeTurn(
        {
            "arch.review": reply(
                [
                    finding("P0", "Regulator contention", refs=["U1"], nets=["RAIL_IO_3", "GND"]),
                    finding("P1", "Rail on sub-sheet", refs=["C1"], nets=["+3V3"]),
                    finding("P1", "Full path cited", refs=["C1"], nets=["/power/+3V3"]),
                    finding("P2", "Leading slash only", refs=["U1"], nets=["/RAIL_IO_3"]),
                    finding("P2", "Invented net", refs=["U1"], nets=["IO_3"]),
                ]
            )
        }
    )
    kicad = FakeKicad(netlist=HIERARCHICAL_NETLIST)
    runner = runner_for(tmp_path, options=RunOptions(until="S1"), kicad=kicad, turn=turn)
    outcome = await runner.run()
    result = by_id(outcome.results)["arch.review"]
    grounded = {item.title: item.grounded for item in result.findings}
    assert grounded["Regulator contention"] is True
    assert grounded["Rail on sub-sheet"] is True
    assert grounded["Full path cited"] is True
    assert grounded["Leading slash only"] is True
    assert grounded["[UNGROUNDED: unknown nets IO_3] Invented net"] is False
    assert result.status is Status.FAIL


@pytest.mark.asyncio
async def test_grounded_p0_fails_and_feeds_next_actions(tmp_path) -> None:
    make_project(tmp_path)
    turn = FakeTurn(
        {
            "arch.review": reply(
                [
                    finding("P2", "Minor", refs=["R1"]),
                    finding("P0", "No bulk cap on +3V3", refs=["U1"], nets=["+3V3"]),
                    finding("P1", "Low confidence guess", refs=["C1"], confidence="low"),
                ]
            )
        }
    )
    runner = runner_for(tmp_path, turn=turn)
    outcome = await runner.run()
    result = by_id(outcome.results)["arch.review"]
    assert result.status is Status.FAIL
    assert [item.severity.value for item in result.findings] == ["P0", "P1", "P2"]
    actions = outcome.next_actions
    assert actions["stage"] == "S1" and actions["stage_title"] == "Architecture"
    assert [item["severity"] for item in actions["actions"]] == ["P0", "P1", "P2"]
    assert actions["actions"][0]["id"] == "arch.review/001"
    assert actions["actions"][0]["locator"] == {"refs": ["U1"], "nets": ["+3V3"]}
    assert actions["rerun"] == "copper-pilot validate --until S1"
    assert "sch.circuits" in actions["checks_blocked_downstream"]
    assert outcome.exit_code == EXIT_FAIL


@pytest.mark.asyncio
async def test_next_actions_only_cover_the_earliest_failing_stage(tmp_path) -> None:
    make_project(tmp_path)
    kicad = FakeKicad(
        drc=[{"severity": "error", "type": "clearance", "description": "x", "items": []}]
    )
    turn = FakeTurn({"arch.review": reply([finding("P1", "Arch issue", refs=["U1"])])})
    runner = runner_for(tmp_path, options=RunOptions(gate=GateMode.FULL), kicad=kicad, turn=turn)
    outcome = await runner.run()
    assert by_id(outcome.results)["pcb.drc"].status is Status.FAIL
    assert outcome.next_actions["stage"] == "S1"
    assert {item["check"] for item in outcome.next_actions["actions"]} == {"arch.review"}


@pytest.mark.asyncio
async def test_per_part_fan_out_batches_and_collapses(tmp_path) -> None:
    make_project(tmp_path)
    turn = FakeTurn()
    config = load_config(tmp_path)
    config.check("parts.footprints").batch = 1
    runner = runner_for(
        tmp_path,
        options=RunOptions(only=["parts.footprints"]),
        turn=turn,
        config=config,
    )
    outcome = await runner.run()
    slices = [item for item in turn.turns if item.check_id == "parts.footprints"]
    assert len(slices) == 4
    prompts = "\n".join(item.prompt for item in slices)
    assert "- R1, R2:" in prompts and "R9" not in prompts
    assert "Your single responsibility: Footprints match" in prompts
    assert "## Output contract" in prompts
    assert "## Design intent" in prompts
    result = by_id(outcome.results)["parts.footprints"]
    assert len(result.threads) == 4
    assert outcome.exit_code == EXIT_INCOMPLETE  # --only leaves other checks SKIPPED
    artifacts = outcome.run_dir / "checks" / "parts.footprints"
    assert (artifacts / "prompt-0-0-1.md").is_file() and (artifacts / "reply-3-0-1.md").is_file()


@pytest.mark.asyncio
async def test_consensus_runs_keep_majority_findings(tmp_path) -> None:
    make_project(tmp_path)
    a = finding("P1", "Shared", refs=["U1"])
    b = finding("P1", "Lone", refs=["C1"])
    turn = FakeTurn({"sch.ratings": [reply([a, b]), reply([a]), reply([])]})
    config = load_config(tmp_path)
    config.check("sch.ratings").repeat = 3
    runner = runner_for(
        tmp_path, options=RunOptions(only=["sch.ratings"]), turn=turn, config=config
    )
    outcome = await runner.run()
    result = by_id(outcome.results)["sch.ratings"]
    assert turn.calls["sch.ratings"] == 3
    assert [item.title for item in result.findings] == ["Shared"]
    assert len(result.threads) == 3


@pytest.mark.asyncio
async def test_repeat_flag_overrides_graph(tmp_path) -> None:
    make_project(tmp_path)
    turn = FakeTurn()
    runner = runner_for(tmp_path, options=RunOptions(only=["sch.ratings"], repeat=2), turn=turn)
    await runner.run()
    assert turn.calls["sch.ratings"] == 2


@pytest.mark.asyncio
async def test_repair_turns_then_error(tmp_path) -> None:
    make_project(tmp_path)
    turn = FakeTurn({"sch.ratings": ["no json", "still no json", reply([])]})
    runner = runner_for(tmp_path, options=RunOptions(only=["sch.ratings"]), turn=turn)
    outcome = await runner.run()
    assert by_id(outcome.results)["sch.ratings"].status is Status.PASS
    assert turn.calls["sch.ratings"] == 3
    assert "corrected json block" in turn.turns[-1].prompt

    turn = FakeTurn({"sch.ratings": ["nope", "nope", "nope"]})
    runner = runner_for(
        tmp_path, options=RunOptions(only=["sch.ratings"], no_cache=True), turn=turn
    )
    outcome = await runner.run()
    result = by_id(outcome.results)["sch.ratings"]
    assert result.status is Status.ERROR
    assert "2 repair turns" in (result.error or "")
    assert outcome.exit_code == EXIT_FAIL


@pytest.mark.asyncio
async def test_cache_reuse_and_invalidation(tmp_path) -> None:
    make_project(tmp_path)
    kicad = FakeKicad()
    turn = FakeTurn()
    options = RunOptions(gate=GateMode.FULL)
    first = await runner_for(tmp_path, options=options, kicad=kicad, turn=turn).run()
    assert not any(result.cached for result in first.results)
    kicad.calls.clear()
    turn.calls.clear()
    second = await runner_for(tmp_path, options=options, kicad=kicad, turn=turn).run()
    cached = {r.check_id for r in second.results if r.cached}
    assert "sch.erc" in cached and "arch.review" in cached and "pcb.drc" in cached
    assert "intake.project" not in cached
    assert turn.calls == {}
    assert "erc" not in kicad.calls and "drc" not in kicad.calls

    # A PCB-only change re-runs only PCB-dependent checks.
    (tmp_path / "board" / "board.kicad_pcb").write_text(PCB + "\n", encoding="utf-8")
    kicad.calls.clear()
    third = await runner_for(tmp_path, options=options, kicad=kicad, turn=turn).run()
    rerun = {r.check_id for r in third.results if not r.cached and r.status != Status.SKIPPED}
    assert "pcb.drc" in rerun and "pcb.placement" in rerun and "parts.footprints" in rerun
    assert "sch.erc" not in rerun and "arch.review" not in rerun and "sch.ratings" not in rerun
    assert "erc" not in kicad.calls and "drc" in kicad.calls

    fourth = await runner_for(
        tmp_path, options=RunOptions(gate=GateMode.FULL, no_cache=True), kicad=kicad, turn=turn
    ).run()
    assert not any(result.cached for result in fourth.results)


@pytest.mark.asyncio
async def test_error_results_are_never_cached(tmp_path) -> None:
    make_project(tmp_path)
    turn = FakeTurn({"sch.ratings": ["nope"] * 3})
    options = RunOptions(only=["sch.ratings"])
    first = await runner_for(tmp_path, options=options, turn=turn).run()
    assert by_id(first.results)["sch.ratings"].status is Status.ERROR
    turn.replies["sch.ratings"] = reply([])
    second = await runner_for(tmp_path, options=options, turn=turn).run()
    result = by_id(second.results)["sch.ratings"]
    assert result.status is Status.PASS and result.cached is False


@pytest.mark.asyncio
async def test_reviewer_that_modifies_the_snapshot_is_an_error(tmp_path) -> None:
    make_project(tmp_path)

    def tamper(turn: ReviewTurn, _call: int) -> str:
        target = turn.snapshot / "board" / "board.kicad_sch"
        os.chmod(target, stat.S_IWUSR | stat.S_IRUSR)
        target.write_text("(kicad_sch tampered)", encoding="utf-8")
        (turn.snapshot / SCRATCH_DIR / "render.png").write_bytes(b"png")
        return reply([])

    turn = FakeTurn({"sch.ratings": tamper})
    runner = runner_for(tmp_path, options=RunOptions(only=["sch.ratings"]), turn=turn)
    outcome = await runner.run()
    result = by_id(outcome.results)["sch.ratings"]
    assert result.status is Status.ERROR
    assert result.error == "reviewer modified the snapshot: board/board.kicad_sch"
    # The real workspace is untouched.
    assert (
        (tmp_path / "board" / "board.kicad_sch")
        .read_text(encoding="utf-8")
        .startswith("(kicad_sch (version")
    )
    assert runner.snapshot is None  # cleaned up


@pytest.mark.asyncio
async def test_release_profile_fails_on_p2_and_low_confidence(tmp_path) -> None:
    make_project(tmp_path)
    turn = FakeTurn({"arch.review": reply([finding("P2", "Nit", refs=["R1"], confidence="low")])})
    dev = await runner_for(tmp_path, options=RunOptions(until="S1"), turn=turn).run()
    assert by_id(dev.results)["arch.review"].status is Status.WARN
    release = await runner_for(
        tmp_path, options=RunOptions(until="S1", profile="release"), turn=turn
    ).run()
    assert by_id(release.results)["arch.review"].status is Status.FAIL
    assert release.report["profile"] == "release"


@pytest.mark.asyncio
async def test_advisory_checks_never_fail(tmp_path) -> None:
    make_project(tmp_path)
    turn = FakeTurn({"pcb.silkscreen": reply([finding("P0", "Tiny text", refs=["R1"])])})
    outcome = await runner_for(
        tmp_path, options=RunOptions(only=["pcb.silkscreen"]), turn=turn
    ).run()
    assert by_id(outcome.results)["pcb.silkscreen"].status is Status.WARN
    assert outcome.next_actions["actions"] == []


@pytest.mark.asyncio
async def test_usage_limit_stops_scheduling_and_exits_4(tmp_path) -> None:
    make_project(tmp_path)
    blocked = TurnResult(
        error={
            "message": "Usage limit reached.",
            "code": "http_402",
            "status": 402,
            "block_reason": "weekly",
            "retryable": False,
        }
    )
    turn = FakeTurn({"arch.review": blocked})
    runner = runner_for(tmp_path, options=RunOptions(gate=GateMode.FULL), turn=turn)
    outcome = await runner.run()
    results = by_id(outcome.results)
    assert results["arch.review"].status is Status.ERROR
    assert results["arch.review"].error == "Usage limit reached."
    assert results["parts.lcsc"].status is Status.SKIPPED
    assert results["parts.lcsc"].blocked_by == ["usage limit: weekly"]
    assert results["parts.bom_fields"].status is not Status.SKIPPED  # builtins still run
    assert turn.calls == {"arch.review": 1}
    assert outcome.exit_code == EXIT_ENVIRONMENT
    assert outcome.report["usage_limit"]["block_reason"] == "weekly"


@pytest.mark.asyncio
async def test_retryable_rate_limit_does_not_halt(tmp_path) -> None:
    make_project(tmp_path)
    busy = TurnResult(
        error={"message": "busy", "status": 429, "block_reason": "busy", "retryable": True}
    )
    turn = FakeTurn({"arch.review": busy})
    outcome = await runner_for(tmp_path, options=RunOptions(gate=GateMode.FULL), turn=turn).run()
    results = by_id(outcome.results)
    assert results["arch.review"].status is Status.ERROR
    assert results["parts.lcsc"].status is Status.PASS
    assert outcome.exit_code == EXIT_FAIL


@pytest.mark.asyncio
async def test_not_logged_in_exits_4_before_running(tmp_path) -> None:
    make_project(tmp_path)
    runner = Runner(
        tmp_path,
        load_config(tmp_path),
        RunOptions(),
        kicad_finder=lambda: FakeKicad(),
        credential_loader=lambda: None,
    )
    from copper_pilot_cli.validate.runner import EnvironmentError_

    with pytest.raises(EnvironmentError_, match="auth login"):
        await runner.run()
    # Builtin-only selections do not need a login.
    runner = Runner(
        tmp_path,
        load_config(tmp_path),
        RunOptions(only=["sch.erc"]),
        kicad_finder=lambda: FakeKicad(),
        credential_loader=lambda: None,
    )
    outcome = await runner.run()
    assert outcome.exit_code == EXIT_INCOMPLETE


@pytest.mark.asyncio
async def test_until_and_only_exit_3_when_clean(tmp_path) -> None:
    make_project(tmp_path)
    outcome = await runner_for(tmp_path, options=RunOptions(until="S0")).run()
    assert outcome.verdict == "INCOMPLETE"
    assert outcome.exit_code == EXIT_INCOMPLETE
    assert by_id(outcome.results)["arch.review"].blocked_by == ["--until S0"]
    with pytest.raises(ConfigError, match="unknown stage"):
        await runner_for(tmp_path, options=RunOptions(until="S42")).run()
    with pytest.raises(ConfigError, match="unknown check"):
        await runner_for(tmp_path, options=RunOptions(only=["nope"])).run()


@pytest.mark.asyncio
async def test_context_files_reach_every_reviewer(tmp_path) -> None:
    make_project(tmp_path)
    checklist = tmp_path / "fab.md"
    checklist.write_text("Fab checklist: 4 layers max", encoding="utf-8")
    turn = FakeTurn()
    outcome = await runner_for(
        tmp_path, options=RunOptions(only=["sch.ratings"], context_files=[checklist]), turn=turn
    ).run()
    assert "## Reference material" in turn.turns[0].prompt
    assert "Fab checklist: 4 layers max" in turn.turns[0].prompt
    assert outcome.exit_code == EXIT_INCOMPLETE
    with pytest.raises(ConfigError, match="context file not found"):
        await runner_for(
            tmp_path, options=RunOptions(context_files=[tmp_path / "missing.md"])
        ).run()


@pytest.mark.asyncio
async def test_json_events_and_junit(tmp_path) -> None:
    make_project(tmp_path)
    events: list[dict[str, Any]] = []
    junit = tmp_path / "out" / "junit.xml"
    kicad = FakeKicad(
        erc=[
            {
                "severity": "warning",
                "type": "lib_symbol_mismatch",
                "description": "Symbol differs",
                "items": [],
            }
        ]
    )
    turn = FakeTurn({"arch.review": reply([finding("P1", "Bad rail", refs=["U1"])])})
    options = RunOptions(until="S3", junit_path=junit, on_event=events.append)
    outcome = await runner_for(tmp_path, options=options, kicad=kicad, turn=turn).run()
    kinds = [event["type"] for event in events]
    assert kinds[0] == "validate.start" and kinds[-1] == "validate.end"
    assert "validate.check" in kinds and "validate.result" in kinds
    assert events[0]["data"]["run_id"] == outcome.run_dir.name
    assert events[-1]["data"]["exit_code"] == EXIT_FAIL
    for event in events:
        json.dumps(event)
    tree = ET.parse(junit)  # noqa: S314
    cases = {case.get("name"): case for case in tree.getroot().iter("testcase")}
    assert cases["arch.review"].find("failure") is not None
    assert cases["sch.erc"].find("skipped") is not None
    assert "stage gate: S1" in (cases["sch.erc"].find("skipped").get("message") or "")
    assert cases["intake.project"].find("failure") is None
    assert cases["parts.bom_fields"].find("skipped") is not None
    assert tree.getroot().get("failures") == "1"


def test_junit_maps_warn_and_error() -> None:
    report = {
        "results": [
            CheckResult(
                check_id="a",
                stage="S0",
                title="a",
                status=Status.WARN,
                gate="blocking",
                findings=[Finding(id="a/001", title="hm")],
            ).model_dump(mode="json"),
            CheckResult(
                check_id="b",
                stage="S0",
                title="b",
                status=Status.ERROR,
                gate="blocking",
                error="boom",
            ).model_dump(mode="json"),
        ]
    }
    xml = render_junit(report)
    root = ET.fromstring(xml)  # noqa: S314
    cases = {case.get("name"): case for case in root.iter("testcase")}
    assert cases["a"].find("system-out").text.startswith("a/001")
    assert cases["b"].find("error").get("message") == "boom"


def test_fix_prompt_cites_ids_and_rules() -> None:
    config = build_config(packaged_default_raw())
    results = [
        CheckResult(
            check_id="sch.erc",
            stage="S3",
            title="ERC",
            status=Status.FAIL,
            gate="blocking",
            findings=[
                Finding(
                    id="sch.erc/001",
                    severity=Severity.P1,
                    title="pin_not_connected",
                    locator=Locator(refs=["U1"]),
                    evidence="erc.json",
                    rule="ERC",
                    suggested_action="Connect it",
                )
            ],
        ),
        CheckResult(
            check_id="pcb.drc",
            stage="S7",
            title="DRC",
            status=Status.SKIPPED,
            gate="blocking",
            blocked_by=["stage gate: S3 (sch.erc)"],
        ),
    ]
    actions = next_actions(results, config)
    assert actions["checks_blocked_downstream"] == ["pcb.drc"]
    text = fix_prompt(actions)
    assert "[sch.erc/001] P1 pin_not_connected" in text
    assert "refs=['U1']" in text
    assert "Do not loosen design rules" in text
    assert "copper-pilot validate --until S3" in text
    assert fix_prompt(next_actions([], config)) == "Validation passed; nothing to fix."


# --------------------------------------------------------------- builtins


@pytest.mark.asyncio
async def test_builtin_findings_group_and_map_severities(tmp_path) -> None:
    make_project(tmp_path, power_track=0.1)
    kicad = FakeKicad(
        drc=[
            {
                "severity": "error",
                "type": "clearance",
                "description": "a",
                "items": [{"description": "Track [SDA] on F.Cu", "pos": {"x": 1.0, "y": 2.0}}],
            },
            {
                "severity": "error",
                "type": "clearance",
                "description": "b",
                "items": [{"description": "Pad 1 [GND] of U1 on F.Cu"}],
            },
            {"severity": "warning", "type": "silk_over_copper", "description": "c", "items": []},
            {"severity": "excluded", "type": "courtyard", "description": "d", "items": []},
        ],
        unconnected=[
            {
                "severity": "error",
                "type": "unconnected_items",
                "description": "u",
                "items": [{"description": "Pad 2 [SDA] of R1"}],
            }
        ],
    )
    outcome = await runner_for(
        tmp_path,
        options=RunOptions(
            only=["pcb.drc", "pcb.parity", "release.outputs", "release.repo", "pcb.rules"],
            gate=GateMode.FULL,
        ),
        kicad=kicad,
    ).run()
    results = by_id(outcome.results)
    drc = {item.title: item for item in results["pcb.drc"].findings}
    assert set(drc) == {"clearance: 2 violation(s)", "silk_over_copper: 1 violation(s)"}
    assert drc["clearance: 2 violation(s)"].severity is Severity.P1
    assert drc["clearance: 2 violation(s)"].locator.nets == ["SDA", "GND"]
    assert drc["clearance: 2 violation(s)"].locator.refs == ["U1"]
    assert drc["clearance: 2 violation(s)"].locator.x_mm == 1.0
    assert drc["silk_over_copper: 1 violation(s)"].severity is Severity.P2
    parity = results["pcb.parity"].findings
    assert parity[0].severity is Severity.P0 and parity[0].locator.refs == ["R1"]
    rules = {item.title for item in results["pcb.rules"].findings}
    assert "Net class Power track width 0.1 mm is below the fab limit 0.127 mm" in rules
    assert "Board minimum clearance is not set" in rules
    assert results["release.outputs"].status is Status.PASS
    assert results["release.repo"].status is Status.PASS


@pytest.mark.asyncio
async def test_bom_fields_and_repo_paths(tmp_path) -> None:
    make_project(tmp_path)
    (tmp_path / "board" / "fp-lib-table").write_text(
        '(fp_lib_table (lib (name "x")(uri "/home/someone/libs/x.pretty")))', encoding="utf-8"
    )
    outcome = await runner_for(
        tmp_path, options=RunOptions(only=["parts.bom_fields", "release.repo"])
    ).run()
    results = by_id(outcome.results)
    bom = {item.title: item for item in results["parts.bom_fields"].findings}
    assert "1 part(s) missing MPN" in bom and bom["1 part(s) missing MPN"].locator.refs == ["C1"]
    assert "1 part(s) missing LCSC" in bom
    assert "1 part(s) missing Datasheet" in bom
    assert results["parts.bom_fields"].status is Status.WARN
    repo = results["release.repo"].findings
    assert repo[0].severity is Severity.P1 and "Absolute user paths" in repo[0].title
    assert any("library table entries outside" in item.title for item in repo)
    assert results["release.repo"].status is Status.FAIL


def test_library_uris_outside_the_project_are_platform_independent() -> None:
    assert _library_uri_outside_project("/home/someone/libs/x.pretty")
    assert _library_uri_outside_project("C:/Users/someone/libs/x.pretty")
    assert _library_uri_outside_project(r"C:\Users\someone\libs\x.pretty")
    assert _library_uri_outside_project("~/libs/x.pretty")
    assert not _library_uri_outside_project("${KIPRJMOD}/libs/x.pretty")
    assert not _library_uri_outside_project("libs/x.pretty")


@pytest.mark.asyncio
async def test_repo_check_scopes_to_the_project_folder(tmp_path) -> None:
    make_project(tmp_path)
    generated = tmp_path / "video" / "tmp_pcbs"
    generated.mkdir(parents=True)
    (generated / "frame.kicad_pcb").write_text('(kicad_pcb (path "/home/x/y"))', encoding="utf-8")
    build = tmp_path / "build" / "out.kicad_pcb"
    build.parent.mkdir()
    build.write_text('(kicad_pcb (path "/home/x/y"))', encoding="utf-8")
    outcome = await runner_for(tmp_path, options=RunOptions(only=["release.repo"])).run()
    assert by_id(outcome.results)["release.repo"].status is Status.PASS
    assert "build/out.kicad_pcb" not in input_hashes(tmp_path, ["all"])
    assert "video/tmp_pcbs/frame.kicad_pcb" in input_hashes(tmp_path, ["all"])


@pytest.mark.asyncio
async def test_kicad_failures_become_errors(tmp_path) -> None:
    make_project(tmp_path, pcb=False)

    class Broken(FakeKicad):
        def erc(self, schematic: Path, output: Path) -> dict[str, Any]:
            raise KicadError("kicad-cli sch erc did not write erc.json")

    outcome = await runner_for(
        tmp_path,
        options=RunOptions(only=["sch.erc", "pcb.drc"], gate=GateMode.FULL),
        kicad=Broken(),
    ).run()
    results = by_id(outcome.results)
    assert results["intake.project"].status is Status.WARN  # no PCB is a P2
    assert results["sch.erc"].status is Status.ERROR
    assert "did not write" in (results["sch.erc"].error or "")
    assert results["pcb.drc"].status is Status.ERROR
    assert outcome.exit_code == EXIT_FAIL
    assert outcome.next_actions["actions"][0]["id"] == "sch.erc/error"


# ----------------------------------------------------------- hosted turn


class FakeClient:
    """A HostedChatClient stand-in that answers with a scripted reply."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[Any] = []
        self.first_frame_timeout = 60.0
        self.idle_frame_timeout = None

    async def stream(self, request, tool_handler):
        self.requests.append(request)
        request_write = ToolRequest(
            tool_call_id="t1",
            tool_name="write_file",
            arguments={"file_path": "board/board.kicad_sch", "content": "bad"},
        )
        try:
            result = await tool_handler(request_write)
        except Exception as exc:  # the real client reports rejections this way  # noqa: BLE001
            result = {"error": f"{type(exc).__name__}: {exc}"}
        assert "ToolRejected" in result["error"]
        yield CopperEvent(EventKind.TEXT, self.text)
        yield CopperEvent(EventKind.FINAL, {"success": True})

    async def cancel(self) -> None:
        return None


@pytest.mark.asyncio
async def test_hosted_turn_runner_uses_agent_mode_and_read_only_broker(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("COPPER_PILOT_CLI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("copper_pilot_cli.sessions._db_path", None)
    make_project(tmp_path)
    client = FakeClient(reply([finding("P1", "Rail", refs=["U1"])], "done"))
    turn = HostedTurnRunner(CREDENTIAL, client_factory=lambda: client)
    runner = Runner(
        tmp_path,
        load_config(tmp_path),
        RunOptions(only=["sch.ratings"]),
        turn=turn,
        kicad_finder=lambda: FakeKicad(),
        credential_loader=lambda: CREDENTIAL,
        snapshot_root=tmp_path.parent / "snapshots",
    )
    outcome = await runner.run()
    result = by_id(outcome.results)["sch.ratings"]
    assert result.status is Status.FAIL
    assert result.findings[0].title == "Rail"
    assert result.summary == "done"
    request = client.requests[0]
    assert request.mode is CopperMode.AGENT
    assert request.workspace_path != str(tmp_path)  # the snapshot, not the workspace
    assert "Ratings, derating" in request.query
    assert result.threads[0].startswith("validate-")
    assert (tmp_path / "board" / "board.kicad_sch").read_text(encoding="utf-8") != "bad"


class FailedFinalClient(FakeClient):
    """The hosted turn ends with the generic failed final."""

    async def stream(self, request, tool_handler):
        self.requests.append(request)
        yield CopperEvent(
            EventKind.FINAL,
            {"success": False, "error": "CopperPilot ran into an error."},
            turn_id="turn-7",
        )


@pytest.mark.asyncio
async def test_hosted_failure_records_the_hosted_turn_id(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COPPER_PILOT_CLI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("copper_pilot_cli.sessions._db_path", None)
    monkeypatch.setattr("copper_pilot_cli.copper_graph.RETRY_BACKOFF_SECONDS", (0.0,) * 4)
    make_project(tmp_path)
    turn = HostedTurnRunner(CREDENTIAL, client_factory=lambda: FailedFinalClient(""))
    runner = Runner(
        tmp_path,
        load_config(tmp_path),
        RunOptions(only=["sch.ratings"]),
        turn=turn,
        kicad_finder=lambda: FakeKicad(),
        credential_loader=lambda: CREDENTIAL,
        snapshot_root=tmp_path.parent / "snapshots",
    )
    outcome = await runner.run()
    result = by_id(outcome.results)["sch.ratings"]
    assert result.status is Status.ERROR
    assert result.error == (
        "CopperPilot ran into an error. (the hosted turn failed; "
        "the service sent no further detail; hosted turn turn-7.)"
    )


# ------------------------------------------------------------------- CLI


def test_cli_run_prints_verdict_and_paths(tmp_path, capsys) -> None:
    make_project(tmp_path)
    code = run_command(
        parse_args([str(tmp_path), "--until", "S0", "--emit-fix-prompt"]),
        turn=FakeTurn(),
        kicad_finder=lambda: FakeKicad(),
        credential_loader=lambda: CREDENTIAL,
    )
    captured = capsys.readouterr()
    assert code == EXIT_INCOMPLETE
    assert captured.out.startswith("INCOMPLETE:")
    assert "report:" in captured.out and "next actions:" in captured.out
    assert "Validation passed; nothing to fix." in captured.out
    assert "PASS     intake.tools" in captured.err


def test_cli_json_streams_events(tmp_path, capsys) -> None:
    make_project(tmp_path)
    code = run_command(
        parse_args([str(tmp_path), "--until", "S0", "--json", "--out", str(tmp_path / "o")]),
        turn=FakeTurn(),
        kicad_finder=lambda: FakeKicad(),
        credential_loader=lambda: CREDENTIAL,
    )
    assert code == EXIT_INCOMPLETE
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines[0]["type"] == "validate.start"
    assert lines[-1]["type"] == "validate.end"
    assert lines[-1]["data"]["exit_code"] == EXIT_INCOMPLETE
    assert (tmp_path / "o" / "latest").exists()


def test_cli_exit_codes(tmp_path, capsys) -> None:
    make_project(tmp_path)
    common: dict[str, Any] = {
        "turn": FakeTurn(),
        "kicad_finder": lambda: FakeKicad(),
        "credential_loader": lambda: CREDENTIAL,
    }
    assert run_command(parse_args([str(tmp_path), "--until", "S9"]), **common) == EXIT_USAGE
    assert run_command(parse_args(["/no/such/dir"]), **common) == EXIT_USAGE
    assert run_command(parse_args([str(tmp_path), "--gate", "full"]), **common) == EXIT_PASS
    assert (
        run_command(
            parse_args([str(tmp_path), "--gate", "full"]),
            turn=FakeTurn(),
            kicad_finder=lambda: FakeKicad(version="7.0"),
            credential_loader=lambda: CREDENTIAL,
        )
        == EXIT_FAIL
    )
    assert run_command(parse_args([str(tmp_path), "--only", "sch.erc"]), **common) == 3
    assert (
        run_command(
            parse_args([str(tmp_path)]),
            kicad_finder=lambda: FakeKicad(),
            credential_loader=lambda: None,
        )
        == EXIT_ENVIRONMENT
    )
    assert "auth login" in capsys.readouterr().err


def test_cli_explain_shows_prompt_and_last_result(tmp_path, capsys) -> None:
    make_project(tmp_path)
    run_command(
        parse_args([str(tmp_path), "--until", "S0"]),
        turn=FakeTurn(),
        kicad_finder=lambda: FakeKicad(),
        credential_loader=lambda: CREDENTIAL,
    )
    capsys.readouterr()
    assert run_command(parse_args(["explain", "sch.ratings", str(tmp_path)])) == 0
    out = capsys.readouterr().out
    assert "# sch.ratings" in out and "## Output contract" in out and "derating" in out
    assert "## Last result" in out and '"status": "SKIPPED"' in out
    assert run_command(parse_args(["explain", "intake.tools", str(tmp_path)])) == 0
    assert "## Builtin: tools" in capsys.readouterr().out
    assert run_command(parse_args(["explain", "nope", str(tmp_path)])) == EXIT_USAGE


def test_parse_args_flags() -> None:
    args = parse_args(
        [
            "board",
            "--only",
            "a,b",
            "--only",
            "c",
            "--skip",
            "d",
            "--context",
            "x.md",
            "y.md",
            "--profile",
            "release",
            "--gate",
            "strict",
            "--until",
            "S3",
            "--repeat",
            "3",
            "--concurrency",
            "2",
            "--no-cache",
            "--junit",
            "j.xml",
            "--effort",
            "intelligence",
        ]
    )
    assert args.action == "run" and args.workspace == "board"
    assert args.only == ["a", "b", "c"] and args.skip == ["d"]
    assert [str(path) for path in args.context] == ["x.md", "y.md"]
    assert args.profile == "release" and args.gate == "strict" and args.until == "S3"
    assert args.repeat == 3 and args.concurrency == 2 and args.no_cache
    assert str(args.junit) == "j.xml" and args.effort == "intelligence"
    assert parse_args(["list"]).action == "list"
    assert parse_args(["explain", "sch.erc"]).check_id == "sch.erc"


# ---------------------------------------------------------- Python API


def test_python_api_returns_validation_report(tmp_path) -> None:
    make_project(tmp_path)
    import asyncio

    report = asyncio.run(
        avalidate(
            tmp_path,
            gate="full",
            turn=FakeTurn(),
            kicad_finder=lambda: FakeKicad(),
            credential_loader=lambda: CREDENTIAL,
        )
    )
    assert report.verdict == "PASS" and report.passed and report.exit_code == 0
    assert report.report["schema"] == "copper-pilot.validate/v1"
    assert any(item.severity is Severity.P2 for item in report.findings)
    assert report.paths["report"].is_file()


@pytest.mark.asyncio
async def test_reviewer_timeout_is_an_error(tmp_path) -> None:
    import asyncio

    async def slow(turn: ReviewTurn) -> TurnResult:
        await asyncio.sleep(5)
        return TurnResult(text=reply())

    config = load_config(tmp_path if make_project(tmp_path) else tmp_path)
    config.reviewer.timeout_s = 1
    runner = Runner(
        tmp_path,
        config,
        RunOptions(only=["sch.ratings"]),
        turn=slow,
        kicad_finder=lambda: FakeKicad(),
        credential_loader=lambda: CREDENTIAL,
        snapshot_root=tmp_path.parent / "snapshots",
    )
    outcome = await runner.run()
    result = by_id(outcome.results)["sch.ratings"]
    assert result.status is Status.ERROR
    assert "timed out after 1 s" in (result.error or "")


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="os.kill(SIGTERM) terminates the process on Windows instead of running the handler",
)
def test_sigterm_writes_partial_report_like_ctrl_c(tmp_path, capsys) -> None:
    import os
    import signal
    import threading

    make_project(tmp_path)

    async def slow(turn: ReviewTurn) -> TurnResult:
        import asyncio

        threading.Timer(0.2, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        await asyncio.sleep(30)
        return TurnResult(text=reply())

    code = run_command(
        parse_args([str(tmp_path), "--only", "sch.ratings", "--out", str(tmp_path / "o")]),
        turn=slow,
        kicad_finder=lambda: FakeKicad(),
        credential_loader=lambda: CREDENTIAL,
        snapshot_root=tmp_path.parent / "snapshots",
    )
    assert code == 130
    out = capsys.readouterr().out
    assert "(interrupted)" in out
    assert (tmp_path / "o" / "latest" / "report.json").is_file()


@pytest.mark.asyncio
async def test_cancelled_run_writes_partial_report(tmp_path) -> None:
    import asyncio

    make_project(tmp_path)
    started = asyncio.Event()

    async def hang(turn: ReviewTurn) -> TurnResult:
        started.set()
        await asyncio.sleep(3600)
        return TurnResult(text=reply())

    runner = Runner(
        tmp_path,
        load_config(tmp_path),
        RunOptions(gate=GateMode.FULL),
        turn=hang,
        kicad_finder=lambda: FakeKicad(),
        credential_loader=lambda: CREDENTIAL,
        snapshot_root=tmp_path.parent / "snapshots",
    )
    task = asyncio.create_task(runner.run())
    await started.wait()
    task.cancel()
    from copper_pilot_cli.validate.runner import _Interrupted

    with pytest.raises(_Interrupted) as excinfo:
        await task
    outcome = excinfo.value.outcome
    assert outcome.interrupted is True
    assert outcome.exit_code == 130
    assert runner.last_outcome is outcome
    assert outcome.report["interrupted"] is True
    results = by_id(outcome.results)
    assert results["intake.project"].status is Status.PASS
    assert results["arch.review"].status is Status.SKIPPED
    assert results["arch.review"].blocked_by == ["interrupted"]
    assert (outcome.run_dir / "report.json").is_file()
    assert runner.snapshot is None


# ------------------------------------------------------- PCB geometry checks


def _fp(
    lib_id: str,
    ref: str,
    at: tuple[float, float, float],
    *,
    value: str = "",
    pads: list[tuple[float, float]] | None = None,
    fab: tuple[float, float, float, float] | None = None,
    models: list[str] | None = None,
    extra: str = "",
) -> str:
    x, y, rotation = at
    pad_text = "".join(
        f'\n\t\t(pad "{n + 1}" smd rect (at {px} {py}) (size 0.6 1.2) (layers "F.Cu"))'
        for n, (px, py) in enumerate(pads or [])
    )
    fab_text = ""
    if fab is not None:
        x0, y0, x1, y1 = fab
        fab_text = (
            f"\n\t\t(fp_rect (start {x0} {y0}) (end {x1} {y1}) "
            '(stroke (width 0.1) (type solid)) (fill no) (layer "F.Fab"))'
        )
    model_text = "".join(f'\n\t\t(model "{path}" (offset (xyz 0 0 0)))' for path in models or [])
    return (
        f'\n\t(footprint "{lib_id}" (layer "F.Cu") (at {x} {y} {rotation})'
        f'\n\t\t(property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))'
        f'\n\t\t(property "Value" "{value or lib_id.split(":")[-1]}" (at 0 0 0) (layer "F.Fab"))'
        f"{extra}{pad_text}{fab_text}{model_text}\n\t)"
    )


# A right-angle connector: pins along the back (local -Y), body running forward to
# the opening at local +Y, the way KiCad's stock horizontal connectors are drawn.
_RA_PADS = [(-2.0, -3.0), (0.0, -3.0), (2.0, -3.0), (-4.0, -1.0), (4.0, -1.0)]
_RA_BODY = (-4.5, -3.5, 4.5, 4.0)


def _edge_board(*footprints: str) -> str:
    return (
        '(kicad_pcb (version 20240108) (generator "pcbnew")'
        '\n\t(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))'
        "\n\t(gr_rect (start 0 0) (end 60 40) (stroke (width 0.05) (type default)) "
        '(fill no) (layer "Edge.Cuts"))' + "".join(footprints) + "\n)\n"
    )


def _write_pcb(root: Path, text: str) -> None:
    (root / "board" / "board.kicad_pcb").write_text(text, encoding="utf-8")


def test_board_parser_reads_footprints_outline_and_rotation() -> None:
    from copper_pilot_cli.validate.pcb import parse_board, rotate

    board = parse_board(
        _edge_board(
            _fp(
                "Connector_USB:USB_C_Test",
                "J1",
                (30, 36, 90),
                pads=_RA_PADS,
                fab=_RA_BODY,
                models=["${KIPRJMOD}/a.step"],
            )
        )
    )
    assert board.outline_bbox() == (0.0, 0.0, 60.0, 40.0)
    (footprint,) = board.footprints
    assert (footprint.ref, footprint.name, footprint.rotation) == ("J1", "USB_C_Test", 90.0)
    assert [model.path for model in footprint.models] == ["${KIPRJMOD}/a.step"]
    assert len(footprint.pads) == 5 and footprint.local_points("F.Fab")
    # KiCad rotates counter-clockwise on screen with Y pointing down.
    right = rotate((0.0, 1.0), 90)
    assert (round(right[0], 6), round(right[1], 6)) == (1.0, 0.0)


@pytest.mark.asyncio
async def test_connector_edge_flags_inward_sideways_and_inboard_connectors(tmp_path) -> None:
    make_project(tmp_path)
    _write_pcb(
        tmp_path,
        _edge_board(
            # Bottom edge (y=40), opening facing +Y: correct.
            _fp("Connector_USB:USB_C_Test", "J1", (10, 37, 0), pads=_RA_PADS, fab=_RA_BODY),
            # Left edge, rotated 90 so the opening faces +X into the board: wrong.
            _fp(
                "lib:TRJG0926HENL",
                "J2",
                (3, 20, 90),
                value="MagJack",
                pads=_RA_PADS,
                fab=_RA_BODY,
                extra='\n\t\t(tags "RJ45 Magjack")',
            ),
            # Right edge, opening facing up along the edge: wrong.
            _fp("lib:USB3_A_Test", "J3", (57, 20, 180), pads=_RA_PADS, fab=_RA_BODY),
            # Middle of the board: not on an edge.
            _fp("lib:HDMI_A_Test", "J4", (30, 20, 0), pads=_RA_PADS, fab=_RA_BODY),
            # Facing the top edge but 5 mm short of it: set back (P2).
            _fp("lib:DC_Jack_Test", "J5", (45, 9, 180), pads=_RA_PADS, fab=_RA_BODY),
            # Excluded families: pin headers, vertical USB, board-to-board, generic values.
            _fp("lib:PinHeader_1x02_P2.54mm_Vertical", "J6", (30, 30, 0), pads=_RA_PADS),
            _fp("lib:USB_A_Vertical_Test", "J7", (20, 30, 0), pads=_RA_PADS, fab=_RA_BODY),
            _fp(
                "lib:Hirose_DF40C-100DS-0.4V_USB",
                "J8",
                (40, 30, 0),
                pads=_RA_PADS,
                fab=_RA_BODY,
            ),
            _fp("lib:Custom", "J9", (50, 30, 0), value="Conn_01x04 USB", pads=_RA_PADS),
            # Not a connector reference: an ESD part named after USB.
            _fp("lib:USBLC6-2SC6", "U1", (30, 30, 0), pads=_RA_PADS, fab=_RA_BODY),
        ),
    )
    outcome = await runner_for(tmp_path, options=RunOptions(only=["pcb.connector_edge"])).run()
    result = by_id(outcome.results)["pcb.connector_edge"]
    assert result.status is Status.FAIL
    found = {item.locator.refs[0]: item for item in result.findings}
    assert set(found) == {"J2", "J3", "J4", "J5"}
    assert found["J2"].severity is Severity.P1
    assert found["J2"].title == "J2 faces into the board"
    assert (
        "faces +X (right)" in found["J2"].detail and "should face -X (left)" in found["J2"].detail
    )
    assert "to -90 deg" in found["J2"].suggested_action
    assert (found["J2"].locator.x_mm, found["J2"].locator.y_mm) == (3.0, 20.0)
    assert found["J3"].title == "J3 faces along the board edge, not out of it"
    assert "should face +X (right)" in found["J3"].detail
    assert found["J4"].title == "J4 is not on a board edge"
    assert found["J5"].severity is Severity.P2 and "set back" in found["J5"].title
    assert "5 external connector(s) checked" in (result.summary or "")


@pytest.mark.asyncio
async def test_connector_edge_reads_named_references(tmp_path) -> None:
    make_project(tmp_path)
    _write_pcb(
        tmp_path,
        _edge_board(
            # Agents often name connectors by role (J_ETH, J_USBA) instead of J1, J2.
            _fp(
                "lib:TRJG0926HENL",
                "J_ETH",
                (3, 20, 90),
                value="MagJack",
                pads=_RA_PADS,
                fab=_RA_BODY,
            ),
            _fp("lib:USB_A_Test", "J-USBA", (10, 37, 0), pads=_RA_PADS, fab=_RA_BODY),
            _fp("lib:PinHeader_2x20_P2.54mm_Vertical", "J_GPIO", (30, 10, 0), pads=_RA_PADS),
            # Prefix must be followed by a digit or separator: JMP_EMMC is a jumper.
            _fp("lib:USB_C_Test", "JMP_EMMC", (30, 20, 0), pads=_RA_PADS, fab=_RA_BODY),
        ),
    )
    outcome = await runner_for(tmp_path, options=RunOptions(only=["pcb.connector_edge"])).run()
    result = by_id(outcome.results)["pcb.connector_edge"]
    assert [item.locator.refs[0] for item in result.findings] == ["J_ETH"]
    assert "2 external connector(s) checked" in (result.summary or "")


@pytest.mark.asyncio
async def test_connector_edge_skips_ambiguous_and_accepts_overhang(tmp_path) -> None:
    make_project(tmp_path)
    symmetric = [(-2.0, -2.0), (2.0, -2.0), (-2.0, 2.0), (2.0, 2.0)]
    _write_pcb(
        tmp_path,
        _edge_board(
            # Pads centred in the body: orientation unknown, so it is not judged.
            _fp("lib:USB_C_Mid", "J1", (30, 20, 0), pads=symmetric, fab=(-4, -4, 4, 4)),
            # Body centre past the right edge, opening facing out: fine.
            _fp("lib:RJ45_Test", "J2", (58, 20, 90), pads=_RA_PADS, fab=_RA_BODY),
        ),
    )
    outcome = await runner_for(tmp_path, options=RunOptions(only=["pcb.connector_edge"])).run()
    result = by_id(outcome.results)["pcb.connector_edge"]
    assert result.status is Status.PASS
    assert "not checked: J1" in (result.summary or "")


@pytest.mark.asyncio
async def test_models_3d_resolves_paths_and_reports_missing(tmp_path, monkeypatch) -> None:
    from copper_pilot_cli.validate import pcb as pcb_module

    make_project(tmp_path)
    board_dir = tmp_path / "board"
    (board_dir / "lib.3dshapes").mkdir()
    (board_dir / "lib.3dshapes" / "present.step").write_text("ISO", encoding="utf-8")
    stock = tmp_path / "stock3d"
    (stock / "Resistor_SMD.3dshapes").mkdir(parents=True)
    (stock / "Resistor_SMD.3dshapes" / "R_0603.step").write_text("ISO", encoding="utf-8")
    monkeypatch.setattr(pcb_module, "_kicad_config_dirs", lambda: [])
    monkeypatch.setattr(pcb_module, "_system_model_dirs", lambda _cli: [stock])
    for name in [n for n in os.environ if n.startswith(("KICAD", "KISYS"))]:
        monkeypatch.delenv(name)
    pads = [(-0.8, 0.0), (0.8, 0.0)]
    _write_pcb(
        tmp_path,
        _edge_board(
            _fp(
                "lib:A",
                "U1",
                (5, 5, 0),
                pads=pads,
                models=["${KIPRJMOD}/lib.3dshapes/present.step"],
            ),
            _fp("lib:A", "U2", (8, 5, 0), pads=pads, models=["lib.3dshapes/present.step"]),
            # Any KiCad version's stock variable falls back to the system 3D directory.
            _fp(
                "lib:R",
                "R1",
                (5, 9, 0),
                pads=pads,
                models=["${KICAD9_3DMODEL_DIR}/Resistor_SMD.3dshapes/R_0603.step"],
            ),
            _fp(
                "lib:R",
                "R2",
                (8, 9, 0),
                pads=pads,
                models=["${KICAD10_3DMODEL_DIR}/Resistor_SMD.3dshapes/R_0603.step"],
            ),
            _fp(
                "lib:J",
                "J1",
                (5, 13, 0),
                pads=pads,
                models=["${KIPRJMOD}/CM5IO.3dshapes/missing.stp"],
            ),
            _fp(
                "lib:J",
                "J2",
                (8, 13, 0),
                pads=pads,
                models=["${KIPRJMOD}/CM5IO.3dshapes/missing.stp"],
            ),
            _fp("lib:X", "Y1", (5, 17, 0), pads=pads, models=["${MY_LIBS}/x.step"]),
            # Bodies without any model: P2. Mounting holes and fiducials are exempt.
            _fp("Package_SO:SOIC-8", "U3", (5, 21, 0), pads=pads),
            _fp("Package_SO:SOIC-8", "U4", (8, 21, 0), pads=pads),
            _fp("MountingHole:MountingHole_3.2mm_M3", "H1", (5, 25, 0), pads=pads),
            _fp("Fiducial:Fiducial_1mm", "FID1", (8, 25, 0), pads=pads),
            _fp("TestPoint:TestPoint_Pad_1.0x1.0mm", "TP1", (11, 25, 0), pads=pads),
        ),
    )
    outcome = await runner_for(tmp_path, options=RunOptions(only=["pcb.models_3d"])).run()
    result = by_id(outcome.results)["pcb.models_3d"]
    assert result.status is Status.FAIL
    by_title = {item.title: item for item in result.findings}
    missing = by_title["3D model missing for J1, J2"]
    assert missing.severity is Severity.P1 and missing.locator.refs == ["J1", "J2"]
    assert "${KIPRJMOD}/CM5IO.3dshapes/missing.stp" in missing.detail
    assert str(board_dir / "CM5IO.3dshapes" / "missing.stp") in missing.detail
    unknown = by_title["3D model missing for Y1"]
    assert "MY_LIBS is not defined" in unknown.detail
    no_model = by_title["No 3D model on SOIC-8 (U3, U4)"]
    assert no_model.severity is Severity.P2
    assert len(result.findings) == 3
    assert result.summary == "4/7 model paths resolve; 2 footprint(s) without a model"


@pytest.mark.asyncio
async def test_pcb_geometry_checks_run_after_a_stage_gate_halts(tmp_path) -> None:
    make_project(tmp_path)
    _write_pcb(
        tmp_path,
        _edge_board(
            _fp("lib:RJ45_Test", "J1", (3, 20, 90), pads=_RA_PADS, fab=_RA_BODY),
        ),
    )
    kicad = FakeKicad(
        erc=[{"severity": "error", "type": "pin_not_connected", "description": "x", "items": []}]
    )
    outcome = await runner_for(tmp_path, kicad=kicad).run()
    results = by_id(outcome.results)
    assert results["sch.erc"].status is Status.FAIL
    assert results["pcb.placement"].blocked_by == ["stage gate: S3 (sch.erc)"]
    assert results["pcb.drc"].status is Status.SKIPPED
    # Ungated deterministic checks still report after the halt.
    assert results["pcb.connector_edge"].status is Status.FAIL
    assert results["pcb.connector_edge"].findings[0].locator.refs == ["J1"]
    assert results["pcb.models_3d"].status is not Status.SKIPPED


def test_default_graph_marks_pcb_geometry_checks_ungated() -> None:
    config = build_config(packaged_default_raw())
    for check_id in ("pcb.connector_edge", "pcb.models_3d"):
        check = config.check(check_id)
        assert check.kind == "builtin" and check.ungated and check.requires == []
    footprints = config.check("parts.footprints").prompt or ""
    assert "3D model is P3" not in footprints and "pcb.models_3d" in footprints
