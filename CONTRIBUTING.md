# Contributing

Thanks for helping improve CopperPilot CLI. By participating you agree to the
[Code of Conduct](CODE_OF_CONDUCT.md).

This repository is the open-source terminal and LangChain client for the hosted
CopperPilot agent. It is not the desktop app or the hosted backend.

## Prerequisites

- Python 3.12 or newer
- [pip](https://pip.pypa.io/en/stable/)
- [make](https://www.gnu.org/software/make/) (optional; the same commands are
  listed below)
- A CopperPilot account only if you run live tests
- KiCad 10 on `PATH` only for [`examples/001_esp32`](examples/001_esp32)

## Set up a development install

```console
git clone https://github.com/CopperPilot/copper-pilot-cli.git
cd copper-pilot-cli
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

On Windows, Git Bash or PowerShell both work; `make` is optional.

## Format, lint, and test

```console
make fmt
make lint
make test
```

Without `make`:

```console
python -m ruff format src tests examples
python -m ruff format --check src tests examples
python -m ruff check src tests examples
python -m ty check src/copper_pilot_cli
python -m pytest -m "not live"
```

`make test` skips tests marked `live`. Live tests exercise device login, the
Textual composer, the hosted stream, and local-tool round trips. They use the
normal private credential store and skip when no credential is present:

```console
copper-pilot auth login
pytest -m live
```

Never commit `~/.copper-pilot/.state/auth.json`, API keys, or private boards.

## Pull requests

1. Create a branch from `main`.
2. Keep the change focused: one bug, feature, or docs fix per PR.
3. Add or update tests. Coverage of new branches in `src/copper_pilot_cli`
   (except `_upstream/`) is expected.
4. Update docs when behavior changes: [README.md](README.md),
   [docs/cli.md](docs/cli.md), [docs/harness.md](docs/harness.md), or
   [docs/development.md](docs/development.md).
5. Run `make fmt && make lint && make test`.
6. Open a pull request with the template. Fill in the test plan.

CI runs lint, tests, `pip-audit`, a license check, and a wheel install on
every pull request.

## Scope of this package

Keep this client a hosted-agent frontend. Do not add:

- Hosted-server or `copper_pilot_core` code
- Model or provider pickers
- Local model execution
- Computer use
- An MCP **host**, generic plugin runtime, or MCP **client** stack
- LangSmith tracing or content telemetry
- Sandbox runtimes

A thin MCP **server** (`copper-pilot mcp`) and a Claude Code plugin that only
wraps that server are in scope. They delegate to the hosted agent; they do not
reimplement it.

Upstream Textual presentation is vendored under
`src/copper_pilot_cli/_upstream/` with provenance in [UPSTREAM.md](UPSTREAM.md)
and [NOTICE](NOTICE). When porting upstream changes, follow that document;
do not reintroduce upstream runtime surfaces.

## Reporting issues

Use the [bug report form](https://github.com/CopperPilot/copper-pilot-cli/issues/new?template=bug_report.yml)
and include steps to reproduce. Security issues go to [SECURITY.md](SECURITY.md),
not the public tracker.

## Release

Maintainers cut a release with `make release`. See
[docs/development.md](docs/development.md#release).
