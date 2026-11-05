"""Client-side journal of one logical chat turn, for the turn resume protocol (v1).

The hosted service keeps no conversation content: when a chat socket closes it
cancels the turn and forgets it. Resuming is therefore the client's job. While
a turn runs, the client records what the user asked for, the assistant text
streamed so far and every tool call with its outcome. When the stream drops,
the next ``chat_start`` sends the ORIGINAL request again plus
``request.resume`` (built from this journal), and the server continues from
there instead of starting over.

The journal lives only on this device, in two places:

* a sidecar file under ``$COPPER_PILOT_CLI_HOME/.state/turns/`` that is
  rewritten while a turn runs, so a crash, kill or Ctrl-C still leaves it
  behind (LangGraph only checkpoints when the node returns), and
* the session checkpoint (state key ``copper_turn_journal``) when a turn ends
  unfinished, so ``copper-pilot -r`` and ``/resume`` can pick it up.

See ``e2e/perf/chat_resilience/PROTOCOL.md`` in the tech repo for the wire format.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from copper_pilot_cli.copper_config import atomic_write_text, paths

logger = logging.getLogger(__name__)

TURN_RESUME_CAPABILITY = "turn_resume_v1"
TURN_RESUME_ENV = "COPPER_PILOT_TURN_RESUME"
JOURNAL_VERSION = 1
MAX_TOOL_EVENTS = 200
# Subagents a turn can report on resume (the server caps spawns at 16).
MAX_SUBAGENTS = 32
MAX_TOOL_RESULT_BYTES = 4 * 1024
MAX_ARGUMENT_BYTES = 4 * 1024
MAX_ASSISTANT_TEXT_BYTES = 256 * 1024
# The server clips anything over 1 MB; staying under it keeps the newest
# tool results intact instead of letting the server cut them arbitrarily.
MAX_RESUME_PAYLOAD_BYTES = 900 * 1024

# Reasons accepted by the protocol (``request.resume.reason``).
RESUME_REASONS = frozenset(
    {
        "connection_lost",
        "connect_failed",
        "idle_timeout",
        "system_suspend",
        "server_restarting",
        "socket_rotation",
        "app_restart",
        "tool_result_undeliverable",
        "provider_busy",
        "manual",
    }
)

# Tool event statuses on the wire. ``pending`` is local only: a request seen
# without its result yet; it is sent as ``interrupted``.
COMPLETED = "completed"
FAILED = "failed"
NOT_RUN = "not_run"
INTERRUPTED = "interrupted"
PENDING = "pending"

STATUS_RUNNING = "running"
STATUS_UNFINISHED = "unfinished"

# Only a bare resume intent resumes the unfinished turn; a message with any
# extra instruction ("continue, but use 0603 resistors") starts a new turn so
# the instruction is not dropped. Compared after lowercasing, dropping a
# leading "/" and punctuation, and collapsing spaces.
_RESUME_INTENTS = frozenset({"continue", "resume", "continue where you left off"})
_NOT_WORD = re.compile(r"[^a-z0-9]+")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def turn_resume_enabled() -> bool:
    """Structured resume is on unless ``COPPER_PILOT_TURN_RESUME=0``.

    Turning it off falls back to the older stitched-transcript resume, for a
    hosted service that does not implement ``turn_resume_v1`` yet.
    """
    value = os.environ.get(TURN_RESUME_ENV, "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def is_resume_request(text: Any) -> bool:
    """Whether a user message is a bare request to continue the unfinished turn.

    Matches ``continue``, ``resume``, ``/resume`` and "continue where you left
    off", ignoring case and punctuation. Anything with more text is a new
    instruction and starts a new turn.
    """
    if not isinstance(text, str) or len(text) > 200:
        return False
    return " ".join(_NOT_WORD.split(text.lower())).strip() in _RESUME_INTENTS


def _clip_text(text: str, limit: int) -> str:
    encoded = text.encode()
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode(errors="ignore") + "\n[truncated]"


def _clip_tail(text: str, limit: int) -> str:
    encoded = text.encode()
    if len(encoded) <= limit:
        return text
    return "[earlier text omitted]\n" + encoded[-limit:].decode(errors="ignore")


def _clip_value(value: Any, limit: int) -> Any:
    """Clip long strings inside JSON-like arguments, keeping their shape."""
    if isinstance(value, str):
        return _clip_text(value, limit)
    if isinstance(value, dict):
        return {str(key): _clip_value(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_clip_value(item, limit) for item in value]
    return value


def _result_text(result: Any) -> str:
    if isinstance(result, dict):
        body = result.get("error") or result.get("result", result)
        if isinstance(body, str):
            text = body
        else:
            text = json.dumps(body, default=str)
        if isinstance(result.get("exit_code"), int) and result["exit_code"] != 0:
            text = f"[exit code {result['exit_code']}] {text}"
        return text
    if isinstance(result, str):
        return result
    return json.dumps(result, default=str)


def tool_result_failed(result: Any) -> bool:
    """A tool that ran but returned an error, a rejection or a non-zero exit."""
    return isinstance(result, dict) and (
        bool(result.get("error"))
        or result.get("rejected") is True
        or (isinstance(result.get("exit_code"), int) and result["exit_code"] != 0)
    )


@dataclass
class TurnJournal:
    """Everything needed to resume one logical turn, without the server's help."""

    query: str
    original_len: int
    conversation_id: str | None = None
    thread_id: str | None = None
    mode: str | None = None
    effort: str | None = None
    model: str | None = None
    root_turn_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    turn_id: str = ""
    previous_turn_id: str | None = None
    attempt: int = 0
    reason: str | None = None
    assistant_text: str = ""
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    subagents: list[dict[str, Any]] = field(default_factory=list)
    status: str = STATUS_RUNNING
    last_error: dict[str, Any] | None = None
    # Chat context protocol v1: the ``session_state`` snapshotted when the
    # turn started and the id of the last history message left out of
    # ``context`` (the compaction cut). Every attempt resends both unchanged.
    session_state: dict[str, Any] | None = None
    context_cut: str | None = None
    # What an interrupted turn knows that never reaches the checkpoint (the
    # node never returns): its latest todo list and context size, and facts
    # carried from earlier interrupted turns (see ``chat_context.journal_facts``).
    todos: dict[str, Any] | None = None
    context_usage: dict[str, Any] | None = None
    carried: dict[str, Any] | None = None
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    version: int = JOURNAL_VERSION

    def __post_init__(self) -> None:
        if not self.turn_id:
            self.turn_id = self.root_turn_id

    # ------------------------------------------------------------ recording

    def record_text(self, text: str) -> None:
        self.assistant_text = _clip_tail(self.assistant_text + text, MAX_ASSISTANT_TEXT_BYTES)
        self.updated_at = time.time()

    def drop_trailing_text(self, text: str) -> None:
        """Forget hosted error copy echoed as response text before a failed final."""
        stripped = text.strip()
        if stripped and self.assistant_text.rstrip().endswith(stripped):
            self.assistant_text = self.assistant_text.rstrip()[: -len(stripped)]

    def record_tool_request(self, tool_call_id: str, tool_name: str, arguments: Any) -> None:
        if any(event["tool_call_id"] == tool_call_id for event in self.tool_events):
            return
        self.tool_events.append(
            {
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "arguments": _clip_value(
                    arguments if isinstance(arguments, dict) else {}, MAX_ARGUMENT_BYTES
                ),
                "status": PENDING,
            }
        )
        del self.tool_events[:-MAX_TOOL_EVENTS]
        self.updated_at = time.time()

    def record_tool_result(self, tool_call_id: str, tool_name: str, result: Any) -> None:
        event = next(
            (item for item in self.tool_events if item["tool_call_id"] == tool_call_id), None
        )
        if event is None:
            self.record_tool_request(tool_call_id, tool_name, {})
            event = self.tool_events[-1]
        event["status"] = FAILED if tool_result_failed(result) else COMPLETED
        event["result"] = _clip_text(_result_text(result), MAX_TOOL_RESULT_BYTES)
        self.updated_at = time.time()

    def record_subagent(self, event: dict[str, Any]) -> None:
        """Latest status of one subagent chip; the workspace manifest stays
        the authoritative record."""
        unit = event.get("unit")
        status = str(event.get("state") or event.get("status") or "")
        if event.get("kind") != "subagent" or not unit or status in ("", "keepalive"):
            return
        entry = {
            "unit": str(unit),
            "status": status,
            "scratch_path": str(event["scratch_path"]) if event.get("scratch_path") else None,
        }
        for index, item in enumerate(self.subagents):
            if item.get("unit") == entry["unit"]:
                self.subagents[index] = entry
                break
        else:
            if len(self.subagents) < MAX_SUBAGENTS:
                self.subagents.append(entry)
        self.updated_at = time.time()

    def settle_pending(self, status: str = INTERRUPTED) -> None:
        """Mark requests whose outcome never arrived (process killed, tool cancelled)."""
        for event in self.tool_events:
            if event["status"] == PENDING:
                event["status"] = status

    def tool_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for event in self.tool_events:
            counts[event["status"]] = counts.get(event["status"], 0) + 1
        return counts

    # --------------------------------------------------------------- resume

    def begin_resume(self, reason: str) -> None:
        """Start the next attempt: a new turn id that points back at the dropped one."""
        self.previous_turn_id = self.turn_id
        self.turn_id = str(uuid.uuid4())
        self.attempt += 1
        self.reason = reason if reason in RESUME_REASONS else "connection_lost"
        self.status = STATUS_RUNNING
        self.updated_at = time.time()

    def resume_payload(self) -> dict[str, Any]:
        """The ``request.resume`` object for the current attempt, bounded in size."""
        events = []
        for event in self.tool_events:
            item = {
                key: event[key]
                for key in ("tool_call_id", "tool_name", "arguments", "status", "result")
                if key in event
            }
            if item["status"] == PENDING:
                item["status"] = INTERRUPTED
            events.append(item)
        payload: dict[str, Any] = {
            "root_turn_id": self.root_turn_id,
            "previous_turn_id": self.previous_turn_id or self.root_turn_id,
            "attempt": self.attempt,
            "reason": self.reason or "connection_lost",
            "assistant_text": self.assistant_text,
            "tool_events": events[-MAX_TOOL_EVENTS:],
        }
        if self.subagents:
            payload["subagents"] = list(self.subagents)
        # Oldest results go first; the newest are what the model needs most.
        index = 0
        while _size(payload) > MAX_RESUME_PAYLOAD_BYTES and index < len(payload["tool_events"]):
            event = payload["tool_events"][index]
            if event.get("result"):
                event["result"] = "[omitted to fit the resume payload]"
            event["arguments"] = _clip_value(event.get("arguments", {}), 256)
            index += 1
        if _size(payload) > MAX_RESUME_PAYLOAD_BYTES:
            payload["assistant_text"] = _clip_tail(self.assistant_text, 64 * 1024)
        return payload

    # ---------------------------------------------------------- persistence

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> TurnJournal | None:
        if not isinstance(data, dict) or data.get("version") != JOURNAL_VERSION:
            return None
        try:
            known = {key: data[key] for key in cls.__dataclass_fields__ if key in data}
            journal = cls(**known)
        except TypeError:
            return None
        if not isinstance(journal.query, str) or not isinstance(journal.original_len, int):
            return None
        journal.tool_events = [
            event
            for event in journal.tool_events
            if isinstance(event, dict) and "tool_call_id" in event and "status" in event
        ]
        journal.subagents = [
            item for item in journal.subagents if isinstance(item, dict) and item.get("unit")
        ]
        return journal


