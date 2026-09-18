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

Python 3.12 or newer. From the repository root:

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

## Tests

- Default suite: `make test` or `pytest -m "not live"`.
- Live suite: `pytest -m live`. Needs `copper-pilot auth login`. Skips when no
  credential is stored.
- ESP32 example helpers are covered in `tests/test_esp32_example.py`. Running
  `python examples/001_esp32/main.py` needs KiCad 10 and a live account.

Do not commit credentials, `.venv/`, or `dist/`.

## Release

Version is the contents of [`VERSION`](../VERSION). `make release` writes that
file, regenerates [`HISTORY.md`](../HISTORY.md) with `gitchangelog`, commits
both, tags `X.Y.Z` (no `v` prefix), and pushes. GitHub Actions then publishes
to PyPI and attaches the dist artifacts to the GitHub Release.

```console
make release   # prompts for X.Y.Z
```

The tag must match `VERSION`. See `.github/workflows/release.yml`.
