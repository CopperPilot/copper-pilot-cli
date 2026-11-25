"""Claude Agent: the user's own installed ``claude`` through the Claude Agent SDK.

Terms we follow (see the bring-your-own-agent spec):

* we spawn the user's unmodified ``claude`` binary (``cli_path`` from PATH),
  never the binary bundled in the SDK wheel;
* we never read, store or proxy Claude credentials and never inject an API
  key; login is ``claude auth login`` in the user's own terminal;
* the session is not persisted by Claude (``--no-session-persistence``);
* agent tokens are reported at zero cost for analytics only.

Install with ``pip install "copper-pilot-cli[claude]"``.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import shutil
import warnings
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from copper_pilot_cli.agents import AgentSelection, Provider
from copper_pilot_cli.agents import errors as agent_errors
from copper_pilot_cli.agents.catalog import compile_claude_selection
from copper_pilot_cli.agents.mcp_server import ProxyBackend, build_mcp_server
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
from copper_pilot_cli.agents.tool_proxy import ToolProxy
from copper_pilot_cli.copper_protocol import CopperEvent, CopperMode, EventKind
from copper_pilot_cli.copper_tools import (
    ApprovalMode,
    ApprovalRequest,
    LocalToolBroker,
    dangerous_shell_command,
    outside_workspace_reason,
)

logger = logging.getLogger(__name__)

CLAUDE_BINARY = "claude"
CLAUDE_INSTALL_HINT = (
    'Claude Agent needs the optional dependency: pip install "copper-pilot-cli[claude]"'
)
MCP_PREFIX = "mcp__copperpilot__"
# File reads: allowed inside the workspace, asked outside it.
_READ_TOOLS = frozenset({"Read", "Glob", "Grep", "LS", "NotebookRead"})
# Always allowed: web, todos, Claude's native subagents (their own tool calls
# reach the same callback) and tool discovery.
_FREE_TOOLS = frozenset(
    {
        "WebSearch",
        "WebFetch",
        "TodoWrite",
        "Task",
        "Agent",
        "BashOutput",
        "KillShell",
        "ToolSearch",
        "ListMcpResourcesTool",
        "ReadMcpResourceTool",
    }
)
PLAN_CAPTURED = "Plan captured; the user will accept or reject it in CopperPilot."
_EDIT_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})
_SUBAGENT_TOOLS = frozenset({"Task", "Agent"})
_DISPLAY_NAMES = {
    "Bash": "execute",
    "Read": "read_file",
    "Write": "write_file",
    "Edit": "edit_file",
    "MultiEdit": "edit_file",
    "Glob": "glob",
    "Grep": "grep",
    "WebSearch": "web_search",
    "WebFetch": "web_fetch",
    "NotebookEdit": "edit_file",
}


def find_claude() -> str | None:
    return shutil.which(CLAUDE_BINARY)


def sdk_available() -> bool:
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class AgentStatus:
    """What ``/provider`` shows for one agent."""

    installed: bool
    logged_in: bool | None
    label: str
    path: str | None = None
    version: str | None = None
    detail: str = ""


async def _run(argv: list[str], timeout: float = 10.0) -> tuple[int | None, str]:  # noqa: ASYNC109
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(timeout):
            stdout, _ = await process.communicate()
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        return None, ""
    return process.returncode, stdout.decode("utf-8", "replace")


async def claude_status() -> AgentStatus:
    """``claude --version`` and ``claude auth status --json``; never touches credentials."""
    path = find_claude()
    if path is None:
        return AgentStatus(False, None, "not installed (`claude` not on PATH)")
    _code, version = await _run([path, "--version"])
    code, raw = await _run([path, "auth", "status", "--json"])
    version = version.strip().split(" ")[0] or None
    try:
        status = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        status = {}
    if not isinstance(status, dict) or "loggedIn" not in status:
        return AgentStatus(True, None, "installed; login status unknown", path, version)
    if status.get("loggedIn") is not True:
        return AgentStatus(
            True, False, "not logged in: run `claude auth login`", path, version, "auth"
        )
    plan = status.get("subscriptionType") or status.get("authMethod") or ""
    method = status.get("authMethod")
    detail = f"{plan}" if plan else ""
    if method and plan and method != plan:
        detail = f"{plan} ({method})"
    label = "Authenticated" + (f" · {detail}" if detail else "")
    del code
    return AgentStatus(True, True, label, path, version)


def permission_mode(mode: CopperMode) -> str:
    """Claude's permission mode: ``plan`` in Plan mode, else ``default``.

    Never ``acceptEdits`` or ``bypassPermissions``: every action the user's own
    Claude rules do not already allow reaches ``can_use_tool``, which applies
    the CLI's approval setting (Manual / Auto / YOLO, dangerous shell).
    """
    return "plan" if mode is CopperMode.PLAN else "default"


def display_tool(name: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Claude's tool names and inputs as the TUI's tool cards know them."""
    if name.startswith(MCP_PREFIX):
        return name.removeprefix(MCP_PREFIX), arguments
    display = _DISPLAY_NAMES.get(name, name)
    if name == "MultiEdit":
        edits = arguments.get("edits")
        return display, {"file_path": arguments.get("file_path"), "edits": len(edits or [])}
    if name == "NotebookEdit":
        return display, {"file_path": arguments.get("notebook_path")}
    return display, arguments


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, dict) and item.get("type") == "image":
                parts.append("[image]")
        return "\n".join(parts)
    return "" if content is None else str(content)


