<p align="center">
  <img src="https://github.com/CopperPilot/copper-pilot-cli/blob/main/docs/assets/logo.png?raw=true" alt="CopperPilot" width="128">
</p>

# CopperPilot CLI

CopperPilot in your terminal: a thin, open-source client for CopperPilot electronics design agent, the best AI for PCB Design.

[![CI](https://github.com/CopperPilot/copper-pilot-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/CopperPilot/copper-pilot-cli/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

<p align="center">
  <img src="https://github.com/CopperPilot/copper-pilot-cli/blob/main/docs/assets/esp32-mini-1-h4.gif?raw=true" alt="CopperPilot building an ESP32-MINI-1 board">
</p>

## Prerequisites

- Python 3.12 or newer
- A [CopperPilot](https://copperpilot.ai) account for login
- KiCad 10 on `PATH` only if you run [`examples/001_esp32`](examples/001_esp32)

## Install and run

```console
pip install copper-pilot-cli
copper-pilot
```

The first interactive launch opens a browser for CopperPilot device login.
With no path argument, the current directory is the workspace and a new chat
starts immediately. Any existing directory is valid.

```console
copper-pilot ./my-board
copper-pilot --resume
copper-pilot threads list
copper-pilot -n -m "Review this board" --json
```

Both `copper-pilot` and `copper-pilot-cli` run the same program.
`copper-pilot --help` lists flags; [docs/cli.md](docs/cli.md) is the full
command reference.

## Configuration

- Production API: `https://copperpilot.ai`
- Development override: `COPPER_PILOT_API_URL`
- User state: `~/.copper-pilot/.state`
- Project artifacts: `.copperpilot/`

Credentials are stored in a private, atomically replaced `auth.json`; on POSIX
the parent directory is mode `0700` and the file is mode `0600`.

## LangChain

```python
from copper_pilot_cli.copper_protocol import CopperMode
from copper_pilot_cli.langchain import CopperPilotAgent
from helper import drc_violations, erc_errors, render_pcb, run

PLAN = CopperMode.PLAN

copper_pilot = CopperPilotAgent(workspace="esp32")

run(copper_pilot, "Build me an ESP32 dev board.", PLAN)
run(copper_pilot, "Let us build this plan.")
run(copper_pilot, "Inspect the PCB, check for any missing 3D models, and apply them accordingly.")

assert erc_errors() == 0
assert drc_violations() == 0
render_pcb("esp32.png")
```

`CopperPilotAgent` is a `Runnable`, not a chat model. It represents a complete
hosted agent run, including local tool requests. Use `as_langgraph()` for graph
composition or `as_compiled_subagent()` for explicit delegation from an outer
Deep Agent. The compatible `deepagents` runtime is installed and pinned by this
package.

That snippet is the runnable example in
[`examples/001_esp32`](examples/001_esp32).

## Security boundary

The hosted agent may request local filesystem and shell tools. Reads and
searches run freely. Writes, edits, deletes, and shell commands are gated by
Manual approval by default. Auto permits only deterministic routine actions;
unknown actions still ask. YOLO must be selected explicitly.

Filesystem, search, edit, and shell execution use Deep Agents' native
`FilesystemMiddleware` tools and `LocalShellBackend`; CopperPilot's adapter only
normalizes hosted tool names, approval decisions, and result envelopes.

Computer use, model/provider selection, local model execution, LangSmith
tracing, content telemetry, MCP/plugin controls, and server implementation code
are not part of this package.

See [UPSTREAM.md](UPSTREAM.md) and [NOTICE](NOTICE) for the retained Textual
client attribution.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Development

```console
pip install -e ".[dev]"
make fmt
make lint
make test
```

`make test` runs `pytest -m "not live"`. Live tests skip unless a CopperPilot
credential is stored. Release tagging is `make release`. Details:
[docs/development.md](docs/development.md).

## Contributing

Bug reports, features, and pull requests are welcome. Please read
[CONTRIBUTING.md](CONTRIBUTING.md) and the
[Code of Conduct](CODE_OF_CONDUCT.md). Use the
[bug report form](https://github.com/CopperPilot/copper-pilot-cli/issues/new?template=bug_report.yml)
so we can reproduce the problem.

## License

[MIT](LICENSE).
