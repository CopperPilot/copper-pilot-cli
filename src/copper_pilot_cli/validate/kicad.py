"""kicad-cli wrappers used by the builtin checks."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

SUPPORTED_MAJORS = (9, 10)
_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
_CANDIDATES = (
    Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"),
    Path("/Applications/KiCad 10.0/KiCad.app/Contents/MacOS/kicad-cli"),
    Path("/Applications/KiCad 9.0/KiCad.app/Contents/MacOS/kicad-cli"),
    Path("/usr/bin/kicad-cli"),
    Path("/usr/local/bin/kicad-cli"),
    Path("C:/Program Files/KiCad/10.0/bin/kicad-cli.exe"),
    Path("C:/Program Files/KiCad/9.0/bin/kicad-cli.exe"),
    Path("C:/Program Files/KiCad/bin/kicad-cli.exe"),
)


class KicadError(RuntimeError):
    """kicad-cli failed or did not write the expected output."""


def kicad_cli_candidates() -> list[Path]:
    found: list[Path] = []
    env = os.environ.get("KICAD_CLI")
    if env:
        found.append(Path(env))
    which = shutil.which("kicad-cli") or shutil.which("kicad-cli.exe")
    if which:
        found.append(Path(which))
    found.extend(path for path in _CANDIDATES if path.is_file())
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in found:
        resolved = path.resolve()
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def parse_version(text: str) -> tuple[int, str]:
    match = _VERSION_RE.search(text or "")
    if match is None:
        raise KicadError(f"could not parse kicad-cli version from {text!r}")
    return int(match.group(1)), match.group(0)


class KicadCli:
    """Thin subprocess wrapper; every output lands in a caller-chosen directory."""

    def __init__(self, executable: Path, timeout_s: float = 600.0) -> None:
        self.executable = executable
        self.timeout_s = timeout_s
        self._version: str | None = None

    def _run(self, args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(  # noqa: S603
                [str(self.executable), *args],
                check=False,
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=self.timeout_s,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise KicadError(f"kicad-cli {' '.join(args[:2])} failed: {exc}") from exc

    def version(self) -> str:
        if self._version is None:
            completed = self._run(["version"])
            _, label = parse_version((completed.stdout or completed.stderr or "").strip())
            self._version = label
        return self._version

    def major(self) -> int:
        return parse_version(self.version())[0]

    def _expect(self, completed: subprocess.CompletedProcess[str], output: Path, what: str) -> None:
        if not output.exists():
            raise KicadError(
                f"kicad-cli {what} did not write {output.name}: "
                f"{(completed.stderr or completed.stdout or '').strip()[:800]}"
            )

    def erc(self, schematic: Path, output: Path) -> dict[str, Any]:
        completed = self._run(
            [
                "sch",
                "erc",
                "--format",
                "json",
                "--severity-all",
                "--output",
                str(output),
                str(schematic),
            ],
            cwd=schematic.parent,
        )
        self._expect(completed, output, "sch erc")
        return json.loads(output.read_text(encoding="utf-8"))

    def drc(self, pcb: Path, output: Path, *, schematic_parity: bool = False) -> dict[str, Any]:
        args = ["pcb", "drc", "--format", "json", "--severity-all"]
        if schematic_parity:
            args.append("--schematic-parity")
        args.extend(["--output", str(output), str(pcb)])
        completed = self._run(args, cwd=pcb.parent)
        self._expect(completed, output, "pcb drc")
        return json.loads(output.read_text(encoding="utf-8"))

    def netlist(self, schematic: Path, output: Path) -> str:
        completed = self._run(
            [
                "sch",
                "export",
                "netlist",
                "--format",
                "kicadxml",
                "--output",
                str(output),
                str(schematic),
            ],
            cwd=schematic.parent,
        )
        self._expect(completed, output, "sch export netlist")
        return output.read_text(encoding="utf-8")

    def gerbers(self, pcb: Path, output_dir: Path) -> list[Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        completed = self._run(
            ["pcb", "export", "gerbers", "--output", f"{output_dir}{os.sep}", str(pcb)],
            cwd=pcb.parent,
        )
        files = sorted(p for p in output_dir.iterdir() if p.is_file())
        if not files:
            raise KicadError(
                f"kicad-cli pcb export gerbers wrote nothing: "
                f"{(completed.stderr or completed.stdout or '').strip()[:800]}"
            )
        return files

    def drill(self, pcb: Path, output_dir: Path) -> list[Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        completed = self._run(
            ["pcb", "export", "drill", "--output", f"{output_dir}{os.sep}", str(pcb)],
            cwd=pcb.parent,
        )
        files = sorted(p for p in output_dir.iterdir() if p.is_file())
        if not files:
            raise KicadError(
                f"kicad-cli pcb export drill wrote nothing: "
                f"{(completed.stderr or completed.stdout or '').strip()[:800]}"
            )
        return files


def find_kicad_cli() -> KicadCli | None:
    """Return the first usable kicad-cli, or None when none is installed."""
    for candidate in kicad_cli_candidates():
        cli = KicadCli(candidate)
        try:
            cli.version()
        except KicadError:
            continue
        return cli
    return None