def _todo_items(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for todo in arguments.get("todos") or []:
        if isinstance(todo, dict) and str(todo.get("content") or "").strip():
            items.append({"content": str(todo["content"]).strip(), "status": todo.get("status")})
    return items


def _answers_from(questions: list[dict[str, Any]], reply: Any) -> dict[str, str]:
    """Map our question UI's reply onto AskUserQuestion's ``answers`` (question → label)."""
    answers: dict[str, str] = {}
    rows = reply.get("answers") if isinstance(reply, dict) else None
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("question"):
                answers[str(row["question"])] = str(row.get("answer") or "")
    elif isinstance(reply, dict) and reply.get("status") == "answered" and questions:
        answers[str(questions[0].get("question") or "")] = str(reply.get("answer") or "")
    return answers


def _usage_numbers(usage: dict[str, Any] | None) -> tuple[int, int, int]:
    usage = usage or {}

    def number(key: str) -> int:
        value = usage.get(key)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    cached = number("cache_read_input_tokens") + number("cache_creation_input_tokens")
    return number("input_tokens") + cached, number("output_tokens"), cached


class ClaudeSession(AgentSession):
    """One live ``claude`` query in streaming-input mode, reused across turns."""

    provider = Provider.CLAUDE

    def __init__(
        self,
        selection: AgentSelection,
        *,
        workspace: Path,
        proxy: ToolProxy,
        broker: Callable[[], LocalToolBroker],
        cli_path: str | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(selection)
        self.workspace = workspace
        self.proxy = proxy
        self._broker = broker
        self.cli_path = cli_path
        self.client_factory = client_factory
        self.launch = compile_claude_selection(
            selection.model, selection.effort, selection.fast_mode
        )
        self._client: Any = None
        self._owner: asyncio.Task[None] | None = None
        self._commands: asyncio.Queue[tuple[str, TurnQueue] | None] = asyncio.Queue()
        self._ready: asyncio.Future[None] | None = None
        self._turn: TurnQueue | None = None
        self._permission_lock = asyncio.Lock()
        self._mode = CopperMode.AGENT
        self._failure: BaseException | None = None

    @property
    def identity(self) -> str:
        return json.dumps(
            {
                "provider": self.provider.value,
                "model": self.launch.model,
                "effort": self.launch.effort,
                "fast": self.launch.fast_mode,
            },
            sort_keys=True,
        )

    @property
    def context_window(self) -> int | None:
        return self.launch.context_window

    @property
    def alive(self) -> bool:
        return (
            not self.closed
            and self._failure is None
            and (self._owner is None or not self._owner.done())
        )

    # ------------------------------------------------------------- startup

    def _options(self) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        with contextlib.suppress(ImportError):
            from claude_agent_sdk.types import CanUseToolShadowedWarning

            # Expected: Auto (acceptEdits) lets Claude run edits without asking us.
            warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)

        extra_args: dict[str, str | None] = {"no-session-persistence": None}
        settings = (
            json.dumps({"fastMode": self.launch.fast_mode})
            if self.launch.fast_mode is not None
            else None
        )
        server = build_mcp_server(ProxyBackend(self.proxy))
        return ClaudeAgentOptions(
            cli_path=self.cli_path,
            cwd=str(self.workspace),
            # The user's own Claude Code system prompt, plus our short guide.
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": self.proxy.guide,
            },
            include_partial_messages=True,
            model=self.launch.model,
            effort=self.launch.effort,  # ty: ignore[invalid-argument-type]
            thinking={"type": "adaptive", "display": "summarized"}
            if self.launch.effort is not None
            else None,
            settings=settings,
            permission_mode=permission_mode(CopperMode.AGENT),  # ty: ignore[invalid-argument-type]
            # The user's own Claude setup: CLAUDE.md, permission rules, MCP
            # servers, hooks, skills and plugins. Ours is added alongside.
            setting_sources=["user", "project", "local"],
            can_use_tool=self._can_use_tool,
            mcp_servers={"copperpilot": {"type": "sdk", "name": "copperpilot", "instance": server}},
            extra_args=extra_args,
        )

    async def start(self) -> None:
        if self._owner is not None:
            return
        if self.cli_path is None and self.client_factory is None:
            self.cli_path = find_claude()
            if self.cli_path is None:
                raise agent_errors.not_installed(Provider.CLAUDE, CLAUDE_BINARY)
        await self.proxy.start()
        self._ready = asyncio.get_running_loop().create_future()
        # Its own task and a clean context: the SDK keeps tasks for the life of
        # the session, which must not belong to the graph run of the first turn.
        self._owner = asyncio.get_running_loop().create_task(
            self._own(), context=contextvars.Context()
        )
        try:
            await self._ready
        except BaseException:
            await self.aclose()
            raise

    async def _own(self) -> None:
        assert self._ready is not None
        try:
            if self.client_factory is not None:
                client = self.client_factory(self._options())
            else:
                from claude_agent_sdk import ClaudeSDKClient

                client = ClaudeSDKClient(self._options())
            await client.connect()
        except BaseException as exc:
            if not self._ready.done():
                self._ready.set_exception(self._classify_startup(exc))
            return
        self._client = client
        self._ready.set_result(None)
        try:
            while (command := await self._commands.get()) is not None:
                prompt, turn = command
                try:
                    await client.query(prompt)
                    async for message in client.receive_response():
                        turn.put(message)
                    turn.end()
                except Exception as exc:  # noqa: BLE001 - surfaced through the turn
                    self._failure = exc
                    turn.fail(self._classify_runtime(exc))
                    return
        finally:
            with contextlib.suppress(Exception):
                await client.disconnect()
            self._client = None

    def _classify_startup(self, exc: BaseException) -> BaseException:
        if isinstance(exc, agent_errors.AgentError):
            return exc
        name = type(exc).__name__
        if name == "CLINotFoundError":
            return agent_errors.not_installed(Provider.CLAUDE, self.cli_path or CLAUDE_BINARY)
        return self._classify_runtime(exc)

    def _classify_runtime(self, exc: BaseException) -> BaseException:
        if isinstance(exc, agent_errors.AgentError):
            return exc
        name = type(exc).__name__
        if name == "ProcessError":
            return agent_errors.process_exited(
                Provider.CLAUDE, getattr(exc, "exit_code", None), str(exc)
            )
        if name in {"CLIConnectionError", "CLIJSONDecodeError"}:
            return agent_errors.process_exited(Provider.CLAUDE, None, str(exc))
        return agent_errors.classify_message(Provider.CLAUDE, str(exc) or name)

    # --------------------------------------------------------------- turns

    async def run_turn(self, prompt: str, context: TurnContext) -> AsyncIterator[CopperEvent]:
        await self.start()
        if not self.alive:
            raise agent_errors.process_exited(Provider.CLAUDE)
        self._mode = context.mode
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.set_permission_mode(permission_mode(context.mode))
        turn = TurnQueue()
        self._turn = turn
        self.proxy.emit = turn.put
        await self._commands.put((prompt, turn))
        translator = _Translator(self)
        finished = False
        try:
            async for item in turn.events():
                if isinstance(item, CopperEvent):
                    yield item
                    continue
                for event in translator.feed(item):
                    yield event
            finished = True
        finally:
            self.proxy.emit = None
            self._turn = None
            if not finished:
                # Cancelled (Stop / Ctrl+C): stop Claude; the owner drains the
                # rest of this turn before it takes the next one.
                await self.interrupt()
        if translator.failure is not None:
            raise translator.failure
        usage = translator.usage
        await self._report_usage(usage, context.turn_id)
        event = context_event(usage)
        if event is not None:
            yield event
        yield final_event(usage, model=usage.model or self.launch.model)

    async def _report_usage(self, usage: TurnUsage, turn_id: str) -> None:
        await self.proxy.session.send_usage(
            provider=self.provider.value,
            model=usage.model or self.launch.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            turn_id=turn_id,
        )

    async def compact(self) -> str:
        """Claude Code's own ``/compact`` on the live session."""
        if self._owner is None or not self.alive:
            return "There is no live Claude Agent session to compact."
        turn = TurnQueue()
        await self._commands.put(("/compact", turn))
        failure = None
        async with asyncio.timeout(600):
            async for item in turn.events():
                if type(item).__name__ == "ResultMessage" and item.is_error:
                    failure = "; ".join(item.errors or []) or str(item.result or "failed")
        return f"Compaction failed: {failure}" if failure else "Claude Agent compacted its context."

    async def interrupt(self) -> None:
        client = self._client
        if client is None:
            return
        with contextlib.suppress(Exception):
            async with asyncio.timeout(5):
                await asyncio.shield(client.interrupt())

    async def aclose(self) -> None:
        self.closed = True
        owner = self._owner
        if owner is not None and not owner.done():
            await self._commands.put(None)
            try:
                async with asyncio.timeout(10):
                    await asyncio.shield(owner)
            except (TimeoutError, Exception):  # noqa: BLE001 - shutting down
                owner.cancel()
                await asyncio.gather(owner, return_exceptions=True)

    # --------------------------------------------------------- permissions

    async def _can_use_tool(self, tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        broker = self._broker()
        if tool_name.startswith(MCP_PREFIX):
            # Our tools: Ask/Plan limits are enforced by the ToolProxy; the files
            # they write go through the bridge broker's approval rules.
            return PermissionResultAllow()
        if tool_name == "AskUserQuestion":
            questions = [
                item for item in tool_input.get("questions") or [] if isinstance(item, dict)
            ]
            prompt = (
                {
                    "questions": [
                        {
                            "question": item.get("question"),
                            "options": [
                                option.get("label")
                                for option in item.get("options") or []
                                if isinstance(option, dict)
                            ],
                        }
                        for item in questions
                    ]
                }
                if len(questions) != 1
                else {
                    "question": questions[0].get("question"),
                    "options": [
                        option.get("label")
                        for option in questions[0].get("options") or []
                        if isinstance(option, dict)
                    ],
                }
            )
            try:
                async with self._permission_lock:
                    reply = await broker.ask_user(prompt)
            except Exception as exc:  # noqa: BLE001 - headless: nobody to ask
                return PermissionResultDeny(message=f"Cannot ask the user here: {exc}")
            answers = _answers_from(questions, reply)
            if not answers:
                return PermissionResultDeny(message="The user skipped the question.")
            return PermissionResultAllow(updated_input={**tool_input, "answers": answers})
        if tool_name == "ExitPlanMode":
            plan = str(tool_input.get("plan") or "").strip()
            if self._mode is CopperMode.PLAN and plan and self._turn is not None:
                self._turn.put(plan_event(plan))
                return PermissionResultDeny(message=PLAN_CAPTURED)
            return PermissionResultAllow()
        writes = tool_name in _EDIT_TOOLS or tool_name == "Bash"
        if self._mode is CopperMode.ASK and writes:
            return PermissionResultDeny(
                message="This chat is in Ask mode (read-only). Answer without changing files "
                "or running commands."
            )
        decision, outside = self._policy(broker, tool_name, tool_input)
        if decision == "allow":
            return PermissionResultAllow()
        if not broker.has_approver:
            return PermissionResultDeny(
                message="Nobody can approve this here; ask the user to run it in the TUI."
            )
        call_id = str(getattr(context, "tool_use_id", None) or tool_name)
        display, arguments = display_tool(tool_name, tool_input)
        reason = str(getattr(context, "title", None) or "") or (
            f"Claude Agent wants to use {tool_name}"
        )
        if outside:
            reason = f"Outside the workspace: {outside}"
        request = ApprovalRequest(
            tool_call_id=call_id,
            tool_name=display,
            arguments=arguments,
            reason=reason,
            canonical_name=display,
            outside_workspace=outside,
        )
        async with self._permission_lock:
            approved = await broker.approve(request)
        if approved:
            return PermissionResultAllow()
        return PermissionResultDeny(message="The user declined this action.")

    def _policy(
        self, broker: LocalToolBroker, tool_name: str, tool_input: dict[str, Any]
    ) -> tuple[str, str | None]:
        """("allow" | "ask", outside-the-workspace reason) under the CLI's approval settings."""
        yolo = broker.mode is ApprovalMode.YOLO
        outside_ok = yolo or (
            broker.mode is ApprovalMode.AUTO and broker.always_allow_dangerous_shell_commands
        )
        if tool_name in _FREE_TOOLS:
            return "allow", None
        if tool_name in _READ_TOOLS:
            raw = (
                tool_input.get("file_path")
                or tool_input.get("path")
                or tool_input.get("notebook_path")
            )
            if raw is None or self._inside_workspace(raw):
                return "allow", None
            return ("allow" if outside_ok else "ask"), f"reads {raw}"
        if tool_name == "Bash":
            command = tool_input.get("command")
            outside = outside_workspace_reason(command, self.workspace)
            if outside is not None:
                return ("allow" if outside_ok else "ask"), outside
            if yolo:
                return "allow", None
            if broker.mode is ApprovalMode.AUTO:
                danger = dangerous_shell_command(command)
                if (danger and broker.always_allow_dangerous_shell_commands) or (
                    not danger and broker.always_allow_shell_commands
                ):
                    return "allow", None
            return "ask", None
        if tool_name in _EDIT_TOOLS:
            raw = tool_input.get("file_path") or tool_input.get("notebook_path")
            if not self._inside_workspace(raw):
                return ("allow" if outside_ok else "ask"), f"writes {raw}"
            return ("allow" if broker.mode is not ApprovalMode.MANUAL else "ask"), None
        # Anything else (the user's own MCP tools, new built-ins): YOLO runs it.
        return ("allow" if yolo else "ask"), None

    def _inside_workspace(self, raw: Any) -> bool:
        if not isinstance(raw, str) or not raw:
            return False
        try:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = self.workspace / path
            path.resolve(strict=False).relative_to(self.workspace.resolve())
        except (OSError, ValueError, RuntimeError):
            return False
        return True


class _Translator:
    """Claude Agent SDK messages → CopperPilot events for one turn."""

    def __init__(self, session: ClaudeSession) -> None:
        self.session = session
        self.usage = TurnUsage(context_window=session.launch.context_window)
        self.failure: agent_errors.AgentError | None = None
        self.tools: dict[str, str] = {}
        self.subagents: set[str] = set()
        self.todos: set[str] = set()
        self.streamed: set[str] = set()
        self.message_id: str | None = None
        self.rate_limit_reset: str | None = None
        self.auth_failed = False

    def feed(self, message: Any) -> list[CopperEvent]:
        name = type(message).__name__
        handler = getattr(self, f"_on_{name}", None)
        return handler(message) if handler is not None else []

    def _on_StreamEvent(self, message: Any) -> list[CopperEvent]:  # noqa: N802
        if message.parent_tool_use_id:
            return []
        event = message.event or {}
        kind = event.get("type")
        if kind == "message_start":
            self.message_id = (event.get("message") or {}).get("id")
            return []
        if kind != "content_block_delta":
            return []
        delta = event.get("delta") or {}
        if self.message_id:
            self.streamed.add(self.message_id)
        if delta.get("type") == "text_delta" and delta.get("text"):
            return [CopperEvent(EventKind.TEXT, str(delta["text"]))]
        if delta.get("type") == "thinking_delta" and delta.get("thinking"):
            return [CopperEvent(EventKind.REASONING, str(delta["thinking"]))]
        return []

    def _on_AssistantMessage(self, message: Any) -> list[CopperEvent]:  # noqa: N802
        if message.parent_tool_use_id:
            return []
        events: list[CopperEvent] = []
        if message.error:
            self._note_error(str(message.error))
        streamed = message.message_id in self.streamed if message.message_id else False
        for block in message.content:
            kind = type(block).__name__
            if kind == "TextBlock" and not streamed and block.text:
                events.append(CopperEvent(EventKind.TEXT, block.text))
            elif kind == "ThinkingBlock" and not streamed and block.thinking:
                events.append(CopperEvent(EventKind.REASONING, block.thinking))
            elif kind == "ToolUseBlock":
                events.extend(self._tool_use(block.id, block.name, dict(block.input or {})))
        if message.model:
            self.usage.model = message.model
        if message.usage:
            # The latest call's prompt plus reply is the context the next call sees.
            prompt, output, _cached = _usage_numbers(message.usage)
            if prompt or output:
                self.usage.context_tokens = prompt + output
        return events

    def _tool_use(self, call_id: str, name: str, arguments: dict[str, Any]) -> list[CopperEvent]:
        if name in _SUBAGENT_TOOLS:
            self.subagents.add(call_id)
            task = str(arguments.get("description") or arguments.get("prompt") or "Subagent")
            return [subagent_event(call_id, task=task[:120], status="running")]
        if name == "TodoWrite":
            self.todos.add(call_id)
            return [todo_event(_todo_items(arguments))]
        display, shown = display_tool(name, arguments)
        self.tools[call_id] = display
        return [tool_request_event(call_id, display, shown)]

    def _on_UserMessage(self, message: Any) -> list[CopperEvent]:  # noqa: N802
        if message.parent_tool_use_id or isinstance(message.content, str):
            return []
        events: list[CopperEvent] = []
        for block in message.content:
            if type(block).__name__ != "ToolResultBlock":
                continue
            call_id = block.tool_use_id
            text = _result_text(block.content)
            if call_id in self.subagents:
                events.append(
                    subagent_event(
                        call_id,
                        status="failed" if block.is_error else "success",
                        **({"error": text} if block.is_error else {"output": text}),
                    )
                )
            elif call_id in self.tools:
                events.append(
                    tool_result_event(
                        call_id, self.tools.pop(call_id), text, error=block.is_error is True
                    )
                )
        return events

    def _on_RateLimitEvent(self, message: Any) -> list[CopperEvent]:  # noqa: N802
        info = getattr(message, "rate_limit_info", None)
        if info is not None and getattr(info, "status", None) == "rejected":
            self.rate_limit_reset = agent_errors.epoch_to_iso(getattr(info, "resets_at", None))
        return []

    def _on_ResultMessage(self, message: Any) -> list[CopperEvent]:  # noqa: N802
        total_in, output, cached = _usage_numbers(message.usage)
        self.usage.input_tokens = total_in
        self.usage.output_tokens = output
        self.usage.cached_input_tokens = cached
        if self.usage.context_tokens is None and (total_in or output):
            self.usage.context_tokens = total_in + output
        reason = str(message.terminal_reason or "")
        if reason.startswith("aborted"):
            return []
        if message.is_error or message.subtype != "success":
            status = message.api_error_status
            text = "; ".join(message.errors or []) or str(message.result or message.subtype)
            if self.auth_failed or status in {401, 403}:
                self.failure = agent_errors.auth_required(Provider.CLAUDE, text)
            elif status == 429 or self.rate_limit_reset:
                self.failure = agent_errors.usage_limit(Provider.CLAUDE, self.rate_limit_reset)
            elif status == 529:
                self.failure = agent_errors.overloaded(Provider.CLAUDE, status)
            else:
                self.failure = agent_errors.classify_message(Provider.CLAUDE, text, status)
        return []

    def _note_error(self, kind: str) -> None:
        if kind == "authentication_failed":
            self.auth_failed = True
        elif kind in {"rate_limit", "billing_error"} and self.failure is None:
            self.failure = agent_errors.usage_limit(Provider.CLAUDE, self.rate_limit_reset)


def make_claude_session(
    selection: AgentSelection,
    *,
    workspace: Path,
    proxy: ToolProxy,
    broker: Callable[[], LocalToolBroker],
) -> ClaudeSession:
    if not sdk_available():
        raise agent_errors.AgentError(
            agent_errors.NOT_INSTALLED, CLAUDE_INSTALL_HINT, provider=Provider.CLAUDE
        )
    path = find_claude()
    if path is None:
        raise agent_errors.not_installed(Provider.CLAUDE, CLAUDE_BINARY)
    return ClaudeSession(selection, workspace=workspace, proxy=proxy, broker=broker, cli_path=path)


__all__ = [
    "CLAUDE_INSTALL_HINT",
    "AgentStatus",
    "ClaudeSession",
    "claude_status",
    "display_tool",
    "find_claude",
    "make_claude_session",
    "permission_mode",
    "sdk_available",
]
