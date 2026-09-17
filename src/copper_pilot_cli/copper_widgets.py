"""Curated Textual widgets adapted from the pinned dcode presentation fork."""

from __future__ import annotations

import asyncio
import json
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path
from time import monotonic
from typing import Any

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.reactive import var
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Checkbox, Input, Label, Markdown, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from copper_pilot_cli.copper_config import paths
from copper_pilot_cli.copper_tools import ApprovalRequest, canonical_tool_name
from copper_pilot_cli.sessions import ThreadInfo, list_threads

MAX_WIDGET_TEXT = 64 * 1024
_TERMINAL_ESCAPE = re.compile(r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def literal_text(value: object, limit: int = MAX_WIDGET_TEXT) -> Content:
    """Render untrusted hosted/local text without interpreting Rich markup."""
    text = _TERMINAL_ESCAPE.sub("", str(value))
    text = "".join(character for character in text if character in "\n\t" or ord(character) >= 32)
    if len(text) > limit:
        text = text[:limit] + "\n[display truncated]"
    return Content(text)


def bounded_widget_text(value: object, limit: int = MAX_WIDGET_TEXT) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "\n[display truncated]"


@dataclass(frozen=True, slots=True)
class CompletionEntry:
    name: str
    description: str
    display_name: str = ""


class ComposerTextArea(TextArea):
    """Text area that reserves bare Enter for prompt submission."""

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    def __init__(self, history: list[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.input_history = history
        self.history_index = len(history)

    def submit(self) -> None:
        value = self.text.strip()
        if value:
            self.text = ""
            self.post_message(self.Submitted(value))

    async def _on_key(self, event: events.Key) -> None:
        completion_active = bool(getattr(self.parent, "completion_active", False))
        if completion_active and event.key in {
            "up",
            "down",
            "tab",
            "enter",
            "space",
            "escape",
        }:
            event.prevent_default()
            return
        if event.key == "enter":
            self.submit()
            event.stop()
            event.prevent_default()
            return
        row, _ = self.cursor_location
        if event.key == "up" and self.input_history and row == 0:
            self.history_index = max(self.history_index - 1, 0)
            self.text = self.input_history[self.history_index]
            self.move_cursor((len(self.document.lines) - 1, len(self.document.lines[-1])))
            event.stop()
            event.prevent_default()
            return
        if event.key == "down" and self.input_history and row == len(self.document.lines) - 1:
            self.history_index = min(self.history_index + 1, len(self.input_history))
            self.text = (
                self.input_history[self.history_index]
                if self.history_index < len(self.input_history)
                else ""
            )
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)


class ChatInput(Vertical):
    """Multiline dcode-style composer with slash and `@file` completion."""

    DEFAULT_CSS = """
    ChatInput {
        height: auto;
        max-height: 16;
        border: solid $primary;
        background: $surface;
    }
    ChatInput TextArea {
        height: auto;
        min-height: 3;
        max-height: 10;
        border: none;
        background: transparent;
    }
    ChatInput OptionList {
        height: auto;
        max-height: 7;
        display: none;
        border-top: solid $primary;
    }
    ChatInput.-completing OptionList {
        display: block;
    }
    """

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value
            self.mode = "normal"

    def __init__(
        self,
        cwd: str | Path | None = None,
        history_file: Path | None = None,
        image_tracker: Any | None = None,
        **kwargs: Any,
    ) -> None:
        del image_tracker
        super().__init__(**kwargs)
        self.cwd = Path(cwd or ".").resolve()
        self.history_file = history_file or paths().history
        try:
            self.history = [
                value
                for line in self.history_file.read_text(encoding="utf-8").splitlines()
                if line
                for value in [json.loads(line)]
                if isinstance(value, str)
            ][-500:]
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            self.history = []
        self.commands: list[CompletionEntry] = []
        self._matches: list[CompletionEntry] = []
        self._completion_kind: str | None = None
        self._selected_index = 0
        self.last_changed_at = 0.0
        self._typing_changed = asyncio.Event()

    @property
    def completion_active(self) -> bool:
        return bool(self._matches)

    def compose(self) -> ComposeResult:
        yield ComposerTextArea(self.history, id="chat-input", soft_wrap=True)
        yield OptionList(id="completions")

    def focus(self, scroll_visible: bool = True) -> ChatInput:
        self.query_one(TextArea).focus(scroll_visible)
        return self

    def update_slash_commands(self, commands: Sequence[Any]) -> None:
        self.commands = [
            CompletionEntry(
                name=str(item.name),
                description=str(item.description),
                display_name=str(getattr(item, "display_name", "") or ""),
            )
            for item in commands
        ]

    def set_cwd(self, cwd: str | Path) -> None:
        self.cwd = Path(cwd).resolve()

    def _current_token(self) -> str:
        text = self.query_one(TextArea).text
        return re.split(r"\s", text)[-1] if text else ""

    def _file_entries(self, token: str) -> list[CompletionEntry]:
        query = token.removeprefix("@").lower()
        entries: list[CompletionEntry] = []
        for path in self.cwd.rglob("*"):
            if len(entries) >= 100:
                break
            if not path.is_file() or any(
                part in {".git", "node_modules", "__pycache__", ".venv"}
                for part in path.relative_to(self.cwd).parts
            ):
                continue
            relative = path.relative_to(self.cwd).as_posix()
            if query in relative.lower():
                entries.append(CompletionEntry(f"@{relative}", "File", f"@{relative}  [file]"))
        return sorted(entries, key=lambda item: (len(item.name), item.name.lower()))

    def _refresh_completions(self) -> None:
        token = self._current_token()
        if token.startswith("/"):
            query = token.lower()
            self._completion_kind = "slash"
            self._matches = [
                item
                for item in self.commands
                if query in item.name.lower() or query in item.description.lower()
            ][:50]
        elif token.startswith("@"):
            self._completion_kind = "file"
            self._matches = self._file_entries(token)
        else:
            self._completion_kind = None
            self._matches = []
        self._selected_index = 0
        options = self.query_one(OptionList)
        options.clear_options()
        options.add_options(
            Option(
                literal_text(f"{item.display_name or item.name}  {item.description}"),
                id=str(index),
            )
            for index, item in enumerate(self._matches)
        )
        options.highlighted = 0 if self._matches else None
        self.set_class(bool(self._matches), "-completing")

    @on(TextArea.Changed)
    def changed(self) -> None:
        self.last_changed_at = monotonic()
        self._typing_changed.set()
        self._refresh_completions()

    async def wait_until_typing_idle(self, idle_for: float = 0.6, max_wait: float = 5.0) -> None:
        """Avoid letting approval shortcuts consume prompt text being typed."""
        try:
            async with asyncio.timeout(max_wait):
                while self.query_one(TextArea).has_focus:
                    remaining = idle_for - (monotonic() - self.last_changed_at)
                    if remaining <= 0:
                        return
                    self._typing_changed.clear()
                    try:
                        async with asyncio.timeout(remaining):
                            await self._typing_changed.wait()
                    except TimeoutError:
                        return
        except TimeoutError:
            return

    @on(OptionList.OptionSelected)
    def complete(self, event: OptionList.OptionSelected) -> None:
        self._apply_completion(int(str(event.option.id)))

    def _dismiss_completion(self) -> None:
        self._matches = []
        self._completion_kind = None
        self._selected_index = 0
        self.query_one(OptionList).clear_options()
        self.set_class(False, "-completing")

    def _apply_completion(self, index: int, *, trailing_space: bool = True) -> None:
        if not 0 <= index < len(self._matches):
            return
        area = self.query_one(TextArea)
        text = area.text
        token = self._current_token()
        suffix = " " if trailing_space else ""
        area.text = text[: len(text) - len(token)] + self._matches[index].name + suffix
        area.move_cursor((len(area.document.lines) - 1, len(area.document.lines[-1])))
        self._dismiss_completion()
        area.focus()

    async def on_key(self, event: events.Key) -> None:
        if not self._matches:
            return
        if event.key in {"up", "down"}:
            delta = -1 if event.key == "up" else 1
            self._selected_index = (self._selected_index + delta) % len(self._matches)
            self.query_one(OptionList).highlighted = self._selected_index
        elif event.key == "escape":
            self._dismiss_completion()
        elif event.key == "enter":
            kind = self._completion_kind
            self._apply_completion(
                self._selected_index,
                trailing_space=kind != "slash",
            )
            if kind == "slash":
                self.query_one(ComposerTextArea).submit()
        elif event.key == "tab" or (event.key == "space" and self._completion_kind == "slash"):
            self._apply_completion(self._selected_index)
        elif event.key == "space":
            self.query_one(ComposerTextArea).insert(" ")
        else:
            return
        event.stop()
        event.prevent_default()

    @on(ComposerTextArea.Submitted)
    def text_submitted(self, event: ComposerTextArea.Submitted) -> None:
        self.history.append(event.value)
        self.history[:] = self.history[-500:]
        area = self.query_one(ComposerTextArea)
        area.history_index = len(self.history)
        try:
            self.history_file.parent.mkdir(parents=True, exist_ok=True)
            with self.history_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event.value) + "\n")
            if self.history_file.exists() and self.history_file.stat().st_size > 2_000_000:
                self.history_file.write_text(
                    "\n".join(json.dumps(value) for value in self.history) + "\n",
                    encoding="utf-8",
                )
            if self.history_file.exists() and self.history_file.parent.exists():
                self.history_file.parent.chmod(stat.S_IRWXU)
                self.history_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        self.post_message(self.Submitted(event.value))


