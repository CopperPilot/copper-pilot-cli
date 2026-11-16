"""A scripted stand-in for ``codex app-server`` (JSON-RPC over stdio, one object per line).

``FAKE_CODEX_LOG`` names a file that receives every client message (JSON
lines). ``FAKE_CODEX_SCENARIO`` picks the turn: ``ok`` (default), ``auth``,
``usage``, ``exit`` or ``input``.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

LOG = os.environ.get("FAKE_CODEX_LOG")
SCENARIO = os.environ.get("FAKE_CODEX_SCENARIO", "ok")
THREAD = "thread-1"
TURN = "turn-1"
_next = 1000


def log(message: dict[str, Any]) -> None:
    if LOG:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(message) + "\n")


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def read() -> dict[str, Any]:
    line = sys.stdin.readline()
    if not line:
        raise SystemExit(0)
    message = json.loads(line)
    log(message)
    return message


def notify(method: str, params: dict[str, Any]) -> None:
    send({"method": method, "params": {"threadId": THREAD, "turnId": TURN, **params}})


def request(method: str, params: dict[str, Any]) -> Any:
    """Ask the client and wait for its answer (other messages are logged)."""
    global _next
    _next += 1
    identifier = _next
    send({"id": identifier, "method": method, "params": params})
    while True:
        message = read()
        if message.get("id") == identifier and "method" not in message:
            return message.get("result")


def run_turn() -> None:
    if SCENARIO == "exit":
        notify("item/agentMessage/delta", {"itemId": "m1", "delta": "Starting"})
        sys.stdout.flush()
        os._exit(3)
    if SCENARIO == "auth":
        notify(
            "turn/completed",
            {
                "turn": {
                    "id": TURN,
                    "items": [],
                    "status": "failed",
                    "error": {"message": "401 Unauthorized", "codexErrorInfo": "unauthorized"},
                }
            },
        )
        return
    if SCENARIO == "usage":
        send(
            {
                "method": "account/rateLimits/updated",
                "params": {"rateLimits": {"primary": {"usedPercent": 100, "resetsAt": 1893456000}}},
            }
        )
        notify(
            "turn/completed",
            {
                "turn": {
                    "id": TURN,
                    "items": [],
                    "status": "failed",
                    "error": {"message": "limit", "codexErrorInfo": "usageLimitExceeded"},
                }
            },
        )
        return
    if SCENARIO == "input":
        answer = request(
            "item/tool/requestUserInput",
            {
                "threadId": THREAD,
                "turnId": TURN,
                "itemId": "q1",
                "isBlocking": True,
                "questions": [
                    {
                        "id": "board",
                        "header": "Board",
                        "question": "Which board?",
                        "options": [{"label": "Rev A", "description": ""}],
                    }
                ],
            },
        )
        notify("item/agentMessage/delta", {"itemId": "m1", "delta": json.dumps(answer)})
        notify("turn/completed", {"turn": {"id": TURN, "items": [], "status": "completed"}})
        return
    notify(
        "item/reasoning/summaryTextDelta", {"itemId": "r1", "delta": "Thinking", "summaryIndex": 0}
    )
    notify("item/agentMessage/delta", {"itemId": "m1", "delta": "Hello "})
    notify("item/agentMessage/delta", {"itemId": "m1", "delta": "board"})
    notify(
        "item/started",
        {
            "item": {
                "type": "commandExecution",
                "id": "c1",
                "command": "ls",
                "status": "inProgress",
                "commandActions": [],
                "cwd": "/",
            }
        },
    )
    decision = request(
        "item/commandExecution/requestApproval",
        {"threadId": THREAD, "turnId": TURN, "itemId": "c1", "command": "ls", "startedAtMs": 0},
    )
    notify(
        "item/completed",
        {
            "item": {
                "type": "commandExecution",
                "id": "c1",
                "command": "ls",
                "status": "completed" if decision.get("decision") == "accept" else "declined",
                "exitCode": 0 if decision.get("decision") == "accept" else None,
                "aggregatedOutput": "board.kicad_sch\n",
                "commandActions": [],
                "cwd": "/",
            }
        },
    )
    notify(
        "item/started",
        {
            "item": {
                "type": "mcpToolCall",
                "id": "t1",
                "server": "copperpilot",
                "tool": "query_design",
                "arguments": {"query": "U1"},
                "status": "inProgress",
            }
        },
    )
    notify(
        "item/completed",
        {
            "item": {
                "type": "mcpToolCall",
                "id": "t1",
                "server": "copperpilot",
                "tool": "query_design",
                "arguments": {"query": "U1"},
                "status": "completed",
                "result": {"content": [{"type": "text", "text": "U1 is an ESP32"}]},
            }
        },
    )
    notify(
        "item/started",
        {
            "item": {
                "type": "mcpToolCall",
                "id": "s1",
                "server": "copperpilot",
                "tool": "copperpilot_subagent",
                "arguments": {"task": "Add a decoupling cap"},
                "status": "inProgress",
            }
        },
    )
    notify("item/mcpToolCall/progress", {"itemId": "s1", "message": "editing"})
    notify(
        "item/completed",
        {
            "item": {
                "type": "mcpToolCall",
                "id": "s1",
                "server": "copperpilot",
                "tool": "copperpilot_subagent",
                "arguments": {"task": "Add a decoupling cap"},
                "status": "completed",
                "result": {"content": [{"type": "text", "text": "Added C9"}]},
            }
        },
    )
    notify("turn/plan/updated", {"plan": [{"step": "Check U1", "status": "completed"}]})
    notify(
        "thread/tokenUsage/updated",
        {
            "tokenUsage": {
                "last": {
                    "inputTokens": 120,
                    "outputTokens": 30,
                    "cachedInputTokens": 100,
                    "reasoningOutputTokens": 0,
                    "totalTokens": 150,
                },
                "total": {
                    "inputTokens": 120,
                    "outputTokens": 30,
                    "cachedInputTokens": 100,
                    "reasoningOutputTokens": 0,
                    "totalTokens": 150,
                },
                "modelContextWindow": 272000,
            }
        },
    )
    notify("turn/completed", {"turn": {"id": TURN, "items": [], "status": "completed"}})


def main() -> None:
    while True:
        message = read()
        method = message.get("method")
        identifier = message.get("id")
        if method == "initialize":
            send(
                {
                    "id": identifier,
                    "result": {
                        "userAgent": "fake",
                        "codexHome": "/codex-home",
                        "platformFamily": "unix",
                        "platformOs": "linux",
                    },
                }
            )
        elif method == "thread/start":
            send(
                {
                    "id": identifier,
                    "result": {
                        "thread": {"id": THREAD},
                        "model": "gpt-fake",
                        "modelProvider": "openai",
                        "cwd": "/",
                        "approvalPolicy": "on-request",
                        "approvalsReviewer": "user",
                        "sandbox": {},
                    },
                }
            )
        elif method == "turn/start":
            send(
                {
                    "id": identifier,
                    "result": {"turn": {"id": TURN, "items": [], "status": "inProgress"}},
                }
            )
            run_turn()
        elif method == "turn/interrupt":
            send({"id": identifier, "result": {}})
        elif method == "account/read":
            send(
                {
                    "id": identifier,
                    "result": {
                        "account": {"type": "chatgpt", "email": None, "planType": "plus"},
                        "requiresOpenaiAuth": True,
                    },
                }
            )
        elif method == "model/list":
            send(
                {
                    "id": identifier,
                    "result": {
                        "data": [
                            {
                                "id": "gpt-fake",
                                "model": "gpt-fake",
                                "displayName": "GPT Fake",
                                "description": "",
                                "hidden": False,
                                "isDefault": True,
                                "defaultReasoningEffort": "medium",
                                "supportedReasoningEfforts": [
                                    {"reasoningEffort": "low", "description": ""},
                                    {"reasoningEffort": "medium", "description": ""},
                                ],
                            }
                        ],
                        "nextCursor": None,
                    },
                }
            )
        elif identifier is not None and method is not None:
            send({"id": identifier, "result": {}})


if __name__ == "__main__":
    main()
