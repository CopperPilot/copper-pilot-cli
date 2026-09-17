"""Hosted CopperPilot tool requests dispatched through Deep Agents native tools."""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import re
import shlex
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from deepagents.backends import LocalShellBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool

from copper_pilot_cli.copper_protocol import ToolRequest


class ApprovalMode(StrEnum):
    """Local side-effect approval mode."""

    MANUAL = "manual"
    AUTO = "auto"
    YOLO = "yolo"


class ToolRejected(RuntimeError):
    """The user or local policy rejected a tool."""


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """One hosted tool call awaiting local review."""

    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    reason: str
    canonical_name: str | None = None


ApprovalHandler = Callable[[ApprovalRequest], Awaitable[bool]]
QuestionHandler = Callable[[Any], Awaitable[Any]]

_ALIASES = {
    "read": "read_file",
    "write": "write_file",
    "edit": "edit_file",
    "delete_file": "delete",
    "bash": "execute",
}
_READ_TOOLS = {"ls", "read_file", "glob", "grep", "read_binary_file"}
_MUTATION_TOOLS = {"write_file", "edit_file", "delete", "write_binary_file"}
_SHELL_CONTROL_RE = re.compile(r"(?:\n|\r|&&|\|\||[;&|`<>]|\$\(|\$\{)")
_ROUTINE_WRITE_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".css",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".ipynb",
        ".java",
        ".js",
        ".jsx",
        ".json",
        ".kt",
        ".md",
        ".mdx",
        ".php",
        ".proto",
        ".py",
        ".rb",
        ".rs",
        ".rst",
        ".scss",
        ".sql",
        ".swift",
        ".tex",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".vue",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_DEPENDENCY_FILES = frozenset(
    {
        "cargo.lock",
        "cargo.toml",
        "go.mod",
        "go.sum",
        "package-lock.json",
        "package.json",
        "pnpm-lock.yaml",
        "poetry.lock",
        "pyproject.toml",
        "requirements.txt",
        "uv.lock",
        "yarn.lock",
    }
)
_SENSITIVE_PARTS = frozenset(
    {
        ".agents",
        ".buildkite",
        ".circleci",
        ".claude",
        ".copperpilot",
        ".deepagents",
        ".devcontainer",
        ".git",
        ".github",
        ".husky",
        ".ssh",
        ".vscode",
        "cron.d",
        "hooks",
        "launchagents",
        "launchdaemons",
        "systemd",
    }
)
_SENSITIVE_NAMES = frozenset(
    {
        ".bash_profile",
        ".bashrc",
        ".env",
        ".mcp.json",
        ".pre-commit-config.yaml",
        ".profile",
        ".zshrc",
        "action.yaml",
        "action.yml",
        "agents.md",
        "authorized_keys",
        "claude.md",
        "codeowners",
        "compose.yaml",
        "compose.yml",
        "conftest.py",
        "docker-compose.yaml",
        "docker-compose.yml",
        "dockerfile",
        "noxfile.py",
        "setup.py",
        "sitecustomize.py",
        "sudoers",
        "tox.ini",
        "usercustomize.py",
    }
)
_SCRIPT_SUFFIXES = frozenset({".bash", ".bat", ".cmd", ".command", ".fish", ".ps1", ".sh", ".zsh"})
_DANGEROUS_EXECUTABLES = frozenset(
    {"rm", "dd", "mv", "cp", "chmod", "chown", "format", "mkfs", "shred", "git"}
)


def canonical_tool_name(tool_name: str) -> str:
    """Map the hosted harness vocabulary to Deep Agents tool names."""

    return _ALIASES.get(tool_name, tool_name)


def _resolve_path(root: Path, raw: object) -> Path | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        return candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


