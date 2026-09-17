"""Browser device login and private credential persistence."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import platform
import re
import socket
import stat
import tempfile
import time
import webbrowser
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from copper_pilot_cli.copper_config import api_base_url, paths

_STORE_VERSION = 1
_KEY_PREFIX = "cf_live_"
_FLOW_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~-]{8,512}$")


class AuthenticationError(RuntimeError):
    """Authentication could not be completed."""


@dataclass(frozen=True, slots=True)
class DeviceCredential:
    """Credential returned by CopperPilot's device login."""

    api_key: str
    fingerprint: str
    base_url: str
    issued_at: str | None = None
    expires_at: str | None = None


def _total_memory() -> int:
    if os.name == "posix":
        with contextlib.suppress(ValueError, OSError, AttributeError):
            return int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
    return 0


def safe_hostname() -> str:
    """Return the same URL-safe hostname shape as the desktop client."""
    value = re.sub(r"[^a-z0-9-]", "_", socket.gethostname().lower())
    value = re.sub(r"_{2,}", "_", value).strip("_")[:50]
    return value or "copper_pilot_client"


def build_fingerprint() -> str:
    """Build a stable, non-secret device fingerprint."""
    signature = "||".join(
        [
            platform.system().lower(),
            platform.machine().lower(),
            str(_total_memory()),
            platform.processor() or "unknown",
        ]
    )
    return f"{safe_hostname()}_{hashlib.sha256(signature.encode()).hexdigest()}"


def normalize_flow_token(value: object) -> str:
    """Validate a device-login flow token before it enters a URL."""
    token = str(value or "").strip()
    if token.lower() in {"", "null", "undefined"} or not _FLOW_TOKEN_RE.fullmatch(token):
        raise AuthenticationError("The device login flow token is invalid.")
    return token


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(stat.S_IRWXU)


def save_credential(credential: DeviceCredential, target: Path | None = None) -> tuple[str, ...]:
    """Atomically save a credential with dcode-equivalent file protections."""
    destination = target or paths().auth
    _private_dir(destination.parent)
    payload = json.dumps(
        {"version": _STORE_VERSION, "credential": asdict(credential)},
        separators=(",", ":"),
    ).encode()
    warnings: list[str] = []
    fd, temporary_name = tempfile.mkstemp(prefix="auth-", suffix=".tmp", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        if os.name != "nt":
            temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
        temporary.replace(destination)
        if os.name != "nt":
            destination.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    if os.name != "nt":
        mode = stat.S_IMODE(destination.stat().st_mode)
        if mode != 0o600:
            warnings.append(f"Could not restrict {destination} to mode 0600.")
    return tuple(warnings)


def load_credential(target: Path | None = None) -> DeviceCredential | None:
    """Load and validate the stored device credential."""
    destination = target or paths().auth
    try:
        data = json.loads(destination.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthenticationError(f"Credential store is unreadable: {destination}") from exc
    if not isinstance(data, dict) or data.get("version") != _STORE_VERSION:
        raise AuthenticationError("Credential store has an unsupported format.")
    item = data.get("credential")
    if not isinstance(item, dict):
        raise AuthenticationError("Credential store has no device credential.")
    credential = DeviceCredential(
        api_key=str(item.get("api_key") or ""),
        fingerprint=str(item.get("fingerprint") or ""),
        base_url=str(item.get("base_url") or ""),
        issued_at=item.get("issued_at"),
        expires_at=item.get("expires_at"),
    )
    if not credential.api_key.startswith(_KEY_PREFIX) or not credential.fingerprint:
        raise AuthenticationError("Stored CopperPilot credential is invalid.")
    return credential


def clear_credential(target: Path | None = None) -> bool:
    """Delete the stored credential."""
    destination = target or paths().auth
    try:
        destination.unlink()
    except FileNotFoundError:
        return False
    return True


async def validate_credential(credential: DeviceCredential) -> bool:
    """Validate credential lifecycle status without logging secret content."""
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{credential.base_url}/api/copperpilot/analyze/token_limits",
            headers={
                "Authorization": f"Bearer {credential.api_key}",
                "X-CopperPilot-Fingerprint": credential.fingerprint,
            },
        )
    if response.status_code in {401, 403}:
        return False
    response.raise_for_status()
    return True


class DeviceLogin:
    """Run CopperPilot's browser-based device login flow."""

    def __init__(self, base_url: str | None = None, client_version: str = "0.1.0") -> None:
        self.base_url = (base_url or api_base_url()).rstrip("/")
        self.client_version = client_version

    async def login(self, *, open_browser: bool = True) -> DeviceCredential:
        """Start a flow, open the browser, and poll until ready."""
        fingerprint = build_fingerprint()
        endpoint = f"{self.base_url}/api/copperpilot/auth/api-keys-device-login-flow"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                endpoint,
                json={
                    "host_name": safe_hostname(),
                    "fingerprint": fingerprint,
                    "platform": platform.system().lower(),
                    "client_version": self.client_version,
                },
            )
            response.raise_for_status()
            body = response.json()
            token = normalize_flow_token(body.get("flow_token"))
            if open_browser:
                webbrowser.open(f"{self.base_url}/createKey?flow_token={quote(token)}")
            started = time.monotonic()
            expiry_seconds = min(
                max(int(body.get("expires_in_seconds") or body.get("expires_in") or 300), 1),
                900,
            )
            while time.monotonic() - started < expiry_seconds:
                elapsed = time.monotonic() - started
                delay = 0.3 if elapsed < 10 else 1.0
                poll = await client.get(endpoint, params={"flow_token": token})
                if poll.status_code == 200:
                    result: dict[str, Any] = poll.json()
                    key = str(result.get("api_key") or "")
                    if result.get("status") == "ready" and key.startswith(_KEY_PREFIX):
                        credential = DeviceCredential(
                            api_key=key,
                            fingerprint=fingerprint,
                            base_url=self.base_url,
                            issued_at=result.get("issued_at"),
                            expires_at=result.get("expires_at"),
                        )
                        save_credential(credential)
                        return credential
                elif poll.status_code in {401, 410}:
                    raise AuthenticationError("Device login expired or was rejected.")
                elif poll.status_code == 409:
                    pending = poll.json()
                    if pending.get("error_code") not in {None, "FLOW_PENDING"}:
                        raise AuthenticationError(
                            str(pending.get("message") or "Device login failed.")
                        )
                    with contextlib.suppress(ValueError, TypeError, json.JSONDecodeError):
                        retry = pending.get("retry_after_ms")
                        if retry is not None:
                            delay = max(0.2, min(float(retry) / 1000, 5))
                else:
                    poll.raise_for_status()
                await asyncio.sleep(delay)
        raise AuthenticationError("Device login timed out.")
