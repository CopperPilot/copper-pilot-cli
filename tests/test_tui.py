from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Input, OptionList, Static, TextArea

import copper_pilot_cli.copper_app as copper_app_module
from copper_pilot_cli.copper_app import ChoiceScreen, CopperPilotApp, QuestionScreen
from copper_pilot_cli.copper_presentation import (
    ChatScroll,
    LoadingWidget,
    StatusBar,
    WelcomeBanner,
)
from copper_pilot_cli.copper_protocol import CopperMode
from copper_pilot_cli.copper_tools import ApprovalMode, ApprovalRequest, LocalToolBroker
from copper_pilot_cli.copper_widgets import (
    MAX_WIDGET_TEXT,
    ApprovalMenu,
    AssistantMessage,
    ChatInput,
    ComposerTextArea,
    ErrorMessage,
    ReasoningMessage,
    ThreadSelectorScreen,
    ToolCallMessage,
    ToolGroupSummary,
    UserMessage,
    WorkRunGroup,
    format_work_duration,
)


class FakeAgent:
    credential = None
    mode = CopperMode.AGENT
    workflow = None
    skill_manifest_id = None
    plan_context = None
    runtime_context = None


class FakeRuntime:
    def __init__(self) -> None:
        self.agent = FakeAgent()
        self.cancelled = False

    def config(self, thread_id, workspace):
        return {"configurable": {"thread_id": thread_id}}

    async def aget_state(self, _config):
        return SimpleNamespace(values={})

    async def acancel(self):
        self.cancelled = True

    async def astream(self, _values, _config):
        yield (), "custom", {"type": "copper.text", "data": "chat response"}


@pytest.mark.asyncio
async def test_tui_mounts_branded_composer_and_status(tmp_path) -> None:
    app = CopperPilotApp(
        FakeRuntime(),
        LocalToolBroker(tmp_path, mode=ApprovalMode.MANUAL),
        tmp_path,
        "thread",
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        composer = app.query_one(ChatInput)
        assert app.theme == "copper-pilot"
        assert app.current_theme.dark is True
        assert app.current_theme.primary.lower() == "#ea580c"
        status = app.query_one(StatusBar)
        assert "manual" in str(status.query_one(".status-approval").render()).lower()
        assert list(app.query(WelcomeBanner))
        assert not list(status.query(".status-context"))
        assert any(entry.name == "/mode" for entry in composer.commands)


@pytest.mark.asyncio
async def test_mouse_up_defers_selection_copy_for_active_screen(tmp_path) -> None:
    app = CopperPilotApp(FakeRuntime(), LocalToolBroker(tmp_path), tmp_path, "thread")
    async with app.run_test(size=(100, 30)) as pilot:
        with patch.object(copper_app_module, "copy_selection_to_clipboard") as copy:
            await pilot.click(WelcomeBanner)
            await pilot.pause()
        copy.assert_called_with(app, screen=app.screen)


@pytest.mark.asyncio
async def test_composer_submits_on_enter(tmp_path) -> None:
    app = CopperPilotApp(
        FakeRuntime(),
        LocalToolBroker(tmp_path),
        tmp_path,
        "thread",
    )
    submitted: list[str] = []

    async def capture(value):
        submitted.append(value)

    app._submit = capture
    async with app.run_test(size=(100, 30)) as pilot:
        area = app.query_one(TextArea)
        area.text = "hello"
        area.focus()
        await pilot.press("enter")
        await pilot.pause()
    assert submitted == ["hello"]


@pytest.mark.asyncio
async def test_real_composer_submission_streams_without_app_state_collision(tmp_path) -> None:
    app = CopperPilotApp(
        FakeRuntime(),
        LocalToolBroker(tmp_path),
        tmp_path,
        "thread",
    )
    async with app.run_test(size=(100, 30)) as pilot:
        area = app.query_one(TextArea)
        area.text = "hello"
        area.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app._turn_task is None
        assert list(app.query(AssistantMessage))[-1].content == "chat response"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "event"),
    [
        ("reasoning", {"type": "copper.reasoning", "data": "thinking"}),
        ("assistant", {"type": "copper.text", "data": "answer"}),
        (
            "tool",
            {
                "type": "copper.tool_request",
                "data": {
                    "tool_call_id": "read-1",
                    "tool_name": "read_file",
                    "arguments": {"file_path": "board.kicad_sch"},
                },
            },
        ),
    ],
)
async def test_stream_events_do_not_steal_user_scroll_position(tmp_path, name, event) -> None:
    del name
    app = CopperPilotApp(FakeRuntime(), LocalToolBroker(tmp_path), tmp_path, "thread")
    async with app.run_test(size=(100, 24)) as pilot:
        for index in range(30):
            await app._mount(UserMessage(f"old message {index}"))
        await pilot.pause()
        chat = app.query_one(ChatScroll)
        assert chat.max_scroll_y > 0
        chat.scroll_end(animate=False, immediate=True)
        chat.release_anchor()
        chat.scroll_home(animate=False, immediate=True)
        await pilot.pause()

        await app._custom_event(event)
        await pilot.pause()
        await pilot.pause()

        assert chat.scroll_y == 0


