"""Credential-aware end-to-end validate run on examples/001_esp32."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from copper_pilot_cli.copper_auth import load_credential
from copper_pilot_cli.validate.cli import parse_args, run_command
from copper_pilot_cli.validate.config import REPORT_SCHEMA, CheckResult
from copper_pilot_cli.validate.kicad import find_kicad_cli

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "001_esp32"


@pytest.mark.live
def test_live_validate_until_s2_on_esp32_example(tmp_path) -> None:
    if load_credential() is None:
        pytest.skip("CopperPilot credentials were not found")
    if find_kicad_cli() is None:
        pytest.skip("kicad-cli was not found")
    out = tmp_path / "validate"
    code = run_command(parse_args([str(EXAMPLE), "--until", "S2", "--no-cache", "--out", str(out)]))
    assert code in {0, 1, 3}
    latest = out / "latest" / "report.json"
    report = json.loads(latest.read_text(encoding="utf-8"))
    assert report["schema"] == REPORT_SCHEMA
    assert report["verdict"] in {"PASS", "FAIL", "INCOMPLETE"}
    assert report["exit_code"] == code
    results = [CheckResult.model_validate(row) for row in report["results"]]
    assert {result.check_id for result in results} >= {"intake.project", "parts.footprints"}
    assert all(
        result.stage in {"S0", "S1", "S2"} or result.status.value == "SKIPPED" for result in results
    )
    assert (out / "latest" / "next_actions.json").is_file()
    assert (out / "latest" / "REPORT.md").is_file()
