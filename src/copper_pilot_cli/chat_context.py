"""Chat context protocol v1 on the client: pinned session state, compaction, search.

The hosted service keeps nothing between turns, so this device owns the
durable parts of a chat's context (see ``e2e/perf/chat_context/PROTOCOL.md``
in the tech repo):

* ``session_state``: what the model must not lose however long the chat gets.
  It holds the compaction summary and the user messages it covers, every
  answered agent question, the latest todo list and the plan the chat works
  on. It is snapshotted once per logical turn (the turn journal keeps it) and
  sent unchanged on every attempt of that turn;
* compaction records in the checkpoint (state key ``copper_compaction``, with
  superseded records kept for audit). After a compaction the request
  ``context`` holds only the messages after the cut;
* the last measured context size (``copper_context_usage``), used for the
  meters and the pre-send auto-compaction estimate;
* the ``search_chat_history`` tool, answered from the full local transcript,
  including messages that were compacted away.

The user's transcript is never rewritten; compaction only changes what the
model is sent.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from copper_pilot_cli.copper_models import (
    DEFAULT_CONTEXT_POLICY,
    ChatModelCatalog,
    ContextPolicy,
)
from copper_pilot_cli.copper_protocol import (
    MAX_CONTEXT_MESSAGE_BYTES,
    PlanContext,
)
from copper_pilot_cli.copper_status_events import COPPER_CONTEXT_KEY

logger = logging.getLogger(__name__)

SESSION_STATE_VERSION = 1
# Checkpoint state keys.
COMPACTION_KEY = "copper_compaction"
SUPERSEDED_KEY = "copper_compaction_superseded"
USAGE_KEY = "copper_context_usage"
# Facts from turns that were interrupted (Ctrl-C, SIGINT, a killed process).
# Such a turn never returns from the graph node, so its messages and events
# never reach the checkpoint; only its turn journal survives. The next turn
# lifts the facts out of that journal (``journal_facts``), carries them in
# its own journal until it completes, then stores them under this key.
FACTS_KEY = "copper_interrupted_facts"
# Graph event for compaction progress (``data.stage``: started, completed,
# failed). Manual compactions append a ``completed`` one to ``copper_events``
# so a reopened chat shows where it was compacted.
COMPACTION_EVENT = "copper.compaction"
CONTEXT_USAGE_KIND = "context_usage"
CONTEXT_COMPACTED_KIND = "context_compacted"
ASK_TOOL = "ask_user_question"

# Server limits (PROTOCOL.md section 4). The client clips to them so the
# server never has to.
MAX_ASK_ANSWERS = 50
MAX_ASK_QUESTION_BYTES = 1024
MAX_ASK_ANSWER_BYTES = 3 * 1024
MAX_TODOS = 200
MAX_PLAN_BODY_BYTES = 32 * 1024
MAX_SUMMARY_BYTES = 128 * 1024
MAX_EARLIER_MESSAGE_BYTES = 16 * 1024
MAX_EARLIER_TOTAL_BYTES = 96 * 1024
# Compaction request digests (PROTOCOL.md section 5).
MAX_TOOL_DIGESTS = 32
MAX_DIGEST_EXCERPT_BYTES = 2 * 1024
MAX_DIGEST_ARGS_CHARS = 200
TOOL_ACTIVITY_TEXT = "(tool activity)"
# search_chat_history (PROTOCOL.md section 7).
SEARCH_SNIPPET_CHARS = 600
DEFAULT_SEARCH_RESULTS = 10
MAX_SEARCH_RESULTS = 20

_PLAN_PATH = re.compile(r"(?:^|[\\/])\.copperpilot[\\/]plans[\\/][^\\/]+\.plan\.md$", re.IGNORECASE)
_PLAN_WRITERS = frozenset({"write_file", "edit_file", "write", "edit"})
_PATH_KEYS = ("file_path", "path")
_DIGEST_ARG_KEYS = ("file_path", "path", "command", "pattern", "query", "question", "description")
_TODO_STATUSES = {
    "done": "done",
    "completed": "done",
    "complete": "done",
    "in_progress": "in_progress",
    "running": "in_progress",
}
_PLAN_STATUSES = {"open": "draft", "running": "running", "done": "done"}


class NothingToCompact(RuntimeError):
    """The chat has too little history after the last cut to summarize."""


class CompactionFailed(RuntimeError):
    """The compaction reply was unusable; history stays as it was."""


@dataclass(frozen=True, slots=True)
class CompactionResult:
    """A stored compaction: the record and the checkpoint updates that store it."""

    record: dict[str, Any]
    updates: dict[str, Any]


# --------------------------------------------------------------------- tokens


def estimate_tokens(text: str) -> int:
    """The protocol's estimate: ``ceil(chars / 4)``."""
    return math.ceil(len(text) / 4) if text else 0


def format_tokens(value: int | float | None) -> str:
    """``182K``, ``1M``, ``1.2M``; the meters' compact token counts."""
    if not isinstance(value, (int, float)) or value < 0:
        return "?"
    if value >= 1_000_000:
        millions = value / 1_048_576 if value % 1_048_576 == 0 else value / 1_000_000
        return f"{millions:.1f}".rstrip("0").rstrip(".") + "M"
    if value >= 1_000:
        return f"{round(value / 1_000)}K"
    return str(int(value))


