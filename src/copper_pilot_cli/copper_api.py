"""Small authenticated HTTP surface used by the CopperPilot TUI."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from copper_pilot_cli.copper_auth import DeviceCredential


async def token_limits(credential: DeviceCredential) -> dict[str, Any]:
    """Fetch the account's authoritative token/usage limits."""
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{credential.base_url}/api/copperpilot/analyze/token_limits",
            headers={
                "Authorization": f"Bearer {credential.api_key}",
                "X-CopperPilot-Fingerprint": credential.fingerprint,
            },
        )
        response.raise_for_status()
        value = response.json()
    return value if isinstance(value, dict) else {}


def _percentage(value: Any) -> str:
    try:
        number = min(100.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        number = 0.0
    return f"{number:.0f}%"


def _local_time(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    local = parsed.astimezone()
    clock = local.strftime("%I:%M %p").lstrip("0")
    return f"{local.strftime('%b')} {local.day}, {local.year} at {clock}"


def format_token_limits(value: dict[str, Any]) -> str:
    """Render the member-safe usage contract without exposing raw token counts."""
    lines: list[str] = []
    window_open = bool(value.get("window_open"))
    block_reason = value.get("block_reason")
    if window_open or block_reason in {"window", "weekly"} or value.get("overage_valid"):
        lines.append(
            f"Current usage window: {_percentage(value.get('window_usage_percentage'))} used"
        )
    else:
        lines.append("Current usage window: Ready")

    window_end = _local_time(value.get("window_ends_at"))
    if window_end and (window_open or block_reason == "window"):
        lines.append(f"Usage limit refreshes: {window_end}")

    week_end = _local_time(value.get("week_ends_at"))
    if week_end:
        label = "Blocked until" if block_reason == "weekly" else "Weekly balance refreshes"
        lines.append(f"{label}: {week_end}")

    if value.get("overage_valid"):
        lines.append(f"Additional usage: {_percentage(value.get('overage_usage_percentage'))} used")

    public_message = value.get("public_message")
    if isinstance(public_message, str) and public_message.strip():
        lines.append(f"Status: {public_message.strip()}")
    if value.get("can_purchase_overage"):
        lines.append("Additional usage is available from your CopperPilot billing page.")
    return "\n".join(lines)
