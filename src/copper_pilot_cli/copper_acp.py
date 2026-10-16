"""Stdio Agent Client Protocol (ACP) server for editors such as Zed.

The server speaks JSON-RPC over stdin/stdout, so nothing in this module may
write to stdout. One ACP session is one CopperPilot thread: the ACP session id
is the checkpoint thread id, and the session ``cwd`` is the workspace.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import httpx
from acp import PROTOCOL_VERSION, Agent, Client, RequestError, run_agent
from acp.helpers import (
    start_tool_call,
    text_block,
    tool_content,
    update_agent_message_text,
    update_agent_thought_text,
    update_current_mode,
    update_tool_call,
    update_user_message_text,
)
from acp.schema import (
    AgentCapabilities,
    AuthenticateResponse,
    Implementation,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PermissionOption,
    PromptResponse,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SessionMode,
    SessionModeState,
    SetSessionConfigOptionResponse,
    SetSessionModeResponse,
    TerminalAuthMethod,
    ToolCallLocation,
    ToolCallUpdate,
)
from langchain_core.messages import HumanMessage
from websockets.exceptions import InvalidStatus

from copper_pilot_cli._version import __version__
from copper_pilot_cli.copper_auth import AuthenticationError, DeviceCredential, load_credential
from copper_pilot_cli.copper_graph import RETRY_EVENT, create_copper_graph
from copper_pilot_cli.copper_preferences import shell_auto_allow_settings
from copper_pilot_cli.copper_protocol import (
    CopperEffort,
    CopperMode,
    EventKind,
    ProtocolError,
    error_message,
    normalize_effort,
)
from copper_pilot_cli.copper_status_events import (
    SubagentStatus,
    normalize_subagent_status,
    subagent_progress_label,
)
from copper_pilot_cli.copper_tools import (
    ApprovalMode,
    ApprovalRequest,
    LocalToolBroker,
    ToolRejected,
    canonical_tool_name,
)
from copper_pilot_cli.copper_workspace import (
    canonical_workspace,
    discover_workspace,
    remember_workspace,
)
from copper_pilot_cli.langchain import CopperPilotAgent
from copper_pilot_cli.sessions import generate_thread_id

logger = logging.getLogger(__name__)

LOGIN_METHOD_ID = "copper-pilot-login"
LOGIN_MESSAGE = "Login required; run `copper-pilot auth login` in a terminal."
QUESTION_MESSAGE = (
    "This clarification needs a free-text answer, which ACP cannot collect. "
    "Continue this thread in the CopperPilot TUI with `copper-pilot --resume`."
)
MODE_CONFIG_ID = "mode"
EFFORT_CONFIG_ID = "effort"
OPTION_ALLOW = "allow"
OPTION_ALLOW_SESSION = "allow_session"
OPTION_REJECT = "reject"
OPTION_SKIP = "skip"
_MAX_TOOL_OUTPUT_CHARS = 4_000

_MODE_LABELS: dict[CopperMode, tuple[str, str]] = {
    CopperMode.ASK: ("Ask", "Read-oriented electronics questions; no design changes."),
    CopperMode.PLAN: ("Plan", "Propose a design plan without applying it."),
    CopperMode.AGENT: ("Agent", "Apply schematic and PCB changes in the workspace."),
}
_EFFORT_LABELS: dict[CopperEffort, tuple[str, str]] = {
    CopperEffort.BALANCED: ("Balanced", "Default effort."),
    CopperEffort.INTELLIGENCE: ("Intelligence", "Deep survey; requires a paid plan."),
}
_TOOL_KINDS: dict[str, str] = {
    "read_file": "read",
    "read_binary_file": "read",
    "ls": "search",
    "glob": "search",
    "grep": "search",
    "write_file": "edit",
    "write_binary_file": "edit",
    "edit_file": "edit",
    "delete": "delete",
    "execute": "execute",
    "ask_user_question": "other",
}
_PATH_KEYS = ("file_path", "path")

SessionUpdateFn = Callable[[str, Any], Awaitable[None]]


@dataclass
class AcpSession:
    """One ACP session bound to a CopperPilot thread."""

    session_id: str
    workspace: Path
    mode: CopperMode
    effort: CopperEffort
    broker: LocalToolBroker
    turn: asyncio.Task[Any] | None = None
    runtime: Any = None
    cancelled: bool = False
    subagents: dict[str, SubagentStatus] = field(default_factory=dict)
    failure: Any = None

    @property
    def busy(self) -> bool:
        return self.turn is not None and not self.turn.done()


def current_credential() -> DeviceCredential | None:
    """Return the stored credential, or None when missing or unreadable."""
    try:
        return load_credential()
    except AuthenticationError:
        return None


def mode_state(mode: CopperMode) -> SessionModeState:
    """Describe CopperPilot modes as ACP session modes."""
    return SessionModeState(
        current_mode_id=mode.value,
        available_modes=[
            SessionMode(id=item.value, name=name, description=description)
            for item, (name, description) in _MODE_LABELS.items()
        ],
    )


def config_options(mode: CopperMode, effort: CopperEffort) -> list[SessionConfigOptionSelect]:
    """Expose mode and effort as ACP config selectors."""
    return [
        SessionConfigOptionSelect(
            id=MODE_CONFIG_ID,
            name="Mode",
            description="Hosted chat mode",
            category="mode",
            type="select",
            current_value=mode.value,
            options=[
                SessionConfigSelectOption(value=item.value, name=name, description=description)
                for item, (name, description) in _MODE_LABELS.items()
            ],
        ),
        SessionConfigOptionSelect(
            id=EFFORT_CONFIG_ID,
            name="Effort",
            description="Hosted chat effort",
            category="thought_level",
            type="select",
            current_value=effort.value,
            options=[
                SessionConfigSelectOption(value=item.value, name=name, description=description)
                for item, (name, description) in _EFFORT_LABELS.items()
            ],
        ),
    ]


def tool_kind(tool_name: str) -> Any:
    """Map a hosted tool name onto an ACP tool kind."""
    return _TOOL_KINDS.get(canonical_tool_name(tool_name), "other")


def tool_title(tool_name: str, arguments: dict[str, Any]) -> str:
    canonical = canonical_tool_name(tool_name)
    if canonical == "execute":
        command = str(arguments.get("command") or "").strip()
        return f"$ {command}" if command else "Shell command"
    for key in _PATH_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return f"{canonical} {value}"
    if canonical == "ask_user_question":
        return str(arguments.get("question") or arguments.get("prompt") or "Clarification")
    return canonical


def tool_locations(arguments: dict[str, Any]) -> list[ToolCallLocation] | None:
    for key in _PATH_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return [ToolCallLocation(path=value)]
    return None


def prompt_text(blocks: list[Any]) -> str:
    """Join the text-bearing prompt blocks into one hosted query."""
    parts: list[str] = []
    for block in blocks:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
            continue
        resource = getattr(block, "resource", None)
        resource_text = getattr(resource, "text", None)
        if isinstance(resource_text, str) and resource_text:
            uri = getattr(resource, "uri", None) or "resource"
            parts.append(f"[{uri}]\n{resource_text}")
    return "\n\n".join(parts).strip()


def question_options(arguments: Any) -> tuple[str, list[str]] | None:
    """Return the prompt and choices for an option-style clarification."""
    if not isinstance(arguments, dict) or isinstance(arguments.get("questions"), list):
        return None
    prompt = str(arguments.get("question") or arguments.get("prompt") or "").strip()
    raw = arguments.get("options")
    options = (
        [str(option).strip() for option in raw if isinstance(option, str) and option.strip()]
        if isinstance(raw, list)
        else []
    )
    if not options:
        return None
    return prompt or "Clarification", options


def _tool_result_payload(result: Any) -> tuple[str, bool, Any]:
    """Return (text, is_error, raw_output) for a hosted tool result."""
    if isinstance(result, dict):
        is_error = bool(result.get("error")) or (
            isinstance(result.get("exit_code"), int) and result["exit_code"] != 0
        )
        content = result.get("error") or result.get("result", result)
    else:
        is_error = False
        content = result
    text = content if isinstance(content, str) else repr(content)
    if len(text) > _MAX_TOOL_OUTPUT_CHARS:
        text = text[:_MAX_TOOL_OUTPUT_CHARS] + "\n[truncated]"
    return text, is_error, result


class CopperAcpAgent:
    """ACP agent that proxies prompts to the hosted CopperPilot agent.

    Implements the subset of ``acp.Agent`` that CopperPilot supports. The SDK
    router answers ``method_not_found`` for the rest (fork, resume, list).
    """

    def __init__(
        self,
        *,
        approval: ApprovalMode = ApprovalMode.AUTO,
        default_mode: CopperMode = CopperMode.AGENT,
        default_effort: CopperEffort = CopperEffort.BALANCED,
    ) -> None:
        self.approval = approval
        self.default_mode = default_mode
        self.default_effort = default_effort
        self.sessions: dict[str, AcpSession] = {}
        self._conn: Client | None = None

    # ------------------------------------------------------------------ wiring

    def on_connect(self, conn: Client) -> None:
        self._conn = conn

    @property
    def conn(self) -> Client:
        if self._conn is None:
            raise RuntimeError("The ACP connection is not established yet.")
        return self._conn

    def _session(self, session_id: str) -> AcpSession:
        session = self.sessions.get(session_id)
        if session is None:
            raise RequestError.invalid_params({"details": f"Unknown session {session_id!r}."})
        return session

    def _require_credential(self) -> DeviceCredential:
        credential = current_credential()
        if credential is None:
            raise RequestError.auth_required({"details": LOGIN_MESSAGE})
        return credential

    def _workspace(self, cwd: str) -> Path:
        try:
            return canonical_workspace(cwd)
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise RequestError.invalid_params({"details": str(exc)}) from exc

    def _new_broker(self, workspace: Path) -> LocalToolBroker:
        normal_shell, dangerous_shell = shell_auto_allow_settings()
        return LocalToolBroker(
            workspace,
            mode=self.approval,
            always_allow_shell_commands=normal_shell,
            always_allow_dangerous_shell_commands=dangerous_shell,
        )

    def _register(
        self,
        session_id: str,
        workspace: Path,
        *,
        mode: CopperMode | None = None,
        effort: CopperEffort | None = None,
    ) -> AcpSession:
        context = discover_workspace(workspace)
        remember_workspace(context)
        session = AcpSession(
            session_id=session_id,
            workspace=workspace,
            mode=mode or self.default_mode,
            effort=effort or self.default_effort,
            broker=self._new_broker(workspace),
        )
        session.broker.approve = self._approval_handler(session)
        session.broker.ask_user = self._question_handler(session)
        self.sessions[session_id] = session
        return session

    # -------------------------------------------------------------- lifecycle

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any = None,
        client_info: Any = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        del client_capabilities, client_info, kwargs
        return InitializeResponse(
            protocol_version=min(protocol_version, PROTOCOL_VERSION),
            agent_capabilities=AgentCapabilities(load_session=True),
            auth_methods=[
                TerminalAuthMethod(
                    type="terminal",
                    id=LOGIN_METHOD_ID,
                    name="Log in to CopperPilot",
                    description=(
                        "Runs the device login (`copper-pilot auth login`), which opens a browser."
                    ),
                    args=["auth", "login"],
                )
            ],
            agent_info=Implementation(
                name="copper-pilot", title="CopperPilot", version=__version__
            ),
        )

    async def authenticate(self, method_id: str, **kwargs: Any) -> AuthenticateResponse | None:
        del kwargs
        if method_id != LOGIN_METHOD_ID:
            raise RequestError.invalid_params({"details": f"Unknown auth method {method_id!r}."})
        self._require_credential()
        return AuthenticateResponse()

    async def new_session(
        self,
        cwd: str,
        additional_directories: Any = None,
        mcp_servers: Any = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        del additional_directories, mcp_servers, kwargs
        workspace = self._workspace(cwd)
        self._require_credential()
        session = self._register(generate_thread_id(), workspace)
        return NewSessionResponse(
            session_id=session.session_id,
            modes=mode_state(session.mode),
            config_options=config_options(session.mode, session.effort),
        )

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: Any = None,
        additional_directories: Any = None,
        **kwargs: Any,
    ) -> LoadSessionResponse | None:
        del mcp_servers, additional_directories, kwargs
        workspace = self._workspace(cwd)
        credential = self._require_credential()
        agent = CopperPilotAgent(workspace, credential=credential, conversation_id=session_id)
        async with create_copper_graph(agent) as runtime:
            state = await runtime.aget_state(runtime.config(session_id, workspace))
        values = getattr(state, "values", None) or {}
        messages = values.get("messages") or []
        if not messages:
            raise RequestError.resource_not_found(session_id)
        mode = _coerce_mode(values.get("copper_mode"), self.default_mode)
        effort = normalize_effort(values.get("copper_effort") or self.default_effort)
        session = self._register(session_id, workspace, mode=mode, effort=effort)
        for update in replay_updates(messages):
            await self.conn.session_update(session_id=session_id, update=update)
        return LoadSessionResponse(
            modes=mode_state(session.mode),
            config_options=config_options(session.mode, session.effort),
        )

    async def set_session_mode(
        self, session_id: str, mode_id: str, **kwargs: Any
    ) -> SetSessionModeResponse | None:
        del kwargs
        session = self._session(session_id)
        session.mode = _parse_mode(mode_id)
        return SetSessionModeResponse()

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: Any
    ) -> SetSessionConfigOptionResponse | None:
        del kwargs
        session = self._session(session_id)
        if config_id == MODE_CONFIG_ID:
            session.mode = _parse_mode(str(value))
        elif config_id == EFFORT_CONFIG_ID:
            session.effort = _parse_effort(str(value))
        else:
            raise RequestError.invalid_params({"details": f"Unknown config option {config_id!r}."})
        return SetSessionConfigOptionResponse(
            config_options=config_options(session.mode, session.effort)
        )

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        del kwargs
        session = self.sessions.get(session_id)
        if session is None or not session.busy:
            return
        session.cancelled = True
        if session.runtime is not None:
            try:
                await session.runtime.acancel()
            except Exception as exc:  # noqa: BLE001 - best effort hosted cancel
                logger.warning("Hosted cancellation failed (%s)", type(exc).__name__)
        await session.broker.cancel_all()
        if session.turn is not None:
            session.turn.cancel()

    # ------------------------------------------------------------------ prompt

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> PromptResponse:
        del kwargs
        session = self._session(session_id)
        if session.busy:
            raise RequestError.invalid_request({"details": "A turn is already running."})
        credential = self._require_credential()
        message = prompt_text(prompt)
        if not message:
            raise RequestError.invalid_params({"details": "The prompt has no text content."})
        session.cancelled = False
        session.subagents = {}
        session.failure = None
        session.turn = asyncio.create_task(self._run_turn(session, credential, message))
        try:
            await session.turn
        except asyncio.CancelledError:
            if not session.cancelled:
                raise
        except Exception as exc:
            if session.cancelled:
                # The hosted side may close the socket before our own task
                # cancellation lands; a cancelled turn is not an error.
                logger.info("Hosted turn ended after cancellation (%s)", type(exc).__name__)
            else:
                raise self._map_turn_failure(exc) from exc
        finally:
            session.turn = None
            session.runtime = None
        return PromptResponse(stop_reason="cancelled" if session.cancelled else "end_turn")

    @staticmethod
    def _map_turn_failure(exc: Exception) -> RequestError:
        if isinstance(exc, ProtocolError):
            logger.warning("Hosted turn failed: %s", exc)
            return _turn_error(str(exc), auth=exc.status in {401, 403})
        if isinstance(exc, InvalidStatus):
            # The websocket handshake itself was refused (for example a revoked key).
            status = exc.response.status_code
            logger.warning("Hosted transport refused the connection (HTTP %s)", status)
            message = f"The hosted service refused the connection (HTTP {status})."
            return _turn_error(message, auth=status in {401, 403})
        if isinstance(exc, (RuntimeError, OSError, httpx.HTTPError)):
            logger.warning("Hosted turn failed: %s", exc)
            return _turn_error(str(exc) or type(exc).__name__)
        logger.exception("Hosted turn failed with %s", type(exc).__name__)
        return _turn_error(f"{type(exc).__name__}: {exc}".strip(": "))

    async def _run_turn(
        self, session: AcpSession, credential: DeviceCredential, message: str
    ) -> None:
        agent = CopperPilotAgent(
            session.workspace,
            credential=credential,
            conversation_id=session.session_id,
            mode=session.mode,
            effort=session.effort,
            tool_broker=session.broker,
        )
        failure: str | None = None
        async with create_copper_graph(agent) as runtime:
            session.runtime = runtime
            config = runtime.config(session.session_id, session.workspace)
            values = {
                "messages": [HumanMessage(content=message)],
                "copper_mode": session.mode.value,
                "copper_effort": session.effort.value,
            }
            async for item in runtime.astream(values, config):
                _namespace, stream_mode, data = item
                if stream_mode != "custom" or not isinstance(data, dict):
                    continue
                failure = await self._emit(session, data) or failure
            state = await runtime.aget_state(config)
        values = getattr(state, "values", None) or {}
        new_mode = _coerce_mode(values.get("copper_mode"), session.mode)
        if new_mode is not session.mode:
            session.mode = new_mode
            await self.conn.session_update(
                session_id=session.session_id, update=update_current_mode(new_mode.value)
            )
        if failure and not session.cancelled:
            payload = session.failure
            if isinstance(payload, dict):
                raise ProtocolError(
                    failure,
                    code=str(payload["code"]) if payload.get("code") is not None else None,
                    status=payload.get("status")
                    if isinstance(payload.get("status"), int)
                    else None,
                    block_reason=payload.get("block_reason"),
                    can_purchase_overage=payload.get("can_purchase_overage"),
                    retryable=payload.get("retryable"),
                )
            raise RuntimeError(failure)

    async def _emit(self, session: AcpSession, event: dict[str, Any]) -> str | None:
        """Translate one hosted event into ACP session updates; return an error."""
        kind = str(event.get("type") or "").removeprefix("copper.")
        payload = event.get("data")
        send = self.conn.session_update
        session_id = session.session_id
        if kind == EventKind.TEXT.value:
            if payload:
                await send(session_id=session_id, update=update_agent_message_text(str(payload)))
        elif kind in {EventKind.REASONING.value, EventKind.STATUS.value}:
            if payload:
                await send(session_id=session_id, update=update_agent_thought_text(str(payload)))
        elif kind == EventKind.STATUS_EVENT.value and isinstance(payload, dict):
            if payload.get("kind") == "subagent":
                identifier = str(payload.get("id") or "")
                status = normalize_subagent_status(payload, session.subagents.get(identifier))
                if status is not None:
                    session.subagents[status.id] = status
                    await send(
                        session_id=session_id,
                        update=update_agent_thought_text(subagent_progress_label(status)),
                    )
            elif payload.get("kind") == "plan_mode_switch":
                await send(
                    session_id=session_id, update=update_agent_thought_text("Switched to Plan mode")
                )
        elif kind == EventKind.TOOL_REQUEST.value and isinstance(payload, dict):
            arguments = payload.get("arguments") or {}
            name = str(payload.get("tool_name") or "tool")
            await send(
                session_id=session_id,
                update=start_tool_call(
                    str(payload.get("tool_call_id") or ""),
                    tool_title(name, arguments),
                    kind=tool_kind(name),
                    status="in_progress",
                    locations=tool_locations(arguments),
                    raw_input=arguments,
                ),
            )
        elif kind == EventKind.TOOL_RESULT.value and isinstance(payload, dict):
            text, is_error, raw = _tool_result_payload(payload.get("result"))
            await send(
                session_id=session_id,
                update=update_tool_call(
                    str(payload.get("tool_call_id") or ""),
                    status="failed" if is_error else "completed",
                    content=[tool_content(text_block(text))] if text else None,
                    raw_output=raw,
                ),
            )
        elif kind == EventKind.ERROR.value:
            session.failure = payload
            return error_message(payload)
        elif event.get("type") == RETRY_EVENT:
            label = str(event.get("title") or "Retrying the hosted turn")
            # Thought chunks are concatenated by clients; keep each retry line separate.
            await send(session_id=session_id, update=update_agent_thought_text(f"{label}\n"))
        elif kind == EventKind.FINAL.value and isinstance(payload, dict):
            if payload.get("success") is False or payload.get("error"):
                return str(payload.get("error") or "hosted failure")
        return None

    # -------------------------------------------------------- broker handlers

    def _approval_handler(
        self, session: AcpSession
    ) -> Callable[[ApprovalRequest], Awaitable[bool]]:
        async def approve(request: ApprovalRequest) -> bool:
            response = await self.conn.request_permission(
                session_id=session.session_id,
                tool_call=ToolCallUpdate(
                    tool_call_id=request.tool_call_id,
                    title=tool_title(request.tool_name, request.arguments),
                    kind=tool_kind(request.tool_name),
                    status="pending",
                    locations=tool_locations(request.arguments),
                    raw_input=request.arguments,
                ),
                options=[
                    PermissionOption(option_id=OPTION_ALLOW, name="Allow", kind="allow_once"),
                    PermissionOption(
                        option_id=OPTION_ALLOW_SESSION,
                        name="Allow all for this session",
                        kind="allow_always",
                    ),
                    PermissionOption(option_id=OPTION_REJECT, name="Reject", kind="reject_once"),
                ],
            )
            choice = _selected_option(response)
            if choice == OPTION_ALLOW_SESSION:
                session.broker.mode = ApprovalMode.YOLO
                return True
            return choice == OPTION_ALLOW

        return approve

    def _question_handler(self, session: AcpSession) -> Callable[[Any], Awaitable[Any]]:
        async def ask(arguments: Any) -> Any:
            parsed = question_options(arguments)
            if parsed is None:
                raise ToolRejected(QUESTION_MESSAGE)
            prompt, options = parsed
            response = await self.conn.request_permission(
                session_id=session.session_id,
                tool_call=ToolCallUpdate(
                    tool_call_id=f"question-{uuid.uuid4().hex}",
                    title=prompt,
                    kind="other",
                    status="pending",
                    raw_input=arguments,
                ),
                options=[
                    *(
                        PermissionOption(
                            option_id=f"option:{index}", name=option, kind="allow_once"
                        )
                        for index, option in enumerate(options)
                    ),
                    PermissionOption(option_id=OPTION_SKIP, name="Skip", kind="reject_once"),
                ],
            )
            choice = _selected_option(response)
            if choice is None or not choice.startswith("option:"):
                return {"status": "skipped"}
            index = int(choice.partition(":")[2])
            if index >= len(options):
                return {"status": "skipped"}
            return {"status": "answered", "answer": options[index], "source": "option"}

        return ask


def _selected_option(response: Any) -> str | None:
    outcome = getattr(response, "outcome", None)
    if getattr(outcome, "outcome", None) != "selected":
        return None
    option_id = getattr(outcome, "option_id", None)
    return str(option_id) if option_id is not None else None


def _parse_mode(value: str) -> CopperMode:
    try:
        return CopperMode(value)
    except ValueError as exc:
        raise RequestError.invalid_params({"details": f"Unknown mode {value!r}."}) from exc


def _parse_effort(value: str) -> CopperEffort:
    try:
        return CopperEffort(value)
    except ValueError as exc:
        raise RequestError.invalid_params({"details": f"Unknown effort {value!r}."}) from exc


def _coerce_mode(value: Any, fallback: CopperMode) -> CopperMode:
    try:
        return CopperMode(str(value)) if value else fallback
    except ValueError:
        return fallback


def _turn_error(message: str, *, auth: bool = False) -> RequestError:
    """Build the JSON-RPC error for a failed turn.

    Clients such as T3 Code display only the error ``message``, so carry the
    hosted explanation there instead of the generic SDK wording.
    """
    code = -32000 if auth else -32603
    return RequestError(code, message, {"details": message})


def replay_updates(messages: list[Any]) -> list[Any]:
    """Project checkpointed messages into ACP history updates for session/load."""
    updates: list[Any] = []
    for message in messages:
        kind = getattr(message, "type", None)
        content = getattr(message, "content", "")
        text = content if isinstance(content, str) else repr(content)
        if kind == "human":
            if text:
                updates.append(update_user_message_text(text))
        elif kind == "ai":
            if text:
                updates.append(update_agent_message_text(text))
            for call in getattr(message, "tool_calls", None) or []:
                name = str(call.get("name") or "tool")
                arguments = call.get("args") or {}
                updates.append(
                    start_tool_call(
                        str(call.get("id") or ""),
                        tool_title(name, arguments),
                        kind=tool_kind(name),
                        status="completed",
                        locations=tool_locations(arguments),
                        raw_input=arguments,
                    )
                )
        elif kind == "tool":
            status = getattr(message, "status", "success")
            updates.append(
                update_tool_call(
                    str(getattr(message, "tool_call_id", "") or ""),
                    status="failed" if status == "error" else "completed",
                    content=[tool_content(text_block(text[:_MAX_TOOL_OUTPUT_CHARS]))]
                    if text
                    else None,
                )
            )
    return updates


def run_acp(approval: ApprovalMode = ApprovalMode.AUTO) -> None:
    """Serve ACP over stdin/stdout. Do not write to stdout."""
    agent = cast(Agent, CopperAcpAgent(approval=approval))
    asyncio.run(run_agent(agent))
