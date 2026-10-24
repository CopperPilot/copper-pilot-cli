from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from test_tui import FakeRuntime
from textual.widgets import Markdown, OptionList, TextArea

from copper_pilot_cli import copper_theme
from copper_pilot_cli.copper_app import CopperPilotApp
from copper_pilot_cli.copper_preferences import saved_theme
from copper_pilot_cli.copper_presentation import welcome_content
from copper_pilot_cli.copper_theme import ColorMode
from copper_pilot_cli.copper_tools import LocalToolBroker
from copper_pilot_cli.copper_widgets import (
    AssistantMessage,
    ChatInput,
    ErrorMessage,
    build_file_index,
    stream_append_safe,
)


def _app(workspace: Path) -> CopperPilotApp:
    return CopperPilotApp(FakeRuntime(), LocalToolBroker(workspace), workspace, "thread")


def test_every_preset_defines_the_same_tokens() -> None:
    names = copper_theme.theme_names()
    assert names[0] == copper_theme.DEFAULT_THEME == "black"
    assert set(names) == {"black", "warm", "light", "high-contrast"}
    keys = set(copper_theme.PRESETS["black"])
    for name in names:
        assert set(copper_theme.PRESETS[name]) == keys, name
        assert copper_theme.css_variables(theme=name)


def test_default_background_is_neutral_near_black() -> None:
    assert copper_theme.TOKENS["bg"].hex.upper() == "#0A0A0A"
    assert copper_theme.is_dark("black")
    assert not copper_theme.is_dark("light")
    # The unlit working dot uses orange-900. Pale peach on the light background is invisible.
    assert copper_theme.PRESETS["light"]["orange-900"].hex.upper() == "#C47A4A"


def test_switching_theme_changes_welcome_colours() -> None:
    options = {"directory": "repo", "branch": "main", "mode": ColorMode.TRUECOLOR, "ascii": False}
    dark = welcome_content(80, **options)
    copper_theme.set_active_theme("light")
    light = welcome_content(80, **options)
    assert dark.plain == light.plain
    assert [str(span.style) for span in dark.spans] != [str(span.style) for span in light.spans]
    with pytest.raises(KeyError):
        copper_theme.set_active_theme("sepia")


@pytest.mark.asyncio
async def test_theme_command_switches_live_and_persists(tmp_path) -> None:
    app = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        assert app.theme == "copper-pilot"
        await app._handle_submission("/theme light")
        await pilot.pause()
        assert app.current_theme.dark is False
        assert copper_theme.active_theme() == "light"
        assert saved_theme() == "light"

    copper_theme.set_active_theme(copper_theme.DEFAULT_THEME)
    second = _app(tmp_path)
    async with second.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert second.theme == "copper-pilot-light"
        assert copper_theme.active_theme() == "light"


@pytest.mark.asyncio
async def test_unknown_theme_name_reports_the_choices(tmp_path) -> None:
    app = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await app._handle_submission("/theme sepia")
        await pilot.pause()
        errors = list(app.query(ErrorMessage))
        assert errors
        assert copper_theme.active_theme() == "black"
        assert saved_theme() == "black"


@pytest.mark.asyncio
async def test_plain_typing_never_repaints_the_option_list(tmp_path) -> None:
    app = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        area = app.query_one(TextArea)
        area.focus()
        await pilot.pause()
        with patch.object(OptionList, "clear_options", autospec=True) as cleared:
            for _ in range(200):
                area.insert("a")
            await pilot.pause()
        assert cleared.call_count == 0


def test_file_index_prunes_ignored_folders(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "board.kicad_sch").write_text("x", encoding="utf-8")
    for ignored in ("node_modules", ".git", ".venv", "__pycache__"):
        (tmp_path / ignored / "deep").mkdir(parents=True)
        (tmp_path / ignored / "deep" / "board.js").write_text("x", encoding="utf-8")
    visited: list[str] = []
    real_walk = os.walk

    def spy(root, *args, **kwargs):
        for current, dirs, files in real_walk(root, *args, **kwargs):
            visited.append(current)
            yield current, dirs, files

    with patch("copper_pilot_cli.copper_widgets.os.walk", spy):
        index = build_file_index(tmp_path)
    assert index == ["src/board.kicad_sch"]
    assert not any("node_modules" in path or ".git" in path for path in visited)
    (tmp_path / "src" / "board.kicad_pcb").write_text("x", encoding="utf-8")
    assert len(build_file_index(tmp_path, limit=1)) == 1