@pytest.mark.asyncio
async def test_active_bottom_follow_tracks_output_until_user_scrolls(tmp_path) -> None:
    app = CopperPilotApp(FakeRuntime(), LocalToolBroker(tmp_path), tmp_path, "thread")
    async with app.run_test(size=(100, 24)) as pilot:
        for index in range(30):
            await app._mount(UserMessage(f"old message {index}"))
        chat = app.query_one(ChatScroll)
        chat.anchor()
        await app._custom_event({"type": "copper.reasoning", "data": "thinking"})
        await pilot.pause()
        await pilot.pause()
        assert chat.scroll_y == chat.max_scroll_y


@pytest.mark.asyncio
async def test_loading_removal_does_not_rearm_released_scroll(tmp_path) -> None:
    app = CopperPilotApp(FakeRuntime(), LocalToolBroker(tmp_path), tmp_path, "thread")
    async with app.run_test(size=(100, 24)) as pilot:
        for index in range(30):
            await app._mount(UserMessage(f"old message {index}"))
        chat = app.query_one(ChatScroll)
        chat.anchor()
        await app._start_loading()
        await pilot.pause()
        chat.release_anchor()
        chat.scroll_home(animate=False, immediate=True)
        await pilot.pause()

        await app._stop_loading()
        await pilot.pause()

        assert chat.scroll_y == 0
        assert chat._follow_bottom_when_scrollable is False


@pytest.mark.asyncio
async def test_short_transcript_stays_top_aligned_while_following(tmp_path) -> None:
    app = CopperPilotApp(FakeRuntime(), LocalToolBroker(tmp_path), tmp_path, "thread")
    async with app.run_test(size=(100, 40)) as pilot:
        chat = app.query_one(ChatScroll)
        chat.anchor()
        await app._custom_event({"type": "copper.reasoning", "data": "thinking"})
        await pilot.pause()
        assert chat.max_scroll_y == 0
        assert chat.scroll_y == 0


@pytest.mark.asyncio
async def test_loading_widget_occupies_a_single_transcript_line(tmp_path) -> None:
    app = CopperPilotApp(FakeRuntime(), LocalToolBroker(tmp_path), tmp_path, "thread")
    async with app.run_test(size=(100, 40)) as pilot:
        await app._start_loading()
        await pilot.pause()
        assert app._loading is not None
        assert app._loading.region.height == 1


@pytest.mark.asyncio
async def test_starting_a_turn_in_a_short_chat_creates_no_scrollback(tmp_path) -> None:
    app = CopperPilotApp(FakeRuntime(), LocalToolBroker(tmp_path), tmp_path, "thread")
    async with app.run_test(size=(100, 40)) as pilot:
        chat = app.query_one(ChatScroll)
        chat.anchor()
        await app._mount(UserMessage("hello"))
        await app._start_loading()
        await pilot.pause()
        assert chat.max_scroll_y == 0
        assert chat.scroll_y == 0

        await app._custom_event({"type": "copper.reasoning", "data": "thinking"})
        await pilot.pause()
        assert chat.max_scroll_y == 0
        assert chat.scroll_y == 0


