from __future__ import annotations

import argparse
import asyncio
import time
from types import SimpleNamespace

import pytest

from copper_pilot_cli import copper_main, copper_update


def test_validate_feature_notice_accepts_only_a_short_single_line() -> None:
    assert copper_update.validate_feature_notice(" Added board review shortcuts. ") == (
        "Added board review shortcuts."
    )
    assert copper_update.validate_feature_notice("line one\nline two") is None
    assert copper_update.validate_feature_notice("x" * 161) is None
    assert copper_update.validate_feature_notice(42) is None


@pytest.mark.asyncio
async def test_background_notice_is_throttled_and_announced_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    update = copper_update.UpdateInfo(
        current="1.0.0",
        latest="1.1.0",
        available=True,
        install_command="pip install --upgrade copper-pilot-cli",
        feature_notice="Added board review shortcuts.",
    )
    monkeypatch.setattr(copper_update, "check_for_update", lambda: _return(update))

    assert await copper_update.check_for_background_notice() == update
    copper_update.mark_notice_shown(update)
    assert await copper_update.check_for_background_notice() is None


@pytest.mark.asyncio
async def test_background_notice_failures_are_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail() -> copper_update.UpdateInfo:
        raise RuntimeError("offline")

    monkeypatch.setattr(copper_update, "check_for_background_notice", fail)
    await copper_update.notify_background_update()