def context_percent(used: int, limit: int) -> int:
    return min(100, max(0, round(100 * used / limit))) if limit > 0 else 0


def meter_text(used: int, limit: int) -> str:
    """The TUI status-bar meter: ``ctx 45% · 470K/1M``."""
    return f"ctx {context_percent(used, limit)}% · {format_tokens(used)}/{format_tokens(limit)}"


def meter_level(used: int, limit: int, policy: ContextPolicy = DEFAULT_CONTEXT_POLICY) -> str:
    """``normal`` below the warn fraction, then ``warn`` (to 90%), ``high`` (95%), ``critical``."""
    if limit <= 0:
        return "normal"
    fraction = used / limit
    if fraction > 0.95:
        return "critical"
    if fraction >= 0.9:
        return "high"
    if fraction >= policy.warn_fraction:
        return "warn"
    return "normal"


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value >= 0 else None


def _clip_bytes(text: str, limit: int) -> tuple[str, bool]:
    encoded = text.encode()
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode(errors="ignore"), True


def _text(content: Any) -> str:
    return content if isinstance(content, str) else json.dumps(content, default=str)


# ---------------------------------------------------------------------- usage


def parse_context_usage(value: Any) -> dict[str, Any] | None:
    """A ``context_usage`` block (final frame or status event), else ``None``."""
    if not isinstance(value, dict):
        return None
    tokens = _int(value.get("context_tokens"))
    if tokens is None:
        return None
    usage: dict[str, Any] = {"context_tokens": tokens}
    for key in ("context_limit", "auto_compact_at", "peak_context_tokens", "next_turn_tokens"):
        number = _int(value.get(key))
        if number:
            usage[key] = number
    if isinstance(value.get("model"), str) and value["model"]:
        usage["model"] = value["model"]
    if isinstance(value.get("compacted_in_turn"), bool):
        usage["compacted_in_turn"] = value["compacted_in_turn"]
    return usage


def usage_record(
    usage: dict[str, Any], *, measured_at: int, source: str = "turn"
) -> dict[str, Any]:
    """The stored measurement; ``measured_at`` is the message count it already covers."""
    return {**usage, "measured_at": measured_at, "source": source, "recorded_at": time.time()}


def _valid_usage(usage: Any) -> dict[str, Any] | None:
    return (
        usage if isinstance(usage, dict) and _int(usage.get("context_tokens")) is not None else None
    )


def stored_usage(values: dict[str, Any] | None) -> dict[str, Any] | None:
    """The newest measurement: the checkpoint's, or an interrupted turn's if later."""
    values = values or {}
    usage = _valid_usage(values.get(USAGE_KEY))
    facts = values.get(FACTS_KEY)
    later = _valid_usage(facts.get("context_usage")) if isinstance(facts, dict) else None
    if later is not None and (
        usage is None or _ts(later.get("recorded_at")) > _ts(usage.get("recorded_at"))
    ):
        return later
    return usage


