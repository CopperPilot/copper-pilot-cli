"""Small non-secret CLI preference store."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

from copper_pilot_cli.copper_config import paths
from copper_pilot_cli.copper_tools import ApprovalMode

YOLO_ACK_VERSION = 1
AUTO_NOTICE_VERSION = 1


def _path() -> Path:
    return paths().state / "config.json"


def load_preferences() -> dict[str, Any]:
    try:
        value = json.loads(_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def save_preferences(value: dict[str, Any]) -> None:
    destination = _path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.parent.chmod(stat.S_IRWXU)
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
    temporary.replace(destination)


def saved_approval_mode() -> ApprovalMode:
    raw = load_preferences().get("approval_mode")
    return ApprovalMode.AUTO if raw == ApprovalMode.AUTO.value else ApprovalMode.MANUAL


def save_approval_mode(mode: ApprovalMode) -> None:
    value = load_preferences()
    value["approval_mode"] = (
        ApprovalMode.AUTO.value if mode is ApprovalMode.AUTO else ApprovalMode.MANUAL.value
    )
    save_preferences(value)


def shell_auto_allow_settings() -> tuple[bool, bool]:
    """Return CLI-owned normal and dangerous shell Auto settings."""
    value = load_preferences()
    return (
        value.get("always_allow_shell_commands", True) is True,
        value.get("always_allow_dangerous_shell_commands", False) is True,
    )


def save_shell_auto_allow_settings(*, normal: bool, dangerous: bool) -> None:
    value = load_preferences()
    value["always_allow_shell_commands"] = normal
    value["always_allow_dangerous_shell_commands"] = dangerous
    save_preferences(value)


def auto_notice_acknowledged() -> bool:
    return load_preferences().get("auto_notice_version") == AUTO_NOTICE_VERSION


def acknowledge_auto_notice() -> None:
    value = load_preferences()
    value["auto_notice_version"] = AUTO_NOTICE_VERSION
    save_preferences(value)


def saved_theme() -> str:
    """The saved ``/theme`` preset, or ``black`` when missing or unknown."""
    from copper_pilot_cli.copper_theme import DEFAULT_THEME, PRESETS

    value = load_preferences().get("theme")
    return value if isinstance(value, str) and value in PRESETS else DEFAULT_THEME


def save_theme(name: str) -> None:
    value = load_preferences()
    value["theme"] = name
    save_preferences(value)


def tips_enabled() -> bool:
    """Tips stay on unless ``config.json`` sets ``tips`` to false."""
    return load_preferences().get("tips") is not False


def last_tip_index() -> int | None:
    """Index of the tip shown last session, if one was stored."""
    value = load_preferences().get("last_tip_index")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def save_last_tip_index(index: int) -> None:
    """Remember which tip was shown so the next session can skip it."""
    value = load_preferences()
    value["last_tip_index"] = index
    save_preferences(value)


def yolo_acknowledged() -> bool:
    return load_preferences().get("yolo_ack_version") == YOLO_ACK_VERSION


def acknowledge_yolo() -> None:
    value = load_preferences()
    value["yolo_ack_version"] = YOLO_ACK_VERSION
    save_preferences(value)
