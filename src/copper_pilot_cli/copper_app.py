"""Branded Textual application for the hosted CopperPilot agent."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import uuid
from pathlib import Path
from typing import Any

import pyperclip
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from textual import on
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.content import Content
from textual.events import MouseUp
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Input, Label, OptionList, TextArea
from textual.widgets.option_list import Option

from copper_pilot_cli import copper_theme
from copper_pilot_cli import textual_patches as _textual_patches  # noqa: F401
from copper_pilot_cli._version import __version__
from copper_pilot_cli.clipboard import copy_selection_to_clipboard
from copper_pilot_cli.copper_api import token_limits
from copper_pilot_cli.copper_auth import DeviceLogin, clear_credential
from copper_pilot_cli.copper_features import (
    LocalSkill,
    encode_runtime_context,
    looks_like_kicad_snippet,
    parse_plan,
    path_first_attachment,
    resolve_skill_manifest,
    scan_local_skills,
)
from copper_pilot_cli.copper_graph import CopperGraphRuntime
from copper_pilot_cli.copper_hooks import HookRunner
from copper_pilot_cli.copper_preferences import (
    acknowledge_auto_notice,
    acknowledge_yolo,
    auto_notice_acknowledged,
    save_approval_mode,
    save_shell_auto_allow_settings,
    shell_auto_allow_settings,
    yolo_acknowledged,
)
from copper_pilot_cli.copper_presentation import (
    ChatScroll,
    LoadingWidget,
    StatusBar,
    TurnEvent,
    TurnPhase,
    TurnState,
    WelcomeBanner,
)
from copper_pilot_cli.copper_protocol import CopperMode, HostedChatClient
from copper_pilot_cli.copper_tools import (
    ApprovalMode,
    ApprovalRequest,
    LocalToolBroker,
)
from copper_pilot_cli.copper_update import check_for_update
from copper_pilot_cli.copper_widgets import (
    MAX_WIDGET_TEXT,
    ApprovalDecision,
    ApprovalMenu,
    AssistantMessage,
    ChatInput,
    CompletionEntry,
    ErrorMessage,
    ReasoningMessage,
    ThreadSelectorScreen,
    ToolCallMessage,
    ToolGroupSummary,
    UserMessage,
    WorkRunGroup,
)
from copper_pilot_cli.copper_workspace import (
    discover_workspace,
    recent_workspaces,
    remember_workspace,
)
from copper_pilot_cli.media_utils import get_clipboard_image
from copper_pilot_cli.sessions import generate_thread_id

logger = logging.getLogger(__name__)


class ChoiceScreen(ModalScreen[str | None]):
    """Small reusable command/approval picker."""

    DEFAULT_CSS = """
    ChoiceScreen {
        align: center middle;
    }
    ChoiceScreen > #choice-box {
        width: 72;
        max-width: 95%;
        height: auto;
        max-height: 80%;
        border: solid $primary;
        background: $surface;
        padding: 1 2;
    }
    ChoiceScreen OptionList {
        height: auto;
        max-height: 16;
    }
    """

    def __init__(self, title: str, options: list[tuple[str, str]]) -> None:
        super().__init__()
        self.title_text = title
        self.options = options

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="choice-box"):
            yield Label(Content(self.title_text))
            yield OptionList(*(Option(Content(label), id=value) for value, label in self.options))

    @on(OptionList.OptionSelected)
    def selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id))


class QuestionScreen(ModalScreen[str | None]):
    """Free-form hosted ask-user prompt."""

    DEFAULT_CSS = """
    QuestionScreen { align: center middle; }
    QuestionScreen > #question-box {
        width: 72;
        max-width: 95%;
        height: auto;
        border: solid $primary;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="question-box"):
            yield Label(Content(self.question))
            yield Input(placeholder="Type your answer and press Enter", id="answer")

    @on(Input.Submitted)
    def answer(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)