def _ts(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def context_estimate(values: dict[str, Any] | None) -> int | None:
    """Pre-send estimate: the last measured size plus ``chars / 4`` of what is new.

    New means every human and assistant message the measurement did not
    cover, which includes the message about to be sent. ``None`` without a
    measurement (an older chat or a server without context usage).
    """
    usage = stored_usage(values)
    if usage is None:
        return None
    messages = list((values or {}).get("messages") or [])
    start = _int(usage.get("measured_at")) or 0
    added = sum(estimate_tokens(text) for _role, text in _prose(messages[start:]))
    # ``next_turn_tokens`` (when the server sends it) is what the next request
    # would cost without the turn's tool results, a better base than the size
    # of the turn's last model call.
    base = _int(usage.get("next_turn_tokens")) or int(usage["context_tokens"])
    return base + added


def auto_compact_threshold(
    values: dict[str, Any] | None,
    catalog: ChatModelCatalog | None,
    effort: Any,
    model: Any,
) -> int | None:
    """When to auto-compact: the server's last ``auto_compact_at``, else the catalog's."""
    usage = stored_usage(values)
    if usage is not None and _int(usage.get("auto_compact_at")):
        return int(usage["auto_compact_at"])
    if catalog is None:
        return None
    return catalog.context_policy.auto_compact_at(catalog.context_window(effort, model))


def context_limit(
    values: dict[str, Any] | None, catalog: ChatModelCatalog | None, effort: Any, model: Any
) -> int | None:
    """The chat model's window: the last measurement's, else the catalog's."""
    usage = stored_usage(values)
    if usage is not None and _int(usage.get("context_limit")):
        return int(usage["context_limit"])
    return catalog.context_window(effort, model) if catalog is not None else None


def should_auto_compact(estimate: int | None, threshold: int | None, *, unfinished: bool) -> bool:
    """Compact before sending when the estimate reaches the threshold, never mid-turn."""
    if unfinished or estimate is None or not threshold:
        return False
    return estimate >= threshold


# ----------------------------------------------------------------- compaction


def current_compaction(values: dict[str, Any] | None) -> dict[str, Any] | None:
    record = (values or {}).get(COMPACTION_KEY)
    return record if isinstance(record, dict) and record.get("summary") else None


def context_cut(values: dict[str, Any] | None) -> str | None:
    """Id of the last message the current compaction summarized, if any."""
    record = current_compaction(values)
    cut = record.get("last_message_id") if record else None
    return cut if isinstance(cut, str) and cut else None


def compaction_record(
    compaction: Any, *, last_message_id: str | None, trigger: str
) -> dict[str, Any] | None:
    """Normalize a server compaction into the stored record, else ``None``.

    An empty summary is never stored: history is never replaced with nothing.
    """
    if not isinstance(compaction, dict):
        return None
    summary = compaction.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None
    summary, _clipped = _clip_bytes(summary, MAX_SUMMARY_BYTES)
    record: dict[str, Any] = {
        "id": str(compaction.get("id") or f"cmp_local_{int(time.time() * 1000)}"),
        "summary": summary,
        "created_at": str(compaction.get("created_at") or ""),
        "trigger": str(compaction.get("trigger") or trigger),
        "model": str(compaction.get("model") or ""),
    }
    for key in ("pre_tokens", "post_tokens", "messages_summarized"):
        number = _int(compaction.get(key))
        if number is not None:
            record[key] = number
    record["last_message_id"] = last_message_id
    record["stored_at"] = time.time()
    return record


def compaction_updates(values: dict[str, Any] | None, record: dict[str, Any]) -> dict[str, Any]:
    """Checkpoint updates that make ``record`` current and supersede the previous one."""
    previous = current_compaction(values)
    superseded = [
        item for item in ((values or {}).get(SUPERSEDED_KEY) or []) if isinstance(item, dict)
    ]
    if previous is not None:
        superseded.append({**previous, "superseded_at": time.time()})
    return {COMPACTION_KEY: record, SUPERSEDED_KEY: superseded}


def session_compaction(record: dict[str, Any]) -> dict[str, Any]:
    """The ``session_state.compaction`` object (local bookkeeping stripped)."""
    return {
        key: record[key]
        for key in (
            "id",
            "summary",
            "created_at",
            "trigger",
            "pre_tokens",
            "post_tokens",
            "model",
            "messages_summarized",
        )
        if key in record and record[key] not in (None, "")
    }


def compaction_notice(record: dict[str, Any] | None) -> str:
    """``Context compacted · 182K → 31K tokens``."""
    record = record or {}
    pre, post = record.get("pre_tokens"), record.get("post_tokens")
    if isinstance(pre, int) and isinstance(post, int):
        return f"Context compacted · {format_tokens(pre)} → {format_tokens(post)} tokens"
    return "Context compacted"


def compaction_event(stage: str, *, trigger: str, **data: Any) -> dict[str, Any]:
    """The ``copper.compaction`` graph event every front-end renders."""
    record = data.get("record")
    if stage == "started":
        title = "Compacting context…"
    elif stage == "completed":
        title = compaction_notice(record if isinstance(record, dict) else None)
    else:
        title = str(data.get("message") or "Context compaction failed; sending the chat unchanged.")
    return {
        "type": COMPACTION_EVENT,
        "data": {"stage": stage, "trigger": trigger, **data},
        "title": title,
    }


# -------------------------------------------------------------- transcript


def after_cut(messages: Sequence[Any], cut: str | None) -> list[Any]:
    """Messages after the compaction cut; an unknown cut keeps everything."""
    if cut:
        for index, message in enumerate(messages):
            if getattr(message, "id", None) == cut:
                return list(messages[index + 1 :])
    return list(messages)


def _cut_index(messages: Sequence[Any], cut: str | None) -> int:
    """Index of the cut message, or -1 when there is none."""
    if cut:
        for index, message in enumerate(messages):
            if getattr(message, "id", None) == cut:
                return index
    return -1


def _assistant_text(message: Any) -> str:
    content = _text(getattr(message, "content", ""))
    hidden = (getattr(message, "additional_kwargs", None) or {}).get(COPPER_CONTEXT_KEY)
    if isinstance(hidden, str) and hidden:
        content = f"{content.rstrip()}\n{hidden}".strip()
    return content


def _prose(messages: Sequence[Any]) -> list[tuple[str, str]]:
    """``(role, text)`` for the human and assistant prose the model is sent."""
    result: list[tuple[str, str]] = []
    for message in messages:
        kind = getattr(message, "type", None)
        if kind == "human":
            result.append(("user", _text(message.content)))
        elif kind == "ai":
            text = _assistant_text(message)
            if text.strip():
                result.append(("assistant", text))
    return result


def _tool_results(messages: Sequence[Any]) -> dict[str, Any]:
    return {
        str(message.tool_call_id): message
        for message in messages
        if isinstance(message, ToolMessage) and message.tool_call_id
    }


def _short_args(arguments: Any) -> str:
    if not isinstance(arguments, dict):
        return _clip_chars(_text(arguments), MAX_DIGEST_ARGS_CHARS)
    picked = [
        f"{key}={_clip_chars(' '.join(str(arguments[key]).split()), 120)}"
        for key in _DIGEST_ARG_KEYS
        if arguments.get(key) not in (None, "")
    ]
    text = ", ".join(picked) if picked else json.dumps(arguments, default=str)
    return _clip_chars(text, MAX_DIGEST_ARGS_CHARS)


def _clip_chars(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tool_status(result: Any) -> str:
    if result is None:
        return "pending"
    if (getattr(result, "additional_kwargs", None) or {}).get("copper_rejected") is True:
        return "rejected"
    artifact = getattr(result, "artifact", None)
    exit_code = artifact.get("exit_code") if isinstance(artifact, dict) else None
    if getattr(result, "status", None) == "error" or (isinstance(exit_code, int) and exit_code):
        return "error"
    return "ok"


def tool_digest(call: dict[str, Any], result: Any) -> dict[str, Any]:
    """One ``tool_digest`` entry: name, short args, status and a 2 KB excerpt."""
    digest: dict[str, Any] = {
        "name": str(call.get("name") or "tool"),
        "args": _short_args(call.get("args")),
        "status": _tool_status(result),
    }
    if result is not None:
        excerpt, _clipped = _clip_bytes(_text(result.content), MAX_DIGEST_EXCERPT_BYTES)
        if excerpt:
            digest["excerpt"] = excerpt
    return digest


def compaction_context(messages: Sequence[Any]) -> tuple[list[dict[str, Any]], list[str | None]]:
    """The compaction request's ``context`` and, per entry, its last message id.

    Tool calls are folded into the next assistant message as ``tool_digest``;
    calls with no assistant text after them become a ``(tool activity)`` entry.
    """
    results = _tool_results(messages)
    entries: list[dict[str, Any]] = []
    ids: list[str | None] = []
    pending: list[dict[str, Any]] = []
    pending_id: str | None = None

    def flush() -> None:
        nonlocal pending, pending_id
        if pending:
            entries.append(
                {
                    "role": "assistant",
                    "content": TOOL_ACTIVITY_TEXT,
                    "tool_digest": pending[-MAX_TOOL_DIGESTS:],
                }
            )
            ids.append(pending_id)
        pending, pending_id = [], None

    for message in messages:
        kind = getattr(message, "type", None)
        if kind == "human":
            flush()
            content, _ = _clip_bytes(_text(message.content), MAX_CONTEXT_MESSAGE_BYTES)
            entries.append({"role": "user", "content": content})
            ids.append(getattr(message, "id", None))
        elif kind == "ai":
            for call in getattr(message, "tool_calls", None) or []:
                pending.append(tool_digest(call, results.get(str(call.get("id") or ""))))
                pending_id = getattr(message, "id", None)
            text = _assistant_text(message)
            if not text.strip():
                continue
            content, _ = _clip_bytes(text, MAX_CONTEXT_MESSAGE_BYTES)
            entry: dict[str, Any] = {"role": "assistant", "content": content}
            if pending:
                entry["tool_digest"] = pending[-MAX_TOOL_DIGESTS:]
            entries.append(entry)
            ids.append(getattr(message, "id", None))
            pending, pending_id = [], None
    flush()
    return entries, ids


def interrupted_entry(journal: Any) -> dict[str, Any] | None:
    """The work of an interrupted turn (from its journal) as one assistant entry."""
    digests = []
    for event in list(getattr(journal, "tool_events", None) or [])[-MAX_TOOL_DIGESTS:]:
        if not isinstance(event, dict):
            continue
        status = {"completed": "ok", "failed": "error"}.get(str(event.get("status")), "interrupted")
        digest: dict[str, Any] = {
            "name": str(event.get("tool_name") or "tool"),
            "args": _short_args(event.get("arguments") or {}),
            "status": status,
        }
        excerpt = _clip_bytes(str(event.get("result") or ""), MAX_DIGEST_EXCERPT_BYTES)[0]
        if excerpt:
            digest["excerpt"] = excerpt
        digests.append(digest)
    text = str(getattr(journal, "assistant_text", "") or "").strip()
    if not text and not digests:
        return None
    entry: dict[str, Any] = {
        "role": "assistant",
        "content": _clip_bytes(
            f"{text}\n[Turn interrupted before completion]" if text else TOOL_ACTIVITY_TEXT,
            MAX_CONTEXT_MESSAGE_BYTES,
        )[0],
    }
    if digests:
        entry["tool_digest"] = digests
    return entry


def history_context(messages: Sequence[Any]) -> list[dict[str, str]]:
    """``[{role, content}]`` of every message, as a turn would send it (for counting)."""
    return [
        {"role": role, "content": _clip_bytes(text, MAX_CONTEXT_MESSAGE_BYTES)[0]}
        for role, text in _prose(messages)
    ]


# ------------------------------------------------------------- session state


def _indexed_answers(messages: Sequence[Any]) -> list[tuple[int, str, dict[str, str]]]:
    """``(transcript index, id, answer)`` for every question in the checkpoint messages."""
    calls: dict[str, dict[str, Any]] = {}
    answers: list[tuple[int, str, dict[str, str]]] = []
    for index, message in enumerate(messages):
        if isinstance(message, AIMessage):
            for call in message.tool_calls or []:
                if call.get("name") == ASK_TOOL and call.get("id"):
                    args = call.get("args")
                    calls[str(call["id"])] = args if isinstance(args, dict) else {}
        elif isinstance(message, ToolMessage):
            call_id = str(message.tool_call_id or "")
            if call_id not in calls and message.name != ASK_TOOL:
                continue
            content = message.content if isinstance(message.content, str) else None
            for row, answer in enumerate(_answers_from(calls.get(call_id, {}), content)):
                answers.append((index, f"{call_id}#{row}", answer))
    return answers


def ask_answers(
    messages: Sequence[Any], facts: dict[str, Any] | None = None
) -> list[dict[str, str]]:
    """Every answered or skipped ``ask_user_question``, oldest first, up to 50.

    Questions of interrupted turns come from ``facts`` (see ``journal_facts``)
    and are placed at their turn's position in the transcript.
    """
    rows = _indexed_answers(messages)
    seen = {identifier for _index, identifier, _answer in rows}
    for item in (facts or {}).get("ask_answers") or []:
        if not isinstance(item, dict) or item.get("id") in seen:
            continue
        answer = {key: str(item.get(key) or "") for key in ("question", "answer", "status")}
        rows.append((_int(item.get("at")) or 0, str(item.get("id")), answer))
    rows.sort(key=lambda row: row[0])
    return [answer for _index, _identifier, answer in rows][-MAX_ASK_ANSWERS:]


def _answers_from(arguments: dict[str, Any], content: str | None) -> list[dict[str, str]]:
    question = str(arguments.get("question") or arguments.get("prompt") or "").strip()
    try:
        result = json.loads(content) if content else None
    except (json.JSONDecodeError, TypeError):
        result = None
    if not isinstance(result, dict):
        # A rejected or interrupted question: the agent asked, nobody answered.
        return [_answer(question, "", "skipped")]
    rows = result.get("answers")
    if isinstance(rows, list):
        prompts = [
            str(row.get("question") or row.get("prompt") or "").strip()
            for row in arguments.get("questions") or []
            if isinstance(row, dict)
        ]
        answers = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            text = str(row.get("answer") or "").strip()
            prompt = str(row.get("question") or "").strip() or (
                prompts[index] if index < len(prompts) else question
            )
            answers.append(_answer(prompt, text, "answered" if text else "skipped"))
        return answers
    answer = str(result.get("answer") or "").strip()
    status = "answered" if result.get("status") == "answered" and answer else "skipped"
    return [_answer(question, answer, status)]


def _answer(question: str, answer: str, status: str) -> dict[str, str]:
    return {
        "question": _clip_bytes(question or "Clarification", MAX_ASK_QUESTION_BYTES)[0],
        "answer": _clip_bytes(answer, MAX_ASK_ANSWER_BYTES)[0],
        "status": status,
    }


def todo_items(payload: Any) -> dict[str, Any]:
    """``{"items": [...]}`` from a hosted ``todo_list`` status event."""
    items = []
    for item in (payload or {}).get("items") or [] if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if content:
            status = _TODO_STATUSES.get(str(item.get("status") or "").lower(), "pending")
            items.append({"content": content, "status": status})
    return {"items": items[:MAX_TODOS]}


def latest_todos(
    events: Sequence[Any], facts: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """The latest hosted ``todo_list``: from ``copper_events`` or an interrupted turn."""
    latest: dict[str, Any] | None = None
    stamp = 0.0
    for event in reversed(events):
        if not isinstance(event, dict) or event.get("type") != "copper.status_event":
            continue
        data = event.get("data")
        if not isinstance(data, dict) or data.get("kind") != "todo_list":
            continue
        latest, stamp = todo_items(data), _ts(event.get("ts"))
        break
    interrupted = (facts or {}).get("todos")
    if isinstance(interrupted, dict) and (latest is None or _ts(interrupted.get("ts")) > stamp):
        return {"items": list(interrupted.get("items") or [])[:MAX_TODOS]}
    return latest


def _plan_paths(messages: Sequence[Any], facts: dict[str, Any] | None = None) -> list[str]:
    """Plan files this chat touched, newest first: writes before mere references."""
    found: list[tuple[bool, int, str]] = []
    for index, message in enumerate(messages):
        for call in getattr(message, "tool_calls", None) or []:
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            path = next((str(args[key]) for key in _PATH_KEYS if args.get(key)), "")
            if path and _PLAN_PATH.search(path):
                found.append((call.get("name") in _PLAN_WRITERS, index, path))
    for item in (facts or {}).get("plan_writes") or []:
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            found.append((True, _int(item.get("at")) or 0, item["path"]))
    found.sort(key=lambda row: (row[0], row[1]), reverse=True)
    paths: list[str] = []
    for _write, _index, path in found:
        if path not in paths:
            paths.append(path)
    return paths


def journal_facts(journal: Any) -> dict[str, Any]:
    """What an interrupted turn's journal knows that the checkpoint never got.

    Its question answers (rejected, unanswered or interrupted ones count as
    skipped), the plan files it wrote, its latest todo list and context size,
    plus whatever it carried from earlier interrupted turns.
    """
    at = max(int(getattr(journal, "original_len", 1) or 1) - 1, 0)
    own: dict[str, Any] = {"ask_answers": [], "plan_writes": []}
    for event in getattr(journal, "tool_events", None) or []:
        if not isinstance(event, dict):
            continue
        name = str(event.get("tool_name") or "")
        arguments = event.get("arguments") if isinstance(event.get("arguments"), dict) else {}
        call_id = str(event.get("tool_call_id") or "")
        if name == ASK_TOOL:
            content = event.get("result") if event.get("status") == "completed" else None
            for row, answer in enumerate(_answers_from(arguments, content)):
                own["ask_answers"].append({**answer, "id": f"{call_id}#{row}", "at": at})
        elif name in _PLAN_WRITERS and event.get("status") == "completed":
            path = next((str(arguments[key]) for key in _PATH_KEYS if arguments.get(key)), "")
            if path and _PLAN_PATH.search(path):
                own["plan_writes"].append({"path": path, "at": at})
    todos = getattr(journal, "todos", None)
    if isinstance(todos, dict):
        own["todos"] = todos
    usage = _valid_usage(getattr(journal, "context_usage", None))
    if usage is not None:
        own["context_usage"] = usage
    carried = getattr(journal, "carried", None)
    return merge_facts(carried if isinstance(carried, dict) else None, own)


def merge_facts(*parts: dict[str, Any] | None) -> dict[str, Any]:
    """Combine interrupted-turn facts; later parts win for todos and context size."""
    merged: dict[str, Any] = {"ask_answers": [], "plan_writes": []}
    ids: set[str] = set()
    for part in parts:
        if not isinstance(part, dict):
            continue
        for item in part.get("ask_answers") or []:
            if isinstance(item, dict) and item.get("id") not in ids:
                ids.add(str(item.get("id")))
                merged["ask_answers"].append(item)
        for item in part.get("plan_writes") or []:
            if isinstance(item, dict) and item not in merged["plan_writes"]:
                merged["plan_writes"].append(item)
        for key, stamp in (("todos", "ts"), ("context_usage", "recorded_at")):
            value = part.get(key)
            current = merged.get(key)
            if isinstance(value, dict) and (
                current is None or _ts(value.get(stamp)) >= _ts(current.get(stamp))
            ):
                merged[key] = value
    merged["ask_answers"] = merged["ask_answers"][-MAX_ASK_ANSWERS:]
    merged["plan_writes"] = merged["plan_writes"][-MAX_ASK_ANSWERS:]
    return merged


def has_facts(facts: dict[str, Any] | None) -> bool:
    return isinstance(facts, dict) and any(facts.get(key) for key in facts)


def _plan_file(path: str, workspace: Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        resolved = candidate.resolve()
        if resolved == workspace or workspace in resolved.parents:
            return resolved
        # Deep Agents' virtual paths are rooted at the workspace.
        return workspace / path.lstrip("/\\")
    return workspace / candidate


def plan_state(
    messages: Sequence[Any],
    workspace: Path | None,
    plan_context: PlanContext | None = None,
    facts: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The plan the chat is working on: a ``/build`` plan, else the latest plan file written."""
    plan: PlanContext | None = plan_context
    if plan is None and workspace is not None:
        from copper_pilot_cli.copper_features import parse_plan

        root = workspace.resolve()
        for path in _plan_paths(messages, facts):
            try:
                plan = parse_plan(_plan_file(path, root), root)
            except Exception as exc:  # noqa: BLE001 - agent-written YAML may not parse
                logger.debug("Plan %s not pinned: %s", path, type(exc).__name__)
                continue
            break
    if plan is None:
        return None
    body, truncated = _clip_bytes(plan.body or "", MAX_PLAN_BODY_BYTES)
    return {
        "file_path": plan.file_path,
        "name": plan.name or "",
        "overall_status": _PLAN_STATUSES.get(plan.overall_status, "draft"),
        "overview": plan.overview or "",
        "todos": [
            {"id": todo.id, "content": todo.content, "status": todo.status}
            for todo in plan.todos[:MAX_TODOS]
        ],
        "body": body,
        "body_truncated": truncated,
    }


def earlier_user_messages(
    messages: Sequence[Any],
    cut: str | None,
    *,
    budget_tokens: int = DEFAULT_CONTEXT_POLICY.user_messages_budget_tokens,
) -> list[dict[str, Any]]:
    """The user messages the summary covers, verbatim, oldest first.

    They are fitted to ``budget_tokens`` (``chars / 4``) and 96 KB newest
    first: the oldest are cut first, and the one that only partly fits is
    clipped and marked ``truncated``. ``index`` is the transcript position.
    """
    end = _cut_index(messages, cut)
    if end < 0:
        return []
    budget = min(budget_tokens * 4, MAX_EARLIER_TOTAL_BYTES)
    kept: list[dict[str, Any]] = []
    for index in range(end, -1, -1):
        message = messages[index]
        if getattr(message, "type", None) != "human":
            continue
        text, truncated = _clip_bytes(_text(message.content), MAX_EARLIER_MESSAGE_BYTES)
        size = len(text.encode())
        if size > budget:
            if budget < 64:
                break
            # The newest message that does not fit keeps its end, the most recent words.
            text = text.encode()[-budget:].decode(errors="ignore")
            size, truncated = len(text.encode()), True
        entry: dict[str, Any] = {"index": index, "text": text}
        if truncated:
            entry["truncated"] = True
        kept.append(entry)
        budget -= size
        if budget <= 0:
            break
    kept.reverse()
    return kept


def build_session_state(
    values: dict[str, Any] | None,
    *,
    workspace: Path | None,
    plan_context: PlanContext | None = None,
    policy: ContextPolicy = DEFAULT_CONTEXT_POLICY,
) -> dict[str, Any]:
    """The ``ChatRequest.session_state`` for the next turn of a checkpointed chat.

    ``history_omitted`` is added when the frame is built (``make_chat_start``),
    because only then is it known whether history still had to be dropped.
    """
    values = values or {}
    messages = list(values.get("messages") or [])
    state: dict[str, Any] = {"version": SESSION_STATE_VERSION}
    record = current_compaction(values)
    if record is not None:
        state["compaction"] = session_compaction(record)
        earlier = earlier_user_messages(
            messages,
            context_cut(values),
            budget_tokens=policy.user_messages_budget_tokens,
        )
        if earlier:
            state["earlier_user_messages"] = earlier
    facts = values.get(FACTS_KEY) if isinstance(values.get(FACTS_KEY), dict) else None
    answers = ask_answers(messages, facts)
    if answers:
        state["ask_answers"] = answers
    todos = latest_todos(list(values.get("copper_events") or []), facts)
    if todos is not None:
        state["todos"] = todos
    plan = plan_state(messages, workspace, plan_context, facts)
    if plan is not None:
        state["plan"] = plan
    return state


# ---------------------------------------------------------------- compaction


async def compact_history(
    agent: Any,
    values: dict[str, Any] | None,
    *,
    trigger: str,
    model: str,
    instructions: str | None = None,
    workspace: Path | None = None,
    policy: ContextPolicy = DEFAULT_CONTEXT_POLICY,
    interrupted: Any = None,
    pending_query: str | None = None,
) -> CompactionResult:
    """Summarize the history after the current cut (``POST /analyze/compact``).

    The request carries the previous compaction in ``session_state``, so the
    new summary is anchored on it. Raises ``NothingToCompact``,
    ``CompactionFailed`` or the client's ``ProtocolError``; the checkpoint is
    untouched until the caller applies ``CompactionResult.updates``.

    Before a new turn, ``interrupted`` is the journal of a superseded
    interrupted turn whose work never reached the checkpoint (it becomes one
    assistant entry with tool digests, so the summary can keep it), and
    ``pending_query`` is the message about to be sent: it ends the context so
    the server's cut keeps it, and it is never summarized itself.
    """
    values = values or {}
    messages = list(values.get("messages") or [])
    visible = after_cut(messages, context_cut(values))
    entries, ids = compaction_context(visible)
    if interrupted is not None and entries:
        work = interrupted_entry(interrupted)
        if work is not None:
            # Covered by the interrupted turn's own (last) message id.
            entries.append(work)
            ids.append(ids[-1])
    if len(entries) < 2:
        raise NothingToCompact("There is not enough conversation to compact yet.")
    history_len = len(entries)
    if pending_query:
        entries.append(
            {"role": "user", "content": _clip_bytes(pending_query, MAX_CONTEXT_MESSAGE_BYTES)[0]}
        )
    effort = getattr(agent, "effort", None)
    payload: dict[str, Any] = {
        "conversation_id": str(getattr(agent, "conversation_id", "") or ""),
        "model": model,
        "effort": str(getattr(effort, "value", effort) or "balanced"),
        "trigger": trigger,
        "context": entries,
        "session_state": build_session_state(
            values,
            workspace=workspace,
            plan_context=getattr(agent, "plan_context", None),
            policy=policy,
        ),
    }
    if instructions and instructions.strip():
        payload["instructions"] = instructions.strip()
    started = time.monotonic()
    response = await agent.acompact(payload)
    compaction = response.get("compaction") if isinstance(response, dict) else None
    keep = _int(response.get("keep_from_index")) if isinstance(response, dict) else None
    if keep is None and isinstance(compaction, dict):
        keep = _int(compaction.get("messages_summarized"))
    if not keep:
        raise CompactionFailed("The compaction reply covered no messages.")
    keep = min(keep, history_len)
    record = compaction_record(compaction, last_message_id=ids[keep - 1], trigger=trigger)
    if record is None or not record["last_message_id"]:
        raise CompactionFailed("The compaction reply had no usable summary.")
    record.setdefault("messages_summarized", keep)
    updates = compaction_updates(values, record)
    post = _int(record.get("post_tokens"))
    if post is not None:
        previous = stored_usage(values) or {}
        updates[USAGE_KEY] = usage_record(
            {
                **{
                    key: previous[key]
                    for key in ("context_limit", "auto_compact_at", "model")
                    if key in previous
                },
                "context_tokens": post,
            },
            measured_at=len(messages),
            source="compaction",
        )
    updates["copper_events"] = [
        compaction_event("completed", trigger=trigger, record=_event_record(record))
    ]
    logger.info(
        "Chat context compacted (trigger=%s, messages=%s, pre=%s, post=%s, %.1fs)",
        trigger,
        keep,
        record.get("pre_tokens"),
        record.get("post_tokens"),
        time.monotonic() - started,
    )
    return CompactionResult(record=record, updates=updates)


def _event_record(record: dict[str, Any]) -> dict[str, Any]:
    """The record as carried on events: everything but the (large) summary."""
    return {key: value for key, value in record.items() if key != "summary"}


async def count_context(
    agent: Any,
    values: dict[str, Any] | None,
    *,
    model: str,
    workspace: Path | None = None,
    query: str = "",
) -> dict[str, Any]:
    """``POST /analyze/context/count`` for the chat as the next turn would send it."""
    values = values or {}
    messages = list(values.get("messages") or [])
    effort = getattr(agent, "effort", None)
    mode = getattr(agent, "mode", None)
    payload = {
        "model": model,
        "effort": str(getattr(effort, "value", effort) or "balanced"),
        "mode": str(getattr(mode, "value", mode) or "agent"),
        "context": history_context(after_cut(messages, context_cut(values))),
        "session_state": build_session_state(values, workspace=workspace),
        "query": query,
    }
    return await agent.acount_context(payload)


def describe_context(
    *,
    used: int | None,
    limit: int | None,
    auto_compact_at: int | None,
    record: dict[str, Any] | None,
    breakdown: dict[str, Any] | None = None,
    estimated: bool = False,
) -> list[str]:
    """Plain lines for ``/context``: size, auto-compaction point, compaction, breakdown."""
    lines: list[str] = []
    label = "Context (estimated)" if estimated else "Context"
    if used is not None and limit:
        lines.append(
            f"{label}: {context_percent(used, limit)}% used · "
            f"{format_tokens(used)} / {format_tokens(limit)} tokens"
        )
    elif used is not None:
        lines.append(f"{label}: {format_tokens(used)} tokens (model window unknown)")
    else:
        lines.append("Context: not measured yet; it is shown after the next turn.")
    if auto_compact_at:
        lines.append(f"Auto-compacts at: {format_tokens(auto_compact_at)} tokens")
    if record is not None:
        pre, post = record.get("pre_tokens"), record.get("post_tokens")
        sizes = (
            f"{format_tokens(pre)} → {format_tokens(post)} tokens"
            if isinstance(pre, int) and isinstance(post, int)
            else "yes"
        )
        lines.append(f"Compacted: {sizes} ({record.get('trigger') or 'manual'})")
    if breakdown:
        parts = [
            f"{key.replace('_', ' ')} {format_tokens(value)}"
            for key, value in breakdown.items()
            if isinstance(value, (int, float))
        ]
        if parts:
            lines.append("Breakdown: " + " · ".join(parts))
    return lines


# --------------------------------------------------------------------- search


def _search_text(message: Any) -> str:
    kind = getattr(message, "type", None)
    if kind == "ai":
        parts = [_assistant_text(message)]
        for call in getattr(message, "tool_calls", None) or []:
            parts.append(f"{call.get('name')}({json.dumps(call.get('args') or {}, default=str)})")
        return "\n".join(part for part in parts if part)
    return _text(getattr(message, "content", ""))


def _snippet(text: str, position: int) -> str:
    if len(text) <= SEARCH_SNIPPET_CHARS:
        return text
    budget = SEARCH_SNIPPET_CHARS - 2
    start = max(0, min(position - budget // 3, len(text) - budget))
    snippet = text[start : start + budget]
    return ("…" if start else "") + snippet + ("…" if start + budget < len(text) else "")


def search_history(messages: Sequence[Any], arguments: dict[str, Any]) -> dict[str, Any]:
    """Answer ``search_chat_history`` from the whole local transcript.

    Case-insensitive; every whitespace-separated term must appear. Newest
    first, at most ``max_results`` (20) snippets of up to 600 characters.
    """
    query = str(arguments.get("query") or "").strip()
    raw_limit = arguments.get("max_results")
    limit = _int(raw_limit) if raw_limit is not None else DEFAULT_SEARCH_RESULTS
    limit = max(1, min(limit or DEFAULT_SEARCH_RESULTS, MAX_SEARCH_RESULTS))
    results: list[dict[str, Any]] = []
    terms = [term.casefold() for term in query.split()]
    if terms:
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            kind = getattr(message, "type", None)
            role = {"human": "user", "ai": "assistant", "tool": "tool"}.get(str(kind))
            if role is None:
                continue
            text = _search_text(message)
            folded = text.casefold()
            if not all(term in folded for term in terms):
                continue
            phrase = folded.find(query.casefold())
            position = phrase if phrase >= 0 else folded.find(terms[0])
            entry: dict[str, Any] = {
                "message_index": index,
                "role": role,
                "snippet": _snippet(text, position),
            }
            created = (getattr(message, "additional_kwargs", None) or {}).get("copper_created_at")
            if isinstance(created, str) and created:
                entry["created_at"] = created
            if role == "tool" and getattr(message, "name", None):
                entry["tool_name"] = message.name
            results.append(entry)
            if len(results) >= limit:
                break
    return {"results": results, "total_messages": len(messages)}


__all__ = [
    "COMPACTION_EVENT",
    "COMPACTION_KEY",
    "FACTS_KEY",
    "CONTEXT_COMPACTED_KIND",
    "CONTEXT_USAGE_KIND",
    "CompactionFailed",
    "CompactionResult",
    "NothingToCompact",
    "SUPERSEDED_KEY",
    "USAGE_KEY",
    "after_cut",
    "ask_answers",
    "auto_compact_threshold",
    "build_session_state",
    "compact_history",
    "compaction_context",
    "compaction_event",
    "compaction_notice",
    "compaction_record",
    "compaction_updates",
    "context_cut",
    "context_estimate",
    "context_limit",
    "count_context",
    "current_compaction",
    "describe_context",
    "earlier_user_messages",
    "estimate_tokens",
    "format_tokens",
    "has_facts",
    "journal_facts",
    "latest_todos",
    "meter_level",
    "meter_text",
    "merge_facts",
    "parse_context_usage",
    "plan_state",
    "search_history",
    "should_auto_compact",
    "stored_usage",
    "tool_digest",
    "usage_record",
]
