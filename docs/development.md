# Development

How to build, test, and release this package. Contributor workflow lives in
[CONTRIBUTING.md](../CONTRIBUTING.md).

## Layout

```
src/copper_pilot_cli/   Python package
tests/                  pytest suite
examples/               Runnable examples
docs/                   User and developer docs
skills/                 Portable Agent Skill for other harnesses
plugins/                Claude Code plugin (not in the PyPI wheel)
.claude-plugin/         Plugin marketplace manifest
.github/                CI, issue forms, pull request template
```

The terminal presentation layer started as a source fork of Deep Agents Code.
See [UPSTREAM.md](../UPSTREAM.md) and [NOTICE](../NOTICE).

## Environment

Python 3.11 or newer. From the repository root:

```console
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

`make help` lists targets. Equivalent commands:

| Target | Command |
|--------|---------|
| `make fmt` | `python -m ruff format src tests examples` |
| `make lint` | `ruff format --check`, `ruff check`, `ty check src/copper_pilot_cli` |
| `make test` | `python -m pytest -m "not live"` |

CI also runs `pip-audit`, `piplicenses` (fails on GPL/AGPL/UNKNOWN), a wheel
build, and `copper-pilot --version` / `copper-pilot-cli --version`.
Required CI covers every supported stable Python minor on Linux and the minimum
supported minor on macOS and Windows. Python 3.15 is probed weekly, outside
pull requests, by `.github/workflows/forward-python.yml`. When that scheduled
run is green, add the `Programming Language :: Python :: 3.15` classifier, add
3.15 to the required CI matrix, and retarget the scheduled probe at the next
unreleased Python.

## Tests

- Default suite: `make test` or `pytest -m "not live"`.
- Live suite: `pytest -m live`. Needs `copper-pilot auth login`. Skips when no
  credential is stored.
- ESP32 example helpers are covered in `tests/test_esp32_example.py`. Running
  `python examples/001_esp32/main.py` needs KiCad 10 and a live account.

Do not commit credentials, `.venv/`, or `dist/`.

## Hosted protocol

The client speaks protocol v1. Agent turns, and plan turns that carry plan
context, use `/api/copperpilot/analyze/agent/ws`. Ask turns and plan turns
without that context use `/api/copperpilot/analyze/chat/ws`.

`make_chat_start` in `copper_protocol.py` bounds history before the socket
send, matching the desktop harness:

| Attempt | Message cap | History cap | Frame cap |
|---------|-------------|-------------|-----------|
| 1 | 64 KiB | 4 MiB | 12 MiB |
| 2 | 32 KiB | 2 MiB | 8 MiB |
| 3 | 8 KiB | 128 KiB | 8 MiB |
| 4 | 4 KiB | 64 KiB | 8 MiB |

History is kept newest-first. Inline `data:` attachments are replaced before
the byte caps apply. A query or workspace snapshot that still cannot fit
raises `ProtocolError` with `code="PAYLOAD_TOO_LARGE"`.

A WebSocket `error` frame keeps `code`, `status`, `block_reason`,
`can_purchase_overage`, and `retryable` on both `ProtocolError` and the
`copper.error` event. Absent fields stay absent. The interactive UI and MCP
`error` string use `public_message`; `--json` and the MCP event digest keep
the object.

Device login and the WebSocket `User-Agent` (`CopperPilot CLI/<version>`)
read the installed package version. That version comes from `VERSION`.

`POST /api/copperpilot/analyze/token_limits` is the account-usage read. It
returns percentages and reset state even when chat is blocked. Do not add
token counts, plan allocations, Stripe checkout, or a per-chat context meter
here. The server does not expose those on this contract.

## Release

Version is the contents of [`VERSION`](../VERSION). `make release` writes that
file, regenerates [`HISTORY.md`](../HISTORY.md) with `gitchangelog`, commits
both, tags `X.Y.Z` (no `v` prefix), and pushes. GitHub Actions then publishes
to PyPI and attaches the dist artifacts to the GitHub Release.

```console
make release   # prompts for X.Y.Z
```

The tag must match `VERSION`. See `.github/workflows/release.yml`.
