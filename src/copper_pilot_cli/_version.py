"""CopperPilot CLI version and update endpoints."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

try:
    __version__ = version("copper-pilot-cli")
except PackageNotFoundError:
    __version__ = (
        Path(__file__).resolve().parents[2].joinpath("VERSION").read_text(encoding="utf-8").strip()
    )

DOCS_URL = "https://copperpilot.ai"
"""URL for CopperPilot documentation."""

PYPI_URL = "https://pypi.org/pypi/copper-pilot-cli/json"
"""PyPI JSON API endpoint for version checks."""

SDK_PYPI_URL = "https://pypi.org/pypi/deepagents/json"
"""PyPI JSON API endpoint for reading `deepagents` SDK release metadata.

The CLI only reads release-age metadata from this endpoint; it never
performs SDK update checks.
"""

CHANGELOG_URL = "https://github.com/CopperPilot/copper-pilot-cli/releases"
"""URL for the full changelog."""

USER_AGENT = f"copper-pilot-cli/{__version__} update-check"
"""User-Agent header sent with PyPI requests."""
