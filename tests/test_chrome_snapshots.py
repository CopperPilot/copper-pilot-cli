"""Snapshots of the v3 welcome, status line, and overlay states."""

from __future__ import annotations

import asyncio
import os
import random
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import Static

from copper_pilot_cli.copper_app import CopperPilotApp
from copper_pilot_cli.copper_chrome import (
    APPROVAL_OPTIONS,
    TIPS,
    approval_hint,
    content_plain,
    pick_tip_index,
    rank_slash,
    rule_content,
    slash_catalog,
    slash_footer,
    slash_row_content,
    status_content,
    tip_content,
    welcome_content,
    working_content,
)
from copper_pilot_cli.copper_logo import logo_lines
from copper_pilot_cli.copper_preferences import (
    last_tip_index,
    load_preferences,
    save_last_tip_index,
    save_preferences,
    tips_enabled,
)
from copper_pilot_cli.copper_presentation import (
    LoadingWidget,
    StatusBar,
    WelcomeBanner,
    _git_branch,
)
from copper_pilot_cli.copper_protocol import CopperMode
from copper_pilot_cli.copper_theme import TOKENS, ColorMode, ascii_mode
from copper_pilot_cli.copper_tools import ApprovalMode, ApprovalRequest, LocalToolBroker
from copper_pilot_cli.copper_widgets import ApprovalMenu, ChatInput

SNAPSHOTS = Path(__file__).parent / "snapshots"


def _digest(content) -> str:
    body = [content.plain, "---"]
    body.extend(f"{span.start}:{span.end}:{span.style}" for span in content.spans)
    return "\n".join(body) + "\n"


def _check(name: str, text: str) -> None:
    path = SNAPSHOTS / name
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return
    assert path.read_text(encoding="utf-8") == text


@pytest.mark.parametrize("width", (120, 80, 50))
@pytest.mark.parametrize(
    ("label", "mode", "ascii"),
    (
        ("color", ColorMode.TRUECOLOR, False),
        ("nocolor", ColorMode.NONE, False),
        ("ascii", ColorMode.TRUECOLOR, True),
    ),
)
def test_idle_welcome_snapshot(width: int, label: str, mode: ColorMode, ascii: bool) -> None:
    content = welcome_content(
        width,
        directory="copper-pilot-cli",
        branch="main",
        mode=mode,
        ascii=ascii,
    )
    _check(f"welcome-{width}-{label}.txt", _digest(content))
    plain = content.plain
    assert "CopperPilot" in plain
    assert "hardware design agent" in plain
    assert "copper-pilot-cli · main" in plain
    assert "quick start" in plain
    assert "v0." not in plain
    if width >= 60 and label == "color":
        assert all(row in plain for row in logo_lines())
        assert content.spans
    else:
        assert "⣀" not in plain
    if width < 60:
        assert "see every command" not in plain
        assert "/help" in plain
    if mode is ColorMode.NONE:
        assert not content.spans
    if ascii:
        assert "✎" not in plain
        assert "ctrl+e" in plain


def test_tip_picker_never_repeats_the_same_tip() -> None:
    rng = random.Random(0)  # noqa: S311
    previous: int | None = None
    for _ in range(200):
        index = pick_tip_index(previous, choose=rng.choice)
        assert index != previous
        assert 0 <= index < len(TIPS)
        previous = index

    offered: list[int] = []

    def choose(options: list[int]) -> int:
        offered.extend(options)
        return options[-1]

    assert pick_tip_index(1, count=4, choose=choose) == 3
    assert offered == [0, 2, 3]


def test_tips_are_one_sentence_and_skip_missing_features() -> None:
    assert len(TIPS) >= 2
    for tip in TIPS:
        assert len(tip) < 90
        assert "!" not in tip
        lowered = tip.lower()
        assert "queued" not in lowered
        assert "pro tip" not in lowered