@pytest.mark.asyncio
async def test_background_notice_is_capped_on_a_stalled_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def stall(**_kwargs: object) -> copper_update.UpdateInfo:
        await asyncio.sleep(60)
        raise AssertionError("unreachable")

    monkeypatch.setattr(copper_update, "BACKGROUND_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(copper_update, "check_for_update", stall)

    started = time.perf_counter()
    assert await copper_update.check_for_background_notice() is None
    assert time.perf_counter() - started < 1


@pytest.mark.asyncio
async def test_interactive_app_shows_notice_as_literal_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from copper_pilot_cli import copper_app

    update = copper_update.UpdateInfo(
        current="1.0.0",
        latest="1.1.0",
        available=True,
        install_command="pip install --upgrade copper-pilot-cli",
        feature_notice="Added [shortcuts](https://example.invalid).",
    )
    shown: list[copper_update.UpdateInfo] = []
    notified: list[tuple[str, bool]] = []

    async def found() -> copper_update.UpdateInfo:
        return update

    monkeypatch.setattr(copper_app, "check_for_background_notice", found)
    monkeypatch.setattr(copper_app, "mark_notice_shown", shown.append)
    fake_app = SimpleNamespace(
        is_running=True,
        notify=lambda text, **kwargs: notified.append((text, kwargs.get("markup", True))),
    )

    await copper_app.CopperPilotApp._show_update_notice(fake_app)

    assert notified == [(copper_update.notice_message(update), False)]
    assert shown == [update]


def test_update_subcommand_parses_check_and_yes() -> None:
    args = copper_main.command_parser("update").parse_args(["--check", "--yes"])
    assert args.command == "update"
    assert args.check is True
    assert args.yes is True


@pytest.mark.asyncio
async def test_update_check_is_read_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    update = copper_update.UpdateInfo(
        current="1.0.0",
        latest="1.1.0",
        available=True,
        install_command="pip install --upgrade copper-pilot-cli",
        feature_notice="Added board review shortcuts.",
    )
    monkeypatch.setattr(copper_main, "check_for_update", lambda: _return(update))

    result = await copper_main._update_command(argparse.Namespace(check=True, yes=False))

    assert result == 0
    assert "1.0.0 → 1.1.0" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_cancelled_check_does_not_consume_the_daily_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def stall(**_kwargs: object) -> copper_update.UpdateInfo:
        await asyncio.sleep(60)
        raise AssertionError("unreachable")

    monkeypatch.setattr(copper_update, "check_for_update", stall)
    task = asyncio.create_task(copper_update.check_for_background_notice())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "update_check" not in copper_update.load_preferences()


@pytest.mark.asyncio
async def test_announcement_keeps_preferences_saved_during_the_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    update = copper_update.UpdateInfo(
        current="1.0.0",
        latest="1.1.0",
        available=True,
        install_command="pip install --upgrade copper-pilot-cli",
        feature_notice="Added board review shortcuts.",
    )

    async def change_theme() -> copper_update.UpdateInfo:
        preferences = copper_update.load_preferences()
        preferences["theme"] = "paper"
        copper_update.save_preferences(preferences)
        return update

    monkeypatch.setattr(copper_update, "check_for_update", change_theme)
    assert await copper_update.check_for_background_notice() == update
    copper_update.mark_notice_shown(update)

    saved = copper_update.load_preferences()
    assert saved["theme"] == "paper"
    assert saved["update_check"]["announced_version"] == "1.1.0"


def test_release_metadata_rejects_unsafe_versions_and_bad_notices() -> None:
    assert copper_update.release_notice_url("../secret") is None
    assert copper_update.release_notice_url("1.2.3").endswith("/1.2.3/release-notice.json")
    payload = {
        "info": {"version": "2.0.0rc1", "yanked": False},
        "releases": {
            "1.2.0": [{"yanked": True}],
            "1.1.0": [{"yanked": False}],
            "2.0.0rc1": [{"yanked": False}],
        },
    }
    assert copper_update.select_latest_version(payload) == "1.1.0"
    assert copper_update.build_release_notice({}, "1.2.0") == {"version": "1.2.0", "feature": None}
    with pytest.raises(ValueError):
        copper_update.build_release_notice({"1.2.0": "first\nsecond"}, "1.2.0")


@pytest.mark.asyncio
async def test_notice_goes_to_stderr_only_after_it_is_shown(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    update = copper_update.UpdateInfo(
        current="1.0.0",
        latest="1.1.0",
        available=True,
        install_command="pip install --upgrade copper-pilot-cli",
        feature_notice="Added board review shortcuts.",
    )
    monkeypatch.setattr(copper_update, "check_for_background_notice", lambda: _return(update))

    await copper_update.notify_background_update()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Added board review shortcuts." in captured.err
    assert copper_update.load_preferences()["update_check"]["announced_version"] == "1.1.0"


@pytest.mark.asyncio
async def test_update_command_uses_the_detected_installer_without_a_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    update = copper_update.UpdateInfo(
        current="1.0.0",
        latest="1.1.0",
        available=True,
        install_command="pip install --upgrade copper-pilot-cli",
        feature_notice=None,
    )
    seen: dict[str, object] = {}

    class Process:
        async def wait(self) -> int:
            return 3

    async def fake_exec(*args: str, **kwargs: object) -> Process:
        seen["args"] = args
        seen["kwargs"] = kwargs
        return Process()

    monkeypatch.setattr(copper_main, "check_for_update", lambda: _return(update))
    monkeypatch.setattr(copper_main, "_editable_installation", lambda: False)
    monkeypatch.setattr(
        copper_main,
        "update_install_command",
        lambda: ["pipx", "upgrade", "copper-pilot-cli"],
    )
    monkeypatch.setattr(copper_main.asyncio, "create_subprocess_exec", fake_exec)

    result = await copper_main._update_command(argparse.Namespace(check=False, yes=True))

    assert result == 3
    assert seen["args"] == ("pipx", "upgrade", "copper-pilot-cli")
    assert seen["kwargs"] == {"stdin": asyncio.subprocess.DEVNULL}


@pytest.mark.asyncio
async def test_update_requires_confirmation_and_refuses_editable_installs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    update = copper_update.UpdateInfo(
        current="1.0.0",
        latest="1.1.0",
        available=True,
        install_command="pip install --upgrade copper-pilot-cli",
        feature_notice=None,
    )
    monkeypatch.setattr(copper_main, "check_for_update", lambda: _return(update))
    monkeypatch.setattr(copper_main, "_editable_installation", lambda: False)
    monkeypatch.setattr(copper_main.sys.stdin, "isatty", lambda: False)

    assert await copper_main._update_command(argparse.Namespace(check=False, yes=False)) == 2
    assert "Use --yes" in capsys.readouterr().out

    monkeypatch.setattr(copper_main.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(copper_main.Confirm, "ask", lambda *_args, **_kwargs: False)
    assert await copper_main._update_command(argparse.Namespace(check=False, yes=False)) == 0

    monkeypatch.setattr(copper_main, "_editable_installation", lambda: True)
    assert await copper_main._update_command(argparse.Namespace(check=False, yes=True)) == 2
    assert "editable install" in capsys.readouterr().out


def test_pip_and_pipx_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(copper_main, "_editable_installation", lambda: False)
    monkeypatch.setattr(copper_main.sys, "prefix", "/Users/dev/.local/pipx/venvs/copper-pilot-cli")
    monkeypatch.setattr(
        copper_main.shutil, "which", lambda command: "/bin/pipx" if command == "pipx" else None
    )
    assert copper_main.update_install_command() == ["pipx", "upgrade", "copper-pilot-cli"]

    monkeypatch.setattr(copper_main, "_pipx_installation", lambda: False)
    monkeypatch.setattr(
        copper_main.importlib.util, "find_spec", lambda name: object() if name == "pip" else None
    )
    command = copper_main.update_install_command()
    assert command is not None
    assert command[1:] == ["-m", "pip", "install", "--upgrade", "copper-pilot-cli"]

    monkeypatch.setattr(copper_main.importlib.util, "find_spec", lambda _name: None)
    assert copper_main.update_install_command() is None


async def _return(value: copper_update.UpdateInfo) -> copper_update.UpdateInfo:
    return value


def test_notice_message_does_not_double_the_period() -> None:
    update = copper_update.UpdateInfo(
        current="1.2.0",
        latest="1.3.0",
        available=True,
        install_command="pip install --upgrade copper-pilot-cli",
        feature_notice="Added board-review shortcuts.",
    )

    assert copper_update.notice_message(update) == (
        "Update available: 1.2.0 → 1.3.0 — Added board-review shortcuts. Run: copper-pilot update"
    )
