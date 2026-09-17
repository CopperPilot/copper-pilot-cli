"""Small authenticated HTTP surface used by the CopperPilot TUI."""

from __future__ import annotations

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