@pytest.mark.asyncio
async def test_at_completion_uses_the_background_index(tmp_path) -> None:
    (tmp_path / "board.kicad_pcb").write_text("x", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "board.js").write_text("x", encoding="utf-8")
    app = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        composer = app.query_one(ChatInput)
        area = app.query_one(TextArea)
        area.text = "@board"
        area.focus()
        for _ in range(40):
            await pilot.pause(0.02)
            if composer.completion_active:
                break
        assert [item.name for item in composer._matches] == ["@board.kicad_pcb"]


@pytest.mark.asyncio
async def test_streaming_appends_instead_of_reparsing_every_delta(tmp_path) -> None:
    app = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        message = AssistantMessage()
        await app._mount(message)
        await pilot.pause()
        with (
            patch.object(Markdown, "update", autospec=True, wraps=Markdown.update) as update,
            patch.object(Markdown, "append", autospec=True, wraps=Markdown.append) as append,
        ):
            for index in range(500):
                await message.append_content(f"word{index} ")
                if index % 50 == 0:
                    await pilot.pause(0.06)
            await message.stop_stream()
            await pilot.pause()
        assert update.call_count <= 1
        assert append.call_count < 500
        markdown = message.query_one(Markdown)
        assert markdown.source == message.content
        visible = "".join(child.render().plain for child in markdown.children)
        assert visible == message.content.strip()


def _visible_markdown(message: AssistantMessage) -> list[str]:
    markdown = message.query_one(Markdown)
    visible: list[str] = []
    for child in markdown.children:
        code = getattr(child, "code", None)
        visible.append(code if code is not None else child.render().plain)
    return visible


@pytest.mark.asyncio
async def test_streaming_keeps_closed_blocks(tmp_path) -> None:
    app = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        message = AssistantMessage()
        await app._mount(message)
        await pilot.pause()
        for chunk in ("Look\n\n", "```\n", "print(1)\n", "```\n\n", "Done"):
            await message.append_content(chunk)
            await message._flush()
        visible = _visible_markdown(message)
        assert "Look" in visible
        assert "print(1)" in visible
        assert "Done" in visible
        assert stream_append_safe("Look")
        assert not stream_append_safe("Look\n\n")
        assert not stream_append_safe("```\ncode\n```")


@pytest.mark.asyncio
async def test_tab_completes_the_token_just_typed(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "board.kicad_pcb").write_text("b", encoding="utf-8")
    app = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        composer = app.query_one(ChatInput)
        area = app.query_one(TextArea)
        area.focus()
        for _ in range(40):
            await pilot.pause(0.02)
            if composer._file_index is not None:
                break
        area.text = "@"
        await pilot.pause(0.1)
        assert composer._matches
        stale = composer._matches[0].name
        with patch("copper_pilot_cli.copper_widgets.FILE_COMPLETION_DEBOUNCE", 30):
            area.text = "@board"
            await pilot.pause()
            assert composer._file_refresh_timer is not None
            assert composer._matches[0].name == stale
            await pilot.press("tab")
        assert area.text == "@board.kicad_pcb "


@pytest.mark.asyncio
async def test_at_completion_picks_up_a_file_added_during_the_session(tmp_path) -> None:
    (tmp_path / "board.kicad_pcb").write_text("b", encoding="utf-8")
    app = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        composer = app.query_one(ChatInput)
        area = app.query_one(TextArea)
        for _ in range(40):
            await pilot.pause(0.02)
            if composer._file_index is not None:
                break
        (tmp_path / "later.txt").write_text("x", encoding="utf-8")
        composer._file_index_built_at = 0
        area.text = "@later"
        area.focus()
        for _ in range(40):
            await pilot.pause(0.02)
            if any(item.name == "@later.txt" for item in composer._matches):
                break
        assert [item.name for item in composer._matches] == ["@later.txt"]