@pytest.mark.asyncio
async def test_thread_picker_filter_row_does_not_crowd_the_thread_list(tmp_path) -> None:
    app = CopperPilotApp(FakeRuntime(), LocalToolBroker(tmp_path), tmp_path, "thread")
    async with app.run_test(size=(100, 40)) as pilot:
        app.push_screen(ThreadSelectorScreen(None, filter_cwd=str(tmp_path)))
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        box = screen.query_one("#thread-box").region.height
        assert screen.query_one(Horizontal).region.height <= 3
        assert screen.query_one("#threads", OptionList).region.height > box // 2


@pytest.mark.asyncio
async def test_mode_command_opens_and_resolves_modal_from_input_handler(tmp_path) -> None:
    app = CopperPilotApp(
        FakeRuntime(),
        LocalToolBroker(tmp_path),
        tmp_path,
        "thread",
    )
    async with app.run_test(size=(100, 30)) as pilot:
        area = app.query_one(TextArea)
        area.text = "/mode"
        area.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ChoiceScreen)
        app.screen.dismiss("plan")
        await pilot.pause()
        assert app.copper_mode is CopperMode.PLAN


@pytest.mark.asyncio
async def test_untrusted_hosted_events_render_as_literal_text(tmp_path) -> None:
    app = CopperPilotApp(
        FakeRuntime(),
        LocalToolBroker(tmp_path),
        tmp_path,
        "thread",
    )
    malformed = "[/ETH_TRD2_N]"
    async with app.run_test(size=(120, 40)) as pilot:
        await app._custom_event({"type": "copper.text", "data": malformed})
        await app._custom_event({"type": "copper.reasoning", "data": malformed})
        await app._custom_event(
            {
                "type": "copper.tool_request",
                "data": {
                    "tool_call_id": "tool-1",
                    "tool_name": "read_file",
                    "arguments": {"file_path": malformed},
                },
            }
        )
        await app._custom_event(
            {
                "type": "copper.tool_result",
                "data": {"tool_call_id": "tool-1", "result": {"result": malformed}},
            }
        )
        await app._custom_event({"type": "copper.error", "data": malformed})
        await app._custom_event({"type": "copper.status", "data": malformed})
        await app._custom_event({"type": "copper.status_event", "data": {"message": malformed}})
        await app._custom_event({"type": "copper.usage", "data": {"tokens_output": 42}})
        await pilot.pause()

        assert malformed in list(app.query(ReasoningMessage))[-1].content
        assert malformed in list(app.query(AssistantMessage))[-1].content
        assert malformed in list(app.query(ToolCallMessage))[-1].output
        assert malformed in str(list(app.query(ErrorMessage))[-1].render())
        assert "Context:" not in str(app.query_one(StatusBar).render())
        assert "Tokens:" not in str(app.query_one(StatusBar).render())


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/mode", "/approval"])
async def test_picker_commands_are_safe_from_composer_worker(tmp_path, command) -> None:
    app = CopperPilotApp(
        FakeRuntime(),
        LocalToolBroker(tmp_path),
        tmp_path,
        "thread",
    )
    async with app.run_test(size=(100, 30)) as pilot:
        area = app.query_one(TextArea)
        area.text = command
        area.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ChoiceScreen)
        app.screen.dismiss(None)
        await pilot.pause()


@pytest.mark.asyncio
async def test_cancel_during_active_turn_does_not_quit_or_crash(tmp_path) -> None:
    runtime = FakeRuntime()
    gate = asyncio.Event()

    async def blocked_stream(_values, _config):
        await gate.wait()
        if False:
            yield None

    runtime.astream = blocked_stream
    app = CopperPilotApp(runtime, LocalToolBroker(tmp_path), tmp_path, "thread")
    async with app.run_test(size=(100, 30)) as pilot:
        await app._submit("long task")
        await pilot.pause()
        assert app._turn_task is not None
        await app.action_cancel_or_quit()
        await pilot.pause()
        assert runtime.cancelled is True
        assert app._turn_task is None


