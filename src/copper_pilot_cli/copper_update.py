"""Low-overhead PyPI-backed update discovery and release notices."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from dataclasses import dataclass
from time import time
from typing import Any

import httpx
from packaging.version import InvalidVersion, Version

from copper_pilot_cli._version import PYPI_URL, __version__
from copper_pilot_cli.copper_preferences import load_preferences, save_preferences

CHECK_INTERVAL_SECONDS = 24 * 60 * 60
REQUEST_TIMEOUT_SECONDS = 3
BACKGROUND_TIMEOUT_SECONDS = 5
MAX_FEATURE_NOTICE_LENGTH = 160
RELEASE_NOTICE_URL = "https://github.com/CopperPilot/copper-pilot-cli/releases/download/{version}/release-notice.json"
_VERSION_TOKEN = re.compile(r"\A[0-9A-Za-z][0-9A-Za-z._+-]{0,63}\Z")


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    current: str
    latest: str
    available: bool
    install_command: str
    feature_notice: str | None = None


def validate_feature_notice(value: object) -> str | None:
    """Return a safe one-line feature notice, or ``None`` for invalid metadata."""
    if not isinstance(value, str):
        return None
    notice = value.strip()
    if (
        not notice
        or len(notice) > MAX_FEATURE_NOTICE_LENGTH
        or "\n" in notice
        or "\r" in notice
        or any(ord(character) < 32 for character in notice)
    ):
        return None
    return notice


def release_notice_url(version: str) -> str | None:
    """Return the notice URL only when the version is safe to interpolate."""
    if not _VERSION_TOKEN.fullmatch(version):
        return None
    return RELEASE_NOTICE_URL.format(version=version)


def select_latest_version(payload: dict[str, Any]) -> str | None:
    """Choose the newest non-yanked final release from a PyPI JSON document."""
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    candidates: list[Version] = []
    releases = payload.get("releases")
    if isinstance(releases, dict):
        for version, files in releases.items():
            if not isinstance(version, str) or not isinstance(files, list) or not files:
                continue
            if any(isinstance(item, dict) and item.get("yanked") is True for item in files):
                continue
            try:
                parsed = Version(version)
            except InvalidVersion:
                continue
            if parsed.is_prerelease:
                continue
            candidates.append(parsed)
    if candidates:
        return str(max(candidates))
    if info.get("yanked") is True:
        return None
    version = info.get("version")
    if not isinstance(version, str):
        return None
    try:
        parsed = Version(version)
    except InvalidVersion:
        return None
    if parsed.is_prerelease:
        return None
    return version


def build_release_notice(notices: object, version: str) -> dict[str, str | None]:
    """Validate the optional one-line feature note for a release."""
    if not isinstance(notices, dict):
        raise ValueError("release-notices.json must be an object keyed by release version")
    feature = notices.get(version)
    if feature is None:
        return {"version": version, "feature": None}
    if not isinstance(feature, str):
        raise ValueError(
            f"Feature notice for {version} must be a one-line string of at most 160 characters"
        )
    cleaned = validate_feature_notice(feature)
    if cleaned is None:
        raise ValueError(
            f"Feature notice for {version} must be a one-line string of at most 160 characters"
        )
    return {"version": version, "feature": cleaned}


async def _feature_notice(client: httpx.AsyncClient, version: str) -> str | None:
    """Fetch the optional immutable release notice without trusting malformed content."""
    url = release_notice_url(version)
    if url is None:
        return None
    try:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != version:
        return None
    return validate_feature_notice(payload.get("feature"))


async def check_for_update(*, include_feature_notice: bool = True) -> UpdateInfo:
    """Resolve the latest PyPI version and, when requested, its feature notice."""
    # Building the TLS context costs tens of milliseconds; keep it off the UI loop.
    client = await asyncio.to_thread(httpx.AsyncClient, timeout=REQUEST_TIMEOUT_SECONDS)
    async with client:
        response = await client.get(PYPI_URL)
        response.raise_for_status()
        selected = select_latest_version(response.json())
        available = selected is not None and Version(selected) > Version(__version__)
        feature_notice = (
            await _feature_notice(client, selected)
            if selected is not None and available and include_feature_notice
            else None
        )
        latest = selected or __version__
    return UpdateInfo(
        current=__version__,
        latest=latest,
        available=available,
        install_command=f"{sys.executable} -m pip install --upgrade copper-pilot-cli",
        feature_notice=feature_notice,
    )


def _update_state(value: dict[str, Any]) -> dict[str, Any]:
    state = value.get("update_check")
    return state if isinstance(state, dict) else {}


def record_update_check(*, checked_at: float, announced_version: str | None = None) -> None:
    """Merge update state into the latest preferences without replacing them."""
    preferences = load_preferences()
    state = _update_state(preferences)
    state["last_checked_at"] = checked_at
    if announced_version is not None:
        state["announced_version"] = announced_version
    preferences["update_check"] = state
    save_preferences(preferences)


async def check_for_background_notice() -> UpdateInfo | None:
    """Return an unannounced feature notice at most once per day.

    The check time is recorded only after the request finishes. Cancelling the
    check therefore leaves the next launch free to try again, while a finished
    network or metadata failure does not retry on every command. Preferences
    are reloaded before writing so a theme change made during the request is kept.
    """
    state = _update_state(load_preferences())
    now = time()
    last_checked = state.get("last_checked_at")
    if isinstance(last_checked, (int, float)) and now - last_checked < CHECK_INTERVAL_SECONDS:
        return None
    announced = state.get("announced_version")

    try:
        async with asyncio.timeout(BACKGROUND_TIMEOUT_SECONDS):
            update = await check_for_update()
    except asyncio.CancelledError:
        raise
    except (TimeoutError, httpx.HTTPError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        try:
            record_update_check(checked_at=now)
        except OSError:
            return None
        return None
    if not update.available or not update.feature_notice or announced == update.latest:
        try:
            record_update_check(checked_at=now)
        except OSError:
            return None
        return None
    return update


def notice_message(update: UpdateInfo) -> str:
    feature = (update.feature_notice or "").rstrip(".")
    return (
        f"Update available: {update.current} → {update.latest} — "
        f"{feature}. Run: copper-pilot update"
    )


def mark_notice_shown(update: UpdateInfo) -> None:
    """Remember a notice only after the user has actually been shown it."""
    try:
        record_update_check(checked_at=time(), announced_version=update.latest)
    except OSError:
        return


async def notify_background_update() -> None:
    """Emit a single non-fatal update notice to stderr when a feature is new.

    Only for headless runs: Textual captures stderr while the app is running.
    The version is marked announced only after the line is written.
    """
    try:
        update = await check_for_background_notice()
    except asyncio.CancelledError:
        raise
    except Exception:  # pragma: no cover - final fail-open boundary
        return
    if update is None:
        return
    print(notice_message(update), file=sys.stderr)
    mark_notice_shown(update)
