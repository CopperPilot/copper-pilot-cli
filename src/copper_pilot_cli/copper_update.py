"""PyPI-backed CopperPilot update checks."""

from __future__ import annotations

import sys
from dataclasses import dataclass

import httpx
from packaging.version import Version

from copper_pilot_cli._version import PYPI_URL, __version__


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    current: str
    latest: str
    available: bool
    install_command: str


async def check_for_update() -> UpdateInfo:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(PYPI_URL)
        response.raise_for_status()
        latest = str(response.json()["info"]["version"])
    return UpdateInfo(
        current=__version__,
        latest=latest,
        available=Version(latest) > Version(__version__),
        install_command=f"{sys.executable} -m pip install --upgrade copper-pilot-cli",
    )
