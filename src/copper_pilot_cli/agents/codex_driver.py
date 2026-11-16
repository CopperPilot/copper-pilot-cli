"""Codex: the user's own ``codex app-server`` over stdio JSON-RPC (no extra dependency).

``initialize {clientInfo: {name: "CopperPilot CLI"}}`` → ``thread/start
{ephemeral: true, cwd, developerInstructions: <guide>, config: {mcp_servers:
{copperpilot: ...}, features: {multi_agent: true}}}`` → ``turn/start`` per
turn. Item notifications become CopperPilot events; approvals, user input
and MCP elicitations go to the TUI. Codex's own login (``codex login``, or
``OPENAI_API_KEY``) is used as is: we never read ``auth.json``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from copper_pilot_cli._version import __version__
from copper_pilot_cli.agents import AgentSelection, Provider
from copper_pilot_cli.agents import errors as agent_errors
from copper_pilot_cli.agents.catalog import AgentModel, codex_models
from copper_pilot_cli.agents.claude_driver import AgentStatus
from copper_pilot_cli.agents.jsonrpc import (
    JsonRpcError,
    JsonRpcProcess,
    MethodNotFound,
    ProcessExited,
)
from copper_pilot_cli.agents.mcp_server import RELAY_TOKEN_ENV, ProxyBackend, RelayServer
from copper_pilot_cli.agents.session import (
    AgentSession,
    TurnContext,
    TurnQueue,
    TurnUsage,
    context_event,
    final_event,
    plan_event,
    subagent_event,
    todo_event,
    tool_request_event,
    tool_result_event,
)
from copper_pilot_cli.agents.tool_proxy import SUBAGENT_TOOL, ToolProxy
from copper_pilot_cli.copper_config import API_URL_ENV, APP_HOME_ENV
from copper_pilot_cli.copper_protocol import CopperEvent, CopperMode, EventKind
from copper_pilot_cli.copper_tools import (
    ApprovalMode,
    ApprovalRequest,
    LocalToolBroker,
    routine_action,
)

logger = logging.getLogger(__name__)

CODEX_BINARY = "codex"
CLIENT_NAME = "CopperPilot CLI"
MCP_SERVER_NAME = "copperpilot"
TOOL_TIMEOUT_SECONDS = 900
_STATUS_TIMEOUT = 20.0
_PLAN_LABELS = {
    "plus": "ChatGPT Plus",
    "pro": "ChatGPT Pro",
    "prolite": "ChatGPT Pro Lite",
    "promax": "ChatGPT Pro Max",
    "team": "ChatGPT Team",
    "business": "ChatGPT Business",
    "enterprise": "ChatGPT Enterprise",
    "edu": "ChatGPT Edu",
    "free": "ChatGPT Free",
    "go": "ChatGPT Go",
}


def find_codex() -> str | None:
    return shutil.which(CODEX_BINARY)


def client_info() -> dict[str, Any]:
    return {"name": CLIENT_NAME, "title": CLIENT_NAME, "version": __version__}


def app_server_argv(codex: str) -> list[str]:
    return [codex, "app-server", "-c", "thread_unload_delay_secs=0"]


def mcp_server_config(relay_address: str, relay_token: str) -> dict[str, Any]:
    """``mcp_servers.copperpilot``: this CLI's ``mcp --tools-session``, relayed to us."""
    env = {RELAY_TOKEN_ENV: relay_token}
    for name in (APP_HOME_ENV, API_URL_ENV):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return {
        "command": sys.executable,
        "args": ["-m", "copper_pilot_cli", "mcp", "--tools-session", "--relay", relay_address],
        "env": env,
        "tool_timeout_sec": TOOL_TIMEOUT_SECONDS,
        "startup_timeout_sec": 60,
    }


