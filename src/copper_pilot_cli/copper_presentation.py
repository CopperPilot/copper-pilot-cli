"""Thin dcode-compatible presentation seam for hosted Copper turns."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.widgets import Static

from copper_pilot_cli._version import __version__

if TYPE_CHECKING:
    from textual.geometry import Size
    from textual.layout import DockArrangeResult

SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


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


def _home_path(path: str | Path) -> str:
    candidate = Path(path)
    try:
        home = Path.home()
        if candidate == home:
            return "~"
        if candidate.is_relative_to(home):
            return "~/" + candidate.relative_to(home).as_posix()
    except (RuntimeError, ValueError):
        pass
    return str(candidate)


def _git_branch(workspace: Path) -> str:
    """Read the current branch without launching a subprocess."""
    current = workspace.resolve()
    for root in (current, *current.parents):
        head = root / ".git" / "HEAD"
        try:
            value = head.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        prefix = "ref: refs/heads/"
        return value.removeprefix(prefix) if value.startswith(prefix) else value[:8]
    return ""


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
    """Compact dcode-style product and version banner."""

    DEFAULT_CSS = """
    WelcomeBanner {
        height: auto;
        border: solid $primary;
        padding: 0 2;
        margin-bottom: 1;
    }
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        super().__init__(self._content())

    def _content(self) -> Content:
        return Content.assemble(
            ("◆ ", "bold $primary"),
            ("CopperPilot", "bold"),
            (f"  v{__version__}", "dim"),
            "\n\n",
            ("directory: ", "dim"),
            (_home_path(self.workspace), "$primary"),
        )

    def update_workspace(self, workspace: Path) -> None:
        self.workspace = workspace
        self.update(self._content())


class LoadingWidget(Static):
    """Animated dcode-style turn activity that pauses during approval."""

    DEFAULT_CSS = """
    LoadingWidget { height: auto; padding: 0 1; margin-bottom: 1; }
    LoadingWidget .loading-container { height: auto; width: 100%; }
    LoadingWidget .loading-spinner,
    LoadingWidget .loading-status { width: auto; color: $primary; }
    LoadingWidget .loading-hint { width: auto; color: $text-muted; margin-left: 1; }
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
        with Horizontal(classes="loading-container"):
            yield Static(SPINNER_FRAMES[0], classes="loading-spinner")
            yield Static(f" {self.status_text}... ", classes="loading-status")
            yield Static("(0s, esc to interrupt)", classes="loading-hint")

    def on_mount(self) -> None:
        self.timer = self.set_interval(0.1, self._tick)

    def on_unmount(self) -> None:
        self.stop()

    def _tick(self) -> None:
        if self.paused_at is not None:
            return
        self.position = (self.position + 1) % len(SPINNER_FRAMES)
        self.query_one(".loading-spinner", Static).update(SPINNER_FRAMES[self.position])
        elapsed = int(monotonic() - self.started_at - self.paused_total)
        self.query_one(".loading-hint", Static).update(f"({elapsed}s, esc to interrupt)")

    def set_status(self, status: str) -> None:
        self.status_text = status
        if self.is_mounted:
            self.query_one(".loading-status", Static).update(f" {status}... ")

    def pause(self, status: str = "Awaiting decision") -> None:
        if self.paused_at is None:
            self.paused_at = monotonic()
        self.set_status(status)
        if self.is_mounted:
            elapsed = int(self.paused_at - self.started_at - self.paused_total)
            self.query_one(".loading-spinner", Static).update("Ⅱ")
            self.query_one(".loading-hint", Static).update(f"(paused at {elapsed}s)")

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


class StatusBar(Vertical):
    """Compact dcode-style status bar for values Copper can report truthfully."""

    DEFAULT_CSS = """
    StatusBar { height: 1; dock: bottom; background: $background; }
    StatusBar > Horizontal { height: 1; width: 1fr; }
    StatusBar .status-approval { width: auto; padding: 0 1; margin-right: 1; }
    StatusBar .status-approval.manual { background: $warning; color: $background; }
    StatusBar .status-approval.auto { background: $success; color: $background; }
    StatusBar .status-approval.yolo { background: $error; color: white; text-style: bold; }
    StatusBar .status-cwd { width: auto; max-width: 55%; color: $text-muted; }
    StatusBar .status-branch { width: auto; color: $text-muted; padding-left: 1; }
    StatusBar .status-message { width: 1fr; color: $text-muted; }
    """

    def __init__(self, workspace: Path, approval_mode: str = "manual") -> None:
        super().__init__(id="status-bar")
        self.workspace = workspace
        self.approval_mode = approval_mode

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Static(self.approval_mode, classes=f"status-approval {self.approval_mode}")
            yield Static(_home_path(self.workspace), classes="status-cwd")
            yield Static(_git_branch(self.workspace), classes="status-branch")
            yield Static("", classes="status-message")

    def on_resize(self, event: events.Resize) -> None:
        self.query_one(".status-cwd", Static).display = event.size.width >= 70

    def set_workspace(self, workspace: Path) -> None:
        self.workspace = workspace
        if self.is_mounted:
            self.query_one(".status-cwd", Static).update(_home_path(workspace))
            self.query_one(".status-branch", Static).update(_git_branch(workspace))

    def set_approval_mode(self, mode: str) -> None:
        self.approval_mode = mode if mode in {"manual", "auto", "yolo"} else "manual"
        if not self.is_mounted:
            return
        widget = self.query_one(".status-approval", Static)
        widget.remove_class("manual", "auto", "yolo")
        widget.add_class(self.approval_mode)
        widget.update("YOLO" if self.approval_mode == "yolo" else self.approval_mode)

    def set_status(self, status: str) -> None:
        if self.is_mounted:
            self.query_one(".status-message", Static).update(Content(status))
