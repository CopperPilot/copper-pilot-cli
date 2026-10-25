"""Stdio Agent Client Protocol (ACP) server for editors such as Zed.

The server speaks JSON-RPC over stdin/stdout, so nothing in this module may
write to stdout. One ACP session is one CopperPilot thread: the ACP session id
is the checkpoint thread id, and the session ``cwd`` is the workspace.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import httpx
from acp import PROTOCOL_VERSION, Agent, Client, RequestError, run_agent
from acp.helpers import (
    plan_entry,
    start_tool_call,
    text_block,
    tool_content,
    update_agent_message_text,
    update_agent_thought_text,
    update_available_commands,
    update_current_mode,
    update_plan,
    update_tool_call,
    update_user_message_text,
)
from acp.schema import (
    AgentCapabilities,
    AuthenticateResponse,
    AvailableCommand,
    ConfigOptionUpdate,
    ElicitationFormSessionMode,
    ElicitationSchema,
    ElicitationStringPropertySchema,
    Implementation,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PermissionOption,
    PlanEntry,
    PlanEntryStatus,
    PromptCapabilities,
    PromptResponse,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SessionMode,
    SessionModeState,
    SetSessionConfigOptionResponse,
    SetSessionModeResponse,
    TerminalAuthMethod,
    ToolCallLocation,
    ToolCallStatus,
    ToolCallUpdate,
    UnstructuredCommandInput,
    UsageUpdate,
)
from langchain_core.messages import HumanMessage
from websockets.exceptions import InvalidStatus

from copper_pilot_cli._version import __version__
from copper_pilot_cli.copper_api import (
    format_token_limits,
    is_usage_block,
    limits_reset_time,
    token_limits,
    usage_limit_message,
)
from copper_pilot_cli.copper_auth import AuthenticationError, DeviceCredential, load_credential
from copper_pilot_cli.copper_features import (
    encode_runtime_context,
    parse_plan,
    path_first_attachment,
)
from copper_pilot_cli.copper_graph import RETRY_EVENT, create_copper_graph
from copper_pilot_cli.copper_models import (
    ChatModelCatalog,
    cached_chat_model_catalog,
    is_model_fallback_event,
    load_chat_model_catalog,
    model_fallback_notice,
    model_selector_enabled,
    normalize_model_id,
)
from copper_pilot_cli.copper_preferences import shell_auto_allow_settings
from copper_pilot_cli.copper_protocol import (
    CopperEffort,
    CopperMode,
    EventKind,
    PlanContext,
    ProtocolError,
    error_message,
    normalize_effort,
)
from copper_pilot_cli.copper_status_events import (
    SubagentStatus,
    normalize_subagent_status,
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
    "This clarification needs a free-text answer, which this ACP client cannot "
    "collect. Continue this thread in the CopperPilot TUI with `copper-pilot --resume`."
)
QUESTION_TIMEOUT_SECONDS = 100.0
QUESTION_TIMEOUT_MESSAGE = (
    "No answer arrived before the hosted two-minute tool limit. End this turn by asking "
    "the question in your reply so the user can answer in their next message."
)
MODE_CONFIG_ID = "mode"
EFFORT_CONFIG_ID = "effort"
MODEL_CONFIG_ID = "model"
EFFORT_COMMAND = "effort"
MODEL_COMMAND = "model"
USAGE_COMMAND = "usage"
# JSON-RPC error code for a turn refused by a usage limit. There is no ACP
# standard; -32003 matches xAI's Grok ACP agent, which T3 Code already reads
# as a usage limit. ``data`` carries ``class``/``resetAt`` for any client.
USAGE_LIMIT_ERROR_CODE = -32003
OPTION_ALLOW = "allow"
OPTION_ALLOW_SESSION = "allow_session"
OPTION_REJECT = "reject"
OPTION_SKIP = "skip"
IMAGE_ONLY_QUERY = "Analyze the attached image."
ATTACHMENT_DIR = Path(".copperpilot") / "attachments"
_MAX_TOOL_OUTPUT_CHARS = 4_000
_IMAGE_SUFFIX = re.compile(r"^[a-z0-9]{1,10}$")
_EFFORT_COMMAND = re.compile(r"^/effort(?:\s+(\S+))?\s*$", re.IGNORECASE)
_MODEL_COMMAND = re.compile(r"^/model(?:\s+(\S+))?\s*$", re.IGNORECASE)
_LIMITS_TIMEOUT_SECONDS = 5.0
_USAGE_COMMAND = re.compile(r"^/usage\s*$", re.IGNORECASE)
_USER_REQUEST = re.compile(r"<user_request>\s*(.*?)\s*</user_request>", re.DOTALL)
_PLAN_ARTIFACT = re.compile(
    r"(?:^|[\\/])\.copperpilot[\\/]plans[\\/][^\\/]+\.plan\.md$", re.IGNORECASE
)
_PLAN_WRITERS = {"write_file", "edit_file"}
_PLAN_TODO_STATUSES: dict[str, PlanEntryStatus] = {
    "open": "pending",
    "running": "in_progress",
    "done": "completed",
}
_SUBAGENT_TOOL_STATUSES: dict[str, ToolCallStatus] = {
    "running": "in_progress",
    "success": "completed",
    "failed": "failed",
    "cancelled": "failed",
}

_MODE_LABELS: dict[CopperMode, tuple[str, str]] = {
    CopperMode.ASK: ("Ask", "Read-oriented electronics questions; no design changes."),
    CopperMode.PLAN: ("Plan", "Propose a design plan without applying it."),
    CopperMode.AGENT: ("Agent", "Apply schematic and PCB changes in the workspace."),
}
_EFFORT_LABELS: dict[CopperEffort, tuple[str, str]] = {
    CopperEffort.BALANCED: ("Balanced", "Default effort; always runs on the Balanced model."),
    CopperEffort.INTELLIGENCE: (
        "Intelligence",
        "Deep survey on the selected model; requires a paid plan.",
    ),
}
# Effort copy that names no model, used while the model selector is off.
_EFFORT_LABELS_WITHOUT_MODELS: dict[CopperEffort, tuple[str, str]] = {
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
    model: str | None = None
    turn: asyncio.Task[Any] | None = None
    runtime: Any = None
    cancelled: bool = False
    subagents: dict[str, SubagentStatus] = field(default_factory=dict)
    tool_paths: dict[str, str] = field(default_factory=dict)
    last_usage: dict[str, Any] | None = None
    commands_sent: bool = False
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


def config_options(
    mode: CopperMode,
    effort: CopperEffort,
    model: str | None = None,
    catalog: ChatModelCatalog | None = None,
) -> list[SessionConfigOptionSelect]:
    """Expose mode, effort, and model as ACP config selectors.

    The ``model`` option uses the ``model`` category, so clients such as T3
    Code feed it to their own model picker. Its current value is always the
    model the next turn will run on: the Balanced model while effort is
    Balanced, otherwise the selected Intelligence model. While the model
    selector is off only mode and effort are exposed.
    """
    source = catalog or cached_chat_model_catalog()
    show_models = model_selector_enabled()
    effort_labels = _EFFORT_LABELS if show_models else _EFFORT_LABELS_WITHOUT_MODELS
    options = [
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
                for item, (name, description) in effort_labels.items()
            ],
        ),
    ]
    if not show_models:
        return options
    options.append(
        SessionConfigOptionSelect(
            id=MODEL_CONFIG_ID,
            name="Model",
            description="Intelligence model; choosing one selects Intelligence (paid plan)",
            category="model",
            type="select",
            current_value=source.effective_model(effort, model),
            options=[
                SessionConfigSelectOption(
                    value=item.id, name=item.label, description=source.describe(item)
                )
                for item in source.models
            ],
        )
    )
    return options


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


def first_text_block(blocks: list[Any]) -> str:
    """Return the first text block, which carries the user's own words.

    Clients append context in later blocks (T3 Code adds ``<runtime_info>`` and
    tool guidance), so only the first block can hold a slash command.
    """
    for block in blocks:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text.strip():
            return text
    return ""


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


def image_suffix(mime_type: Any) -> str:
    """Return a file suffix for an image MIME type, defaulting to ``png``."""
    subtype = str(mime_type or "").partition("/")[2].split(";")[0].split("+")[0].strip().lower()
    subtype = "jpg" if subtype == "jpeg" else subtype
    return subtype if _IMAGE_SUFFIX.match(subtype) else "png"


def prompt_images(blocks: list[Any]) -> list[tuple[bytes, str]]:
    """Decode the ``image`` prompt blocks into ``(bytes, suffix)`` pairs."""
    images: list[tuple[bytes, str]] = []
    for block in blocks:
        if getattr(block, "type", None) != "image":
            continue
        try:
            payload = base64.b64decode(str(getattr(block, "data", "") or ""), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RequestError.invalid_params(
                {"details": "An image block is not valid base64."}
            ) from exc
        if not payload:
            raise RequestError.invalid_params({"details": "An image block is empty."})
        images.append((payload, image_suffix(getattr(block, "mime_type", None))))
    return images


def save_attachments(images: list[tuple[bytes, str]], workspace: Path) -> list[dict[str, Any]]:
    """Write prompt images under the workspace and describe them path-first.

    The hosted agent reads attachments through its file tools, the same way the
    TUI passes pasted clipboard images, so no image bytes go over the socket.
    """
    if not images:
        return []
    root = workspace / ATTACHMENT_DIR
    root.mkdir(parents=True, exist_ok=True)
    attachments: list[dict[str, Any]] = []
    for payload, suffix in images:
        file = root / f"{uuid.uuid4()}.{suffix}"
        file.write_bytes(payload)
        attachments.append(path_first_attachment(file, workspace))
    return attachments


def with_attachment_paths(message: str, attachments: list[dict[str, Any]]) -> str:
    """Name each saved image in the query so the agent reads the workspace copy.

    Clients such as T3 Code also mention their own copy, which lives outside the
    workspace where the hosted agent's file tools are rejected.
    """
    if not attachments:
        return message
    lines = "\n".join(f"- {item['path']}" for item in attachments)
    return f"{message}\n\nAttached images, saved in the workspace:\n{lines}"


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


def available_commands() -> list[AvailableCommand]:
    """Slash commands the server answers locally, without a hosted turn."""
    commands = [
        AvailableCommand(
            name=EFFORT_COMMAND,
            description="Set hosted effort: balanced (default) or intelligence (paid plan).",
            input=UnstructuredCommandInput(hint="balanced | intelligence"),
        ),
    ]
    if model_selector_enabled():
        commands.append(
            AvailableCommand(
                name=MODEL_COMMAND,
                description=(
                    "Pick the Intelligence model by id (selects Intelligence); "
                    "`/model` alone lists the ids."
                ),
                input=UnstructuredCommandInput(hint="model id"),
            )
        )
    commands.append(
        AvailableCommand(
            name=USAGE_COMMAND,
            description="Show the CopperPilot usage window, weekly balance, and reset times.",
        )
    )
    return commands


def user_request_text(message: str) -> str:
    """Unwrap the user's own text from a client instruction envelope.

    T3 Code may wrap a prompt as ``<t3_code_instructions>…</t3_code_instructions>``
    followed by ``<user_request>…</user_request>``.
    """
    match = _USER_REQUEST.search(message)
    return match.group(1) if match else message


def effort_command(message: str) -> tuple[bool, str | None]:
    """Return ``(is_effort_command, argument)`` for a prompt."""
    match = _EFFORT_COMMAND.match(user_request_text(message).strip())
    if match is None:
        return False, None
    return True, match.group(1)


def model_command(message: str) -> tuple[bool, str | None]:
    """Return ``(is_model_command, argument)`` for a prompt."""
    match = _MODEL_COMMAND.match(user_request_text(message).strip())
    if match is None:
        return False, None
    return True, match.group(1)


def usage_command(message: str) -> bool:
    """Return whether a prompt is the ``/usage`` command."""
    return _USAGE_COMMAND.match(user_request_text(message).strip()) is not None


def usage_update(payload: Any) -> UsageUpdate | None:
    """Map hosted per-turn token counts onto an ACP usage update.

    The hosted service reports the prompt tokens a turn consumed but not the
    model's context window, so ``size`` is 0 (unknown); clients such as T3 Code
    then show the count alone. The raw counts ride along in ``_meta``.
    """
    if not isinstance(payload, dict):
        return None
    used = next(
        (payload[key] for key in ("tokens_input", "input_tokens") if key in payload),
        None,
    )
    if not isinstance(used, int) or isinstance(used, bool) or used < 0:
        return None
    return UsageUpdate(session_update="usage_update", used=used, size=0, field_meta=dict(payload))


def is_plan_artifact(path: str) -> bool:
    """True for `.copperpilot/plans/<name>.plan.md` paths, relative or absolute."""
    return bool(_PLAN_ARTIFACT.search(path))


def plan_entries_from_todo_list(payload: dict[str, Any]) -> list[PlanEntry]:
    """Project a hosted ``todo_list`` status event onto ACP plan entries."""
    entries: list[PlanEntry] = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        status: PlanEntryStatus = "completed" if item.get("status") == "done" else "pending"
        entries.append(plan_entry(content, status=status))
    return entries


def plan_entries_from_plan(plan: PlanContext) -> list[PlanEntry]:
    """Project the todos of a plan artifact onto ACP plan entries."""
    return [
        plan_entry(todo.content, status=_PLAN_TODO_STATUSES.get(todo.status, "pending"))
        for todo in plan.todos
        if todo.content.strip()
    ]


def subagent_tool_update(status: SubagentStatus, *, started: bool) -> Any:
    """Render a hosted survey worker as an ACP tool call card.

    T3 Code and Zed render tool calls with a status and collapsible output,
    which matches the Electron client's subagent card better than thought text.
    """
    tool_status = _SUBAGENT_TOOL_STATUSES.get(status.status, "failed")
    body = status.error if status.status in {"failed", "cancelled"} else status.output
    content = [tool_content(text_block(body[:_MAX_TOOL_OUTPUT_CHARS]))] if body.strip() else None
    title = f"Survey: {status.task or 'Survey'}"
    tool_call_id = f"subagent:{status.id}"
    if not started:
        return start_tool_call(
            tool_call_id,
            title,
            kind="think",
            status=tool_status,
            content=content,
            raw_input={"task": status.task, "goal": status.goal},
        )
    return update_tool_call(tool_call_id, title=title, status=tool_status, content=content)


def elicitation_questions(arguments: Any) -> list[tuple[str, str]] | None:
    """Return ``(property id, prompt)`` rows for a free-text clarification."""
    if isinstance(arguments, dict):
        rows = arguments.get("questions")
        if isinstance(rows, list) and rows:
            questions = [
                (
                    f"q{index}",
                    str(row.get("question") or row.get("prompt") or "").strip()
                    or f"Question {index + 1}",
                )
                for index, row in enumerate(rows)
                if isinstance(row, dict)
            ]
            return questions or None
        prompt = str(arguments.get("question") or arguments.get("prompt") or "").strip()
    else:
        prompt = str(arguments or "").strip()
    return [("answer", prompt or "Clarification")]


def elicitation_schema(
    questions: list[tuple[str, str]], *, options: list[str] | None = None
) -> ElicitationSchema:
    """Build the ACP form schema for clarifying questions.

    ``options`` turns a single question into an enum so clients render choices.
    """
    single = len(questions) == 1
    return ElicitationSchema(
        properties={
            key: ElicitationStringPropertySchema(
                type="string",
                title="Answer" if single else f"Question {index + 1}",
                description=prompt,
                enum=list(options) if single and options else None,
            )
            for index, (key, prompt) in enumerate(questions)
        }
    )


def _supports_form_elicitation(capabilities: Any) -> bool:
    """Read ``clientCapabilities.elicitation.form`` from a model or a dict."""
    if isinstance(capabilities, dict):
        elicitation = capabilities.get("elicitation")
    else:
        elicitation = getattr(capabilities, "elicitation", None)
    if isinstance(elicitation, dict):
        return elicitation.get("form") is not None
    return getattr(elicitation, "form", None) is not None


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
        self.elicitation_form = False
        self.sessions: dict[str, AcpSession] = {}
        self._conn: Client | None = None

    @property
    def catalog(self) -> ChatModelCatalog:
        """Server model catalog when loaded, else the bundled list."""
        return cached_chat_model_catalog()

    def _config_options(self, session: AcpSession) -> list[SessionConfigOptionSelect]:
        return config_options(session.mode, session.effort, session.model, self.catalog)

    def _set_model(self, session: AcpSession, value: str) -> ChatModelCatalog:
        """Apply a model choice; any model but the Balanced one selects Intelligence."""
        catalog = self.catalog
        model = catalog.get(value)
        if model is None:
            raise RequestError.invalid_params(
                {"details": f"Unknown model {value!r}. Known ids: {', '.join(catalog.ids)}."}
            )
        session.model = model.id
        if session.effort is not CopperEffort.INTELLIGENCE and model.id != catalog.balanced_model:
            session.effort = CopperEffort.INTELLIGENCE
        return catalog

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
        model: str | None = None,
    ) -> AcpSession:
        context = discover_workspace(workspace)
        remember_workspace(context)
        session = AcpSession(
            session_id=session_id,
            workspace=workspace,
            mode=mode or self.default_mode,
            effort=effort or self.default_effort,
            model=normalize_model_id(model) or None,
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
        del client_info, kwargs
        self.elicitation_form = _supports_form_elicitation(client_capabilities)
        return InitializeResponse(
            protocol_version=min(protocol_version, PROTOCOL_VERSION),
            agent_capabilities=AgentCapabilities(
                load_session=True, prompt_capabilities=PromptCapabilities(image=True)
            ),
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
        credential = self._require_credential()
        if model_selector_enabled():
            await load_chat_model_catalog(credential)
        session = self._register(generate_thread_id(), workspace)
        return NewSessionResponse(
            session_id=session.session_id,
            modes=mode_state(session.mode),
            config_options=self._config_options(session),
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
        if model_selector_enabled():
            await load_chat_model_catalog(credential)
        agent = CopperPilotAgent(workspace, credential=credential, conversation_id=session_id)
        async with create_copper_graph(agent) as runtime:
            state = await runtime.aget_state(runtime.config(session_id, workspace))
        values = getattr(state, "values", None) or {}
        messages = values.get("messages") or []
        if not messages:
            raise RequestError.resource_not_found(session_id)
        mode = _coerce_mode(values.get("copper_mode"), self.default_mode)
        effort = normalize_effort(values.get("copper_effort") or self.default_effort)
        session = self._register(
            session_id, workspace, mode=mode, effort=effort, model=values.get("copper_model")
        )
        for update in replay_updates(messages):
            await self.conn.session_update(session_id=session_id, update=update)
        await self._announce_commands(session)
        return LoadSessionResponse(
            modes=mode_state(session.mode),
            config_options=self._config_options(session),
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
        elif config_id == MODEL_CONFIG_ID and model_selector_enabled():
            self._set_model(session, str(value))
        else:
            raise RequestError.invalid_params({"details": f"Unknown config option {config_id!r}."})
        return SetSessionConfigOptionResponse(config_options=self._config_options(session))

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
        images = prompt_images(prompt)
        if not message and not images:
            raise RequestError.invalid_params({"details": "The prompt has no text or images."})
        await self._announce_commands(session)
        first_block = first_text_block(prompt)
        is_command, argument = effort_command(first_block)
        if is_command:
            await self._apply_effort_command(session, argument)
            return PromptResponse(stop_reason="end_turn")
        is_command, argument = model_command(first_block)
        if is_command and model_selector_enabled():
            await self._apply_model_command(session, argument)
            return PromptResponse(stop_reason="end_turn")
        if usage_command(first_block):
            await self._answer_usage_command(session, credential)
            return PromptResponse(stop_reason="end_turn")
        attachments = save_attachments(images, session.workspace)
        session.cancelled = False
        session.subagents = {}
        session.tool_paths = {}
        session.failure = None
        query = with_attachment_paths(message or IMAGE_ONLY_QUERY, attachments)
        session.turn = asyncio.create_task(self._run_turn(session, credential, query, attachments))
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
                limited = await self._usage_limit_failure(exc, credential)
                raise (limited or self._map_turn_failure(exc)) from exc
        finally:
            session.turn = None
            session.runtime = None
        return PromptResponse(stop_reason="cancelled" if session.cancelled else "end_turn")

    @staticmethod
    async def _usage_limit_failure(
        exc: Exception, credential: DeviceCredential
    ) -> RequestError | None:
        """Explain a turn refused by a usage limit, including when it resets.

        The hosted error frame names the spent limit and its reset time. When
        it does not (a refused handshake, an older server) the token-limits
        endpoint fills them in; a bare 402 the account view does not confirm
        as blocked (the Intelligence plan gate) is left to the generic path.
        """
        if isinstance(exc, ProtocolError):
            status, reason = exc.status, exc.block_reason
            resets_at, can_purchase = exc.resets_at, exc.can_purchase_overage
            hosted, retryable = str(exc), exc.retryable
        elif isinstance(exc, InvalidStatus):
            status, reason, resets_at, can_purchase = exc.response.status_code, None, None, None
            hosted, retryable = "", None
        else:
            return None
        if status not in {402, 429} or (status == 429 and not reason):
            return None
        if resets_at is None and reason in {None, "window", "weekly"}:
            try:
                limits = await token_limits(credential, request_timeout=_LIMITS_TIMEOUT_SECONDS)
            except Exception as lookup:
                logger.info("Usage limits lookup failed (%s)", type(lookup).__name__)
            else:
                reason = reason or _optional_str(limits.get("block_reason"))
                resets_at = limits_reset_time({**limits, "block_reason": reason})
                if can_purchase is None and isinstance(limits.get("can_purchase_overage"), bool):
                    can_purchase = limits["can_purchase_overage"]
        if not is_usage_block(status, reason):
            return None
        message = usage_limit_message(
            block_reason=reason,
            resets_at=resets_at,
            can_purchase_overage=can_purchase,
            hosted_message=hosted,
        )
        logger.warning("Hosted turn refused by a usage limit (%s)", reason or status)
        return usage_limit_error(
            message,
            block_reason=reason,
            resets_at=resets_at,
            can_purchase_overage=can_purchase,
            retryable=bool(retryable),
        )

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
        self,
        session: AcpSession,
        credential: DeviceCredential,
        message: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        agent = CopperPilotAgent(
            session.workspace,
            credential=credential,
            conversation_id=session.session_id,
            mode=session.mode,
            effort=session.effort,
            model=session.model,
            runtime_context=encode_runtime_context(attachments=attachments),
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
                "copper_model": session.model or "",
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
                    resets_at=_optional_str(payload.get("resets_at")),
                )
            raise RuntimeError(failure)

    async def _announce_commands(self, session: AcpSession) -> None:
        """Advertise local slash commands once per session."""
        if session.commands_sent:
            return
        session.commands_sent = True
        await self.conn.session_update(
            session_id=session.session_id, update=update_available_commands(available_commands())
        )

    def _model_summary(self, session: AcpSession) -> str:
        catalog = self.catalog
        effective = catalog.effective_model(session.effort, session.model)
        return f"{catalog.label(effective)} (`{effective}`)"

    def _effort_summary(self, session: AcpSession) -> str:
        """Effort label, plus the model while the model selector is on."""
        label = _EFFORT_LABELS[session.effort][0]
        if not model_selector_enabled():
            return label
        return f"{label}; model {self._model_summary(session)}"

    async def _publish_config(self, session: AcpSession) -> None:
        await self.conn.session_update(
            session_id=session.session_id,
            update=ConfigOptionUpdate(
                session_update="config_option_update",
                config_options=self._config_options(session),
            ),
        )

    async def _apply_effort_command(self, session: AcpSession, argument: str | None) -> None:
        """Answer ``/effort [balanced|intelligence]`` without a hosted turn."""
        session_id = session.session_id
        if argument is None:
            text = (
                f"Effort is {self._effort_summary(session)}. "
                "Use `/effort balanced` or `/effort intelligence`."
            )
        else:
            try:
                session.effort = CopperEffort(argument.lower())
            except ValueError:
                text = f"Unknown effort {argument!r}; use `balanced` or `intelligence`."
            else:
                await self._publish_config(session)
                text = f"Effort set to {self._effort_summary(session)}."
        await self.conn.session_update(
            session_id=session_id, update=update_agent_message_text(text)
        )

    async def _apply_model_command(self, session: AcpSession, argument: str | None) -> None:
        """Answer ``/model [id]`` without a hosted turn."""
        catalog = self.catalog
        listing = "\n".join(
            f"- `{item.id}` — {item.label}"
            + (" (Intelligence default)" if item.id == catalog.default_intelligence_model else "")
            + (" (Balanced model)" if item.id == catalog.balanced_model else "")
            for item in catalog.models
        )
        if argument is None:
            text = (
                f"Model is {self._model_summary(session)} on "
                f"{_EFFORT_LABELS[session.effort][0]}. Use `/model <id>` to pick an "
                f"Intelligence model (paid plan):\n{listing}"
            )
        else:
            was_intelligence = session.effort is CopperEffort.INTELLIGENCE
            try:
                self._set_model(session, argument)
            except RequestError:
                text = f"Unknown model {argument!r}. Known ids:\n{listing}"
            else:
                await self._publish_config(session)
                text = f"Model set to {self._model_summary(session)}."
                if session.effort is CopperEffort.INTELLIGENCE and not was_intelligence:
                    text += " Effort set to Intelligence (paid plan)."
                elif session.effort is not CopperEffort.INTELLIGENCE:
                    text += " Effort stays Balanced."
        await self.conn.session_update(
            session_id=session.session_id, update=update_agent_message_text(text)
        )

    async def _answer_usage_command(
        self, session: AcpSession, credential: DeviceCredential
    ) -> None:
        """Answer ``/usage`` from the token-limits endpoint without a hosted turn."""
        try:
            limits = await token_limits(credential, request_timeout=_LIMITS_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.info("Usage limits lookup failed (%s)", type(exc).__name__)
            text = "Could not read CopperPilot usage limits right now; try again shortly."
        else:
            text = "\n".join(f"- {line}" for line in format_token_limits(limits).splitlines())
            reason = _optional_str(limits.get("block_reason"))
            if reason is not None:
                notice = usage_limit_message(
                    block_reason=reason,
                    resets_at=limits_reset_time(limits),
                    can_purchase_overage=bool(limits.get("can_purchase_overage")),
                    hosted_message=str(limits.get("public_message") or ""),
                )
                text = f"{notice}\n\n{text}"
        await self.conn.session_update(
            session_id=session.session_id, update=update_agent_message_text(text)
        )

    async def _emit_plan_artifact(self, session: AcpSession, path: str) -> None:
        """Publish the todos of a freshly written plan file as an ACP plan."""
        try:
            plan = parse_plan(session.workspace / path, session.workspace)
        except (OSError, ValueError) as exc:
            logger.debug("Plan artifact %s not published: %s", path, exc)
            return
        entries = plan_entries_from_plan(plan)
        if entries:
            await self.conn.session_update(
                session_id=session.session_id, update=update_plan(entries)
            )

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
                previous = session.subagents.get(identifier)
                status = normalize_subagent_status(payload, previous)
                if status is not None:
                    session.subagents[status.id] = status
                    await send(
                        session_id=session_id,
                        update=subagent_tool_update(status, started=previous is not None),
                    )
            elif payload.get("kind") == "todo_list":
                entries = plan_entries_from_todo_list(payload)
                if entries:
                    await send(session_id=session_id, update=update_plan(entries))
            elif payload.get("kind") == "plan_mode_switch":
                await send(
                    session_id=session_id, update=update_agent_thought_text("Switched to Plan mode")
                )
            elif is_model_fallback_event(payload):
                # No ACP system-message type exists; surface the server copy in the
                # transcript so the user knows which model answered.
                notice = model_fallback_notice(payload, self.catalog)
                await send(
                    session_id=session_id, update=update_agent_message_text(f"\n\n*{notice}*\n\n")
                )
        elif kind == EventKind.USAGE.value and isinstance(payload, dict):
            update = usage_update(payload)
            if update is not None and payload != session.last_usage:
                session.last_usage = dict(payload)
                await send(session_id=session_id, update=update)
        elif kind == EventKind.TOOL_REQUEST.value and isinstance(payload, dict):
            arguments = payload.get("arguments") or {}
            name = str(payload.get("tool_name") or "tool")
            tool_call_id = str(payload.get("tool_call_id") or "")
            locations = tool_locations(arguments)
            if locations and canonical_tool_name(name) in _PLAN_WRITERS:
                session.tool_paths[tool_call_id] = locations[0].path
            await send(
                session_id=session_id,
                update=start_tool_call(
                    tool_call_id,
                    tool_title(name, arguments),
                    kind=tool_kind(name),
                    status="in_progress",
                    locations=locations,
                    raw_input=arguments,
                ),
            )
        elif kind == EventKind.TOOL_RESULT.value and isinstance(payload, dict):
            text, is_error, raw = _tool_result_payload(payload.get("result"))
            tool_call_id = str(payload.get("tool_call_id") or "")
            await send(
                session_id=session_id,
                update=update_tool_call(
                    tool_call_id,
                    status="failed" if is_error else "completed",
                    content=[tool_content(text_block(text))] if text else None,
                    raw_output=raw,
                ),
            )
            path = session.tool_paths.pop(tool_call_id, None)
            if path and not is_error and is_plan_artifact(path):
                await self._emit_plan_artifact(session, path)
        elif kind == EventKind.ERROR.value:
            session.failure = payload
            return error_message(payload)
        elif event.get("type") == RETRY_EVENT:
            data = payload if isinstance(payload, dict) else {}
            if data.get("stage") == "failed" and not data.get("will_retry"):
                # The final attempt's failure becomes the turn error; a thought
                # repeating it would show the raw hosted wording twice.
                return None
            label = str(event.get("title") or "Retrying the hosted turn")
            # Thought chunks are concatenated by clients; keep each retry line separate.
            await send(session_id=session_id, update=update_agent_thought_text(f"{label}\n"))
        elif kind == EventKind.FINAL.value and isinstance(payload, dict):
            if payload.get("success") is False or payload.get("error"):
                message = str(payload.get("error") or "hosted failure")
                # The hosted copy is generic; the turn id lets the service
                # owners find the server-side cause.
                turn_id = event.get("turn_id")
                return f"{message} (hosted turn {turn_id})" if turn_id else message
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

    async def _await_answer(self, request: Coroutine[Any, Any, Any]) -> Any | None:
        """Bound a question to the hosted tool-result window (two minutes).

        The hosted service drops the turn when a tool result takes longer, so an
        unanswered question is skipped first and the model is told to ask again
        in its reply.
        """
        try:
            return await asyncio.wait_for(request, QUESTION_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning(
                "No answer within %.0fs; skipping the question", QUESTION_TIMEOUT_SECONDS
            )
            return None

    async def _ask_elicitation(self, session: AcpSession, arguments: Any) -> Any:
        """Collect an answer through ACP form elicitation, with or without options.

        Permission prompts are auto-answered by clients running in a hands-off
        mode (T3 Code full access picks the first option), so a form is the only
        way to make sure a human sees the question.
        """
        parsed = question_options(arguments)
        options: list[str] = []
        multi = False
        if parsed is not None:
            prompt, options = parsed
            questions: list[tuple[str, str]] | None = [("answer", prompt)]
        else:
            questions = elicitation_questions(arguments)
            multi = isinstance(arguments, dict) and isinstance(arguments.get("questions"), list)
        if questions is None:
            raise ToolRejected(QUESTION_MESSAGE)
        message = "CopperPilot needs a few answers to continue." if multi else questions[0][1]
        response = await self._await_answer(
            self.conn.create_elicitation(
                message,
                ElicitationFormSessionMode(
                    session_id=session.session_id,
                    requested_schema=elicitation_schema(questions, options=options),
                ),
            )
        )
        if response is None:
            return {"status": "skipped", "reason": QUESTION_TIMEOUT_MESSAGE}
        content = (
            getattr(response, "content", None)
            if getattr(response, "action", None) == "accept"
            else None
        )
        answers = content if isinstance(content, dict) else {}
        if multi:
            return {
                "answers": [
                    {"question": prompt, "answer": str(answers.get(key) or "").strip()}
                    for key, prompt in questions
                ]
            }
        answer = str(answers.get("answer") or "").strip()
        if not answer:
            return {"status": "skipped"}
        source = "option" if answer in options else "custom"
        return {"status": "answered", "answer": answer, "source": source}

    def _question_handler(self, session: AcpSession) -> Callable[[Any], Awaitable[Any]]:
        async def ask(arguments: Any) -> Any:
            if self.elicitation_form:
                return await self._ask_elicitation(session, arguments)
            parsed = question_options(arguments)
            if parsed is None:
                raise ToolRejected(QUESTION_MESSAGE)
            prompt, options = parsed
            response = await self._await_answer(
                self.conn.request_permission(
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
            )
            if response is None:
                return {"status": "skipped", "reason": QUESTION_TIMEOUT_MESSAGE}
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


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None and str(value).strip() else None


def usage_limit_error(
    message: str,
    *,
    block_reason: str | None,
    resets_at: str | None,
    can_purchase_overage: bool | None,
    retryable: bool,
) -> RequestError:
    """Build the JSON-RPC error for a turn refused by a usage limit.

    The message is complete on its own (clients such as T3 Code show only the
    message); ``data`` adds machine-readable fields for clients that render a
    usage-limit notice or schedule a retry at ``resetAt``.
    """
    data: dict[str, Any] = {
        "details": message,
        "class": "usage_limit",
        "blockReason": block_reason,
        "resetAt": resets_at,
        "canPurchaseOverage": can_purchase_overage,
        "retryable": retryable,
    }
    return RequestError(USAGE_LIMIT_ERROR_CODE, message, data)


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
