"""CopperPilot hosted agent, terminal client, and LangChain adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING

from copper_pilot_cli._version import __version__

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "__version__",
    "cli_main",  # noqa: F822  # resolved lazily by __getattr__
]


def __getattr__(name: str) -> Callable[[], None]:
    """Lazy import for `cli_main` to avoid loading `main.py` at package import.

    `copper_main.py` pulls in Textual and startup machinery
    that isn't needed when submodules like `config` or `widgets` are
    imported directly.

    Returns:
        The requested callable.

    Raises:
        AttributeError: If *name* is not a lazily-provided attribute.

    """
    if name == "cli_main":
        from copper_pilot_cli.copper_main import cli_main

        return cli_main
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
