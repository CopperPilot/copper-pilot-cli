"""Chat context protocol v1: session state, compaction, context size and history search."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from copper_pilot_cli import copper_graph, copper_models
from copper_pilot_cli.chat_context import (
    COMPACTION_EVENT,
    COMPACTION_KEY,
    FACTS_KEY,
    SUPERSEDED_KEY,
    USAGE_KEY,
    NothingToCompact,
    ask_answers,
    auto_compact_threshold,
    build_session_state,
    compact_history,
    compaction_context,
    compaction_notice,
    context_estimate,
    describe_context,
    earlier_user_messages,
    format_tokens,
    latest_todos,
    meter_level,
    meter_text,
    parse_context_usage,
    plan_state,
    search_history,
    should_auto_compact,
    usage_record,
)
from copper_pilot_cli.copper_graph import MAX_TURN_ATTEMPTS, create_copper_graph
from copper_pilot_cli.copper_models import (
    ChatModelCatalog,
    ContextPolicy,
    catalog_from_payload,
)
from copper_pilot_cli.copper_protocol import (
    CHAT_HISTORY_SEARCH_CAPABILITY,
    COMPACT_PATH,
    CONTEXT_COUNT_PATH,
    ChatMessage,
    ChatRequest,
    CopperEffort,
    CopperEvent,
    CopperMode,
    EventKind,
    HostedChatClient,
    ProtocolError,
    ToolRequest,
    make_chat_start,
)
from copper_pilot_cli.deepagents_tools import LocalToolBroker, ToolRejected
from copper_pilot_cli.langchain import _context

PLAN = """---
name: ESP32 board
overview: Build the dev board
overall_status: running
todos:
  - id: power
    content: Power tree
    status: done
  - id: usb
    content: USB-C
    status: open
---
# ESP32 board