def turn_policy(approval: ApprovalMode, mode: CopperMode) -> dict[str, Any]:
    """``approvalPolicy`` and ``sandboxPolicy`` for one turn (T3's runtime-mode table)."""
    if mode is not CopperMode.AGENT:
        return {"approvalPolicy": "on-request", "sandboxPolicy": {"type": "readOnly"}}
    if approval is ApprovalMode.YOLO:
        return {
            "approvalPolicy": {
                "granular": {
                    "mcp_elicitations": True,
                    "request_permissions": False,
                    "rules": False,
                    "sandbox_approval": False,
                    "skill_approval": False,
                }
            },
            "sandboxPolicy": {"type": "dangerFullAccess"},
        }
    if approval is ApprovalMode.AUTO:
        return {"approvalPolicy": "on-request", "sandboxPolicy": {"type": "workspaceWrite"}}
    return {"approvalPolicy": "untrusted", "sandboxPolicy": {"type": "readOnly"}}


def account_label(payload: Any) -> tuple[bool | None, str]:
    """(logged_in, label) from ``account/read``."""
    if not isinstance(payload, dict):
        return None, "login status unknown"
    account = payload.get("account")
    if isinstance(account, dict):
        kind = account.get("type")
        if kind == "chatgpt":
            plan = str(account.get("planType") or "")
            return True, f"Authenticated · {_PLAN_LABELS.get(plan, 'ChatGPT ' + plan).strip()}"
        if kind == "apiKey":
            return True, "Authenticated · API key"
        return True, f"Authenticated · {kind}"
    if payload.get("requiresOpenaiAuth") is True:
        return False, "not logged in: run `codex login` (or set OPENAI_API_KEY)"
    return True, "Authenticated"


async def _probe(method: str, params: dict[str, Any]) -> Any:
    codex = find_codex()
    if codex is None:
        return None
    rpc = JsonRpcProcess(app_server_argv(codex))
    await rpc.start()
    try:
        await rpc.request(
            "initialize",
            {"clientInfo": client_info(), "capabilities": {"experimentalApi": True}},
            timeout=_STATUS_TIMEOUT,
        )
        await rpc.notify("initialized")
        return await rpc.request(method, params, timeout=_STATUS_TIMEOUT)
    finally:
        await rpc.aclose()


async def codex_status() -> AgentStatus:
    path = find_codex()
    if path is None:
        return AgentStatus(False, None, "not installed (`codex` not on PATH)")
    try:
        payload = await _probe("account/read", {"refreshToken": False})
    except (JsonRpcError, ProcessExited, TimeoutError, OSError) as exc:
        return AgentStatus(
            True, None, f"installed; status unavailable ({type(exc).__name__})", path
        )
    logged_in, label = account_label(payload)
    return AgentStatus(True, logged_in, label, path, detail="" if logged_in else "auth")


async def list_codex_models() -> list[AgentModel]:
    """Models from the user's own Codex (``model/list``)."""
    try:
        payload = await _probe("model/list", {"includeHidden": False})
    except (JsonRpcError, ProcessExited, TimeoutError, OSError) as exc:
        logger.info("codex model/list failed (%s)", type(exc).__name__)
        return []
    return codex_models(payload)


def _error_info_kind(info: Any) -> str | None:
    if isinstance(info, str):
        return info
    if isinstance(info, dict) and info:
        return next(iter(info))
    return None