class CopperPilotApp(App[None]):
    """Textual client retaining dcode's core chat widgets."""

    TITLE = "CopperPilot"
    SUB_TITLE = "Hosted electronics design agent"
    BINDINGS = [
        ("ctrl+c", "cancel_or_quit", "Cancel"),
        ("ctrl+v", "paste_or_attach", "Paste"),
        ("ctrl+r", "resume_thread", "Resume"),
        ("shift+tab", "cycle_approval", "Approval"),
        ("ctrl+q", "quit", "Quit"),
    ]
    CSS = """
    Screen {
        background: $background;
    }
    #transcript {
        height: 1fr;
        padding: 1 2;
    }
    #copper-status {
        height: 1;
        padding: 0 2;
        color: $text-muted;
        background: $surface;
    }
    """

    def __init__(
        self,
        runtime: CopperGraphRuntime,
        broker: LocalToolBroker,
        workspace: Path,
        thread_id: str,
        *,
        mode: CopperMode = CopperMode.AGENT,
        initial_message: str | None = None,
    ) -> None:
        super().__init__()
        self.runtime = runtime
        self.broker = broker
        self.workspace = workspace
        self.thread_id = thread_id
        self.copper_mode = mode
        self.initial_message = initial_message
        self._turn_task: asyncio.Task[None] | None = None
        self._assistant: AssistantMessage | None = None
        self._last_assistant_content = ""
        self._reasoning: ReasoningMessage | None = None
        self._tools: dict[str, ToolCallMessage] = {}
        self._tool_groups: dict[str, ToolGroupSummary] = {}
        self._tool_group: ToolGroupSummary | None = None
        self._work_run: WorkRunGroup | None = None
        self._pending_media: list[Any] = []
        self._skills: dict[str, LocalSkill] = {}
        self._selected_skill: str | None = None
        self._turn_error_rendered = False
        self._loading: LoadingWidget | None = None
        self._turn_state = TurnState()
        self._register_copper_theme()
        self.hooks = HookRunner(workspace, broker)
        self.broker.approve = self.request_approval
        self.broker.ask_user = self.request_question

    def _register_copper_theme(self) -> None:
        self.register_theme(
            Theme(
                name="copper-pilot",
                primary="#EA580C",
                secondary="#FB923C",
                accent="#F97316",
                foreground="#C0CAF5",
                background="#11121D",
                surface="#1A1B2E",
                panel="#25283B",
                success="#9ECE6A",
                warning="#F97316",
                error="#F7768E",
                dark=True,
            )
        )
        self.theme = "copper-pilot"

    def get_theme_variable_defaults(self) -> dict[str, str]:
        return copper_theme.css_variable_defaults()

    def compose(self) -> ComposeResult:
        with ChatScroll(id="transcript"):
            yield WelcomeBanner(self.workspace)
        yield ChatInput(cwd=self.workspace)
        yield StatusBar(self.workspace, self.broker.mode.value)

    async def on_mount(self) -> None:
        await self._hydrate()
        self._load_skills()
        if self.broker.mode is ApprovalMode.YOLO and not yolo_acknowledged():
            self.broker.mode = ApprovalMode.MANUAL
            self.call_after_refresh(self._activate_approval, ApprovalMode.YOLO)
        self._update_status()
        self.query_one(ChatInput).focus()
        if self.initial_message:
            self.call_after_refresh(self._submit, self.initial_message)

    def on_mouse_up(self, event: MouseUp) -> None:  # noqa: ARG002
        """Copy selection after Textual completes its click-chain update."""
        self.call_after_refresh(
            copy_selection_to_clipboard,
            self,
            screen=self.screen,
        )

    async def on_unmount(self) -> None:
        try:
            if self._turn_task and not self._turn_task.done():
                with contextlib.suppress(Exception):
                    await self.runtime.acancel()
                self._turn_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._turn_task
        finally:
            await self.broker.cancel_all()

    def _config(self) -> dict[str, Any]:
        return self.runtime.config(self.thread_id, self.workspace)

    async def _hydrate(self) -> None:
        state = await self.runtime.aget_state(self._config())
        values = getattr(state, "values", {}) or {}
        self._tools.clear()
        self._tool_groups.clear()
        self._tool_group = None
        self._work_run = None
        self._assistant = None
        self._reasoning = None
        messages = values.get("messages", [])
        for message in messages if isinstance(messages, list) else []:
            if isinstance(message, HumanMessage):
                self._finalize_work_run()
                await self._mount(UserMessage(str(message.content)))
            elif isinstance(message, ToolMessage):
                tool = self._tools.get(str(message.tool_call_id))
                if tool:
                    if message.additional_kwargs.get("copper_rejected") is True:
                        tool.set_rejected(str(message.content))
                        if tool.group is not None and tool.group.work_run is not None:
                            tool.group.work_run.mark_failed()
                    elif getattr(message, "status", None) == "error" or (
                        isinstance(message.artifact, dict)
                        and isinstance(message.artifact.get("exit_code"), int)
                        and message.artifact["exit_code"] != 0
                    ):
                        tool.set_error(str(message.content))
                        if tool.group is not None and tool.group.work_run is not None:
                            tool.group.work_run.mark_failed()
                    else:
                        tool.set_success(str(message.content))
            elif isinstance(message, AIMessage):
                if message.content:
                    self._finalize_work_run()
                    self._last_assistant_content = str(message.content)[:MAX_WIDGET_TEXT]
                    await self._mount(AssistantMessage(str(message.content)))
                for call in message.tool_calls:
                    arguments = call.get("args")
                    tool = ToolCallMessage(
                        str(call.get("name") or "tool"),
                        arguments if isinstance(arguments, dict) else {},
                        workspace=self.workspace,
                    )
                    tool_id = str(call.get("id") or "")
                    self._tools[tool_id] = tool
                    group = await self._ensure_tool_group(timed=False)
                    self._tool_groups[tool_id] = group
                    await group.add_tool(tool)
                    tool.set_running()
        self._finalize_work_run()
        try:
            self.copper_mode = CopperMode(values.get("copper_mode", self.copper_mode.value))
        except ValueError:
            self.copper_mode = CopperMode.AGENT
        self.runtime.agent.mode = self.copper_mode
        events = values.get("copper_events", [])
        for event in events if isinstance(events, list) else []:
            if not isinstance(event, dict):
                continue
            if str(event.get("type")) not in {
                "copper.text",
                "copper.tool_request",
                "copper.tool_result",
            }:
                await self._custom_event(event)
        if messages or events:
            self.query_one("#transcript", ChatScroll).scroll_end(animate=False)

    def _load_skills(self) -> None:
        skills, diagnostics = scan_local_skills(self.workspace)
        self._skills = {skill.id: skill for skill in skills}
        command_names = {
            "context",
            "copy",
            "feedback",
            "help",
            "hooks",
            "mode",
            "resume",
            "threads",
            "approval",
            "clear",
            "cwd",
            "auth",
            "tools",
            "tokens",
            "theme",
            "update",
            "version",
            "build",
            "quit",
            "exit",
        }
        entries = [
            CompletionEntry(
                name=f"/{name}",
                description=f"Command · {name}",
                display_name=f"/{name}  [command]",
            )
            for name in sorted(command_names)
        ]
        entries.extend(skill.command_entry() for skill in skills if skill.id not in command_names)
        self.query_one(ChatInput).update_slash_commands(entries)
        for message in diagnostics[:3]:
            self.notify(message, severity="warning", markup=False)
        if skills:
            self.run_worker(self._resolve_skills(skills), exclusive=True, group="skills")

    async def _resolve_skills(self, skills: list[LocalSkill]) -> None:
        credential = self.runtime.agent.credential
        if credential is None:
            return
        try:
            response = await resolve_skill_manifest(credential, skills)
        except Exception:
            return
        self.runtime.agent.skill_manifest_id = response.get("effective_manifest_id")

    async def _mount(self, widget: Any) -> None:
        transcript = self.query_one("#transcript", ChatScroll)
        if self._loading is not None and widget is not self._loading and self._loading.is_mounted:
            await transcript.mount(widget, before=self._loading)
        else:
            await transcript.mount(widget)

    async def _ensure_tool_group(self, *, timed: bool = True) -> ToolGroupSummary:
        work_run = await self._ensure_work_run(timed=timed)
        if self._tool_group is None:
            self._tool_group = ToolGroupSummary()
            self._tool_group.work_run = work_run
            await work_run.add_unit(self._tool_group)
        else:
            work_run.touch_unit(self._tool_group)
        return self._tool_group

    async def _ensure_work_run(self, *, timed: bool = True) -> WorkRunGroup:
        if self._work_run is None:
            self._work_run = WorkRunGroup(timed=timed)
            await self._mount(self._work_run)
        return self._work_run

    def _finalize_tool_group(self) -> None:
        if self._tool_group is not None:
            self._tool_group.finalize()
            self._tool_group = None

    def _finalize_work_run(self) -> None:
        self._finalize_tool_group()
        if self._work_run is not None:
            self._work_run.finalize()
            self._work_run = None

    async def _reset_transcript(self) -> None:
        transcript = self.query_one("#transcript", ChatScroll)
        await transcript.remove_children()
        await transcript.mount(WelcomeBanner(self.workspace))
        self._assistant = None
        self._reasoning = None
        self._tools.clear()
        self._tool_groups.clear()
        self._tool_group = None
        self._work_run = None

    async def _wait_for_modal(self, screen: ModalScreen[Any]) -> Any:
        """Await a modal without requiring the caller to be a Textual worker."""
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

        def dismissed(result: Any) -> None:
            if not future.done():
                future.set_result(result)

        self.push_screen(screen, dismissed)
        try:
            return await future
        except asyncio.CancelledError:
            if self.screen is screen:
                screen.dismiss(None)
            raise

    def _update_status(self, detail: str = "") -> None:
        bar = self.query_one(StatusBar)
        bar.set_approval_mode(self.broker.mode.value)
        bar.set_status(detail)

    async def _start_loading(self) -> None:
        await self._stop_loading()
        self._loading = LoadingWidget()
        await self._mount(self._loading)

    async def _stop_loading(self) -> None:
        if self._loading is None:
            return
        loading, self._loading = self._loading, None
        loading.stop()
        if loading.is_mounted:
            await loading.remove()

    async def _close_streams(self) -> None:
        if self._assistant is not None:
            await self._assistant.stop_stream()
        if self._reasoning is not None:
            await self._reasoning.stop_stream()

    def _settle_active_tools(self, message: str) -> None:
        for tool in self._tools.values():
            if tool.status in {"pending", "running", "approval"}:
                tool.set_error(message)

    async def _finalize_turn(self, phase: TurnPhase, message: str | None = None) -> None:
        await self._close_streams()
        await self._stop_loading()
        if phase is TurnPhase.CANCELLED:
            self._settle_active_tools(message or "Cancelled by user")
        elif phase is TurnPhase.FAILED:
            self._settle_active_tools(message or "Turn failed before the tool completed")
        if phase in {TurnPhase.CANCELLED, TurnPhase.FAILED} and self._work_run is not None:
            self._work_run.mark_failed()
        self._finalize_work_run()
        self._turn_state.phase = phase
        self._update_status()

    @on(ChatInput.Submitted)
    def submitted(self, event: ChatInput.Submitted) -> None:
        self.run_worker(
            self._handle_submission(event.value),
            group="chat-submission",
            exclusive=True,
        )

    async def _handle_submission(self, submitted_value: str) -> None:
        value = submitted_value.strip()
        if not value:
            return
        token, _, remainder = value.partition(" ")
        skill = self._skills.get(token.removeprefix("/").lower())
        if skill is not None:
            self._selected_skill = skill.id
            self.runtime.agent.workflow = skill.id
            await self._submit(remainder.strip() or f"Apply the {skill.label} skill.")
        elif value.startswith("/"):
            await self._command(value)
        else:
            await self._submit(self._prepare_attachments(value))

    def _prepare_attachments(self, value: str) -> str:
        attachments: list[dict[str, Any]] = []
        media = list(self._pending_media)
        if media:
            root = self.workspace / ".copperpilot" / "attachments"
            root.mkdir(parents=True, exist_ok=True)
            for item in media:
                suffix = str(item.format).lower().replace("jpeg", "jpg")
                file = root / f"{uuid.uuid4()}.{suffix}"
                file.write_bytes(base64.b64decode(item.base64_data))
                attachments.append(path_first_attachment(file, self.workspace))
            for item in media:
                value = value.replace(item.placeholder, "", 1).strip()
            self._pending_media.clear()
        snippet = value if looks_like_kicad_snippet(value) else None
        self.runtime.agent.runtime_context = encode_runtime_context(
            attachments=attachments,
            snippet=snippet,
        )
        return "Analyze the pasted KiCad snippet." if snippet else value

    async def _submit(self, value: str) -> None:
        if self._turn_task and not self._turn_task.done():
            self.notify("A turn is already running.", severity="warning", markup=False)
            return
        self._finalize_work_run()
        self.query_one("#transcript", ChatScroll).anchor()
        await self._mount(UserMessage(value))
        self._assistant = None
        self._last_assistant_content = ""
        self._reasoning = None
        self._turn_error_rendered = False
        self._turn_task = asyncio.create_task(self._run_turn(value))

    async def _run_turn(self, value: str) -> None:
        self._turn_state = TurnState()
        self._turn_state.apply(TurnEvent("turn_started"))
        await self._start_loading()
        self._update_status("Thinking")
        try:
            await self.hooks.run("before_turn")
            async for item in self.runtime.astream(
                {
                    "messages": [HumanMessage(content=value)],
                    "copper_mode": self.copper_mode.value,
                },
                self._config(),
            ):
                namespace, mode, data = item
                del namespace
                if mode == "custom" and isinstance(data, dict):
                    await self._custom_event(data)
            await self.hooks.run("after_turn")
            await self._finalize_turn(TurnPhase.COMPLETED)
        except asyncio.CancelledError:
            self.notify("Turn cancelled.", severity="warning", markup=False)
            await self._finalize_turn(TurnPhase.CANCELLED)
        except Exception as exc:
            if not self._turn_error_rendered:
                await self._mount(ErrorMessage(str(exc)))
            await self._finalize_turn(TurnPhase.FAILED, str(exc))
        finally:
            self.runtime.agent.workflow = None
            self.runtime.agent.plan_context = None
            self.runtime.agent.runtime_context = None
            self._selected_skill = None
            self._turn_task = None

    async def _custom_event(self, event: dict[str, Any]) -> None:
        kind = str(event.get("type") or "")
        data = event.get("data")
        state_kind = kind.removeprefix("copper.")
        state_data = data
        if state_kind == "tool_request":
            state_kind = "tool_requested"
        elif state_kind == "tool_result" and isinstance(data, dict):
            result = data.get("result")
            rejected = isinstance(result, dict) and result.get("rejected") is True
            failed = isinstance(result, dict) and (
                bool(result.get("error"))
                or (isinstance(result.get("exit_code"), int) and result["exit_code"] != 0)
            )
            state_kind = "tool_phase"
            state_data = {
                "tool_call_id": data.get("tool_call_id"),
                "phase": "rejected" if rejected else "error" if failed else "success",
            }
        elif state_kind == "final":
            failed = isinstance(data, dict) and (
                data.get("success") is False or bool(data.get("error"))
            )
            state_kind = "turn_failed" if failed else "turn_completed"
        elif state_kind == "error":
            state_kind = "turn_failed"
        if not self._turn_state.apply(
            TurnEvent(
                state_kind,
                state_data,
                title=str(event.get("title")) if event.get("title") else None,
                turn_id=str(event.get("turn_id")) if event.get("turn_id") else None,
                sequence=event.get("sequence") if isinstance(event.get("sequence"), int) else None,
                resume_attempt=(
                    event.get("resume_attempt")
                    if isinstance(event.get("resume_attempt"), int)
                    else None
                ),
            )
        ):
            return
        if kind == "copper.text":
            self._finalize_work_run()
            if self._reasoning is not None:
                await self._reasoning.stop_stream()
                self._reasoning = None
            self._last_assistant_content = (self._last_assistant_content + str(data))[
                :MAX_WIDGET_TEXT
            ]
            if self._assistant is None:
                self._assistant = AssistantMessage()
                await self._mount(self._assistant)
            await self._assistant.append_content(str(data))
        elif kind == "copper.reasoning":
            if self._reasoning is None:
                self._reasoning = ReasoningMessage()
                work_run = await self._ensure_work_run()
                await work_run.add_unit(self._reasoning)
            await self._reasoning.append_content(str(data))
        elif kind == "copper.tool_request" and isinstance(data, dict):
            await self._close_streams()
            self._assistant = None
            self._reasoning = None
            arguments = data.get("arguments")
            tool = ToolCallMessage(
                str(data.get("tool_name") or "tool"),
                arguments if isinstance(arguments, dict) else {},
                workspace=self.workspace,
            )
            tool_id = str(data.get("tool_call_id"))
            self._tools[tool_id] = tool
            group = await self._ensure_tool_group()
            self._tool_groups[tool_id] = group
            await group.add_tool(tool)
            tool.set_running()
        elif kind == "copper.tool_result" and isinstance(data, dict):
            tool = self._tools.get(str(data.get("tool_call_id")))
            if tool:
                result = data.get("result")
                if isinstance(result, dict) and result.get("rejected") is True:
                    tool.set_rejected(str(result.get("error") or "Rejected by user"))
                    if tool.group is not None and tool.group.work_run is not None:
                        tool.group.work_run.mark_failed()
                elif isinstance(result, dict) and (
                    result.get("error")
                    or (isinstance(result.get("exit_code"), int) and result["exit_code"] != 0)
                ):
                    tool.set_error(str(result.get("error") or result.get("result") or result))
                    if tool.group is not None and tool.group.work_run is not None:
                        tool.group.work_run.mark_failed()
                else:
                    tool.set_success(result)
            self._assistant = None
            self._reasoning = None
        elif kind == "copper.status":
            self._update_status(str(data)[:120].replace("\n", " "))
        elif kind == "copper.status_event" and isinstance(data, dict):
            label = data.get("message") or data.get("status") or data.get("type")
            if label:
                self._update_status(str(label)[:120].replace("\n", " "))
        elif kind == "copper.final" and isinstance(data, dict):
            if (data.get("success") is False or data.get("error")) and self._work_run is not None:
                self._work_run.mark_failed()
            self._finalize_work_run()
            await self._close_streams()
            await self._stop_loading()
            if data.get("success") is False or data.get("error"):
                await self._mount(ErrorMessage(str(data.get("error") or "Hosted chat failed.")))
        elif kind == "copper.error":
            self._turn_error_rendered = True
            self._settle_active_tools(str(data))
            if self._work_run is not None:
                self._work_run.mark_failed()
            self._finalize_work_run()
            await self._mount(ErrorMessage(str(data)))

    async def _command(self, value: str) -> None:
        command, _, argument = value.partition(" ")
        if command in {"/quit", "/exit"}:
            self.exit()
        elif command == "/help":
            await self._mount(
                AssistantMessage(
                    "Commands: `/mode`, `/resume`, `/approval`, `/context`, `/copy`, "
                    "`/cwd`, `/build`, `/hooks`, `/auth`, `/tools`, `/tokens`, "
                    "`/theme`, `/update`, `/version`, `/feedback`, `/clear`, `/quit`"
                )
            )
        elif command == "/clear":
            await self._reset_transcript()
        elif command == "/copy":
            if self._last_assistant_content:
                pyperclip.copy(self._last_assistant_content)
                self.notify("Copied the last CopperPilot response.", markup=False)
            else:
                await self._mount(ErrorMessage("There is no assistant response to copy."))
        elif command == "/context":
            context = self.runtime.agent.context
            await self._mount(
                AssistantMessage(
                    "\n".join(
                        [
                            f"- Workspace: `{self.workspace}`",
                            f"- Thread: `{self.thread_id}`",
                            f"- Copper mode: `{self.copper_mode.value}`",
                            f"- Approval: `{self.broker.mode.value}`",
                            f"- Schematic: `{context.schematic_path or 'none'}`",
                            f"- PCB: `{context.pcb_path or 'none'}`",
                        ]
                    )
                )
            )
        elif command == "/cwd":
            recent = recent_workspaces()
            if not recent:
                await self._mount(AssistantMessage("No recent project folders."))
                return
            choice = await self._wait_for_modal(
                ChoiceScreen(
                    "Switch project folder",
                    [(str(path), str(path)) for path in recent],
                )
            )
            if choice:
                workspace = Path(choice)
                self.workspace = workspace
                self.thread_id = generate_thread_id()
                self.broker.set_workspace(workspace)
                self.hooks = HookRunner(workspace, self.broker)
                self.runtime.agent.workspace = workspace
                self.runtime.agent.context = discover_workspace(workspace)
                self.runtime.agent.conversation_id = self.thread_id
                self.query_one(ChatInput).set_cwd(workspace)
                self.query_one(StatusBar).set_workspace(workspace)
                remember_workspace(self.runtime.agent.context)
                await self._reset_transcript()
                self._load_skills()
                self._update_status()
        elif command == "/mode":
            choice = await self._wait_for_modal(
                ChoiceScreen(
                    "Copper mode",
                    [(item.value, item.value.title()) for item in CopperMode],
                )
            )
            if choice:
                self.copper_mode = CopperMode(choice)
                self.runtime.agent.mode = self.copper_mode
                self._update_status()
        elif command == "/build":
            if not argument:
                await self._mount(ErrorMessage("Usage: /build .copperpilot/plans/<name>.plan.md"))
                return
            try:
                plan = parse_plan(self.workspace / argument, self.workspace)
            except (OSError, ValueError) as exc:
                await self._mount(ErrorMessage(str(exc)))
                return
            self.runtime.agent.plan_context = plan
            self.runtime.agent.mode = CopperMode.AGENT
            self.copper_mode = CopperMode.AGENT
            await self._submit(f"Build the attached plan: {plan.name}")
        elif command in {"/resume", "/threads"}:
            await self.action_resume_thread()
        elif command == "/approval":
            if argument.strip().lower() == "shell":
                await self._choose_shell_approval()
            else:
                await self._choose_approval()
        elif command == "/tools":
            await self._mount(
                AssistantMessage(
                    "Local tools: read_file, write_file, edit_file, delete, "
                    "glob, grep, execute. Copper aliases (bash, read, write, edit, "
                    "delete_file) are normalized by the hosted-protocol adapter."
                )
            )
        elif command == "/hooks":
            before = self.hooks.commands("before_turn")
            after = self.hooks.commands("after_turn")
            await self._mount(
                AssistantMessage(
                    f"Before-turn hooks: {len(before)}\n\nAfter-turn hooks: {len(after)}"
                )
            )
        elif command == "/feedback":
            await self._mount(
                AssistantMessage("Send CopperPilot feedback at https://copperpilot.ai.")
            )
        elif command == "/auth":
            action = argument.strip().lower()
            if action == "logout":
                clear_credential()
                self.runtime.agent.credential = None
                await self._mount(AssistantMessage("Logged out. Use `/auth login` to reconnect."))
            elif action == "login":
                try:
                    credential = await DeviceLogin().login()
                except Exception as exc:
                    await self._mount(ErrorMessage(str(exc)))
                else:
                    self.runtime.agent.credential = credential
                    self.runtime.agent._client = HostedChatClient(
                        credential.base_url,
                        credential.api_key,
                        credential.fingerprint,
                    )
                    await self._mount(AssistantMessage("Authenticated with CopperPilot."))
            else:
                state = "authenticated" if self.runtime.agent.credential else "not authenticated"
                await self._mount(AssistantMessage(f"CopperPilot is {state}."))
        elif command == "/tokens":
            credential = self.runtime.agent.credential
            if credential is None:
                await self._mount(ErrorMessage("Not authenticated."))
            else:
                try:
                    limits = await token_limits(credential)
                except Exception as exc:
                    await self._mount(ErrorMessage(str(exc)))
                else:
                    await self._mount(
                        AssistantMessage(
                            "```json\n" + json.dumps(limits, indent=2, default=str) + "\n```"
                        )
                    )
        elif command == "/update":
            try:
                update = await check_for_update()
            except Exception as exc:
                await self._mount(ErrorMessage(str(exc)))
            else:
                message = (
                    f"CopperPilot CLI {update.latest} is available.\n\n`{update.install_command}`"
                    if update.available
                    else f"CopperPilot CLI {update.current} is current."
                )
                await self._mount(AssistantMessage(message))
        elif command == "/theme":
            await self._mount(
                AssistantMessage("CopperPilot uses the dcode dark theme with Copper accents.")
            )
        elif command == "/version":
            await self._mount(AssistantMessage(f"CopperPilot CLI {__version__}"))
        elif argument:
            await self._mount(ErrorMessage(f"Unknown command: {value}"))
        else:
            await self._mount(ErrorMessage(f"Unknown command: {command}"))

    async def _choose_approval(self) -> None:
        choice = await self._wait_for_modal(
            ChoiceScreen(
                "Local approval mode",
                [(item.value, item.value.title()) for item in ApprovalMode],
            )
        )
        if choice:
            await self._activate_approval(ApprovalMode(choice))

    async def _choose_shell_approval(self) -> None:
        normal, dangerous = shell_auto_allow_settings()
        choice = await self._wait_for_modal(
            ChoiceScreen(
                f"Auto shell settings (normal={'ON' if normal else 'OFF'}, "
                f"dangerous={'ON' if dangerous else 'OFF'})",
                [
                    ("safe", "Normal shell ON; dangerous shell OFF"),
                    ("manual", "All shell commands require approval"),
                    ("unrestricted", "Normal and dangerous shell ON"),
                ],
            )
        )
        if not choice:
            return
        normal = choice in {"safe", "unrestricted"}
        dangerous = choice == "unrestricted"
        save_shell_auto_allow_settings(normal=normal, dangerous=dangerous)
        self.broker.set_shell_auto_allow(normal=normal, dangerous=dangerous)
        self.notify("Auto shell settings updated.", markup=False)

    async def _activate_approval(self, mode: ApprovalMode) -> ApprovalMode:
        if mode is ApprovalMode.AUTO and not auto_notice_acknowledged():
            accepted = await self._wait_for_modal(
                ChoiceScreen(
                    "Auto allows routine in-workspace edits and normal shell commands. "
                    "Deletes, sensitive files, external paths, and dangerous shell commands "
                    "still require approval.",
                    [
                        ("cancel", "Stay in Manual"),
                        ("enable", "Enable Auto (normal shell ON, dangerous shell OFF)"),
                    ],
                )
            )
            if accepted != "enable":
                mode = ApprovalMode.MANUAL
            else:
                save_shell_auto_allow_settings(normal=True, dangerous=False)
                self.broker.set_shell_auto_allow(normal=True, dangerous=False)
                acknowledge_auto_notice()
        if mode is ApprovalMode.YOLO and not yolo_acknowledged():
            accepted = await self._wait_for_modal(
                ChoiceScreen(
                    "YOLO executes local side effects without review. Continue?",
                    [("no", "Stay in Manual"), ("yes", "I understand; enable YOLO")],
                )
            )
            if accepted != "yes":
                mode = ApprovalMode.MANUAL
            else:
                acknowledge_yolo()
        self.broker.mode = mode
        if mode is not ApprovalMode.YOLO:
            save_approval_mode(mode)
        self._update_status()
        return mode

    async def action_cycle_approval(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        menus = list(self.query(ApprovalMenu))
        if menus:
            menus[-1].focus()
            return
        order = [ApprovalMode.MANUAL, ApprovalMode.AUTO, ApprovalMode.YOLO]
        mode = order[(order.index(self.broker.mode) + 1) % len(order)]
        await self._activate_approval(mode)

    async def action_resume_thread(self) -> None:
        choice = await self._wait_for_modal(
            ThreadSelectorScreen(
                current_thread=self.thread_id,
                filter_cwd=str(self.workspace),
            )
        )
        if choice and choice != self.thread_id:
            self.thread_id = choice
            self.runtime.agent.conversation_id = choice
            await self._reset_transcript()
            await self._hydrate()

    async def request_approval(self, request: ApprovalRequest) -> bool:
        tool = self._tools.get(request.tool_call_id)
        if tool:
            tool.set_awaiting_approval()
        if self._loading is not None:
            self._loading.pause()
        composer = self.query_one(ChatInput)
        if composer.query_one(TextArea).has_focus:
            self._update_status("Waiting for typing to finish…")
            await composer.wait_until_typing_idle()
            self._update_status()
        decision: ApprovalDecision | None = None
        try:
            while True:
                menu = ApprovalMenu(request, self.workspace)
                await self._mount(menu)
                try:
                    decision = await menu.wait()
                finally:
                    if menu.is_mounted:
                        await menu.remove()
                if not isinstance(decision, ApprovalDecision) or decision.type != "auto":
                    break
                if await self._activate_approval(ApprovalMode.AUTO) is ApprovalMode.AUTO:
                    break
        finally:
            if self._loading is not None:
                self._loading.resume()
            if composer.is_mounted:
                composer.focus()
        if not isinstance(decision, ApprovalDecision):
            if tool:
                tool.set_rejected()
            return False
        if decision.type == "auto":
            # The mode was activated inside the loop so cancelling its notice
            # leaves this approval pending instead of approving by accident.
            assert self.broker.mode is ApprovalMode.AUTO
        if decision.type in {"approve", "auto"}:
            if tool:
                tool.set_running()
            return True
        if tool:
            tool.set_rejected(decision.reason)
        return False

    async def request_question(self, arguments: Any) -> Any:
        questions = arguments.get("questions") if isinstance(arguments, dict) else None
        if isinstance(questions, list) and questions:
            answers: list[dict[str, Any]] = []
            for row in questions:
                if not isinstance(row, dict):
                    continue
                prompt = str(row.get("question") or row.get("prompt") or "")
                answer = await self._wait_for_modal(QuestionScreen(prompt))
                answers.append({"question": prompt, "answer": answer or ""})
            return {"answers": answers}
        prompt = (
            str(arguments.get("question") or arguments.get("prompt") or "")
            if isinstance(arguments, dict)
            else str(arguments)
        )
        return await self._wait_for_modal(QuestionScreen(prompt)) or "No response"

    async def action_cancel_or_quit(self) -> None:
        if self._turn_task and not self._turn_task.done():
            try:
                await self.runtime.acancel()
            except Exception as exc:
                logger.warning("Hosted cancellation failed (%s)", type(exc).__name__)
            finally:
                self._turn_task.cancel()
                if isinstance(self.screen, ModalScreen):
                    self.screen.dismiss(None)
                await self.broker.cancel_all()
        else:
            self.exit()

    async def action_paste_or_attach(self) -> None:
        image = await asyncio.to_thread(get_clipboard_image)
        area = self.query_one(ChatInput).query_one(TextArea)
        if image is not None:
            image.placeholder = f"[image {len(self._pending_media) + 1}]"
            self._pending_media.append(image)
            area.insert(image.placeholder + " ")
            self.notify("Attached clipboard image.", markup=False)
            return
        try:
            text = await asyncio.to_thread(pyperclip.paste)
        except pyperclip.PyperclipException:
            return
        if text:
            area.insert(str(text))