def _size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), default=str).encode())


class JournalStore:
    """Crash-safe sidecar copy of the running turn's journal, one file per thread."""

    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory

    @property
    def directory(self) -> Path:
        return self._directory or (paths().state / "turns")

    def path(self, thread_id: str) -> Path:
        return self.directory / f"{_SAFE_NAME.sub('_', thread_id)}.json"

    def save(self, journal: TurnJournal) -> None:
        if not journal.thread_id:
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                self.directory.chmod(0o700)
            atomic_write_text(
                self.path(journal.thread_id),
                json.dumps(journal.to_dict(), separators=(",", ":"), default=str),
                mode=0o600,
            )
        except OSError as exc:
            logger.warning("Could not save the turn journal (%s)", type(exc).__name__)

    def load(self, thread_id: str) -> TurnJournal | None:
        try:
            raw = self.path(thread_id).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("Could not read the turn journal (%s)", type(exc).__name__)
            return None
        try:
            return TurnJournal.from_dict(json.loads(raw))
        except json.JSONDecodeError:
            return None

    def delete(self, thread_id: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path(thread_id).unlink()


__all__ = [
    "COMPLETED",
    "FAILED",
    "INTERRUPTED",
    "JournalStore",
    "MAX_TOOL_EVENTS",
    "MAX_TOOL_RESULT_BYTES",
    "NOT_RUN",
    "PENDING",
    "RESUME_REASONS",
    "STATUS_RUNNING",
    "STATUS_UNFINISHED",
    "TURN_RESUME_CAPABILITY",
    "TurnJournal",
    "is_resume_request",
    "tool_result_failed",
    "turn_resume_enabled",
]