@pytest.mark.asyncio
async def test_cancel_settles_active_tool_and_removes_loading(tmp_path) -> None:
    runtime = FakeRuntime()
    gate = asyncio.Event()

    async def tool_then_wait(_values, _config):
        yield (
            (),
            "custom",
            {
                "type": "copper.tool_request",
                "data": {
                    "tool_call_id": "read-1",
                    "tool_name": "read_file",
                    "arguments": {"file_path": "board.kicad_sch"},
                },
            },
        )
        await gate.wait()

    runtime.astream = tool_then_wait
    app = CopperPilotApp(runtime, LocalToolBroker(tmp_path), tmp_path, "thread")
    async with app.run_test(size=(100, 30)) as pilot:
        await app._submit("long task")
        await pilot.pause()
        assert list(app.query(LoadingWidget))
        await app.action_cancel_or_quit()
        await pilot.pause()
        assert app._tools["read-1"].status == "error"
        assert not list(app.query(LoadingWidget))


@pytest.mark.asyncio
async def test_cancel_still_cleans_up_when_transport_cancel_fails(tmp_path) -> None:
    class FailingCancelRuntime(FakeRuntime):
        async def acancel(self):
            self.cancelled = True
            raise OSError("socket already closed")

    runtime = FailingCancelRuntime()
    gate = asyncio.Event()

    async def blocked_stream(_values, _config):
        await gate.wait()
        if False:
            yield None

    runtime.astream = blocked_stream
    app = CopperPilotApp(runtime, LocalToolBroker(tmp_path), tmp_path, "thread")
    async with app.run_test(size=(100, 30)) as pilot:
        await app._submit("long task")
        await pilot.pause()
        await app.action_cancel_or_quit()
        await pilot.pause()
        assert runtime.cancelled is True
        assert app._turn_task is None


@pytest.mark.asyncio
async def test_hosted_error_event_is_rendered_only_once(tmp_path) -> None:
    runtime = FakeRuntime()

    async def failing_stream(_values, _config):
        yield (), "custom", {"type": "copper.error", "data": "hosted failure"}
        raise RuntimeError("hosted failure")

    runtime.astream = failing_stream
    app = CopperPilotApp(runtime, LocalToolBroker(tmp_path), tmp_path, "thread")
    async with app.run_test(size=(100, 30)) as pilot:
        await app._submit("fail")
        async with asyncio.timeout(2):
            while app._turn_task is not None:
                await pilot.pause()
        assert len(list(app.query(ErrorMessage))) == 1


@pytest.mark.asyncio
async def test_failed_final_is_visible(tmp_path) -> None:
    app = CopperPilotApp(FakeRuntime(), LocalToolBroker(tmp_path), tmp_path, "thread")
    async with app.run_test(size=(100, 30)) as pilot:
        await app._custom_event(
            {
                "type": "copper.final",
                "data": {"success": False, "error": "final failure"},
            }
        )
        await pilot.pause()
        assert "final failure" in str(list(app.query(ErrorMessage))[-1].render())


@pytest.mark.asyncio
async def test_malformed_checkpoint_state_does_not_abort_hydration(tmp_path) -> None:
    runtime = FakeRuntime()

    async def malformed_state(_config):
        return SimpleNamespace(
            values={
                "messages": "not-a-list",
                "copper_mode": "obsolete-mode",
                "copper_events": [
                    None,
                    {
                        "type": "copper.tool_request",
                        "data": {"tool_call_id": "bad", "arguments": "not-a-dict"},
                    },
                ],
            }
        )

    runtime.aget_state = malformed_state
    app = CopperPilotApp(runtime, LocalToolBroker(tmp_path), tmp_path, "thread")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert app.copper_mode is CopperMode.AGENT
        assert list(app.query(ToolCallMessage)) == []


@pytest.mark.asyncio
async def test_hydration_preserves_canonical_message_and_tool_order(tmp_path) -> None:
    runtime = FakeRuntime()

    async def ordered_state(_config):
        return SimpleNamespace(
            values={
                "messages": [
                    HumanMessage(content="Review"),
                    AIMessage(
                        content="Before tool",
                        tool_calls=[
                            {
                                "id": "read-1",
                                "name": "read_file",
                                "args": {"file_path": "board.kicad_sch"},
                            }
                        ],
                    ),
                    ToolMessage(content="file contents", tool_call_id="read-1"),
                    AIMessage(content="After tool"),
                ],
                "copper_events": [],
            }
        )

    runtime.aget_state = ordered_state
    app = CopperPilotApp(runtime, LocalToolBroker(tmp_path), tmp_path, "thread")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        children = list(app.query_one("#transcript", VerticalScroll).children)
        assert [type(child).__name__ for child in children] == [
            "WelcomeBanner",
            "UserMessage",
            "AssistantMessage",
            "WorkRunGroup",
            "AssistantMessage",
        ]
        assert list(children[3].query(ToolCallMessage))[0].output == "file contents"
        assert str(children[3].query_one(".work-run-header", Static).render()) == "Worked"


