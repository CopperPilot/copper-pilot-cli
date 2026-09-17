"""Build an ESP32 dev board with CopperPilot."""

from __future__ import annotations

import sys
from pathlib import Path

from copper_pilot_cli.copper_auth import AuthenticationError
from copper_pilot_cli.copper_protocol import CopperMode
from copper_pilot_cli.langchain import CopperPilotAgent

from helper import drc_violations, erc_errors, render_pcb, run

PLAN = CopperMode.PLAN
WORKSPACE_PATH = Path(__file__).resolve().parent


def main() -> None:
    copper_pilot = CopperPilotAgent(workspace=WORKSPACE_PATH)
    run(copper_pilot, "Build me an ESP32 dev board.", PLAN)
    run(copper_pilot, "Let us build this plan.")
    run(
        copper_pilot,
        "Inspect the PCB, check for any missing 3D models, and apply them accordingly.",
    )
    assert erc_errors() == 0
    assert drc_violations() == 0
    render_pcb("esp32.png")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (AssertionError, AuthenticationError, FileNotFoundError, RuntimeError) as exc:
        sys.stderr.write(f"esp32 example: {exc}\n")
        raise SystemExit(1) from exc