Use the AMS1117 regulator.
"""


def asked(call_id: str, question: str, result: dict[str, Any] | str, **extra: Any) -> list[Any]:
    content = result if isinstance(result, str) else json.dumps(result)
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": call_id,
                    "name": "ask_user_question",
                    "args": {"question": question, **extra},
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content=content, tool_call_id=call_id, name="ask_user_question"),
    ]


def transcript() -> list[Any]:
    """A small chat with ids, as the checkpoint stores it."""
    messages = [
        HumanMessage(content="Build me an ESP32 dev board", id="h1"),
        *asked("q1", "Which module?", {"status": "answered", "answer": "S3-WROOM-1"}),
        AIMessage(content="Using the S3-WROOM-1.", id="a1"),
        HumanMessage(content="Add USB-C", id="h2"),
        AIMessage(
            content="",
            id="a2",
            tool_calls=[
                {
                    "id": "w1",
                    "name": "write_file",
                    "args": {"file_path": "/.copperpilot/plans/esp32.plan.md", "content": "x"},
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content="Updated file", tool_call_id="w1", name="write_file", id="t1"),
        AIMessage(content="Plan written.", id="a3"),
        HumanMessage(content="Now route it", id="h3"),
    ]
    return messages


# ------------------------------------------------------------- session state


def test_ask_answers_cover_answered_skipped_multi_and_rejected() -> None:
    messages = [
        *asked("q1", "Which module?", {"status": "answered", "answer": "S3", "source": "option"}),
        *asked("q2", "Which LDO?", {"status": "skipped"}),
        *asked("q3", "Bad", "ToolRejected: no free text here"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "q4",
                    "name": "ask_user_question",
                    "args": {"questions": [{"question": "Size?"}, {"question": "Layers?"}]},
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(
            content=json.dumps(
                {"answers": [{"question": "Size?", "answer": "50x50"}, {"answer": ""}]}
            ),
            tool_call_id="q4",
            name="ask_user_question",
        ),
    ]
    assert ask_answers(messages) == [
        {"question": "Which module?", "answer": "S3", "status": "answered"},
        {"question": "Which LDO?", "answer": "", "status": "skipped"},
        {"question": "Bad", "answer": "", "status": "skipped"},
        {"question": "Size?", "answer": "50x50", "status": "answered"},
        {"question": "Layers?", "answer": "", "status": "skipped"},
    ]


def test_ask_answers_keep_the_newest_fifty_and_clip_long_answers() -> None:
    messages: list[Any] = []
    for index in range(60):
        messages += asked(f"q{index}", f"Q{index}", {"status": "answered", "answer": "x" * 10_000})
    answers = ask_answers(messages)
    assert len(answers) == 50 and answers[0]["question"] == "Q10"
    assert all(len(item["answer"].encode()) <= 3 * 1024 for item in answers)


def test_latest_todos_come_from_the_newest_todo_list_event() -> None:
    events = [
        {
            "type": "copper.status_event",
            "data": {"kind": "todo_list", "items": [{"content": "Old"}]},
        },
        {"type": "copper.text", "data": "hi"},
        {
            "type": "copper.status_event",
            "data": {
                "kind": "todo_list",
                "items": [
                    {"index": 0, "content": "Power", "status": "done"},
                    {"index": 1, "content": "USB", "status": "pending"},
                    {"content": " "},
                ],
            },
        },
        {"type": "copper.status_event", "data": {"kind": "subagent", "id": "w1"}},
    ]
    assert latest_todos(events) == {
        "items": [{"content": "Power", "status": "done"}, {"content": "USB", "status": "pending"}]
    }
    assert latest_todos([]) is None


def test_plan_state_pins_the_latest_written_plan_and_caps_the_body(tmp_path) -> None:
    plans = tmp_path / ".copperpilot" / "plans"
    plans.mkdir(parents=True)
    (plans / "esp32.plan.md").write_text(PLAN + "z" * 40_000, encoding="utf-8")
    plan = plan_state(transcript(), tmp_path)
    assert plan is not None
    assert plan["file_path"] == ".copperpilot/plans/esp32.plan.md"
    assert plan["name"] == "ESP32 board" and plan["overall_status"] == "running"
    assert [todo["status"] for todo in plan["todos"]] == ["done", "open"]
    assert plan["body_truncated"] is True and len(plan["body"].encode()) == 32 * 1024
    # A plan file that no longer exists is skipped.
    (plans / "esp32.plan.md").unlink()
    assert plan_state(transcript(), tmp_path) is None


def test_earlier_user_messages_fit_the_budget_oldest_cut_first() -> None:
    messages = [
        HumanMessage(content="a" * 400, id="h1"),
        AIMessage(content="ok", id="a1"),
        HumanMessage(content="b" * 400, id="h2"),
        AIMessage(content="ok", id="a2"),
        HumanMessage(content="c" * 100, id="h3"),
        AIMessage(content="after the cut", id="a3"),
        HumanMessage(content="d", id="h4"),
    ]
    # 150 tokens = 600 bytes: b (400) fits, a keeps its last 200; c is after the cut.
    earlier = earlier_user_messages(messages, "a2", budget_tokens=150)
    assert [item["index"] for item in earlier] == [0, 2]
    assert earlier[0]["truncated"] is True and len(earlier[0]["text"]) == 200
    assert "truncated" not in earlier[1]
    everything = earlier_user_messages(messages, "a3", budget_tokens=20_000)
    assert [item["index"] for item in everything] == [0, 2, 4]
    assert earlier_user_messages(messages, None) == []


def test_build_session_state_carries_compaction_answers_todos_and_plan(tmp_path) -> None:
    plans = tmp_path / ".copperpilot" / "plans"
    plans.mkdir(parents=True)
    (plans / "esp32.plan.md").write_text(PLAN, encoding="utf-8")
    values = {
        "messages": transcript(),
        "copper_events": [
            {
                "type": "copper.status_event",
                "data": {"kind": "todo_list", "items": [{"content": "Route"}]},
            }
        ],
        COMPACTION_KEY: {
            "id": "cmp_1",
            "summary": "## Session intent\nESP32",
            "trigger": "manual",
            "pre_tokens": 182_000,
            "post_tokens": 31_000,
            "last_message_id": "a1",
            "stored_at": 1.0,
        },
    }
    state = build_session_state(values, workspace=tmp_path)
    assert state["version"] == 1
    assert state["compaction"] == {
        "id": "cmp_1",
        "summary": "## Session intent\nESP32",
        "trigger": "manual",
        "pre_tokens": 182_000,
        "post_tokens": 31_000,
    }
    assert state["earlier_user_messages"] == [{"index": 0, "text": "Build me an ESP32 dev board"}]
    assert state["ask_answers"] == [
        {"question": "Which module?", "answer": "S3-WROOM-1", "status": "answered"}
    ]
    assert state["todos"] == {"items": [{"content": "Route", "status": "pending"}]}
    assert state["plan"]["name"] == "ESP32 board"
    assert "history_omitted" not in state
    assert build_session_state({"messages": [HumanMessage(content="hi")]}, workspace=None) == {
        "version": 1
    }


# ------------------------------------------------------------------- history


def test_context_after_a_cut_sends_only_later_messages() -> None:
    messages = transcript()
    assert [m.content for m in _context(messages)] == [
        "Build me an ESP32 dev board",
        "Using the S3-WROOM-1.",
        "Add USB-C",
        "Plan written.",
    ]
    assert [m.content for m in _context(messages, "a1")] == ["Add USB-C", "Plan written."]
    # An unknown cut keeps everything rather than losing history.
    assert len(_context(messages, "missing")) == 4


def test_chat_start_reports_dropped_history_and_advertises_search() -> None:
    big = [ChatMessage(role="user", content="x" * 60_000) for _ in range(100)]
    request = ChatRequest(
        query="hi",
        context=big,
        workspace_path="/tmp/p",  # noqa: S108
        conversation_id="c",
        session_state={"version": 1},
    )
    frame = make_chat_start(request)
    state = frame["request"]["session_state"]
    kept = [m for m in frame["request"]["context"] if m["role"] != "system"]
    assert state["history_omitted"] == {"messages": 100 - len(kept)} and len(kept) < 100
    assert frame["request"]["context"][0]["content"].endswith("older messages omitted]")
    assert CHAT_HISTORY_SEARCH_CAPABILITY in frame["capabilities"]
    assert make_chat_start(request) == frame  # the same request, the same frame
    plain = make_chat_start(request.model_copy(update={"session_state": None, "context": []}))
    assert "session_state" not in plain["request"]
    assert CHAT_HISTORY_SEARCH_CAPABILITY not in plain.get("capabilities", [])


def test_compaction_context_folds_tool_calls_into_digests() -> None:
    entries, ids = compaction_context(transcript())
    assert [entry["role"] for entry in entries] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert ids == ["h1", "a1", "h2", "a3", "h3"]
    (ask,) = entries[1]["tool_digest"]
    assert ask["name"] == "ask_user_question" and ask["status"] == "ok"
    assert "S3-WROOM-1" in ask["excerpt"]
    (write,) = entries[3]["tool_digest"]
    assert write["args"] == "file_path=/.copperpilot/plans/esp32.plan.md"
    tail, tail_ids = compaction_context(
        [
            HumanMessage(content="go", id="h"),
            AIMessage(
                content="",
                id="x",
                tool_calls=[{"id": "c", "name": "execute", "args": {"command": "make"}}],
            ),
        ]
    )
    assert (
        tail[-1]["content"] == "(tool activity)"
        and tail[-1]["tool_digest"][0]["status"] == "pending"
    )
    assert tail_ids[-1] == "x"


# --------------------------------------------------------- estimate and policy


def test_estimate_adds_new_prose_to_the_last_measurement() -> None:
    messages = transcript()
    values = {
        "messages": messages,
        USAGE_KEY: usage_record({"context_tokens": 1000, "auto_compact_at": 1010}, measured_at=6),
    }
    # After the measurement: "Plan written." (13 chars -> 4) and "Now route it" (12 -> 3).
    assert context_estimate(values) == 1007
    assert context_estimate({"messages": messages}) is None
    assert should_auto_compact(1010, 1010, unfinished=False)
    assert not should_auto_compact(1009, 1010, unfinished=False)
    assert not should_auto_compact(5000, 1010, unfinished=True)
    assert not should_auto_compact(None, 1010, unfinished=False)
    assert not should_auto_compact(5000, None, unfinished=False)


def test_threshold_prefers_the_server_then_the_catalog_policy() -> None:
    catalog = catalog_from_payload(
        {
            "models": [
                {"id": "gemini-4-argon", "label": "Gemini 4 Argon", "context_window": 1_048_576}
            ],
            "balanced_model": "gemini-3.8-flash",
            "balanced_context_window": 1_000_000,
            "context_policy": {"auto_compact_fraction": 0.5, "reserve_output_tokens": 48_576},
        }
    )
    assert catalog is not None
    assert catalog.context_window(CopperEffort.INTELLIGENCE, None) == 1_048_576
    assert catalog.context_window(CopperEffort.BALANCED, None) == 1_000_000
    assert auto_compact_threshold({}, catalog, CopperEffort.INTELLIGENCE, None) == 500_000
    server = {USAGE_KEY: {"context_tokens": 1, "auto_compact_at": 812_972}}
    assert auto_compact_threshold(server, catalog, CopperEffort.INTELLIGENCE, None) == 812_972
    assert auto_compact_threshold({}, ChatModelCatalog(), CopperEffort.BALANCED, None) is None
    assert ContextPolicy(auto_compact_max_tokens=100).auto_compact_at(1_048_576) == 100


def test_meter_and_notice_copy() -> None:
    assert meter_text(470_000, 1_048_576) == "ctx 45% · 470K/1M"
    assert format_tokens(182_000) == "182K" and format_tokens(1_200_000) == "1.2M"
    assert meter_level(700, 1000) == "normal" and meter_level(760, 1000) == "warn"
    assert meter_level(920, 1000) == "high" and meter_level(960, 1000) == "critical"
    assert (
        compaction_notice({"pre_tokens": 182_000, "post_tokens": 31_000})
        == "Context compacted · 182K → 31K tokens"
    )
    assert parse_context_usage({"context_tokens": "x"}) is None
    lines = describe_context(
        used=470_000,
        limit=1_048_576,
        auto_compact_at=812_972,
        record={"pre_tokens": 182_000, "post_tokens": 31_000, "trigger": "auto"},
        breakdown={"system_and_tools": 23_000, "history": 26_000},
    )
    assert lines == [
        "Context: 45% used · 470K / 1M tokens",
        "Auto-compacts at: 813K tokens",
        "Compacted: 182K → 31K tokens (auto)",
        "Breakdown: system and tools 23K · history 26K",
    ]


# ---------------------------------------------------------------- compaction


@dataclass
class Compactor:
    """Fake agent with the compaction endpoint."""

    replies: list[Any] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)
    conversation_id: str = "conversation"
    effort: Any = CopperEffort.BALANCED
    plan_context: Any = None

    async def acompact(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(payload)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def reply(keep: int, **compaction: Any) -> dict[str, Any]:
    return {
        "compaction": {
            "id": "cmp_2",
            "summary": "## Session intent\nRoute",
            "created_at": "2026-11-04T00:00:00Z",
            "trigger": "manual",
            "pre_tokens": 182_000,
            "post_tokens": 31_000,
            "model": "gemini-3.8-flash",
            "messages_summarized": keep,
            **compaction,
        },
        "keep_from_index": keep,
    }


@pytest.mark.asyncio
async def test_compaction_stores_the_record_and_supersedes_the_previous_one() -> None:
    old = {"id": "cmp_1", "summary": "old", "last_message_id": "h1"}
    values = {
        "messages": transcript(),
        COMPACTION_KEY: old,
        USAGE_KEY: {"context_tokens": 182_000, "context_limit": 1_048_576},
    }
    agent = Compactor([reply(2)])
    result = await compact_history(
        agent, values, trigger="manual", model="gemini-3.8-flash", instructions=" power "
    )
    (request,) = agent.requests
    # Anchored re-compaction: only messages after the old cut, plus the old summary.
    assert [entry["content"] for entry in request["context"]][:2] == [
        "Using the S3-WROOM-1.",
        "Add USB-C",
    ]
    assert request["session_state"]["compaction"]["id"] == "cmp_1"
    assert request["instructions"] == "power" and request["trigger"] == "manual"
    record = result.record
    assert record["last_message_id"] == "h2" and record["messages_summarized"] == 2
    assert result.updates[COMPACTION_KEY] is record
    assert [item["id"] for item in result.updates[SUPERSEDED_KEY]] == ["cmp_1"]
    usage = result.updates[USAGE_KEY]
    assert usage["context_tokens"] == 31_000 and usage["context_limit"] == 1_048_576
    assert usage["measured_at"] == len(values["messages"])
    (event,) = result.updates["copper_events"]
    assert event["type"] == COMPACTION_EVENT and "summary" not in event["data"]["record"]
    assert event["title"] == "Context compacted · 182K → 31K tokens"


@pytest.mark.asyncio
async def test_compaction_refuses_empty_summaries_and_tiny_chats() -> None:
    values = {"messages": transcript()}
    with pytest.raises(RuntimeError, match="summary"):
        await compact_history(Compactor([reply(2, summary=" ")]), values, trigger="auto", model="m")
    with pytest.raises(NothingToCompact):
        await compact_history(
            Compactor(),
            {"messages": [HumanMessage(content="hi", id="h")]},
            trigger="auto",
            model="m",
        )


# ------------------------------------------------------------------ REST client


@pytest.mark.asyncio
async def test_compact_and_count_post_with_device_auth(monkeypatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == COMPACT_PATH:
            return httpx.Response(200, json=reply(1))
        return httpx.Response(200, json={"estimated_tokens": 5})

    client = HostedChatClient("https://example.test", "cf_live_key", "fp")
    transport = httpx.MockTransport(handler)
    assert (await client.compact({"a": 1}, transport=transport))["keep_from_index"] == 1
    assert (await client.count_context({}, transport=transport))["estimated_tokens"] == 5
    assert [request.url.path for request in seen] == [COMPACT_PATH, CONTEXT_COUNT_PATH]
    assert seen[0].headers["authorization"] == "Bearer cf_live_key"
    assert seen[0].headers["x-copperpilot-fingerprint"] == "fp"


@pytest.mark.asyncio
async def test_rest_errors_explain_missing_endpoints_and_failures() -> None:
    client = HostedChatClient("https://example.test", "key", "fp")

    async def status(code: int, body: Any = None) -> ProtocolError:
        transport = httpx.MockTransport(lambda _r: httpx.Response(code, json=body))
        with pytest.raises(ProtocolError) as caught:
            await client.compact({}, transport=transport)
        return caught.value

    missing = await status(404, {"detail": "Not Found"})
    assert "does not support context compaction yet" in str(missing)
    failed = await status(
        502, {"detail": {"code": "compaction_failed", "message": "Empty summary"}}
    )
    assert failed.code == "compaction_failed" and str(failed) == "Empty summary"
    limited = await status(402, {"detail": {"block_reason": "window", "message": "Limit"}})
    assert limited.status == 402 and limited.block_reason == "window"


# --------------------------------------------------------------------- search


def test_search_history_is_newest_first_and_bounded() -> None:
    messages = [
        HumanMessage(content="Use the AMS1117 regulator", id="h1"),
        ToolMessage(
            content="AMS1117-3.3 in stock " + "x" * 2000, tool_call_id="t", name="read_file"
        ),
        AIMessage(content="The ams1117 drops 1.1 V.", id="a1"),
        HumanMessage(content="unrelated", id="h2"),
    ]
    result = search_history(messages, {"query": "ams1117", "max_results": 50})
    assert result["total_messages"] == 4
    assert [(item["message_index"], item["role"]) for item in result["results"]] == [
        (2, "assistant"),
        (1, "tool"),
        (0, "user"),
    ]
    assert all(len(item["snippet"]) <= 600 for item in result["results"])
    assert result["results"][1]["tool_name"] == "read_file"
    assert len(search_history(messages, {"query": "AMS1117", "max_results": 1})["results"]) == 1
    assert search_history(messages, {"query": "  "})["results"] == []


@pytest.mark.asyncio
async def test_broker_routes_search_chat_history(tmp_path) -> None:
    broker = LocalToolBroker(tmp_path)
    request = ToolRequest(
        tool_call_id="s1", tool_name="search_chat_history", arguments={"query": "x"}
    )
    with pytest.raises(ToolRejected):
        await broker.execute(request)
    broker.history_search = lambda arguments: {"results": [], "query": arguments["query"]}
    request = ToolRequest(
        tool_call_id="s2", tool_name="search_chat_history", arguments={"query": "y"}
    )
    assert await broker.execute(request) == {"results": [], "query": "y"}


# --------------------------------------------------------------------- graph


@dataclass
class GraphAgent:
    """Fake hosted agent: scripted attempts plus the compaction endpoint."""

    attempts: list[list[Any]]
    compactions: list[Any] = field(default_factory=list)
    mode: CopperMode = CopperMode.AGENT
    effort: Any = CopperEffort.BALANCED
    model: str | None = None
    cancel_requested: bool = False
    tool_broker: Any = None
    conversation_id: str = "conversation"
    workspace: Any = None
    plan_context: Any = None
    payloads: list[dict[str, Any]] = field(default_factory=list)
    compact_requests: list[dict[str, Any]] = field(default_factory=list)
    hanging: asyncio.Event = field(default_factory=asyncio.Event)

    async def astream_events_raw(self, value):
        self.payloads.append(dict(value))
        for item in self.attempts.pop(0):
            if item is HANG:
                # Wait like a long hosted turn until the test interrupts it.
                self.hanging.set()
                await asyncio.Event().wait()
            if isinstance(item, BaseException):
                raise item
            if callable(item):
                item()
                continue
            yield item

    async def acompact(self, payload):
        self.compact_requests.append(payload)
        result = self.compactions.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def server_reachable(self) -> bool:
        return True

    async def acancel(self):
        self.cancel_requested = True


HANG = object()
DROP = ProtocolError("dropped", code="STREAM_ENDED_WITHOUT_FINAL", retryable=True)


def final(**extra: Any) -> CopperEvent:
    return CopperEvent(EventKind.FINAL, {"success": True, **extra})


def usage(tokens: int, *, at: int = 1000, limit: int = 1_048_576) -> dict[str, Any]:
    return {
        "model": "gemini-3.8-flash",
        "context_tokens": tokens,
        "context_limit": limit,
        "auto_compact_at": at,
    }


@pytest.fixture
def graph_env(monkeypatch, tmp_path):
    monkeypatch.setenv("COPPER_PILOT_CLI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("copper_pilot_cli.sessions._db_path", None)
    monkeypatch.setattr(copper_graph, "RETRY_BACKOFF_SECONDS", (0.0,) * MAX_TURN_ATTEMPTS)
    monkeypatch.setattr(copper_graph, "HEARTBEAT_POLL_SECONDS", (0.0,))
    copper_models.reset_chat_model_catalog()
    workspace = tmp_path / "project"
    workspace.mkdir()
    yield workspace
    copper_models.reset_chat_model_catalog()


async def turn(agent, workspace, values, *, thread: str = "t"):
    async with create_copper_graph(agent) as runtime:
        config = runtime.config(thread, workspace)
        events = [
            data
            async for _ns, mode, data in runtime.astream(values, config)
            if mode == "custom" and isinstance(data, dict)
        ]
        state = await runtime.aget_state(config)
    return events, state.values


def say(text: str) -> dict[str, Any]:
    return {"messages": [HumanMessage(content=text)], "copper_mode": "agent"}


@pytest.mark.asyncio
async def test_turns_store_usage_send_session_state_and_answer_history_search(
    graph_env,
) -> None:
    broker = LocalToolBroker(graph_env)
    search = ToolRequest(
        tool_call_id="s1", tool_name="search_chat_history", arguments={"query": "ldo"}
    )
    found: list[Any] = []

    async def run_search() -> None:
        found.append(await broker.execute(search))

    agent = GraphAgent(
        [
            [CopperEvent(EventKind.TEXT, "Use an LDO."), final(context_usage=usage(500))],
            [
                CopperEvent(EventKind.STATUS_EVENT, {"kind": "context_usage", **usage(700)}),
                final(context_usage=usage(800)),
            ],
        ],
        tool_broker=broker,
        workspace=graph_env,
    )
    _events, values = await turn(agent, graph_env, say("Pick a regulator"))
    assert values[USAGE_KEY]["context_tokens"] == 500
    assert values[USAGE_KEY]["measured_at"] == 1
    assert agent.payloads[0]["session_state"] == {"version": 1}
    assert "context_cut" not in agent.payloads[0]

    # The broker answers search_chat_history while a turn runs, from the whole transcript.
    original = agent.astream_events_raw

    async def searching(value):
        await run_search()
        async for item in original(value):
            yield item

    agent.astream_events_raw = searching  # type: ignore[method-assign]
    _events, values = await turn(agent, graph_env, say("Why that one?"))
    assert found[0]["results"][0]["snippet"] == "Use an LDO."
    assert values[USAGE_KEY]["context_tokens"] == 800 and values[USAGE_KEY]["measured_at"] == 3
    assert broker.history_search is None


@pytest.mark.asyncio
async def test_auto_compaction_runs_before_a_large_turn_and_cuts_history(graph_env) -> None:
    agent = GraphAgent(
        [
            [CopperEvent(EventKind.TEXT, "First answer."), final(context_usage=usage(990))],
            [CopperEvent(EventKind.TEXT, "Second answer."), final(context_usage=usage(100))],
        ],
        compactions=[reply(2, trigger="auto")],
        workspace=graph_env,
    )
    await turn(agent, graph_env, say("Design the power tree"))
    events, values = await turn(agent, graph_env, say("Now add USB-C " + "x" * 100))

    (request,) = agent.compact_requests
    assert request["trigger"] == "auto"
    assert [entry["role"] for entry in request["context"]] == ["user", "assistant", "user"]
    stages = [e["data"]["stage"] for e in events if e["type"] == COMPACTION_EVENT]
    assert stages == ["started", "completed"]
    payload = agent.payloads[-1]
    first = values["messages"][1]
    assert payload["context_cut"] == first.id
    assert payload["session_state"]["compaction"]["id"] == "cmp_2"
    assert payload["session_state"]["earlier_user_messages"] == [
        {"index": 0, "text": "Design the power tree"}
    ]
    assert [m.content for m in _context(payload["messages"], payload["context_cut"])] == []
    assert values[COMPACTION_KEY]["trigger"] == "auto"
    assert values[USAGE_KEY]["context_tokens"] == 100
    # The compaction events are recorded once, so a reopened chat shows the marker.
    recorded = [e for e in values["copper_events"] if e["type"] == COMPACTION_EVENT]
    assert [e["data"]["stage"] for e in recorded] == ["started", "completed"]


@pytest.mark.asyncio
async def test_auto_compaction_failure_sends_the_turn_unchanged(graph_env) -> None:
    agent = GraphAgent(
        [
            [CopperEvent(EventKind.TEXT, "First."), final(context_usage=usage(2000))],
            [CopperEvent(EventKind.TEXT, "Second."), final()],
        ],
        compactions=[ProtocolError("This CopperPilot server does not support it yet.")],
        workspace=graph_env,
    )
    await turn(agent, graph_env, say("one"))
    events, values = await turn(agent, graph_env, say("two"))
    (failed,) = [
        e for e in events if e["type"] == COMPACTION_EVENT and e["data"]["stage"] == "failed"
    ]
    assert "sending the chat unchanged" in failed["title"]
    assert "context_cut" not in agent.payloads[-1]
    assert values.get(COMPACTION_KEY) is None
    assert values["messages"][-1].content == "Second."


@pytest.mark.asyncio
async def test_resume_attempts_never_auto_compact(graph_env, monkeypatch) -> None:
    monkeypatch.setattr(copper_graph, "MAX_AUTO_RESUMES", 0)
    agent = GraphAgent(
        [
            [CopperEvent(EventKind.TEXT, "First."), final(context_usage=usage(2000))],
            [CopperEvent(EventKind.TEXT, "Half"), DROP],
            [CopperEvent(EventKind.TEXT, "Still half"), DROP],
            [CopperEvent(EventKind.TEXT, "Done."), final()],
        ],
        compactions=[ProtocolError("unavailable")],
        workspace=graph_env,
    )
    await turn(agent, graph_env, say("one"))
    # The second turn: auto-compaction runs (no unfinished turn yet), then the turn drops.
    await turn(agent, graph_env, say("two"))
    assert len(agent.compact_requests) == 1
    # Resuming that turn, by /resume or a "continue" message, never compacts.
    await turn(agent, graph_env, {"copper_resume": {"reason": "manual"}})
    await turn(agent, graph_env, say("Continue where you left off."))
    assert len(agent.compact_requests) == 1
    assert [p.get("resume", {}).get("reason") for p in agent.payloads[2:]] == ["manual", "manual"]
    assert agent.payloads[2]["session_state"] == agent.payloads[3]["session_state"]


@pytest.mark.asyncio
async def test_new_message_after_an_interrupted_turn_auto_compacts_first(graph_env) -> None:
    """u1 is interrupted at 153K (threshold 120K); a new message compacts before it is sent."""
    big = {**usage(153_131, at=120_000), "next_turn_tokens": 130_000}
    agent = GraphAgent(
        [
            [
                CopperEvent(EventKind.TEXT, "Surveying the board."),
                *ask("q1", "Which module?", REJECTED),
                *tool_call("r1", "read_file", {"file_path": "board.kicad_sch"}, "(kicad_sch)"),
                CopperEvent(EventKind.STATUS_EVENT, {"kind": "context_usage", **big}),
                HANG,
            ],
            [CopperEvent(EventKind.TEXT, "Placed."), final(context_usage=usage(40_000))],
        ],
        compactions=[reply(2, trigger="auto", pre_tokens=153_131, post_tokens=9_000)],
        workspace=graph_env,
    )
    await interrupted_turn(agent, graph_env, say("Build me an ESP32 Dev board"))
    events, values = await turn(agent, graph_env, say("Decisions: 2-layer, 50x30 mm"))

    (request,) = agent.compact_requests
    assert request["trigger"] == "auto"
    # The interrupted turn's work comes from its journal; the new message ends the context.
    assert [entry["role"] for entry in request["context"]] == ["user", "assistant", "user"]
    work = request["context"][1]
    assert work["content"].startswith("Surveying the board.")
    assert [digest["name"] for digest in work["tool_digest"]] == ["ask_user_question", "read_file"]
    assert request["context"][2]["content"] == "Decisions: 2-layer, 50x30 mm"
    stages = [e["data"]["stage"] for e in events if e["type"] == COMPACTION_EVENT]
    assert stages == ["started", "completed"]

    payload = agent.payloads[-1]
    assert payload["context_cut"] == values["messages"][0].id
    assert _context(payload["messages"], payload["context_cut"]) == []
    state = payload["session_state"]
    assert state["compaction"]["post_tokens"] == 9_000
    assert state["earlier_user_messages"] == [{"index": 0, "text": "Build me an ESP32 Dev board"}]
    # The carried facts of the interrupted turn are still pinned.
    assert state["ask_answers"] == [
        {"question": "Which module?", "answer": "", "status": "skipped"}
    ]
    assert values[COMPACTION_KEY]["trigger"] == "auto"


def test_estimate_prefers_next_turn_tokens() -> None:
    values = {
        "messages": [HumanMessage(content="x" * 40)],
        USAGE_KEY: {"context_tokens": 153_131, "next_turn_tokens": 30_000, "measured_at": 0},
    }
    assert context_estimate(values) == 30_010
    assert (
        parse_context_usage({"context_tokens": 5, "next_turn_tokens": 3})["next_turn_tokens"] == 3
    )


@pytest.mark.asyncio
async def test_resume_resends_the_same_session_state_and_cut(graph_env, monkeypatch) -> None:
    monkeypatch.setattr(copper_graph, "MAX_AUTO_RESUMES", 1)
    agent = GraphAgent(
        [
            [CopperEvent(EventKind.TEXT, "ok"), final()],
            [
                CopperEvent(
                    EventKind.STATUS_EVENT,
                    {"kind": "todo_list", "items": [{"content": "Route", "status": "pending"}]},
                ),
                DROP,
            ],
            [DROP],
            [CopperEvent(EventKind.TEXT, "done"), final()],
        ],
        workspace=graph_env,
    )
    await turn(agent, graph_env, say("one"))
    _events, values = await turn(agent, graph_env, say("two"))
    assert values["copper_turn_journal"]["session_state"] == agent.payloads[1]["session_state"]
    # The todo list arrived during the turn; the next attempts keep the turn-start snapshot.
    await turn(agent, graph_env, {"copper_resume": {"reason": "manual"}})
    states = [payload["session_state"] for payload in agent.payloads[1:]]
    assert states[0] == states[1] == states[2] == {"version": 1}
    assert agent.payloads[-1]["resume"]["reason"] == "manual"


@pytest.mark.asyncio
async def test_in_turn_compaction_from_the_final_covers_the_sent_history(graph_env) -> None:
    compaction = reply(1)["compaction"] | {"trigger": "in_turn"}
    agent = GraphAgent(
        [
            [CopperEvent(EventKind.TEXT, "First."), final()],
            [CopperEvent(EventKind.TEXT, "Second."), final(compaction=compaction)],
            [CopperEvent(EventKind.TEXT, "Third."), final()],
        ],
        workspace=graph_env,
    )
    await turn(agent, graph_env, say("one"))
    events, values = await turn(agent, graph_env, say("two"))
    compacted = [
        e
        for e in events
        if e["type"] == "copper.status_event" and e["data"].get("kind") == "context_compacted"
    ]
    assert len(compacted) == 1
    record = values[COMPACTION_KEY]
    # Everything sent with turn two, i.e. through the first answer.
    assert record["last_message_id"] == values["messages"][1].id
    assert record["trigger"] == "in_turn"
    await turn(agent, graph_env, say("three"))
    payload = agent.payloads[-1]
    assert [m.content for m in _context(payload["messages"], payload["context_cut"])] == [
        "two",
        "Second.",
    ]
    assert payload["session_state"]["compaction"]["trigger"] == "in_turn"


# --------------------------------------------------------- interrupted turns


REJECTED = {
    "error": "ToolRejected: Interactive clarification is unavailable in headless and MCP runs.",
    "rejected": True,
}


def ask(call_id: str, question: str, result: Any) -> list[CopperEvent]:
    return [
        CopperEvent(
            EventKind.TOOL_REQUEST,
            ToolRequest(
                tool_call_id=call_id,
                tool_name="ask_user_question",
                arguments={"question": question, "options": ["A", "B"]},
            ),
        ),
        CopperEvent(
            EventKind.TOOL_RESULT,
            {"tool_call_id": call_id, "tool_name": "ask_user_question", "result": result},
        ),
    ]


def tool_call(call_id: str, name: str, arguments: dict[str, Any], result: Any) -> list[CopperEvent]:
    return [
        CopperEvent(
            EventKind.TOOL_REQUEST,
            ToolRequest(tool_call_id=call_id, tool_name=name, arguments=arguments),
        ),
        CopperEvent(
            EventKind.TOOL_RESULT, {"tool_call_id": call_id, "tool_name": name, "result": result}
        ),
    ]


def plan_write(call_id: str = "w1") -> list[CopperEvent]:
    arguments = {"file_path": "/.copperpilot/plans/esp32.plan.md", "content": PLAN}
    return [
        CopperEvent(
            EventKind.TOOL_REQUEST,
            ToolRequest(tool_call_id=call_id, tool_name="write_file", arguments=arguments),
        ),
        CopperEvent(
            EventKind.TOOL_RESULT,
            {"tool_call_id": call_id, "tool_name": "write_file", "result": "Updated file"},
        ),
    ]


interrupt = HANG


async def interrupted_turn(agent, workspace, values) -> None:
    """A turn cut off by Ctrl-C/SIGINT: the task is cancelled and the node never returns."""
    agent.hanging.clear()
    task = asyncio.create_task(turn(agent, workspace, values))
    await asyncio.wait_for(agent.hanging.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_interrupted_turn_facts_reach_the_next_session_state(graph_env) -> None:
    plans = graph_env / ".copperpilot" / "plans"
    plans.mkdir(parents=True)
    (plans / "esp32.plan.md").write_text(PLAN, encoding="utf-8")
    todo = {"kind": "todo_list", "items": [{"content": "Route USB", "status": "pending"}]}
    agent = GraphAgent(
        [
            [
                *ask("q1", "Which ESP32 module?", REJECTED),
                *ask("q2", "Which LDO?", {"status": "answered", "answer": "AMS1117"}),
                *plan_write(),
                CopperEvent(EventKind.STATUS_EVENT, todo),
                CopperEvent(EventKind.STATUS_EVENT, {"kind": "context_usage", **usage(61_000)}),
                CopperEvent(EventKind.STATUS_EVENT, {"kind": "plan_mode_switch"}),
                interrupt,
            ],
            [CopperEvent(EventKind.TEXT, "Continuing."), final()],
            [CopperEvent(EventKind.TEXT, "Third."), final()],
        ],
        workspace=graph_env,
    )
    await interrupted_turn(agent, graph_env, say("Build me an ESP32 dev board"))
    # Only the user's message reached the checkpoint; the rest lives in the journal.
    async with create_copper_graph(agent) as runtime:
        state = await runtime.aget_state(runtime.config("t", graph_env))
    assert [m.type for m in state.values["messages"]] == ["human"]

    _events, values = await turn(agent, graph_env, say("Use a USB-C connector"))
    expected_answers = [
        {"question": "Which ESP32 module?", "answer": "", "status": "skipped"},
        {"question": "Which LDO?", "answer": "AMS1117", "status": "answered"},
    ]
    state = agent.payloads[1]["session_state"]
    assert state["ask_answers"] == expected_answers
    assert state["todos"] == {"items": [{"content": "Route USB", "status": "pending"}]}
    assert state["plan"]["file_path"] == ".copperpilot/plans/esp32.plan.md"
    # The interrupted turn's context size is the newest measurement.
    assert values[FACTS_KEY]["context_usage"]["context_tokens"] == 61_000

    # Once the next turn completed, the facts are in the checkpoint for good,
    # even though the interrupted turn's journal is gone.
    await turn(agent, graph_env, say("Now route it"))
    later = agent.payloads[2]["session_state"]
    assert later["ask_answers"] == expected_answers
    assert later["todos"] == state["todos"] and later["plan"] == state["plan"]


@pytest.mark.asyncio
async def test_facts_chain_through_consecutive_interrupted_turns(graph_env) -> None:
    agent = GraphAgent(
        [
            [*ask("q1", "Which module?", REJECTED), interrupt],
            [*ask("q2", "Which LDO?", {"status": "answered", "answer": "AP2112"}), interrupt],
            [CopperEvent(EventKind.TEXT, "ok"), final()],
        ],
        workspace=graph_env,
    )
    await interrupted_turn(agent, graph_env, say("one"))
    await interrupted_turn(agent, graph_env, say("two"))
    await turn(agent, graph_env, say("three"))
    assert agent.payloads[2]["session_state"]["ask_answers"] == [
        {"question": "Which module?", "answer": "", "status": "skipped"},
        {"question": "Which LDO?", "answer": "AP2112", "status": "answered"},
    ]


@pytest.mark.asyncio
async def test_a_turn_with_only_a_rejected_question_records_it_as_skipped(graph_env) -> None:
    agent = GraphAgent(
        [
            [*ask("q1", "Which module?", REJECTED), CopperEvent(EventKind.TEXT, "ok"), final()],
            [CopperEvent(EventKind.TEXT, "ok"), final()],
        ],
        workspace=graph_env,
    )
    await turn(agent, graph_env, say("one"))
    await turn(agent, graph_env, say("two"))
    assert agent.payloads[1]["session_state"]["ask_answers"] == [
        {"question": "Which module?", "answer": "", "status": "skipped"}
    ]


@pytest.mark.asyncio
async def test_unfinished_turn_in_the_checkpoint_is_not_counted_twice(
    graph_env, monkeypatch
) -> None:
    monkeypatch.setattr(copper_graph, "MAX_AUTO_RESUMES", 0)
    agent = GraphAgent(
        [
            [*ask("q1", "Which module?", {"status": "answered", "answer": "S3"}), DROP],
            [CopperEvent(EventKind.TEXT, "ok"), final()],
        ],
        workspace=graph_env,
    )
    await turn(agent, graph_env, say("one"))
    await turn(agent, graph_env, say("two"))
    assert agent.payloads[1]["session_state"]["ask_answers"] == [
        {"question": "Which module?", "answer": "S3", "status": "answered"}
    ]


def test_session_state_failure_never_fails_the_turn(monkeypatch) -> None:
    def broken(*_args, **_kwargs):
        raise RuntimeError("bad plan")

    monkeypatch.setattr(copper_graph, "build_session_state", broken)
    agent = type("Agent", (), {"workspace": None, "plan_context": None})()
    assert copper_graph._session_state(agent, {"messages": []}) == {"version": 1}


@pytest.mark.asyncio
async def test_history_after_interrupted_turns_new_message_versus_continue(graph_env) -> None:
    """New messages after interrupted turns send all earlier user messages; a
    "continue …" message instead resumes the unfinished turn by resending its
    original request (query = that turn's message, context = history before it)."""
    agent = GraphAgent(
        [
            [HANG],
            [HANG],
            [HANG],
            [CopperEvent(EventKind.TEXT, "Resumed."), final()],
        ],
        workspace=graph_env,
    )
    await interrupted_turn(agent, graph_env, say("Build me an ESP32 Dev board"))
    await interrupted_turn(agent, graph_env, say("Decisions: 2-layer, 50x30 mm"))
    await interrupted_turn(agent, graph_env, say("Add a USB-C connector"))
    sent = [_context(p["messages"], p.get("context_cut")) for p in agent.payloads]
    assert [len(context) for context in sent] == [0, 1, 2]
    assert [m.content for m in sent[2]] == [
        "Build me an ESP32 Dev board",
        "Decisions: 2-layer, 50x30 mm",
    ]
    await turn(agent, graph_env, say("Continue with the build."))
    resumed = agent.payloads[-1]
    assert resumed["resume"]["reason"] == "manual"
    assert resumed["messages"][-1].content == "Add a USB-C connector"
    assert len(_context(resumed["messages"], resumed.get("context_cut"))) == 2