class UserMessage(Static):
    DEFAULT_CSS = """
    UserMessage {
        height: auto;
        padding: 1;
        margin-bottom: 1;
        background: $primary 12%;
        border-left: wide $primary;
    }
    """

    def __init__(self, content: str) -> None:
        super().__init__(Content(content))


class AssistantMessage(Vertical):
    DEFAULT_CSS = """
    AssistantMessage { height: auto; padding: 0 1; margin-bottom: 1; }
    AssistantMessage Markdown { padding: 0; }
    """

    def __init__(self, content: str = "") -> None:
        super().__init__()
        self.content = bounded_widget_text(content)
        self._rendered_content = self.content
        self._flush_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield Markdown(self.content)

    async def append_content(self, text: str) -> None:
        self.content = bounded_widget_text(self.content + text)
        if self._flush_timer is None:
            self._flush_timer = self.set_timer(0.05, self._flush)

    async def _flush(self) -> None:
        self._flush_timer = None
        if self.content != self._rendered_content and self.is_mounted:
            self._rendered_content = self.content
            await self.query_one(Markdown).update(self.content)

    async def stop_stream(self) -> None:
        if self._flush_timer is not None:
            self._flush_timer.stop()
            self._flush_timer = None
        await self._flush()


class ReasoningMessage(Vertical):
    DEFAULT_CSS = """
    ReasoningMessage { height: auto; color: $text-muted; padding: 0 1; }
    ReasoningMessage #reasoning { display: block; }
    ReasoningMessage.-collapsed #reasoning { display: none; }
    """
    collapsed = var(False, toggle_class="-collapsed")

    def __init__(self, content: str = "") -> None:
        super().__init__()
        self.content = bounded_widget_text(content)

    def compose(self) -> ComposeResult:
        yield Static("Reasoning  [click to collapse]", classes="reasoning-header")
        yield Static(literal_text(self.content), id="reasoning")

    @on(events.Click)
    def toggle(self) -> None:
        self.collapsed = not self.collapsed

    async def append_content(self, text: str) -> None:
        self.content = bounded_widget_text(self.content + text)
        self.query_one("#reasoning", Static).update(literal_text(self.content))

    async def stop_stream(self) -> None:
        self.collapsed = True