class CodexSession(AgentSession):
    """One ``codex app-server`` process with one ephemeral thread."""

    provider = Provider.CODEX

    def __init__(
        self,
        selection: AgentSelection,
        *,
        workspace: Path,
        proxy: ToolProxy,
        broker: Callable[[], LocalToolBroker],
        codex_path: str | None = None,
        argv: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        super().__init__(selection)
        self.workspace = workspace
        self.proxy = proxy
        self._broker = broker
        self.codex_path = codex_path
        self.argv = argv
        self.env = env
        self.relay = RelayServer(ProxyBackend(proxy))
        self.rpc: JsonRpcProcess | None = None
        self.thread_id: str | None = None
        self.model: str | None = selection.model
        self.window: int | None = None
        self._turn: TurnQueue | None = None
        self._turn_id: str | None = None
        self._translator: _Translator | None = None
        self._mode = CopperMode.AGENT
        self._file_changes: dict[str, list[str]] = {}
        self._rate_limits: dict[str, Any] = {}
        self._prompt_lock = asyncio.Lock()
        self._exit_watch: asyncio.Task[None] | None = None

    @property
    def context_window(self) -> int | None:
        return self.window

    @property
    def alive(self) -> bool:
        return not self.closed and (self.rpc is None or self.rpc.running)

    async def start(self) -> None:
        if self.rpc is not None:
            return
        argv = self.argv
        if argv is None:
            codex = self.codex_path or find_codex()
            if codex is None:
                raise agent_errors.not_installed(Provider.CODEX, CODEX_BINARY)
            argv = app_server_argv(codex)
        await self.proxy.start()
        address = await self.relay.start()
        self.rpc = JsonRpcProcess(
            argv,
            on_request=self._on_request,
            on_notification=self._on_notification,
            env=self.env,
            cwd=str(self.workspace),
        )
        try:
            await self.rpc.start()
        except OSError as exc:
            raise agent_errors.not_installed(Provider.CODEX, argv[0]) from exc
        try:
            await self.rpc.request(
                "initialize",
                {"clientInfo": client_info(), "capabilities": {"experimentalApi": True}},
                timeout=60,
            )
            await self.rpc.notify("initialized")
            params: dict[str, Any] = {
                "ephemeral": True,
                "cwd": str(self.workspace),
                # The guide: RAM only, never logged or stored.
                "developerInstructions": self.proxy.guide or None,
                "config": {
                    "mcp_servers": {MCP_SERVER_NAME: mcp_server_config(address, self.relay.token)},
                    "features": {"multi_agent": True},
                },
                **turn_policy(self._broker().mode, CopperMode.AGENT),
            }
            params["sandbox"] = {
                "readOnly": "read-only",
                "workspaceWrite": "workspace-write",
                "dangerFullAccess": "danger-full-access",
            }[params.pop("sandboxPolicy")["type"]]
            if self.selection.model:
                params["model"] = self.selection.model
            response = await self.rpc.request("thread/start", params, timeout=120)
        except JsonRpcError as exc:
            await self.aclose()
            raise self._classify(str(exc), exc.data) from exc
        except (ProcessExited, TimeoutError) as exc:
            await self.aclose()
            detail = self.rpc.stderr_tail if self.rpc is not None else ""
            raise agent_errors.process_exited(
                Provider.CODEX, getattr(exc, "returncode", None), detail
            ) from exc
        thread = response.get("thread") if isinstance(response, dict) else None
        self.thread_id = str((thread or {}).get("id") or "")
        if isinstance(response, dict) and isinstance(response.get("model"), str):
            self.model = response["model"]
        if not self.thread_id:
            await self.aclose()
            raise agent_errors.AgentError(
                agent_errors.PROTOCOL, "Codex did not start a thread.", provider=Provider.CODEX
            )

    def _classify(self, message: str, data: Any = None) -> agent_errors.AgentError:
        lowered = message.lower()
        if "login" in lowered or "auth" in lowered or "unauthorized" in lowered:
            return agent_errors.auth_required(Provider.CODEX, message)
        del data
        return agent_errors.classify_message(Provider.CODEX, message)

    # ---------------------------------------------------------------- turns

    async def run_turn(self, prompt: str, context: TurnContext) -> AsyncIterator[CopperEvent]:
        await self.start()
        assert self.rpc is not None and self.thread_id
        if not self.rpc.running:
            raise agent_errors.process_exited(Provider.CODEX, None, self.rpc.stderr_tail)
        self._mode = context.mode
        turn = TurnQueue()
        translator = _Translator(self)
        self._turn, self._translator = turn, translator
        self.proxy.emit = turn.put
        params: dict[str, Any] = {
            "threadId": self.thread_id,
            "input": [{"type": "text", "text": prompt, "text_elements": []}],
            **turn_policy(context.broker.mode, context.mode),
        }
        if self.selection.model:
            params["model"] = self.selection.model
        if self.selection.effort:
            params["effort"] = self.selection.effort
        watch = asyncio.create_task(self._watch_exit(turn))
        finished = False
        try:
            try:
                response = await self.rpc.request("turn/start", params, timeout=120)
            except JsonRpcError as exc:
                raise self._classify(str(exc), exc.data) from exc
            except ProcessExited as exc:
                raise agent_errors.process_exited(
                    Provider.CODEX, exc.returncode, exc.stderr_tail
                ) from exc
            identifier = ((response or {}).get("turn") or {}).get("id")
            self._turn_id = str(identifier) if identifier else None
            translator.started(self._turn_id)
            async for event in turn.events():
                yield event
            finished = True
        finally:
            watch.cancel()
            self.proxy.emit = None
            self._turn = None
            if not finished:
                await self.interrupt()
        if translator.failure is not None:
            raise translator.failure
        usage = translator.usage
        await self.proxy.session.send_usage(
            provider=self.provider.value,
            model=self.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            turn_id=context.turn_id,
        )
        event = context_event(usage)
        if event is not None:
            yield event
        yield final_event(usage, model=self.model)

    async def _watch_exit(self, turn: TurnQueue) -> None:
        assert self.rpc is not None and self.rpc.exited is not None
        code = await asyncio.shield(self.rpc.exited)
        turn.fail(agent_errors.process_exited(Provider.CODEX, code, self.rpc.stderr_tail))

    async def compact(self) -> str:
        """Codex's own compaction (``thread/compact/start``)."""
        if self.rpc is None or not self.rpc.running or not self.thread_id:
            return "There is no live Codex session to compact."
        try:
            await self.rpc.request(
                "thread/compact/start", {"threadId": self.thread_id}, timeout=600
            )
        except (JsonRpcError, ProcessExited, TimeoutError) as exc:
            return f"Compaction failed: {exc}"
        return "Codex compacted its context."

    async def interrupt(self) -> None:
        if self.rpc is None or not self.rpc.running or not self.thread_id or not self._turn_id:
            return
        with contextlib.suppress(Exception):
            await self.rpc.request(
                "turn/interrupt", {"threadId": self.thread_id, "turnId": self._turn_id}, timeout=5
            )

    async def aclose(self) -> None:
        self.closed = True
        if self.rpc is not None:
            await self.rpc.aclose()
        await self.relay.aclose()

    # -------------------------------------------------------- notifications

    def _on_notification(self, method: str, params: Any) -> None:
        if not isinstance(params, dict):
            return
        if method == "account/rateLimits/updated":
            limits = params.get("rateLimits")
            self._rate_limits = limits if isinstance(limits, dict) else {}
            return
        if method == "thread/tokenUsage/updated" and isinstance(params.get("tokenUsage"), dict):
            window = params["tokenUsage"].get("modelContextWindow")
            if isinstance(window, int) and window > 0:
                self.window = window
        translator, turn = self._translator, self._turn
        if translator is None or turn is None:
            return
        if params.get("threadId") not in {None, self.thread_id}:
            return
        for event in translator.feed(method, params):
            turn.put(event)
        if translator.done:
            turn.end()

    def usage_limit_reset(self) -> str | None:
        windows = [self._rate_limits.get("primary"), self._rate_limits.get("secondary")]
        resets = [
            item.get("resetsAt")
            for item in windows
            if isinstance(item, dict) and (item.get("usedPercent") or 0) >= 100
        ]
        resets = [value for value in resets if isinstance(value, int)]
        return agent_errors.epoch_to_iso(max(resets)) if resets else None

    # ------------------------------------------------------------- requests

    async def _on_request(self, method: str, params: Any) -> Any:
        params = params if isinstance(params, dict) else {}
        broker = self._broker()
        if method == "item/commandExecution/requestApproval":
            return {"decision": await self._approve_command(broker, params)}
        if method == "item/fileChange/requestApproval":
            return {"decision": await self._approve_file_change(broker, params)}
        if method == "item/permissions/requestApproval":
            approved = await self._ask(
                broker,
                ApprovalRequest(
                    tool_call_id=str(params.get("itemId") or "permissions"),
                    tool_name="permissions",
                    arguments={"permissions": params.get("permissions")},
                    reason=str(params.get("reason") or "Codex asks for more permissions"),
                ),
            )
            return {
                "permissions": params.get("permissions") if approved else {},
                "scope": "turn",
            }
        if method == "item/tool/requestUserInput":
            return {"answers": await self._user_input(broker, params)}
        if method == "mcpServer/elicitation/request":
            return await self._elicitation(broker, params)
        if method in {"execCommandApproval", "applyPatchApproval"}:
            return {"decision": "denied"}
        raise MethodNotFound(method)

    async def _ask(self, broker: LocalToolBroker, request: ApprovalRequest) -> bool:
        if not broker.has_approver:
            return False
        async with self._prompt_lock:
            return await broker.approve(request)

    async def _approve_command(self, broker: LocalToolBroker, params: dict[str, Any]) -> str:
        if self._mode is CopperMode.ASK:
            return "decline"
        command = str(params.get("command") or "")
        reason = str(params.get("reason") or "Codex wants to run a command outside its sandbox")
        approved = await self._ask(
            broker,
            ApprovalRequest(
                tool_call_id=str(params.get("itemId") or "command"),
                tool_name="execute",
                arguments={"command": command, "cwd": params.get("cwd")},
                reason=reason,
                canonical_name="execute",
            ),
        )
        return "accept" if approved else "decline"

    async def _approve_file_change(self, broker: LocalToolBroker, params: dict[str, Any]) -> str:
        if self._mode is not CopperMode.AGENT:
            return "decline"
        paths = self._file_changes.get(str(params.get("itemId") or ""), [])
        if (
            broker.mode is ApprovalMode.AUTO
            and paths
            and all(
                routine_action("write_file", {"file_path": path}, self.workspace) for path in paths
            )
        ):
            return "accept"
        approved = await self._ask(
            broker,
            ApprovalRequest(
                tool_call_id=str(params.get("itemId") or "patch"),
                tool_name="write_file",
                arguments={"file_path": ", ".join(paths) or "(files)"},
                reason=str(params.get("reason") or "Codex wants to change files"),
                canonical_name="write_file",
            ),
        )
        return "accept" if approved else "decline"

    async def _user_input(self, broker: LocalToolBroker, params: dict[str, Any]) -> dict[str, Any]:
        answers: dict[str, Any] = {}
        for question in params.get("questions") or []:
            if not isinstance(question, dict):
                continue
            options = [
                str(option.get("label"))
                for option in question.get("options") or []
                if isinstance(option, dict) and option.get("label")
            ]
            async with self._prompt_lock:
                try:
                    reply = await broker.ask_user(
                        {"question": question.get("question"), "options": options}
                    )
                except Exception:  # noqa: BLE001 - headless: no answer
                    reply = None
            answer = reply.get("answer") if isinstance(reply, dict) else None
            answers[str(question.get("id") or "")] = {"answers": [str(answer)] if answer else []}
        return answers

    async def _elicitation(self, broker: LocalToolBroker, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("serverName") == MCP_SERVER_NAME:
            # Codex asking whether it may call our own tools: they are read-only,
            # and copperpilot_subagent asks for its own writes.
            return {"action": "accept", "content": _default_content(params)}
        schema = params.get("requestedSchema") if isinstance(params, dict) else None
        properties = (schema or {}).get("properties") if isinstance(schema, dict) else None
        message = str(params.get("message") or "An MCP server asks for input.")
        if not isinstance(properties, dict) or not properties:
            approved = await self._ask(
                broker,
                ApprovalRequest(
                    tool_call_id="elicitation",
                    tool_name=f"mcp:{params.get('serverName')}",
                    arguments={},
                    reason=message,
                ),
            )
            return {"action": "accept" if approved else "decline", "content": {}}
        content: dict[str, Any] = {}
        for key, spec in properties.items():
            spec = spec if isinstance(spec, dict) else {}
            if spec.get("type") == "boolean":
                content[key] = await self._ask(
                    broker,
                    ApprovalRequest(
                        tool_call_id=f"elicitation:{key}",
                        tool_name=f"mcp:{params.get('serverName')}",
                        arguments={},
                        reason=f"{message} ({spec.get('title') or key})",
                    ),
                )
                continue
            options = [str(item) for item in spec.get("enum") or []]
            async with self._prompt_lock:
                try:
                    reply = await broker.ask_user(
                        {"question": f"{message}\n{spec.get('title') or key}", "options": options}
                    )
                except Exception:  # noqa: BLE001 - headless: decline
                    return {"action": "decline"}
            answer = reply.get("answer") if isinstance(reply, dict) else None
            if answer is None:
                return {"action": "decline"}
            content[key] = answer
        return {"action": "accept", "content": content}


def _default_content(params: dict[str, Any]) -> dict[str, Any]:
    schema = params.get("requestedSchema")
    properties = schema.get("properties") if isinstance(schema, dict) else None
    content: dict[str, Any] = {}
    for key, spec in (properties or {}).items():
        if isinstance(spec, dict) and spec.get("type") == "boolean":
            content[key] = True
    return content


class _Translator:
    """``codex app-server`` notifications → CopperPilot events for one turn."""

    def __init__(self, session: CodexSession) -> None:
        self.session = session
        self.turn_id: str | None = None
        self.usage = TurnUsage(model=session.model)
        self.failure: agent_errors.AgentError | None = None
        self.done = False
        self.tools: dict[str, str] = {}
        self.subagents: set[str] = set()
        self.streamed: set[str] = set()
        self.reasoned: set[str] = set()
        self.last_error: dict[str, Any] | None = None
        self.plan_sent = False
        self.last_text = ""
        self.backlog: list[tuple[str, dict[str, Any]]] = []

    def started(self, turn_id: str | None) -> None:
        self.turn_id = turn_id
        backlog, self.backlog = self.backlog, []
        turn = self.session._turn
        for method, params in backlog:
            for event in self.feed(method, params):
                if turn is not None:
                    turn.put(event)
        if self.done and turn is not None:
            turn.end()

    def feed(self, method: str, params: dict[str, Any]) -> list[CopperEvent]:
        turn = params.get("turnId") or (params.get("turn") or {}).get("id")
        if self.turn_id is None and turn is not None:
            # Notifications can arrive before turn/start answers.
            self.backlog.append((method, params))
            return []
        if turn is not None and self.turn_id is not None and turn != self.turn_id:
            return []
        if method == "item/agentMessage/delta":
            self.streamed.add(str(params.get("itemId")))
            return [CopperEvent(EventKind.TEXT, str(params.get("delta") or ""))]
        if method in {"item/reasoning/summaryTextDelta", "item/reasoning/textDelta"}:
            self.reasoned.add(str(params.get("itemId")))
            return [CopperEvent(EventKind.REASONING, str(params.get("delta") or ""))]
        if method == "item/started":
            return self._started(params.get("item") or {})
        if method == "item/completed":
            return self._completed(params.get("item") or {})
        if method == "item/mcpToolCall/progress":
            item = str(params.get("itemId") or "")
            if item in self.subagents:
                return [
                    CopperEvent(EventKind.STATUS, f"CopperPilot subagent: {params.get('message')}")
                ]
            return []
        if method == "turn/plan/updated":
            items = [
                {"content": str(step.get("step")), "status": step.get("status")}
                for step in params.get("plan") or []
                if isinstance(step, dict) and step.get("step")
            ]
            return [todo_event(items)]
        if method == "thread/tokenUsage/updated":
            self._usage(params.get("tokenUsage") or {})
            return []
        if method == "error":
            if params.get("willRetry") is not True and isinstance(params.get("error"), dict):
                self.last_error = params["error"]
            return []
        if method == "turn/completed":
            self._turn_completed(params.get("turn") or {})
            self.done = True
            if (
                self.session._mode is CopperMode.PLAN
                and not self.plan_sent
                and self.failure is None
                and self.last_text.strip()
            ):
                # No separate plan item: the Plan-mode answer is the plan.
                return [plan_event(self.last_text.strip())]
            return []
        return []

    def _usage(self, usage: dict[str, Any]) -> None:
        raw = usage.get("last")
        last: dict[str, Any] = raw if isinstance(raw, dict) else {}

        def number(key: str) -> int:
            value = last.get(key)
            return int(value) if isinstance(value, int) else 0

        self.usage.input_tokens += number("inputTokens")
        self.usage.output_tokens += number("outputTokens")
        self.usage.cached_input_tokens += number("cachedInputTokens")
        total = number("totalTokens") or number("inputTokens") + number("outputTokens")
        if total:
            self.usage.context_tokens = total
        window = usage.get("modelContextWindow")
        if isinstance(window, int) and window > 0:
            self.usage.context_window = window

    def _started(self, item: dict[str, Any]) -> list[CopperEvent]:
        kind = item.get("type")
        identifier = str(item.get("id") or "")
        if kind == "commandExecution":
            self.tools[identifier] = "execute"
            return [tool_request_event(identifier, "execute", {"command": item.get("command")})]
        if kind == "fileChange":
            paths = [
                str(change.get("path"))
                for change in item.get("changes") or []
                if isinstance(change, dict) and change.get("path")
            ]
            self.session._file_changes[identifier] = paths
            self.tools[identifier] = "edit_file"
            return [
                tool_request_event(
                    identifier, "edit_file", {"file_path": ", ".join(paths) or "(files)"}
                )
            ]
        if kind == "mcpToolCall":
            server, tool = str(item.get("server") or ""), str(item.get("tool") or "")
            raw_arguments = item.get("arguments")
            arguments: dict[str, Any] = raw_arguments if isinstance(raw_arguments, dict) else {}
            if server == MCP_SERVER_NAME and tool == SUBAGENT_TOOL:
                self.subagents.add(identifier)
                task = str(arguments.get("task") or "CopperPilot subagent")
                return [
                    subagent_event(identifier, task=task.splitlines()[0][:120], status="running")
                ]
            name = tool if server == MCP_SERVER_NAME else f"{server}.{tool}"
            self.tools[identifier] = name
            return [tool_request_event(identifier, name, arguments)]
        if kind == "webSearch":
            self.tools[identifier] = "web_search"
            return [tool_request_event(identifier, "web_search", {"query": item.get("query")})]
        if kind == "collabAgentToolCall" and item.get("tool") == "spawnAgent":
            self.subagents.add(identifier)
            task = str(item.get("prompt") or "Codex subagent")
            return [subagent_event(identifier, task=task.splitlines()[0][:120], status="running")]
        return []

    def _completed(self, item: dict[str, Any]) -> list[CopperEvent]:
        kind = item.get("type")
        identifier = str(item.get("id") or "")
        status = str(item.get("status") or "")
        failed = status in {"failed", "declined"}
        if kind == "plan" and item.get("text") and self.session._mode is CopperMode.PLAN:
            self.plan_sent = True
            return [plan_event(str(item["text"]))]
        if kind == "agentMessage" and item.get("text"):
            self.last_text = str(item["text"])
        if kind == "agentMessage" and identifier not in self.streamed and item.get("text"):
            return [CopperEvent(EventKind.TEXT, str(item["text"]))]
        if kind == "reasoning" and identifier not in self.reasoned:
            summary = "\n".join(str(part) for part in item.get("summary") or [] if part)
            return [CopperEvent(EventKind.REASONING, summary)] if summary else []
        if identifier in self.subagents:
            if kind == "mcpToolCall":
                text = _mcp_text(item.get("result")) or _mcp_error(item.get("error"))
                failed = failed or bool(item.get("error"))
            else:
                states = item.get("agentsStates") or {}
                text = "\n".join(
                    str(state.get("message") or "")
                    for state in states.values()
                    if isinstance(state, dict)
                )
            return [
                subagent_event(
                    identifier,
                    status="failed" if failed else "success",
                    **({"error": text or "Failed."} if failed else {"output": text}),
                )
            ]
        name = self.tools.pop(identifier, None)
        if name is None:
            return []
        if kind == "commandExecution":
            code = item.get("exitCode")
            output = str(item.get("aggregatedOutput") or "")
            if status == "declined":
                output = output or "Declined by the user."
            return [
                tool_result_event(
                    identifier,
                    name,
                    output,
                    error=failed and not isinstance(code, int),
                    exit_code=code if isinstance(code, int) else None,
                )
            ]
        if kind == "fileChange":
            text = "\n".join(
                f"{change.get('path')}"
                for change in item.get("changes") or []
                if isinstance(change, dict)
            )
            return [
                tool_result_event(
                    identifier, name, "Declined." if status == "declined" else text, error=failed
                )
            ]
        if kind == "mcpToolCall":
            error = item.get("error")
            if error:
                return [tool_result_event(identifier, name, _mcp_error(error), error=True)]
            return [tool_result_event(identifier, name, _mcp_text(item.get("result")))]
        if kind == "webSearch":
            return [tool_result_event(identifier, name, str(item.get("query") or ""))]
        return []

    def _turn_completed(self, turn: dict[str, Any]) -> None:
        status = turn.get("status")
        if status == "interrupted":
            return
        error = turn.get("error") if isinstance(turn.get("error"), dict) else self.last_error
        if status != "failed" and error is None:
            return
        error = error or {}
        message = str(error.get("message") or "Codex turn failed.")
        kind = _error_info_kind(error.get("codexErrorInfo"))
        if kind == "unauthorized":
            self.failure = agent_errors.auth_required(Provider.CODEX, message)
        elif kind in {"usageLimitExceeded", "rateLimitExceeded", "sessionBudgetExceeded"}:
            self.failure = agent_errors.usage_limit(
                Provider.CODEX, self.session.usage_limit_reset()
            )
        elif kind == "serverOverloaded":
            self.failure = agent_errors.overloaded(Provider.CODEX)
        elif kind == "contextWindowExceeded":
            self.failure = agent_errors.context_full(Provider.CODEX)
        else:
            self.failure = agent_errors.classify_message(Provider.CODEX, message)


def _mcp_text(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    parts = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
        elif isinstance(item, dict) and item.get("type") == "image":
            parts.append("[image]")
    return "\n".join(parts) or json.dumps(result.get("structuredContent") or "", default=str)


def _mcp_error(error: Any) -> str:
    if isinstance(error, dict):
        return str(error.get("message") or "Tool failed.")
    return str(error or "Tool failed.")


def make_codex_session(
    selection: AgentSelection,
    *,
    workspace: Path,
    proxy: ToolProxy,
    broker: Callable[[], LocalToolBroker],
) -> CodexSession:
    path = find_codex()
    if path is None:
        raise agent_errors.not_installed(Provider.CODEX, CODEX_BINARY)
    return CodexSession(selection, workspace=workspace, proxy=proxy, broker=broker, codex_path=path)


__all__ = [
    "CLIENT_NAME",
    "CodexSession",
    "account_label",
    "app_server_argv",
    "client_info",
    "codex_status",
    "find_codex",
    "list_codex_models",
    "make_codex_session",
    "mcp_server_config",
    "turn_policy",
]