@pytest.mark.asyncio
async def test_stream_widgets_bound_accumulated_content(tmp_path) -> None:
    app = CopperPilotApp(FakeRuntime(), LocalToolBroker(tmp_path), tmp_path, "thread")
    async with app.run_test(size=(100, 30)) as pilot:
        await app._custom_event({"type": "copper.text", "data": "x" * (MAX_WIDGET_TEXT + 1_000)})
        await app._custom_event(
            {"type": "copper.reasoning", "data": "y" * (MAX_WIDGET_TEXT + 1_000)}
        )
        await pilot.pause()
        assistant = list(app.query(AssistantMessage))[-1]
        reasoning = list(app.query(ReasoningMessage))[-1]
        assert len(assistant.content) < MAX_WIDGET_TEXT + 100
        assert len(reasoning.content) < MAX_WIDGET_TEXT + 100
        assert assistant.content.endswith("[display truncated]")


@pytest.mark.asyncio
async def test_reasoning_collapses_at_tool_boundary_and_duplicate_sequence_is_ignored(
    tmp_path,
) -> None:
    app = CopperPilotApp(FakeRuntime(), LocalToolBroker(tmp_path), tmp_path, "thread")
    async with app.run_test(size=(100, 30)) as pilot:
        await app._custom_event(
            {"type": "copper.reasoning", "data": "first", "turn_id": "t", "sequence": 1}
        )
        await app._custom_event(
            {"type": "copper.reasoning", "data": "duplicate", "turn_id": "t", "sequence": 1}
        )
        reasoning = list(app.query(ReasoningMessage))[-1]
        assert reasoning.content == "first"
        await app._custom_event(
            {
                "type": "copper.tool_request",
                "turn_id": "t",
                "sequence": 2,
                "data": {
                    "tool_call_id": "read-1",
                    "tool_name": "read_file",
                    "arguments": {"file_path": "board.kicad_sch"},
                },
            }
        )
        await pilot.pause()
        assert reasoning.collapsed is True


@pytest.mark.asyncio
async def test_resume_updates_checkpoint_and_hosted_conversation_identity(tmp_path) -> None:
    runtime = FakeRuntime()
    app = CopperPilotApp(runtime, LocalToolBroker(tmp_path), tmp_path, "old-thread")

    async def select_thread(_screen):
        return "resumed-thread"

    app._wait_for_modal = select_thread
    async with app.run_test(size=(100, 30)) as pilot:
        await app.action_resume_thread()
        await pilot.pause()
        assert app.thread_id == "resumed-thread"
        assert runtime.agent.conversation_id == "resumed-thread"


@pytest.mark.asyncio
async def test_hosted_ask_user_modal_round_trip(tmp_path) -> None:
    app = CopperPilotApp(FakeRuntime(), LocalToolBroker(tmp_path), tmp_path, "thread")
    async with app.run_test(size=(100, 30)) as pilot:
        answer_task = asyncio.create_task(
            app.request_question({"question": "Which rail [/VCC_5V]?"})
        )
        await pilot.pause()
        assert isinstance(app.screen, QuestionScreen)
        answer = app.screen.query_one(Input)
        answer.value = "VCC_5V"
        answer.focus()
        await pilot.press("enter")
        assert await answer_task == "VCC_5V"


