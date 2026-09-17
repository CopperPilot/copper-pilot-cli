from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
VENDORED = ROOT / "src/copper_pilot_cli/_upstream/dcode_0_1_69"


def test_vendored_upstream_files_match_recorded_hashes() -> None:
    provenance = json.loads((VENDORED / "PROVENANCE.json").read_text(encoding="utf-8"))
    assert provenance["revision"] == "1d3232c0852c47af09119edea10eeec887e4f0da"
    assert provenance["package_version"] == "0.1.69"

    for entry in provenance["files"]:
        path = VENDORED / entry["local_path"]
        assert path.is_file(), entry["local_path"]
        # Git on Windows may check out CRLF; provenance hashes are LF bytes.
        digest = hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        assert digest == entry["sha256"], entry["local_path"]
        assert entry["patch_status"] in {"unchanged", "patched"}


def test_every_vendored_source_has_a_manifest_entry() -> None:
    provenance = json.loads((VENDORED / "PROVENANCE.json").read_text(encoding="utf-8"))
    recorded = {entry["local_path"] for entry in provenance["files"]}
    actual = {
        path.relative_to(VENDORED).as_posix()
        for path in VENDORED.rglob("*.py")
        if path.name != "__init__.py"
    }
    assert actual == recorded