def _tool_path(args: dict[str, Any]) -> str:
    return str(args.get("file_path") or args.get("path") or "")


def _tool_label(tool_name: str, args: dict[str, Any]) -> str:
    name = canonical_tool_name(tool_name)
    if name == "execute":
        command = str(args.get("command") or "")
        return f"$ {command}" if command else "Run shell command"
    if name == "read_file":
        return f"Read {_tool_path(args)}"
    if name == "write_file":
        return f"Write {_tool_path(args)}"
    if name == "edit_file":
        return f"Edit {_tool_path(args)}"
    if name == "delete":
        return f"Delete {_tool_path(args)}"
    if name == "glob":
        return f"Glob {args.get('pattern') or ''}"
    if name == "grep":
        suffix = f" in {args['path']}" if args.get("path") else ""
        return f"Grep {args.get('pattern') or ''}{suffix}"
    if name == "read_binary_file":
        return f"Read binary {_tool_path(args)}"
    if name == "write_binary_file":
        return f"Write binary {_tool_path(args)}"
    if name == "ask_user_question":
        return "Ask user"
    return tool_name


def _display_output(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if value.get("error"):
            return str(value["error"])
        if "result" in value:
            result = value["result"]
            return result if isinstance(result, str) else json.dumps(result, default=str, indent=2)
    return json.dumps(value, default=str, indent=2)


def _compact_command(command: str, limit: int = 120) -> str:
    """Keep shell rows to one visual line until explicitly expanded."""
    line = " ".join(command.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


_TOOL_ACTIVITY = {
    "execute": ("Running", "Ran", "shell command"),
    "read_file": ("Reading", "Read", "file"),
    "read_binary_file": ("Reading", "Read", "binary file"),
    "write_file": ("Writing", "Wrote", "file"),
    "write_binary_file": ("Writing", "Wrote", "binary file"),
    "edit_file": ("Editing", "Edited", "file"),
    "delete": ("Deleting", "Deleted", "file"),
    "glob": ("Searching", "Searched", "path"),
    "grep": ("Searching", "Searched", "file"),
}


def format_work_duration(elapsed_seconds: float) -> str:
    """Format elapsed work using the Copper desktop client's duration contract."""
    total_seconds = max(1, round(max(0.0, elapsed_seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        label = f"{hours} {'hr' if hours == 1 else 'hrs'}"
        return label if not minutes else f"{label} {minutes} {'min' if minutes == 1 else 'mins'}"
    if minutes:
        label = f"{minutes} {'min' if minutes == 1 else 'mins'}"
        return label if not seconds else f"{label} {seconds}s"
    return f"{total_seconds}s"


class WorkRunGroup(Vertical):
    """Electron-style live work window that disappears into a duration label."""

    DEFAULT_CSS = """
    WorkRunGroup { height: auto; margin-bottom: 1; }
    WorkRunGroup .work-run-header { height: 1; color: $text-muted; padding: 0 1; }
    WorkRunGroup .work-run-body { height: auto; }
    WorkRunGroup.-complete .work-run-body { display: none; }
    WorkRunGroup.-failed .work-run-body { display: block; }
    """

    def __init__(self, *, timed: bool = True) -> None:
        super().__init__()
        self.units: list[Widget] = []
        self.started_at = monotonic() if timed else None
        self.finished_at: float | None = None
        self.failed = False
        self._timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield Static(self._label(), classes="work-run-header")
        yield Vertical(classes="work-run-body")

    def on_mount(self) -> None:
        self._refresh_header()
        if self.started_at is not None:
            self._timer = self.set_interval(1.0, self._refresh_header)

    async def add_unit(self, widget: Widget) -> None:
        self.units.append(widget)
        widget.add_class("work-run-unit")
        await self.query_one(".work-run-body", Vertical).mount(widget)
        self._refresh_visibility()

    def touch_unit(self, widget: Widget) -> None:
        """Move a reused live summary to the latest position in the work window."""
        if widget not in self.units:
            return
        self.units.remove(widget)
        self.units.append(widget)
        body = self.query_one(".work-run-body", Vertical)
        if body.children and body.children[-1] is not widget:
            body.move_child(widget, after=body.children[-1])
        self._refresh_visibility()

    def mark_failed(self) -> None:
        self.failed = True
        self.add_class("-failed")
        self._refresh_visibility()

    def finalize(self) -> None:
        if self.finished_at is not None:
            return
        self.finished_at = monotonic() if self.started_at is not None else None
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self.add_class("-complete")
        self._refresh_header()
        self._refresh_visibility()

    def _refresh_header(self) -> None:
        if not self.is_mounted:
            return
        self.query_one(".work-run-header", Static).update(literal_text(self._label()))

    def _label(self) -> str:
        label = "Worked"
        if self.started_at is not None:
            end = self.finished_at if self.finished_at is not None else monotonic()
            label = f"Worked for {format_work_duration(end - self.started_at)}"
        return label

    def _refresh_visibility(self) -> None:
        recent = set(self.units[-3:])
        for unit in self.units:
            failed_tool_group = isinstance(unit, ToolGroupSummary) and unit.has_failures
            unit.display = unit in recent or (self.failed and failed_tool_group)


class ToolGroupSummary(Vertical):
    """Dcode-style aggregate that folds consecutive successful tool calls."""

    DEFAULT_CSS = """
    ToolGroupSummary { height: auto; margin-bottom: 1; }
    ToolGroupSummary .tool-group-summary { height: 1; color: $text-muted; padding: 0 1; }
    ToolGroupSummary.-live .tool-group-summary { color: $tool; }
    ToolGroupSummary > ToolCallMessage { display: none; }
    ToolGroupSummary.-expanded > ToolCallMessage { display: block; }
    ToolGroupSummary > ToolCallMessage.-status-error,
    ToolGroupSummary > ToolCallMessage.-status-rejected { display: block; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.tools: list[ToolCallMessage] = []
        self.finalized = False
        self.expanded = False
        self.work_run: WorkRunGroup | None = None

    def compose(self) -> ComposeResult:
        yield Static("", classes="tool-group-summary")

    async def add_tool(self, tool: ToolCallMessage) -> None:
        self.tools.append(tool)
        tool.group = self
        await self.mount(tool)
        self.refresh_summary()

    @on(events.Click, ".tool-group-summary")
    def toggle(self) -> None:
        if not self.finalized or any(tool.status != "success" for tool in self.tools):
            return
        self.expanded = not self.expanded
        self.set_class(self.expanded, "-expanded")
        self.refresh_summary()

    def finalize(self) -> None:
        self.finalized = True
        self.refresh_summary()

    @property
    def has_failures(self) -> bool:
        return any(tool.status in {"error", "rejected"} for tool in self.tools)

    def refresh_summary(self) -> None:
        if not self.is_mounted:
            return
        running = [tool for tool in self.tools if tool.status in {"pending", "running", "approval"}]
        failed = [tool for tool in self.tools if tool.status in {"error", "rejected"}]
        succeeded = [tool for tool in self.tools if tool.status == "success"]
        self.set_class(bool(running), "-live")

        if failed:
            self.query_one(".tool-group-summary", Static).display = bool(succeeded or running)
            text = self._summary(succeeded, completed=True)
            if running:
                live = self._summary(running, completed=False)
                text = f"{text}, {live.lower()}" if text else live
        elif running:
            self.query_one(".tool-group-summary", Static).display = True
            text = self._summary(running, completed=False) + "…"
        else:
            self.query_one(".tool-group-summary", Static).display = True
            disclosure = "▾" if self.expanded else "▸"
            text = f"{disclosure} {self._summary(succeeded, completed=True)}"
        self.query_one(".tool-group-summary", Static).update(literal_text(text))

    @staticmethod
    def _summary(tools: list[ToolCallMessage], *, completed: bool) -> str:
        counts: dict[str, int] = {}
        order: list[str] = []
        for tool in tools:
            if tool.canonical_name not in counts:
                counts[tool.canonical_name] = 0
                order.append(tool.canonical_name)
            counts[tool.canonical_name] += 1
        parts: list[str] = []
        for name in order:
            present, past, noun = _TOOL_ACTIVITY.get(name, ("Running", "Ran", "tool"))
            count = counts[name]
            verb = past if completed else present
            parts.append(f"{verb} {count} {noun}{'' if count == 1 else 's'}")
        return ", ".join(parts) or ("Completed work" if completed else "Working")


class ToolCallMessage(Vertical):
    """Pinned dcode-style tool lifecycle row for hosted tool calls."""

    DEFAULT_CSS = """
    ToolCallMessage {
        height: auto;
        padding: 0 1 0 0;
        margin-bottom: 1;
        background: transparent;
        border-left: wide $tool;
    }
    ToolCallMessage .tool-name {
        color: $tool;
        text-style: bold;
        margin-left: 1;
    }
    ToolCallMessage .tool-status {
        color: $warning;
        margin-left: 3;
    }
    ToolCallMessage .tool-output {
        color: $text-muted;
        margin-left: 3;
        height: auto;
    }
    ToolCallMessage .tool-output-hint {
        color: $text-muted;
        margin-left: 3;
    }
    ToolCallMessage.-status-success {
        border-left: wide $success;
        background: $success 8%;
    }
    ToolCallMessage.-status-error {
        border-left: wide $error;
        background: $error 10%;
    }
    ToolCallMessage.-status-rejected {
        border-left: wide $warning;
        background: $warning 8%;
    }
    """

    def __init__(
        self,
        tool_name: str,
        args: dict[str, Any] | None = None,
        *,
        workspace: Path | None = None,
    ) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.canonical_name = canonical_tool_name(tool_name)
        self.args = args or {}
        self.workspace = workspace
        self.status = "pending"
        self.output = ""
        self.expanded = False
        self._started: float | None = None
        self._timer: Timer | None = None
        self.group: ToolGroupSummary | None = None
        self._before_content = self._capture_before()

    def compose(self) -> ComposeResult:
        yield Static(literal_text(self._label()), classes="tool-name")
        yield Static("", classes="tool-status")
        yield Static("", classes="tool-output")
        yield Static("", classes="tool-output-hint")

    def _label(self) -> str:
        if self.canonical_name == "execute":
            command = str(self.args.get("command") or "")
            if self.expanded:
                return f"$ {command}" if command else "Run shell command"
            return f"$ {_compact_command(command)}" if command else "Run shell command"
        return _tool_label(self.tool_name, self.args)

    async def on_mount(self) -> None:
        if self.status == "running" and self._timer is None:
            self._timer = self.set_interval(0.25, self._refresh_display)
        self._refresh_display()

    @on(events.Click)
    def toggle_output(self) -> None:
        if self._has_hidden_output() or self._has_hidden_arguments():
            self.expanded = not self.expanded
            self._refresh_display()

    def set_running(self) -> None:
        self.status = "running"
        self._started = monotonic()
        if self.is_mounted and self._timer is None:
            self._timer = self.set_interval(0.25, self._refresh_display)
        self._refresh_display()

    def set_awaiting_approval(self) -> None:
        self.status = "approval"
        self._stop_timer()
        self._refresh_display()

    def set_success(self, result: object = "") -> None:
        self._finish("success", self._mutation_diff() or _display_output(result))

    def set_error(self, error: object) -> None:
        if self.status == "rejected":
            return
        self.expanded = True
        self._finish("error", _display_output(error))

    def set_rejected(self, reason: str | None = None) -> None:
        if self.status == "rejected" and self.output:
            return
        self.expanded = True
        self._finish("rejected", reason or "Rejected by user")

    def _finish(self, status: str, output: str) -> None:
        self.status = status
        self.output = bounded_widget_text(output)
        self._stop_timer()
        self._refresh_display()

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _preview(self) -> str:
        if self.expanded:
            return self.output
        lines = self.output.splitlines()
        shown = "\n".join(lines[:4])
        return shown[:400]

    def _has_hidden_output(self) -> bool:
        return len(self.output) > 400 or len(self.output.splitlines()) > 4

    def _has_hidden_arguments(self) -> bool:
        command = str(self.args.get("command") or "")
        return self.canonical_name == "execute" and (
            len(command) > 120 or len(command.splitlines()) > 1
        )

    def _capture_before(self) -> str | None:
        path_value = _tool_path(self.args)
        if (
            self.workspace is None
            or not path_value
            or self.canonical_name not in {"write_file", "edit_file", "delete"}
            or _sensitive_path(path_value)
        ):
            return None
        root = self.workspace.resolve()
        candidate = Path(path_value)
        resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
        try:
            resolved.relative_to(root)
            return resolved.read_text(encoding="utf-8") if resolved.exists() else ""
        except (OSError, UnicodeError, ValueError):
            return None

    def _mutation_diff(self) -> str:
        if self._before_content is None:
            return ""
        after = self._before_content
        if self.canonical_name == "write_file":
            after = str(self.args.get("content") or "")
        elif self.canonical_name == "edit_file":
            old = str(self.args.get("old_string") or "")
            new = str(self.args.get("new_string") or "")
            if not old or old not in after:
                return ""
            after = after.replace(old, new, -1 if self.args.get("replace_all") else 1)
        elif self.canonical_name == "delete":
            after = ""
        else:
            return ""
        path = _tool_path(self.args)
        return "\n".join(
            unified_diff(
                self._before_content.splitlines(),
                after.splitlines(),
                fromfile=path,
                tofile=path if self.canonical_name != "delete" else "/dev/null",
                lineterm="",
            )
        )

    def _refresh_display(self) -> None:
        if not self.is_mounted:
            return
        for name in ("success", "error", "rejected"):
            self.set_class(self.status == name, f"-status-{name}")
        elapsed = monotonic() - self._started if self._started is not None else 0
        statuses = {
            "pending": "",
            "approval": "Awaiting approval",
            "running": f"Running…{f' ({elapsed:.0f}s)' if elapsed >= 10 else ''}",
            "success": "Success",
            "error": "Error",
            "rejected": "Rejected",
        }
        status = statuses.get(self.status, self.status)
        self.query_one(".tool-name", Static).update(literal_text(self._label()))
        self.query_one(".tool-status", Static).update(literal_text(status))
        self.query_one(".tool-status", Static).display = bool(status)
        preview = self._preview()
        self.query_one(".tool-output", Static).update(literal_text(preview))
        self.query_one(".tool-output", Static).display = bool(preview)
        hint = ""
        if self._has_hidden_output() or self._has_hidden_arguments():
            hint = "Click to collapse" if self.expanded else "Click to expand"
        self.query_one(".tool-output-hint", Static).update(literal_text(hint))
        self.query_one(".tool-output-hint", Static).display = bool(hint)
        if self.group is not None:
            self.group.refresh_summary()


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """Decision returned by the tool approval screen."""

    type: str
    reason: str | None = None


def _sensitive_path(path: str) -> bool:
    parts = {part.lower() for part in Path(path).parts}
    name = Path(path).name.lower()
    return bool(parts & {".git", ".ssh", ".copperpilot"}) or name == ".env"


def _approval_preview(request: ApprovalRequest, workspace: Path) -> str:
    name = request.canonical_name or canonical_tool_name(request.tool_name)
    args = request.arguments
    path_value = _tool_path(args)
    if name == "execute":
        return str(args.get("command") or "")
    if _sensitive_path(path_value):
        return f"File: {path_value}\n\nContents hidden — file may contain credentials"
    if name in {"write_file", "write_binary_file"}:
        content = args.get("content", "")
        if name == "write_binary_file":
            content = "[base64 binary content omitted]"
        return f"File: {path_value}\n\n{content}"
    if name == "edit_file":
        before = str(args.get("old_string") or "")
        after = str(args.get("new_string") or "")
        resolved = Path(path_value)
        if not resolved.is_absolute():
            resolved = workspace / resolved
        try:
            current = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            current = ""
        if before and before in current:
            updated = current.replace(
                before,
                after,
                -1 if args.get("replace_all") else 1,
            )
            old_lines = current.splitlines()
            new_lines = updated.splitlines()
        else:
            old_lines = before.splitlines()
            new_lines = after.splitlines()
        diff = "\n".join(
            unified_diff(old_lines, new_lines, fromfile=path_value, tofile=path_value, lineterm="")
        )
        return diff or f"File: {path_value}\n\nNo changes to display"
    if name == "delete":
        resolved = Path(path_value)
        if not resolved.is_absolute():
            resolved = workspace / resolved
        try:
            content = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return f"Delete: {path_value}"
        removed = "\n".join(
            unified_diff(
                content.splitlines(),
                [],
                fromfile=path_value,
                tofile="/dev/null",
                lineterm="",
            )
        )
        return removed
    return json.dumps(args, default=str, indent=2)


def _unicode_warning(arguments: dict[str, Any]) -> str:
    text = json.dumps(arguments, default=str, ensure_ascii=False)
    suspicious = [
        character
        for character in text
        if ord(character) in {*range(0x202A, 0x202F), *range(0x2066, 0x206A)}
    ]
    return "Warning: hidden bidirectional Unicode detected." if suspicious else ""


class ApprovalMenu(Container):
    """Inline dcode-style approval menu driven by a Copper approval request."""

    can_focus = True
    can_focus_children = False
    BINDINGS = [
        Binding("up", "move_up", "Up", show=False),
        Binding("k", "move_up", "Up", show=False),
        Binding("down", "move_down", "Down", show=False),
        Binding("j", "move_down", "Down", show=False),
        Binding("enter", "select", "Select", show=False),
        Binding("1", "select_position(0)", "Approve", show=False),
        Binding("2", "select_position(1)", "Auto", show=False),
        Binding("3", "select_position(2)", "Reject", show=False),
        Binding("y", "approve", "Approve", show=False),
        Binding("a", "auto", "Auto", show=False),
        Binding("n", "reject", "Reject", show=False),
        Binding("escape", "reject", "Reject", show=False),
        Binding("tab", "reason", "Reject with feedback", show=False),
    ]
    DEFAULT_CSS = """
    ApprovalMenu {
        height: auto;
        margin: 1 0;
        padding: 0 1;
        background: $surface;
        border: solid $warning;
    }
    ApprovalMenu .approval-title { height: auto; color: $warning; text-style: bold; }
    ApprovalMenu .approval-preview { height: auto; max-height: 10; color: $text-muted; }
    ApprovalMenu .approval-option { height: 1; padding: 0 1; }
    ApprovalMenu .approval-option-selected { background: $primary; text-style: bold; }
    ApprovalMenu .approval-help { height: auto; color: $text-muted; text-style: italic; }
    ApprovalMenu .approval-reason-input {
        display: none; height: 3; margin-top: 1; border: solid $warning;
    }
    """

    def __init__(self, request: ApprovalRequest, workspace: Path) -> None:
        super().__init__()
        self.request = request
        self.workspace = workspace
        self._selected = 0
        self._options: list[tuple[str, str]] = [
            ("Approve (y)", "approve"),
            ("Enable Auto (a)", "auto"),
            ("Reject (n)", "reject"),
        ]
        self._option_widgets: list[Static] = []
        self._reason_active = False
        self._future: asyncio.Future[ApprovalDecision] = asyncio.get_running_loop().create_future()

    def compose(self) -> ComposeResult:
        yield Static(
            literal_text(
                f">>> {canonical_tool_name(self.request.tool_name)} Requires Approval <<<"
            ),
            classes="approval-title",
        )
        warning = _unicode_warning(self.request.arguments)
        if warning:
            yield Static(literal_text(warning), classes="approval-warning")
        with VerticalScroll(classes="approval-preview"):
            yield Static(
                literal_text(_approval_preview(self.request, self.workspace), limit=32 * 1024)
            )
        for index, (_label, _decision) in enumerate(self._options):
            widget = Static("", id=f"approval-option-{index}", classes="approval-option")
            self._option_widgets.append(widget)
            yield widget
        yield Input(
            placeholder="Reason (Enter to submit, Esc to cancel)",
            classes="approval-reason-input",
            select_on_focus=False,
        )
        yield Static("", classes="approval-help")

    def on_mount(self) -> None:
        self._refresh_options()
        self.focus()

    async def wait(self) -> ApprovalDecision:
        return await self._future

    def _refresh_options(self) -> None:
        for index, ((label, _decision), widget) in enumerate(
            zip(self._options, self._option_widgets, strict=True)
        ):
            widget.update(f"{'›' if index == self._selected else ' '} {index + 1}. {label}")
            widget.set_class(index == self._selected, "approval-option-selected")
        help_text = (
            "Enter submit • Esc cancel • leave blank to reject"
            if self._reason_active
            else "↑/↓ or j/k navigate • Enter select • y/a/n quick keys • Tab reject with feedback"
        )
        self.query_one(".approval-help", Static).update(help_text)

    def action_move_up(self) -> None:
        if not self._reason_active:
            self._selected = (self._selected - 1) % len(self._options)
            self._refresh_options()

    def action_move_down(self) -> None:
        if not self._reason_active:
            self._selected = (self._selected + 1) % len(self._options)
            self._refresh_options()

    def action_select(self) -> None:
        if self._reason_active:
            self._submit_reason(self.query_one(Input).value)
        else:
            self._decide(self._options[self._selected][1])

    def action_select_position(self, position: int) -> None:
        if not self._reason_active and 0 <= position < len(self._options):
            self._decide(self._options[position][1])

    @on(events.Click, ".approval-option")
    def clicked_option(self, event: events.Click) -> None:
        if event.widget is None:
            return
        identifier = event.widget.id or ""
        try:
            position = int(identifier.rsplit("-", 1)[-1])
        except ValueError:
            return
        self.action_select_position(position)

    def action_approve(self) -> None:
        if not self._reason_active:
            self._decide("approve")

    def action_auto(self) -> None:
        if not self._reason_active:
            self._decide("auto")

    def action_reject(self) -> None:
        if self._reason_active:
            self._exit_reason()
        else:
            self._decide("reject")

    def action_reason(self) -> None:
        if self._reason_active:
            return
        self._selected = 2
        self._reason_active = True
        field = self.query_one(Input)
        field.display = True
        self._refresh_options()
        field.focus()

    @on(Input.Submitted)
    def submitted_reason(self, event: Input.Submitted) -> None:
        if event.input is self.query_one(Input) and self._reason_active:
            event.stop()
            self._submit_reason(event.value)

    def _submit_reason(self, value: str) -> None:
        self._reason_active = False
        self._decide("reject", value.strip() or None)

    def _exit_reason(self) -> None:
        self._reason_active = False
        self.query_one(Input).display = False
        self._refresh_options()
        self.focus()

    def _decide(self, decision: str, reason: str | None = None) -> None:
        if not self._future.done():
            self._future.set_result(ApprovalDecision(decision, reason))
        self.display = False

    def on_blur(self) -> None:
        if not self._reason_active:
            self.call_after_refresh(self.focus)


class ErrorMessage(Static):
    DEFAULT_CSS = """
    ErrorMessage { height: auto; color: $error; border-left: wide $error; padding: 0 1; }
    """

    def __init__(self, content: str) -> None:
        super().__init__(literal_text(content))


class ThreadSelectorScreen(ModalScreen[str | None]):
    """Folder-scoped thread picker with an explicit all-folders toggle."""

    DEFAULT_CSS = """
    ThreadSelectorScreen { align: center middle; }
    ThreadSelectorScreen #thread-box {
        width: 90%;
        max-width: 120;
        height: 80%;
        border: solid $primary;
        background: $surface;
        padding: 1 2;
    }
    ThreadSelectorScreen #thread-box > Horizontal { height: auto; }
    ThreadSelectorScreen OptionList { height: 1fr; }
    """

    def __init__(self, current_thread: str | None = None, *, filter_cwd: str | None = None) -> None:
        super().__init__()
        self.current_thread = current_thread
        self.filter_cwd = filter_cwd
        self.threads: list[ThreadInfo] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="thread-box"):
            yield Label("Previous chats")
            with Horizontal():
                yield Input(placeholder="Filter chats", id="thread-filter")
                yield Checkbox("All folders", id="all-folders")
            yield OptionList(id="threads")

    async def on_mount(self) -> None:
        await self._load()

    async def _load(self) -> None:
        all_folders = self.query_one("#all-folders", Checkbox).value
        self.threads = await list_threads(
            limit=200,
            cwd=None if all_folders else self.filter_cwd,
            include_message_count=True,
            sort_by="updated",
        )
        self._render_threads()

    def _render_threads(self) -> None:
        query = self.query_one("#thread-filter", Input).value.lower()
        options = self.query_one("#threads", OptionList)
        options.clear_options()
        for row in self.threads:
            prompt = str(row.get("initial_prompt") or "")
            identifier = row["thread_id"]
            label = (
                f"{'* ' if identifier == self.current_thread else ''}"
                f"{identifier[:10]}  {row.get('message_count', 0)} msgs  "
                f"{row.get('updated_at') or ''}  {prompt}"
            )
            if query in label.lower():
                options.add_option(Option(literal_text(label), id=identifier))

    @on(Input.Changed)
    def filtered(self) -> None:
        self._render_threads()

    @on(Checkbox.Changed)
    async def scope_changed(self) -> None:
        await self._load()

    @on(OptionList.OptionSelected)
    def selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id))

    async def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss(None)