@pytest.mark.asyncio
async def test_tool_approval_uses_dcode_style_preview_and_resumes_row(tmp_path) -> None:
    app = CopperPilotApp(FakeRuntime(), LocalToolBroker(tmp_path), tmp_path, "thread")
    request = ApprovalRequest(
        tool_call_id="write-1",
        tool_name="write",
        canonical_name="write_file",
        arguments={"file_path": "notes.txt", "content": "copper"},
        reason="Local side effect",
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await app._custom_event(
            {
                "type": "copper.tool_request",
                "data": {
                    "tool_call_id": "write-1",
                    "tool_name": "write",
                    "arguments": request.arguments,
                },
            }
        )
        decision = asyncio.create_task(app.request_approval(request))
        await pilot.pause()
        menu = app.query_one(ApprovalMenu)
        assert "copper" in str(menu.query_one(".approval-preview Static").render())
        await pilot.press("y")
        assert await decision is True
        assert app._tools["write-1"].status == "running"


@pytest.mark.asyncio
async def test_inline_approval_tab_collects_rejection_feedback(tmp_path) -> None:
    app = CopperPilotApp(FakeRuntime(), LocalToolBroker(tmp_path), tmp_path, "thread")
    request = ApprovalRequest(
        tool_call_id="delete-1",
        tool_name="delete_file",
        canonical_name="delete",
        arguments={"file_path": "notes.txt"},
        reason="Local side effect",
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await app._custom_event(
            {
                "type": "copper.tool_request",
                "data": {
                    "tool_call_id": "delete-1",
                    "tool_name": "delete_file",
                    "arguments": request.arguments,
                },
            }
        )
        decision = asyncio.create_task(app.request_approval(request))
        await pilot.pause()
        await pilot.press("tab")
        reason = app.query_one(ApprovalMenu).query_one(Input)
        assert reason.display is True
        await pilot.press("k", "e", "e", "p", "space", "i", "t", "enter")
        assert await decision is False
        assert app._tools["delete-1"].output == "keep it"


@pytest.mark.asyncio
async def test_cancelling_auto_notice_keeps_current_approval_pending(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(copper_app_module, "auto_notice_acknowledged", lambda: False)
    monkeypatch.setattr(copper_app_module, "save_approval_mode", lambda _mode: None)
    request = ApprovalRequest(
        tool_call_id="write-1",
        tool_name="write",
        canonical_name="write_file",
        arguments={"file_path": "notes.txt", "content": "copper"},
        reason="Local side effect",
    )
    app = CopperPilotApp(FakeRuntime(), LocalToolBroker(tmp_path), tmp_path, "thread")
    async with app.run_test(size=(100, 30)) as pilot:
        await app._custom_event(
            {
                "type": "copper.tool_request",
                "data": {
                    "tool_call_id": "write-1",
                    "tool_name": "write",
                    "arguments": request.arguments,
                },
            }
        )
        decision = asyncio.create_task(app.request_approval(request))
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, ChoiceScreen)
        app.screen.dismiss("cancel")
        await pilot.pause()
        assert list(app.query(ApprovalMenu))
        assert decision.done() is False
        await pilot.press("y")
        assert await decision is True
        assert app.broker.mode is ApprovalMode.MANUAL


@pytest.mark.asyncio
async def test_successful_tools_fold_into_one_expandable_summary(tmp_path) -> None:
    app = CopperPilotApp(FakeRuntime(), LocalToolBroker(tmp_path), tmp_path, "thread")
    async with app.run_test(size=(100, 30)) as pilot:
        for tool_id, name in (("shell-1", "bash"), ("read-1", "read_file")):
            await app._custom_event(
                {
                    "type": "copper.tool_request",
                    "data": {
                        "tool_call_id": tool_id,
                        "tool_name": name,
                        "arguments": (
                            {"command": "printf ok"}
                            if name == "bash"
                            else {"file_path": "board.kicad_sch"}
                        ),
                    },
                }
            )
            await app._custom_event(
                {
                    "type": "copper.tool_result",
                    "data": {"tool_call_id": tool_id, "result": {"result": "ok"}},
                }
            )
        await app._custom_event({"type": "copper.text", "data": "Done"})
        await pilot.pause()

        group = list(app.query(ToolGroupSummary))[-1]
        summary = str(group.query_one(".tool-group-summary").render())
        assert "Ran 1 shell command" in summary
        assert "Read 1 file" in summary
        assert group.expanded is False
        assert all(tool.display is False for tool in group.tools)
        group.toggle()
        assert group.expanded is True
        assert all(tool.display is True for tool in group.tools)


@pytest.mark.asyncio
async def test_failed_shell_stays_visible_and_is_not_reported_as_success(tmp_path) -> None:
    app = CopperPilotApp(FakeRuntime(), LocalToolBroker(tmp_path), tmp_path, "thread")
    async with app.run_test(size=(100, 30)) as pilot:
        await app._custom_event(
            {
                "type": "copper.tool_request",
                "data": {
                    "tool_call_id": "shell-1",
                    "tool_name": "bash",
                    "arguments": {"command": "python -c 'bad'"},
                },
            }
        )
        await app._custom_event(
            {
                "type": "copper.tool_result",
                "data": {
                    "tool_call_id": "shell-1",
                    "result": {"result": "SyntaxError\nExit code: 1", "exit_code": 1},
                },
            }
        )
        await pilot.pause()

        tool = app._tools["shell-1"]
        assert tool.status == "error"
        assert tool.display is True


@pytest.mark.asyncio
async def test_rejected_tool_result_remains_rejected(tmp_path) -> None:
    app = CopperPilotApp(FakeRuntime(), LocalToolBroker(tmp_path), tmp_path, "thread")
    async with app.run_test(size=(100, 30)) as pilot:
        await app._custom_event(
            {
                "type": "copper.tool_request",
                "data": {
                    "tool_call_id": "delete-1",
                    "tool_name": "delete_file",
                    "arguments": {"file_path": "notes.txt"},
                },
            }
        )
        app._tools["delete-1"].set_rejected("Keep this file")
        await app._custom_event(
            {
                "type": "copper.tool_result",
                "data": {
                    "tool_call_id": "delete-1",
                    "result": {"error": "ToolRejected: rejected", "rejected": True},
                },
            }
        )
        await pilot.pause()
        assert app._tools["delete-1"].status == "rejected"
        assert "rejected" not in app._tools["delete-1"].output.lower()
        assert app._tools["delete-1"].output == "Keep this file"


def test_work_duration_matches_desktop_contract() -> None:
    assert format_work_duration(4.2) == "4s"
    assert format_work_duration(60) == "1 min"
    assert format_work_duration(125) == "2 mins 5s"
    assert format_work_duration(3_660) == "1 hr 1 min"


@pytest.mark.asyncio
async def test_uninterrupted_reasoning_and_tools_become_one_work_run(tmp_path) -> None:
    app = CopperPilotApp(FakeRuntime(), LocalToolBroker(tmp_path), tmp_path, "thread")
    async with app.run_test(size=(100, 30)) as pilot:
        for index in range(4):
            await app._custom_event({"type": "copper.reasoning", "data": f"step {index}"})
            await app._custom_event(
                {
                    "type": "copper.tool_request",
                    "data": {
                        "tool_call_id": f"shell-{index}",
                        "tool_name": "bash",
                        "arguments": {"command": f"printf {index}"},
                    },
                }
            )
            await app._custom_event(
                {
                    "type": "copper.tool_result",
                    "data": {
                        "tool_call_id": f"shell-{index}",
                        "result": {"result": str(index)},
                    },
                }
            )
        await pilot.pause()

        work_runs = list(app.query(WorkRunGroup))
        assert len(work_runs) == 1
        work_run = work_runs[0]
        assert str(work_run.query_one(".work-run-header", Static).render()).startswith(
            "Worked for "
        )
        assert sum(unit.display for unit in work_run.units) == 3
        assert len(list(app.query(ToolGroupSummary))) == 1

        await app._custom_event({"type": "copper.text", "data": "Done"})
        await pilot.pause()
        assert work_run.has_class("-complete")
        assert work_run.query_one(".work-run-body").display is False


@pytest.mark.asyncio
async def test_failed_work_remains_visible_after_work_run_finishes(tmp_path) -> None:
    app = CopperPilotApp(FakeRuntime(), LocalToolBroker(tmp_path), tmp_path, "thread")
    async with app.run_test(size=(100, 30)) as pilot:
        await app._custom_event(
            {
                "type": "copper.tool_request",
                "data": {
                    "tool_call_id": "shell-failed",
                    "tool_name": "bash",
                    "arguments": {"command": "false"},
                },
            }
        )
        await app._custom_event(
            {
                "type": "copper.tool_result",
                "data": {
                    "tool_call_id": "shell-failed",
                    "result": {"result": "failed", "exit_code": 1},
                },
            }
        )
        await app._custom_event({"type": "copper.text", "data": "Could not finish"})
        await pilot.pause()

        work_run = list(app.query(WorkRunGroup))[-1]
        assert work_run.has_class("-failed")
        assert work_run.query_one(".work-run-body").display is True
        assert app._tools["shell-failed"].display is True


@pytest.mark.asyncio
async def test_slash_completion_uses_dcode_keyboard_contract(tmp_path) -> None:
    app = CopperPilotApp(FakeRuntime(), LocalToolBroker(tmp_path), tmp_path, "thread")
    submitted: list[str] = []

    async def capture(value: str) -> None:
        submitted.append(value)

    app._handle_submission = capture
    async with app.run_test(size=(100, 30)) as pilot:
        area = app.query_one(TextArea)
        area.text = "/"
        area.focus()
        await pilot.pause()
        options = app.query_one("#completions", OptionList)
        await pilot.press("down")
        await pilot.pause()
        assert options.highlighted == 1
        expected = app.query_one(ChatInput)._matches[1].name

        await pilot.press("enter")
        await pilot.pause()
        assert submitted == [expected]
        assert area.text == ""

        area.text = "/"
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one(ChatInput).completion_active is False


@pytest.mark.asyncio
async def test_file_completion_applies_without_submitting(tmp_path) -> None:
    (tmp_path / "alpha.txt").write_text("a", encoding="utf-8")
    (tmp_path / "beta.txt").write_text("b", encoding="utf-8")
    app = CopperPilotApp(FakeRuntime(), LocalToolBroker(tmp_path), tmp_path, "thread")
    submitted: list[str] = []

    async def capture(value: str) -> None:
        submitted.append(value)

    app._handle_submission = capture
    async with app.run_test(size=(100, 30)) as pilot:
        area = app.query_one(TextArea)
        area.text = "@"
        area.focus()
        await pilot.pause()
        await pilot.press("down", "enter")
        await pilot.pause()
        assert area.text in {"@alpha.txt ", "@beta.txt "}
        assert submitted == []


@pytest.mark.asyncio
async def test_completion_wrap_space_tab_and_history_boundaries(tmp_path) -> None:
    (tmp_path / "alpha.txt").write_text("a", encoding="utf-8")
    app = CopperPilotApp(FakeRuntime(), LocalToolBroker(tmp_path), tmp_path, "thread")
    async with app.run_test(size=(100, 30)) as pilot:
        composer = app.query_one(ChatInput)
        area = app.query_one(TextArea)
        area.text = "/"
        area.focus()
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert composer._selected_index == len(composer._matches) - 1
        expected = composer._matches[-1].name
        await pilot.press("tab")
        await pilot.pause()
        assert area.text == f"{expected} "

        area.text = "/he"
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()
        assert area.text == "/help "

        area.text = "@alpha"
        area.move_cursor((0, len(area.text)))
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()
        assert area.text == "@alpha "

        history_area = app.query_one(ComposerTextArea)
        history_area.input_history = ["older prompt"]
        history_area.history_index = 1
        history_area.text = "line one\nline two"
        history_area.move_cursor((1, 4))
        await pilot.press("up")
        await pilot.pause()
        assert history_area.text == "line one\nline two"
        history_area.move_cursor((0, 0))
        await pilot.press("up")
        await pilot.pause()
        assert history_area.text == "older prompt"


@pytest.mark.asyncio
async def test_cancelling_modal_wait_dismisses_prompt(tmp_path) -> None:
    app = CopperPilotApp(FakeRuntime(), LocalToolBroker(tmp_path), tmp_path, "thread")
    async with app.run_test(size=(100, 30)) as pilot:
        answer_task = asyncio.create_task(app.request_question({"question": "Continue?"}))
        await pilot.pause()
        assert isinstance(app.screen, QuestionScreen)
        answer_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await answer_task
        await pilot.pause()
        assert not isinstance(app.screen, QuestionScreen)
