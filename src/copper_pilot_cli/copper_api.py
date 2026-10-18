"""Small authenticated HTTP surface used by the CopperPilot TUI."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx

from copper_pilot_cli.copper_auth import DeviceCredential
from copper_pilot_cli.copper_protocol import error_message


async def token_limits(
    credential: DeviceCredential, *, request_timeout: float = 15
) -> dict[str, Any]:
    """Fetch the account's authoritative token/usage limits."""
    async with httpx.AsyncClient(timeout=request_timeout) as client:
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


def is_usage_block(status: Any, block_reason: Any) -> bool:
    """A 402 or 429 that names a block reason is a usage limit.

    A bare 402 is not: the Intelligence plan gate is a 402 without one.
    """
    return status in {402, 429} and bool(block_reason)


def limits_reset_time(value: dict[str, Any]) -> str | None:
    """Return when the block in a token-limits payload lifts, if it is timed."""
    reason = value.get("block_reason")
    if reason == "weekly":
        reset = value.get("week_ends_at")
    elif reason == "window":
        reset = value.get("window_ends_at")
    else:
        reset = None
    return reset if isinstance(reset, str) and reset.strip() else None


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _until(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "in under a minute"
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"in {days}d {hours}h" if hours else f"in {days}d"
    if hours:
        return f"in {hours}h {minutes}m" if minutes else f"in {hours}h"
    return f"in {minutes}m"


def describe_reset(value: str | None, now: datetime | None = None) -> str | None:
    """Render a reset timestamp as local wall time plus a countdown.

    ``"at 3:20 AM (in 2h 23m)"`` today, ``"on Oct 21 at 3:20 AM (in 2d 4h)"``
    on a later day. Returns None for a missing, unparsable, or past time.
    """
    reset = _parse_time(value) if value else None
    if reset is None:
        return None
    current = now or datetime.now().astimezone()
    if reset <= current:
        return None
    local = reset.astimezone(current.tzinfo)
    clock = local.strftime("%I:%M %p").lstrip("0")
    day = "" if local.date() == current.date() else f"on {local.strftime('%b')} {local.day} "
    return f"{day}at {clock} ({_until(reset - current)})"


def usage_limit_message(
    *,
    block_reason: str | None,
    resets_at: str | None,
    can_purchase_overage: bool | None,
    hosted_message: str,
    now: datetime | None = None,
) -> str:
    """Explain a usage block: which limit is spent and when it lifts.

    Window and weekly blocks get local reset times in place of the hosted
    copy's raw UTC timestamp. Other blocks (team cost cap, billing, busy, seat)
    have no reset time, so the hosted copy stands.
    """
    when = describe_reset(resets_at, now)
    if block_reason == "weekly":
        lead = "CopperPilot weekly usage limit reached."
    elif block_reason == "window":
        lead = "CopperPilot usage limit reached: the current usage window is used up."
    else:
        return hosted_message.strip() or "CopperPilot usage limit reached."
    if when:
        text = f"{lead} It resets {when}."
    else:
        text = f"{lead} Reset time unavailable; try again later."
    if can_purchase_overage:
        text += " You can buy additional usage from your CopperPilot billing page."
    return text


def usage_error_message(value: Any) -> str:
    """Read a hosted error event's message, explaining usage blocks locally."""
    message = error_message(value)
    if not isinstance(value, dict):
        return message
    reason = value.get("block_reason")
    if not is_usage_block(value.get("status"), reason):
        return message
    resets_at = value.get("resets_at")
    return usage_limit_message(
        block_reason=reason if isinstance(reason, str) else None,
        resets_at=resets_at if isinstance(resets_at, str) else None,
        can_purchase_overage=value.get("can_purchase_overage") is True,
        hosted_message=message,
    )
