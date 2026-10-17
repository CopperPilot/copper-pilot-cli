"""`copper-pilot validate` engine tests; no credential and no KiCad required."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest
import yaml

from copper_pilot_cli.validate.cli import parse_args, render_graph, render_list, run_command
from copper_pilot_cli.validate.config import (
    CheckResult,
    ConfigError,
    GateMode,
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
from copper_pilot_cli.validate.runner import (
    EXIT_FAIL,
    EXIT_INCOMPLETE,
    EXIT_PASS,
    EXIT_USAGE,
    Runner,
    RunOptions,
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


def runner_for(
    workspace: Path,
    *,
    options: RunOptions | None = None,
    kicad: FakeKicad | None = None,
    config: ValidateConfig | None = None,
) -> Runner:
    kicad = kicad if kicad is not None else FakeKicad()
    return Runner(
        workspace,
        config or load_config(workspace),
        options or RunOptions(),
        kicad_finder=lambda: kicad,
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
    assert "intake_project --> sch_erc" in out


def test_default_graph_is_valid_and_covers_every_stage() -> None:
    config = build_config(packaged_default_raw())
    assert [stage.id for stage in config.stages] == [f"S{n}" for n in range(9)]
    stages_with_checks = {check.stage for check in config.checks}
    assert stages_with_checks <= {stage.id for stage in config.stages}
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
                    {"id": "intake.intent", "enabled": False},
                    {"id": "pcb.drc", "gate": "advisory"},
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
    assert config.check("intake.intent").enabled is False
    assert config.check("pcb.drc").gate == "advisory"
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


# ----------------------------------------------------------------- runner


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
        kicad_finder=lambda: None,
    )
    outcome = await runner.run()
    results = by_id(outcome.results)
    assert results["intake.tools"].status is Status.FAIL
    assert results["sch.erc"].status is Status.SKIPPED
    assert outcome.exit_code == EXIT_FAIL
    assert outcome.next_actions["actions"][0]["title"] == "kicad-cli was not found"


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


# ------------------------------------------------------------------- CLI


def test_cli_run_prints_verdict_and_paths(tmp_path, capsys) -> None:
    make_project(tmp_path)
    code = run_command(
        parse_args([str(tmp_path), "--until", "S0"]),
        kicad_finder=lambda: FakeKicad(),
    )
    captured = capsys.readouterr()
    assert code == EXIT_INCOMPLETE
    assert captured.out.startswith("INCOMPLETE:")
    assert "report:" in captured.out and "next actions:" in captured.out
    assert "PASS     intake.tools" in captured.err


def test_cli_exit_codes(tmp_path, capsys) -> None:
    make_project(tmp_path)
    common: dict[str, Any] = {"kicad_finder": lambda: FakeKicad()}
    assert run_command(parse_args([str(tmp_path), "--until", "S9"]), **common) == EXIT_USAGE
    assert run_command(parse_args(["/no/such/dir"]), **common) == EXIT_USAGE
    assert run_command(parse_args([str(tmp_path), "--gate", "full"]), **common) == EXIT_PASS
    assert (
        run_command(
            parse_args([str(tmp_path), "--gate", "full"]),
            kicad_finder=lambda: FakeKicad(version="7.0"),
        )
        == EXIT_FAIL
    )
    assert run_command(parse_args([str(tmp_path), "--only", "sch.erc"]), **common) == 3


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
            "--profile",
            "release",
            "--gate",
            "strict",
            "--until",
            "S3",
            "--no-cache",
        ]
    )
    assert args.action == "run" and args.workspace == "board"
    assert args.only == ["a", "b", "c"] and args.skip == ["d"]
    assert args.profile == "release" and args.gate == "strict" and args.until == "S3"
    assert args.no_cache
    assert parse_args(["list"]).action == "list"


# ---------------------------------------------------------- Python API
