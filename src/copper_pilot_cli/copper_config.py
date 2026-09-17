"""CopperPilot CLI configuration and data paths."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_API_URL = "https://copperpilot.ai"
APP_HOME_ENV = "COPPER_PILOT_CLI_HOME"
API_URL_ENV = "COPPER_PILOT_API_URL"


def normalize_base_url(value: str | None) -> str:
    """Return a normalized HTTP(S) origin or an empty string."""
    raw = (value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def app_home() -> Path:
    """Return the private per-user CopperPilot CLI home."""
    override = os.getenv(APP_HOME_ENV)
    return Path(override).expanduser() if override else Path.home() / ".copper-pilot"


def api_base_url() -> str:
    """Return the configured API URL, defaulting to production."""
    return normalize_base_url(os.getenv(API_URL_ENV)) or DEFAULT_API_URL


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Resolved application paths."""

    home: Path
    state: Path
    auth: Path
    sessions: Path
    history: Path
    logs: Path
    recent_projects: Path


def paths() -> AppPaths:
    """Resolve application paths at call time for testability."""
    home = app_home()
    state = home / ".state"
    return AppPaths(
        home=home,
        state=state,
        auth=state / "auth.json",
        sessions=state / "sessions.db",
        history=state / "history.jsonl",
        logs=state / "logs",
        recent_projects=state / "recent-projects.json",
    )
