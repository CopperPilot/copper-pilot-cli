"""CopperPilot CLI configuration and data paths."""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
from collections.abc import Generator
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


def atomic_write_text(destination: Path, text: str, *, mode: int | None = None) -> None:
    """Replace ``destination`` via a temp file unique to this write.

    A shared temp name lets concurrent processes move or clobber each other's
    half-written file, so every write gets its own name in the same directory.
    """
    fd, name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        if mode is not None:
            temporary.chmod(mode)
        temporary.replace(destination)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


@contextlib.contextmanager
def state_lock(destination: Path) -> Generator[None]:
    """Serialize read-modify-write of a state file across processes (POSIX only)."""
    if sys.platform == "win32":
        yield
        return
    import fcntl

    with open(destination.with_name(f".{destination.name}.lock"), "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
