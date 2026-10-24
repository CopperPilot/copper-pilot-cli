"""Thin dcode-compatible presentation seam for hosted CopperPilot turns."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any

from textual import events
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.content import Content
from textual.widget import Widget
from textual.widgets import Static

from copper_pilot_cli.copper_chrome import (
    help_content,
    status_content,
    welcome_content,
    working_content,
)
from copper_pilot_cli.copper_theme import ascii_mode, color_mode_for_app

if TYPE_CHECKING:
    from textual.geometry import Size
    from textual.layout import DockArrangeResult


class ChatScroll(VerticalScroll):
    """Dcode-compatible transcript anchoring that respects manual scrolling."""

    FOCUS_ON_CLICK = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._follow_bottom_when_scrollable = False

    def anchor(self, anchor: bool = True) -> None:
        """Follow output only after it overflows, keeping short chats top-aligned."""
        self._follow_bottom_when_scrollable = anchor
        if not anchor:
            super().anchor(False)
            return
        self._anchor_released = False
        if self.max_scroll_y > 0:
            super().anchor(True)
            return
        super().anchor(False)
        self.scroll_y = 0
        self.scroll_target_y = 0

    def release_anchor(self) -> None:
        """Stop following as soon as the user moves away from the bottom."""
        self._follow_bottom_when_scrollable = False
        super().release_anchor()

    def arrange(self, size: Size, optimal: bool = False) -> DockArrangeResult:
        """Engage the armed anchor once newly streamed content begins overflowing."""
        result = super().arrange(size, optimal=optimal)
        if not self._follow_bottom_when_scrollable or self._anchor_released:
            return result
        viewport_height = self.container_size.height - self.scrollbar_size_horizontal
        if result.spatial_map.total_region.bottom > viewport_height:
            self._anchored = True
        else:
            self._anchored = False
            self.set_reactive(VerticalScroll.scroll_y, 0.0)
            self.set_reactive(VerticalScroll.scroll_target_y, 0.0)
        return result


def _git_branch(workspace: Path) -> str:
    """Read the current branch without launching a subprocess.

    A submodule ``.git`` is a file (``gitdir: ...``), not a directory. Follow
    that pointer instead of walking up into the parent repository.
    """
    current = workspace.resolve()
    for root in (current, *current.parents):
        git_path = root / ".git"
        if git_path.is_file():
            head = _gitdir_head(root, git_path)
            if head is None:
                continue
        elif git_path.is_dir():
            head = git_path / "HEAD"
        else:
            continue
        try:
            value = head.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        if not value:
            return ""
        prefix = "ref: refs/heads/"
        return value.removeprefix(prefix) if value.startswith(prefix) else value[:8]
    return ""


def _gitdir_head(root: Path, git_path: Path) -> Path | None:
    """Resolve ``HEAD`` from a ``gitdir:`` file, if that is what ``.git`` is."""
    try:
        pointer = git_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    marker = "gitdir:"
    if not pointer.lower().startswith(marker):
        return None
    gitdir = Path(pointer.split(":", 1)[1].strip())
    if not gitdir.is_absolute():
        gitdir = (root / gitdir).resolve()
    return gitdir / "HEAD"


class TurnPhase(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    AWAITING_LOCAL_INPUT = "awaiting_local_input"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class TurnEvent:
    """Transport-neutral event consumed by the Textual presenter."""

    kind: str
    data: Any = None
    title: str | None = None
    turn_id: str | None = None
    sequence: int | None = None
    resume_attempt: int | None = None


@dataclass(slots=True)
class TurnState:
    phase: TurnPhase = TurnPhase.IDLE
    status: str = ""
    tools: dict[str, str] = field(default_factory=dict)
    input_tokens: int | None = None
    output_tokens: int | None = None
    context_tokens: int | None = None
    context_limit: int | None = None
    last_sequence: int | None = None
    turn_id: str | None = None
    resume_attempt: int | None = None

    def apply(self, event: TurnEvent) -> bool:
        """Apply an event, returning false for a duplicate/older sequence."""
        if event.turn_id is not None and event.turn_id != self.turn_id:
            self.turn_id = event.turn_id
            self.resume_attempt = event.resume_attempt
            self.last_sequence = None
        elif event.resume_attempt is not None:
            if self.resume_attempt is not None and event.resume_attempt < self.resume_attempt:
                return False
            if self.resume_attempt is None or event.resume_attempt > self.resume_attempt:
                self.resume_attempt = event.resume_attempt
                self.last_sequence = None
        if event.sequence is not None:
            if self.last_sequence is not None and event.sequence <= self.last_sequence:
                return False
            self.last_sequence = event.sequence
        if event.kind == "turn_started":
            self.phase = TurnPhase.RUNNING
        elif event.kind == "status":
            self.status = str(event.data or "")
        elif event.kind == "tool_requested" and isinstance(event.data, dict):
            self.tools[str(event.data.get("tool_call_id") or "")] = "running"
        elif event.kind == "tool_phase" and isinstance(event.data, dict):
            self.tools[str(event.data.get("tool_call_id") or "")] = str(
                event.data.get("phase") or "running"
            )
        elif event.kind == "usage" and isinstance(event.data, dict):
            aliases = {
                "input_tokens": ("input_tokens", "tokens_input"),
                "output_tokens": ("output_tokens", "tokens_output"),
                "context_tokens": ("context_tokens",),
                "context_limit": ("context_limit",),
            }
            for key, candidates in aliases.items():
                value = next(
                    (event.data[name] for name in candidates if name in event.data),
                    None,
                )
                if isinstance(value, int) and value >= 0:
                    setattr(self, key, value)
        elif event.kind == "turn_completed":
            self.phase = TurnPhase.COMPLETED
        elif event.kind == "turn_failed":
            self.phase = TurnPhase.FAILED
        elif event.kind == "turn_cancelled":
            self.phase = TurnPhase.CANCELLED
        return True


class WelcomeBanner(Static):
    """Flat wordmark, repo line, and the quick-start list."""

    DEFAULT_CSS = """
    WelcomeBanner {
        height: auto;
        margin-bottom: 1;
    }
    """

    def __init__(self, workspace: Path, *, model: str = "balanced") -> None:
        del model
        self.workspace = workspace
        self._quick_start = True
        super().__init__("")

    def on_mount(self) -> None:
        self._redraw()

    def on_resize(self, event: events.Resize) -> None:
        self._redraw(event.size.width)

    def update_workspace(self, workspace: Path) -> None:
        self.workspace = workspace
        self._redraw()

    def set_model(self, model: str) -> None:
        """Effort lives on the status line, not in the header."""
        del model

    def hide_quick_start(self) -> None:
        if not self._quick_start:
            return
        self._quick_start = False
        self._redraw()

    def _redraw(self, width: int | None = None) -> None:
        if width is None:
            width = self.size.width or 80
        mode = color_mode_for_app(self.app) if self.app else None
        self.update(
            welcome_content(
                width,
                directory=self.workspace.name,
                branch=_git_branch(self.workspace) or None,
                show_quick_start=self._quick_start,
                mode=mode,
                ascii=ascii_mode(),
            )
        )


class LoadingWidget(Widget):
    """Animated dcode-style turn activity that pauses during approval."""

    DEFAULT_CSS = """
    LoadingWidget { height: 1; padding: 0 1; margin-bottom: 1; }
    LoadingWidget .loading-line { width: 1fr; height: 1; }
    """

    def __init__(self, status: str = "Thinking") -> None:
        super().__init__()
        self.status_text = status
        self.started_at = monotonic()
        self.paused_at: float | None = None
        self.paused_total = 0.0
        self.position = 0
        self.timer = None

    def compose(self) -> ComposeResult:
        yield Static(self._line(0), classes="loading-line")

    def on_mount(self) -> None:
        self.timer = self.set_interval(0.3, self._tick)
        self._refresh_line(0)

    def on_resize(self, event: events.Resize) -> None:
        del event
        elapsed = int(monotonic() - self.started_at - self.paused_total)
        self._refresh_line(elapsed)

    def on_unmount(self) -> None:
        self.stop()

    def _color_mode(self):
        """Use the app flag. Compose runs before ``is_mounted``, after NO_COLOR is popped."""
        try:
            return color_mode_for_app(self.app)
        except RuntimeError:
            return None

    def _line_width(self) -> int | None:
        if not self.is_mounted or not self.size.width:
            return None
        return max(1, self.size.width - 2)

    def _line(self, elapsed: int) -> Content:
        return working_content(
            self.position,
            self.status_text,
            elapsed,
            width=self._line_width(),
            mode=self._color_mode(),
            ascii=ascii_mode(),
        )

    def _refresh_line(self, elapsed: int) -> None:
        line = self.query(".loading-line")
        if line:
            first = line.first()
            if isinstance(first, Static):
                first.update(self._line(elapsed))

    def _tick(self) -> None:
        if self.paused_at is not None:
            return
        self.position = (self.position + 1) % 3
        elapsed = int(monotonic() - self.started_at - self.paused_total)
        self._refresh_line(elapsed)

    def set_status(self, status: str) -> None:
        self.status_text = status
        if self.is_mounted:
            elapsed = int(monotonic() - self.started_at - self.paused_total)
            self._refresh_line(elapsed)

    def pause(self, status: str = "Awaiting decision") -> None:
        if self.paused_at is None:
            self.paused_at = monotonic()
        self.set_status(status)

    def resume(self) -> None:
        if self.paused_at is None:
            return
        self.paused_total += monotonic() - self.paused_at
        self.paused_at = None
        self.set_status("Thinking")

    def stop(self) -> None:
        if self.timer is not None:
            self.timer.stop()
            self.timer = None


class StatusBar(Static):
    """Single status line under the prompt."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        dock: bottom;
        background: $bg;
        padding: 0 1;
    }
    """

    def __init__(
        self,
        workspace: Path,
        approval_mode: str = "manual",
        *,
        mode: str = "agent",
        effort: str = "balanced",
    ) -> None:
        super().__init__("", id="status-bar")
        self.workspace = workspace
        self.approval_mode = approval_mode or "manual"
        self.session_mode = mode or "agent"
        self.session_effort = effort or "balanced"
        self._flash: str | None = None
        self._flash_until = 0.0
        self._flash_timer = None

    def on_mount(self) -> None:
        self._redraw()

    def on_resize(self, event: events.Resize) -> None:
        self._redraw(event.size.width)

    def set_workspace(self, workspace: Path) -> None:
        self.workspace = workspace
        self._redraw()

    def set_approval_mode(self, mode: str) -> None:
        """Approval is changed with ``/approval``, not this line."""
        self.approval_mode = mode or "manual"

    def set_session(self, *, mode: str, effort: str) -> None:
        self.session_mode = mode or "agent"
        self.session_effort = effort or "balanced"
        self._redraw()

    def set_context_percent(self, percent: int | None) -> None:
        """Context percentage is not shown; the CLI only has it on some turns."""
        del percent

    def set_status(self, status: str) -> None:
        """Activity text belongs on the working line, not this footer."""
        del status

    def flash(self, kind: str) -> None:
        """Replace one key hint with that option's description for two seconds."""
        self._flash = kind
        self._flash_until = monotonic() + 2
        if self._flash_timer is not None:
            self._flash_timer.stop()
        self._flash_timer = self.set_timer(2, self._clear_flash)
        self._redraw()

    def _clear_flash(self) -> None:
        self._flash = None
        self._flash_timer = None
        self._redraw()

    def _redraw(self, width: int | None = None) -> None:
        if not self.is_mounted:
            return
        if width is None:
            width = self.size.width or 80
        content_width = max(1, width - 2)
        mode = color_mode_for_app(self.app)
        flash = self._flash if monotonic() < self._flash_until else None
        self.update(
            status_content(
                content_width,
                chat_mode=self.session_mode,
                effort=self.session_effort,
                flash=flash,
                mode=mode,
                ascii=ascii_mode(),
            )
        )


class HelpCard(Static):
    """Two-column list of the commands and keys that exist."""

    DEFAULT_CSS = """
    HelpCard { height: auto; margin: 0 0 1 0; }
    """

    def __init__(self, *, include_model: bool = False) -> None:
        super().__init__("")
        self.include_model = include_model

    def on_mount(self) -> None:
        self._redraw()

    def on_resize(self, event: events.Resize) -> None:
        self._redraw(event.size.width)

    def _redraw(self, width: int | None = None) -> None:
        if width is None:
            width = self.size.width or 80
        mode = color_mode_for_app(self.app) if self.app else None
        self.update(
            help_content(
                width,
                include_model=self.include_model,
                mode=mode,
                ascii=ascii_mode(),
            )
        )