def test_tips_flag_and_last_index_round_trip(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COPPER_PILOT_CLI_HOME", str(tmp_path / "home"))
    assert tips_enabled() is True
    assert last_tip_index() is None
    save_preferences({"tips": False, "approval_mode": "manual"})
    assert tips_enabled() is False
    assert load_preferences()["approval_mode"] == "manual"
    save_preferences({"tips": True, "approval_mode": "manual"})
    save_last_tip_index(2)
    assert tips_enabled() is True
    assert last_tip_index() == 2


def test_rank_slash_fuzzy_ignores_case() -> None:
    ranked = rank_slash("/rb", [("/ReviewBoard", "Skill")])
    assert ranked == [("/ReviewBoard", "Skill")]


def test_working_line_keeps_the_stop_hint() -> None:
    content = working_content(0, "x" * 200, 3, width=40, mode=ColorMode.NONE, ascii=False)
    assert content.plain.endswith("esc to stop")
    assert len(content.plain) <= 40


def test_git_branch_follows_a_gitdir_file(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    gitdir = tmp_path / "modules" / "proj"
    workspace = parent / "proj"
    parent.mkdir()
    (parent / ".git").mkdir()
    (parent / ".git" / "HEAD").write_text("ref: refs/heads/parent\n", encoding="utf-8")
    gitdir.mkdir(parents=True)
    (gitdir / "HEAD").write_text("ref: refs/heads/feature\n", encoding="utf-8")
    workspace.mkdir()
    (workspace / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    assert _git_branch(workspace) == "feature"

    relative = tmp_path / "relative"
    relative.mkdir()
    linked = relative / "repo"
    store = relative / "store"
    linked.mkdir()
    store.mkdir()
    (store / "HEAD").write_text("ref: refs/heads/linked\n", encoding="utf-8")
    (linked / ".git").write_text("gitdir: ../store\n", encoding="utf-8")
    assert _git_branch(linked) == "linked"


def _stack(*contents) -> str:
    chunks = [_digest(content).rstrip("\n") for content in contents]
    return "\n\n".join(chunks) + "\n"


@pytest.mark.parametrize("width", (120, 80, 50))
@pytest.mark.parametrize(
    ("label", "mode", "ascii"),
    (
        ("color", ColorMode.TRUECOLOR, False),
        ("nocolor", ColorMode.NONE, False),
        ("ascii", ColorMode.TRUECOLOR, True),
    ),
)
def test_idle_input_and_tip_snapshots(width: int, label: str, mode: ColorMode, ascii: bool) -> None:
    tip = tip_content(width, TIPS[2], mode=mode, ascii=ascii)
    rule = rule_content(width, busy=False, mode=mode, ascii=ascii)
    busy = rule_content(width, busy=True, mode=mode, ascii=ascii)
    _check(f"idle-input-{width}-{label}.txt", _stack(tip, rule, busy))
    if width < 60:
        assert tip.plain == ""
    else:
        assert tip.plain.startswith("tip  ")
    mark = "-" if ascii else "─"
    assert rule.plain == mark * width
    assert busy.plain == mark * width
    if mode is ColorMode.NONE:
        assert not tip.spans
        assert not rule.spans
        assert not busy.spans
    elif width:
        assert any(TOKENS["rule"].hex in str(span.style) for span in rule.spans)
        assert any(TOKENS["divider"].hex in str(span.style) for span in busy.spans)


@pytest.mark.parametrize("chat_mode", ("plan", "ask", "agent"))
@pytest.mark.parametrize("effort", ("balanced", "intelligence"))
def test_status_mode_and_effort_snapshots(chat_mode: str, effort: str) -> None:
    content = status_content(
        120,
        chat_mode=chat_mode,
        effort=effort,
        mode=ColorMode.TRUECOLOR,
        ascii=False,
    )
    _check(f"status-{chat_mode}-{effort}.txt", _digest(content))
    plain = content.plain
    assert chat_mode in plain
    assert "shift+tab" in plain
    assert ("intelligent" if effort == "intelligence" else "balanced") in plain
    assert "ctrl+e" in plain
    assert "/help" in plain
    assert "ask before edits" not in plain
    narrow = content_plain(
        status_content(30, chat_mode=chat_mode, effort=effort, mode=ColorMode.NONE, ascii=False)
    )
    assert chat_mode not in narrow
    assert "shift+tab" not in narrow


def test_ascii_mode_follows_an_explicit_request_or_a_non_utf8_locale(monkeypatch) -> None:
    monkeypatch.delenv("COPPERPILOT_ASCII", raising=False)
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    assert ascii_mode() is False
    monkeypatch.setenv("COPPERPILOT_ASCII", "1")
    assert ascii_mode() is True
    monkeypatch.delenv("COPPERPILOT_ASCII", raising=False)
    monkeypatch.setenv("LC_ALL", "C")
    assert ascii_mode() is True


def test_slash_working_and_approval_snapshots() -> None:
    catalog = slash_catalog(include_model=False)
    rows = [
        content_plain(
            slash_row_content(name, description, 72, selected=index == 0, mode=ColorMode.NONE)
        )
        for index, (name, description) in enumerate(catalog[:6])
    ]
    slash = "\n".join(rows) + "\n" + slash_footer(ascii=False) + "\n"
    _check("slash-menu.txt", slash)
    assert len(rows) == 6
    assert "esc close" in slash

    working = working_content(1, "Reading power_stage.kicad_sch", 12, mode=ColorMode.TRUECOLOR)
    _check("working-color.txt", _digest(working))
    assert "esc to stop" in working.plain
    assert working.plain.count("•") == 3

    approval = "\n".join(
        [
            "Run command?",
            "kicad-cli version",
            "  ".join(APPROVAL_OPTIONS),
            approval_hint(ascii=False),
            "Apply edit to board.kicad_sch?",
        ]
    )
    _check("approval.txt", approval + "\n")
    assert "always for this file" not in approval
    assert "auto-accept edits" in approval


class _FakeAgent:
    credential = None
    mode = CopperMode.AGENT
    workflow = None
    skill_manifest_id = None
    plan_context = None
    runtime_context = None


class _FakeRuntime:
    def __init__(self) -> None:
        self.agent = _FakeAgent()

    def config(self, thread_id, workspace):
        return {"configurable": {"thread_id": thread_id}}

    async def aget_state(self, _config):
        return SimpleNamespace(values={})

    async def acancel(self):
        return None

    async def astream(self, _values, _config):
        if False:
            yield ()


def _has_color(widget) -> bool:
    rendered = widget.render()
    return bool(getattr(rendered, "spans", None))


@pytest.mark.asyncio
@pytest.mark.parametrize("columns", (120, 80, 50))
@pytest.mark.parametrize("no_color", (False, True))
async def test_idle_screen_at_widths(tmp_path, columns: int, no_color: bool, monkeypatch) -> None:
    if no_color:
        monkeypatch.setenv("NO_COLOR", "1")
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.delenv("COPPERPILOT_ASCII", raising=False)
    app = CopperPilotApp(
        _FakeRuntime(),
        LocalToolBroker(tmp_path, mode=ApprovalMode.MANUAL),
        tmp_path,
        "thread",
    )
    async with app.run_test(size=(columns, 36)) as pilot:
        await pilot.pause()
        welcome = str(app.query_one(WelcomeBanner).render())
        status = str(app.query_one(StatusBar).render())
        assert "CopperPilot" in welcome
        assert "hardware design agent" in welcome
        assert "quick start" in welcome
        assert "agent" in status
        assert "/help" in status or columns < 40
        assert app.no_color is no_color
        composer = app.query_one(ChatInput)
        mark = "─"
        rules = [widget.render().plain for widget in composer.query(".input-rule")]
        assert rules == [mark * columns, mark * columns]
        if columns >= 60:
            assert composer.has_class("-tips")
            assert str(composer.query_one("#tip-line").render()).startswith("tip  ")
        else:
            assert not composer.has_class("-tips")
        if not no_color:
            top = composer.query_one("#rule-top").render()
            assert any(TOKENS["rule"].hex in str(span.style) for span in top.spans)
        if no_color or columns < 60:
            assert "⣀" not in welcome
        else:
            assert _has_color(app.query_one(WelcomeBanner))


@pytest.mark.asyncio
async def test_tips_false_hides_the_line_and_a_message_dismisses_it(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COPPER_PILOT_CLI_HOME", str(tmp_path / "home"))
    save_preferences({"tips": False})
    hidden = CopperPilotApp(
        _FakeRuntime(),
        LocalToolBroker(tmp_path, mode=ApprovalMode.MANUAL),
        tmp_path,
        "thread",
    )
    async with hidden.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert not hidden.query_one(ChatInput).has_class("-tips")
        assert hidden.query_one("#rule-top").render().plain == "─" * 100

    save_preferences({"tips": True})
    shown = CopperPilotApp(
        _FakeRuntime(),
        LocalToolBroker(tmp_path, mode=ApprovalMode.MANUAL),
        tmp_path,
        "thread",
    )
    async with shown.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        composer = shown.query_one(ChatInput)
        assert composer.has_class("-tips")
        assert last_tip_index() is not None
        shown.query_one("#chat-input").text = "check the sheet"
        await pilot.press("enter")
        await pilot.pause()
        assert not composer.has_class("-tips")

    narrow_home = tmp_path / "narrow-home"
    monkeypatch.setenv("COPPER_PILOT_CLI_HOME", str(narrow_home))
    save_preferences({})
    narrow = CopperPilotApp(
        _FakeRuntime(),
        LocalToolBroker(tmp_path, mode=ApprovalMode.MANUAL),
        tmp_path,
        "thread",
    )
    async with narrow.run_test(size=(50, 30)) as pilot:
        await pilot.pause()
        assert not narrow.query_one(ChatInput).has_class("-tips")
        assert last_tip_index() is None


@pytest.mark.asyncio
async def test_slash_working_approval_and_mode_cycle(tmp_path) -> None:
    app = CopperPilotApp(
        _FakeRuntime(),
        LocalToolBroker(tmp_path, mode=ApprovalMode.MANUAL),
        tmp_path,
        "thread",
    )
    request = ApprovalRequest(
        tool_call_id="shell-1",
        tool_name="execute",
        canonical_name="execute",
        arguments={"command": "kicad-cli version"},
        reason="Local side effect",
    )
    async with app.run_test(size=(100, 36)) as pilot:
        area = app.query_one(ChatInput).query_one("#chat-input")
        area.text = "/"
        area.focus()
        await pilot.pause()
        composer = app.query_one(ChatInput)
        assert composer.completion_active
        assert len(composer._matches) <= 6
        assert "esc close" in str(composer.query_one(".completion-hint").render())
        area.text = ""
        await pilot.pause()

        await app.action_cycle_mode()
        await pilot.pause()
        assert app.copper_mode is CopperMode.PLAN
        assert "drafts a plan" in str(app.query_one(StatusBar).render())
        await app.action_cycle_effort()
        await pilot.pause()
        assert "intelligent" in str(app.query_one(StatusBar).render())

        await app._start_loading()
        await pilot.pause()
        loading = app.query_one(LoadingWidget)
        assert loading.region.height == 1
        assert "esc to stop" in str(loading.query_one(".loading-line").render())
        await app._stop_loading()

        decision = asyncio.create_task(app.request_approval(request))
        async with asyncio.timeout(3):
            while not app.query(ApprovalMenu):
                await pilot.pause(0.05)
        await pilot.pause()
        menu = app.query_one(ApprovalMenu)
        text = "\n".join(str(widget.render()) for widget in menu.query(Static))
        assert "Run command?" in text
        assert "kicad-cli version" in text
        assert "yes" in text
        assert "esc no" in text
        decision.cancel()
        await asyncio.gather(decision, return_exceptions=True)