def _is_within(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _routine_write_allowed(root: Path, arguments: dict[str, Any]) -> bool:
    path = _resolve_path(root, arguments.get("file_path"))
    if path is None or not _is_within(root, path):
        return False
    relative_parts = tuple(part.lower() for part in path.relative_to(root).parts)
    if any(part in _SENSITIVE_PARTS for part in relative_parts):
        return False
    if path.name.lower() in _SENSITIVE_NAMES | _DEPENDENCY_FILES:
        return False
    return path.suffix.lower() in _ROUTINE_WRITE_SUFFIXES - _SCRIPT_SUFFIXES


def _read_only_git_allowed(command: object, root: Path) -> bool:
    if not isinstance(command, str) or not command.strip() or _SHELL_CONTROL_RE.search(command):
        return False
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if len(parts) < 2 or parts[0] != "git":
        return False
    if parts[1] not in {"diff", "log", "ls-files", "rev-parse", "show", "status"}:
        return False
    for token in parts[2:]:
        candidate = token.split("=", 1)[-1] if "=" in token else token
        if not (
            candidate.startswith(("/", "~", "../", "..\\"))
            or "/../" in candidate
            or "\\..\\" in candidate
        ):
            continue
        path = _resolve_path(root, candidate)
        if path is None or not _is_within(root, path):
            return False
    return True


def dangerous_shell_command(command: object) -> bool:
    """Classify shell commands using the desktop client's conservative tier."""
    if not isinstance(command, str) or not command.strip():
        return False
    try:
        parts = shlex.split(command)
    except ValueError:
        return True
    if not parts:
        return False
    executable = Path(parts[0]).name.lower()
    return executable in _DANGEROUS_EXECUTABLES


def routine_action(
    tool_name: str,
    arguments: dict[str, Any],
    workspace: Path | None = None,
) -> bool:
    """Pinned dcode deterministic Auto policy for locally supported tools."""

    root = (workspace or Path.cwd()).resolve()
    canonical = canonical_tool_name(tool_name)
    if canonical in _READ_TOOLS:
        return True
    if canonical in {"write_file", "edit_file"}:
        return _routine_write_allowed(root, arguments)
    if canonical == "execute":
        return _read_only_git_allowed(arguments.get("command"), root)
    return False


class LocalToolBroker:
    """Thin CopperPilot protocol adapter over Deep Agents filesystem tools."""

    def __init__(
        self,
        workspace: Path,
        *,
        mode: ApprovalMode = ApprovalMode.MANUAL,
        approve: ApprovalHandler | None = None,
        ask_user: QuestionHandler | None = None,
        always_allow_shell_commands: bool = True,
        always_allow_dangerous_shell_commands: bool = False,
    ) -> None:
        self.workspace = workspace.resolve()
        self.mode = mode
        self.approve = approve or self._reject
        self.ask_user = ask_user or self._no_question_handler
        self.always_allow_shell_commands = always_allow_shell_commands
        self.always_allow_dangerous_shell_commands = always_allow_dangerous_shell_commands
        self._results: dict[str, Any] = {}
        self._inflight: dict[str, asyncio.Task[Any]] = {}
        self._middleware: FilesystemMiddleware[Any, Any] | None = None
        self._backend: LocalShellBackend | None = None
        self._tools: dict[str, BaseTool] = {}
        self._configure_native_tools()

    def _configure_native_tools(self) -> None:
        self._backend = LocalShellBackend(
            root_dir=self.workspace,
            virtual_mode=True,
            timeout=120,
            max_output_bytes=5_000_000,
            inherit_env=True,
        )
        self._middleware = FilesystemMiddleware(
            backend=self._backend,
            tools="all",
            max_execute_timeout=3600,
            grep_max_count=1000,
        )
        self._tools = {tool.name: tool for tool in self._middleware.tools}

    def set_workspace(self, workspace: Path) -> None:
        """Rebind native tools to a newly selected workspace."""

        self.workspace = workspace.resolve()
        self._configure_native_tools()

    def set_shell_auto_allow(self, *, normal: bool, dangerous: bool) -> None:
        self.always_allow_shell_commands = normal
        self.always_allow_dangerous_shell_commands = dangerous

    async def _reject(self, _request: ApprovalRequest) -> bool:
        return False

    async def _no_question_handler(self, _question: Any) -> Any:
        raise ToolRejected("No interactive question handler is available.")

    def begin_turn(self) -> None:
        """Scope idempotent tool-call results to one hosted turn."""

        if self._inflight:
            raise RuntimeError("Cannot begin a turn while a local tool is running.")
        self._results.clear()

    async def cancel_all(self) -> None:
        """Cancel adapter-owned in-flight calls.

        Deep Agents owns operation execution; this adapter owns only awaiting tasks.
        """

        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def execute(self, request: ToolRequest) -> Any:
        if request.tool_call_id in self._results:
            return self._results[request.tool_call_id]
        if request.tool_call_id in self._inflight:
            return await self._inflight[request.tool_call_id]
        task = asyncio.create_task(self._execute_once(request))
        self._inflight[request.tool_call_id] = task
        try:
            result = await task
            self._results[request.tool_call_id] = result
            return result
        finally:
            self._inflight.pop(request.tool_call_id, None)

    async def _authorized(
        self,
        request: ToolRequest,
        canonical_name: str,
        *,
        external: bool = False,
    ) -> None:
        if external:
            raise ToolRejected("Paths outside the workspace are not available.")
        gated = canonical_name in _MUTATION_TOOLS or canonical_name == "execute"
        if not gated or self.mode is ApprovalMode.YOLO:
            return
        if self.mode is ApprovalMode.AUTO:
            if canonical_name == "execute":
                danger = dangerous_shell_command(request.arguments.get("command"))
                if danger and self.always_allow_dangerous_shell_commands:
                    return
                if not danger and self.always_allow_shell_commands:
                    return
            elif routine_action(canonical_name, request.arguments, self.workspace):
                return
        approved = await self.approve(
            ApprovalRequest(
                tool_call_id=request.tool_call_id,
                tool_name=request.tool_name,
                arguments=request.arguments,
                reason="Local side effect",
                canonical_name=canonical_name,
            )
        )
        if not approved:
            raise ToolRejected(f"{request.tool_name} was rejected.")

    def _virtual_path(self, raw: object) -> tuple[str, bool]:
        resolved = _resolve_path(self.workspace, raw)
        if resolved is None:
            raise ValueError("file_path must be a non-empty valid path.")
        external = not _is_within(self.workspace, resolved)
        if external:
            return str(resolved), True
        relative = resolved.relative_to(self.workspace)
        return "/" + PurePosixPath(relative).as_posix(), False

    def _normalize_arguments(
        self,
        request: ToolRequest,
        canonical_name: str,
    ) -> tuple[dict[str, Any], bool]:
        arguments = dict(request.arguments)
        external = False
        if canonical_name in {
            "read_file",
            "write_file",
            "edit_file",
            "delete",
            "read_binary_file",
            "write_binary_file",
        }:
            arguments["file_path"], external = self._virtual_path(arguments.get("file_path"))
        elif canonical_name == "ls":
            arguments["path"], external = self._virtual_path(arguments.get("path") or ".")
        elif canonical_name in {"glob", "grep"} and arguments.get("path") is not None:
            arguments["path"], external = self._virtual_path(arguments["path"])
        if canonical_name == "execute":
            timeout_ms = request.timeout_ms or arguments.pop("timeout_ms", None)
            if timeout_ms is not None and "timeout" not in arguments:
                arguments["timeout"] = max(1, (int(timeout_ms) + 999) // 1000)
            arguments.pop("kill_on_timeout", None)
            arguments.pop("background", None)
        elif canonical_name == "read_file" and arguments.get("offset") is not None:
            # CopperPilot's desktop harness uses 1-indexed offsets; Deep Agents uses
            # zero-indexed offsets.
            arguments["offset"] = max(int(arguments["offset"]) - 1, 0)
        return arguments, external

    def _runtime(self, request: ToolRequest) -> ToolRuntime[Any, dict[str, Any]]:
        return ToolRuntime(
            state={},
            context=None,
            config={},
            stream_writer=lambda _value: None,
            tool_call_id=request.tool_call_id,
            store=None,
            tools=list(self._tools.values()),
        )

    @staticmethod
    def _message_result(message: ToolMessage) -> dict[str, Any]:
        if message.status == "error":
            return {"error": str(message.content)}
        result: dict[str, Any] = {"result": message.content}
        artifact = message.artifact
        if isinstance(artifact, dict):
            for key in ("exit_code", "truncated"):
                if key in artifact:
                    result[key] = artifact[key]
        return result

    async def _execute_native(
        self,
        request: ToolRequest,
        canonical_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        native_tool = self._tools.get(canonical_name)
        if native_tool is None:
            raise ValueError(f"Unsupported local tool: {request.tool_name}")
        payload = {
            "type": "tool_call",
            "id": request.tool_call_id,
            "name": canonical_name,
            "args": {**arguments, "runtime": self._runtime(request)},
        }
        message = await native_tool.ainvoke(payload)
        if not isinstance(message, ToolMessage):
            raise TypeError(f"{canonical_name} returned an invalid result.")
        return self._message_result(message)

    async def _execute_once(self, request: ToolRequest) -> Any:
        if request.tool_name == "ask_user_question":
            timeout = max((request.timeout_ms or 105_000) / 1000, 1)
            try:
                return await asyncio.wait_for(self.ask_user(request.arguments), timeout)
            except TimeoutError:
                return {"error": "Ask-user prompt timed out."}

        canonical_name = canonical_tool_name(request.tool_name)
        arguments, external = self._normalize_arguments(request, canonical_name)
        await self._authorized(request, canonical_name, external=external)

        if canonical_name == "read_binary_file":
            return await self._read_binary(arguments["file_path"])
        if canonical_name == "write_binary_file":
            return await self._write_binary(arguments)
        return await self._execute_native(request, canonical_name, arguments)

    async def _read_binary(self, file_path: str) -> dict[str, Any]:
        assert self._backend is not None
        result = await self._backend.aread(file_path)
        if result.error:
            return {"error": result.error}
        if result.file_data is None:
            return {"error": f"No data returned for {file_path}"}
        content = result.file_data["content"]
        encoding = result.file_data.get("encoding", "utf-8")
        if encoding != "base64":
            content = base64.b64encode(content.encode()).decode()
        mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        return {
            "result": f"data:{mime_type};base64,{content}",
            "encoding": "base64",
            "mime_type": mime_type,
            "byte_length": len(base64.b64decode(content)),
        }

    async def _write_binary(self, arguments: dict[str, Any]) -> dict[str, Any]:
        assert self._backend is not None
        encoding = str(arguments.get("encoding") or "base64")
        if encoding != "base64":
            return {"error": f"write_binary_file: unsupported encoding '{encoding}'"}
        encoded = str(arguments.get("data") or arguments.get("content") or "")
        if not encoded:
            return {"error": "write_binary_file: data is required (base64 string)"}
        try:
            content = base64.b64decode("".join(encoded.split()), validate=True)
        except ValueError as exc:
            return {"error": f"write_binary_file: invalid base64 payload: {exc}"}
        responses = await self._backend.aupload_files([(arguments["file_path"], content)])
        response = responses[0]
        if response.error:
            return {"error": str(response.error)}
        mime_type = (
            str(arguments.get("mime_type") or "")
            or mimetypes.guess_type(arguments["file_path"])[0]
            or "application/octet-stream"
        )
        return {
            "result": f"Updated file {response.path}",
            "encoding": "base64",
            "mime_type": mime_type,
            "byte_length": len(content),
        }


def normalized_tool_names(names: Sequence[str]) -> list[str]:
    """Return canonical names while preserving request order."""

    return [canonical_tool_name(name) for name in names]


__all__ = [
    "ApprovalMode",
    "ApprovalRequest",
    "LocalToolBroker",
    "ToolRejected",
    "canonical_tool_name",
    "dangerous_shell_command",
    "normalized_tool_names",
    "routine_action",
]
