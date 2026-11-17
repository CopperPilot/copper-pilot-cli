"""Classified failures of the user's own agent.

Each class has one clear message. There is no automatic fallback to
CopperPilot: the user decides what to do (log in, wait, switch with
``/provider``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from copper_pilot_cli.agents import PROVIDER_LABELS, Provider

NOT_INSTALLED = "not_installed"
AUTH_REQUIRED = "auth_required"
USAGE_LIMIT = "usage_limit"
OVERLOADED = "overloaded"
PROCESS_EXITED = "process_exited"
CONTEXT_FULL = "context_full"
TOOL_SOCKET = "tool_socket"
PROTOCOL = "agent_protocol"
UNKNOWN = "agent_error"

_RETRYABLE = frozenset({OVERLOADED, PROCESS_EXITED, TOOL_SOCKET})
_LOGIN_COMMANDS = {
    Provider.CLAUDE: "claude auth login",
    Provider.CODEX: "codex login",
}
_INSTALL_HINTS = {
    Provider.CLAUDE: "Install Claude Code (https://claude.com/claude-code) so `claude` is on PATH",
    Provider.CODEX: "Install Codex (`npm install -g @openai/codex`) so `codex` is on PATH",
}


class AgentError(RuntimeError):
    """A classified failure of the user's own agent."""

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        provider: Provider | None = None,
        retryable: bool | None = None,
        resets_at: str | None = None,
        status: int | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.provider = provider
        self.retryable = kind in _RETRYABLE if retryable is None else retryable
        self.resets_at = resets_at
        self.status = status
        self.detail = detail

    def as_event_data(self) -> dict[str, Any]:
        """The ``copper.error`` payload (no ``block_reason``: it is not our limit)."""
        data: dict[str, Any] = {
            "message": str(self),
            "type": self.kind,
            "code": self.kind,
            "retryable": self.retryable,
        }
        if self.provider is not None:
            data["provider"] = self.provider.value
        if self.resets_at:
            data["resets_at"] = self.resets_at
        if self.status is not None:
            data["status"] = self.status
        return data


def _label(provider: Provider) -> str:
    return PROVIDER_LABELS[provider]


def not_installed(provider: Provider, binary: str) -> AgentError:
    return AgentError(
        NOT_INSTALLED,
        f"{_label(provider)} is not available: `{binary}` was not found. "
        f"{_INSTALL_HINTS[provider]}, then try again, or switch with /provider.",
        provider=provider,
    )


def auth_required(provider: Provider, detail: str | None = None) -> AgentError:
    extra = " (or set OPENAI_API_KEY)" if provider is Provider.CODEX else ""
    return AgentError(
        AUTH_REQUIRED,
        f"{_label(provider)} is not logged in. Run `{_LOGIN_COMMANDS[provider]}`{extra} "
        "in a terminal, then send your message again.",
        provider=provider,
        detail=detail,
    )


def reset_phrase(resets_at: str | None, now: datetime | None = None) -> str:
    """`` It resets at 3:40 PM (in 2h 5m).`` in local time, or empty."""
    if not resets_at:
        return ""
    try:
        moment = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    seconds = max(0, int((moment - current).total_seconds()))
    hours, minutes = divmod(seconds // 60, 60)
    span = f"{hours}h {minutes}m" if hours else f"{max(minutes, 1)}m"
    clock = moment.astimezone().strftime("%I:%M %p").lstrip("0")
    return f" It resets at {clock} (in {span})."


def epoch_to_iso(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    # Some sources send milliseconds.
    seconds = value / 1000 if value > 10_000_000_000 else value
    return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")


def usage_limit(
    provider: Provider, resets_at: str | None = None, detail: str | None = None
) -> AgentError:
    owner = "Claude plan" if provider is Provider.CLAUDE else "Codex plan"
    said = f" ({_clip(detail)})" if detail else ""
    return AgentError(
        USAGE_LIMIT,
        f"Your {owner}'s usage limit is reached{said}.{reset_phrase(resets_at)} "
        "CopperPilot does not meter or bill your agent; switch with /provider to keep working.",
        provider=provider,
        resets_at=resets_at,
        retryable=False,
        detail=detail,
    )


def _clip(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def overloaded(provider: Provider, status: int | None = None) -> AgentError:
    return AgentError(
        OVERLOADED,
        f"{_label(provider)}'s service is overloaded right now. Send your message again "
        "in a moment.",
        provider=provider,
        status=status,
    )


def process_exited(provider: Provider, code: int | None = None, detail: str = "") -> AgentError:
    suffix = f" (exit code {code})" if code is not None else ""
    return AgentError(
        PROCESS_EXITED,
        f"{_label(provider)} stopped unexpectedly{suffix}. Send your message again to "
        "restart it; the chat so far is handed over.",
        provider=provider,
        detail=detail or None,
    )


def context_full(provider: Provider) -> AgentError:
    return AgentError(
        CONTEXT_FULL,
        f"{_label(provider)}'s context window is full. Start a new chat (/clear), or "
        "switch provider with /provider to continue with a shorter handoff.",
        provider=provider,
        retryable=False,
    )


def tool_socket(message: str, *, retryable: bool = True, status: int | None = None) -> AgentError:
    return AgentError(
        TOOL_SOCKET,
        f"CopperPilot tools are unavailable: {message}",
        retryable=retryable,
        status=status,
    )


def classify_message(provider: Provider, text: str, status: int | None = None) -> AgentError:
    """Best-effort classification of an agent's free-text failure."""
    lowered = text.lower()
    if status in {401, 403} or any(
        marker in lowered
        for marker in (
            "authentication_failed",
            "not logged in",
            "invalid api key",
            "please run /login",
            "unauthorized",
            "login required",
        )
    ):
        return auth_required(provider, text)
    if status == 529 or "overloaded" in lowered:
        return overloaded(provider, status)
    if status == 429 or "usage limit" in lowered or "rate_limit" in lowered:
        return usage_limit(provider)
    if "context window" in lowered or "prompt is too long" in lowered:
        return context_full(provider)
    return AgentError(UNKNOWN, f"{_label(provider)} failed: {text}", provider=provider)


__all__ = [
    "AUTH_REQUIRED",
    "CONTEXT_FULL",
    "NOT_INSTALLED",
    "OVERLOADED",
    "PROCESS_EXITED",
    "TOOL_SOCKET",
    "USAGE_LIMIT",
    "AgentError",
    "auth_required",
    "classify_message",
    "context_full",
    "epoch_to_iso",
    "not_installed",
    "overloaded",
    "process_exited",
    "reset_phrase",
    "tool_socket",
    "usage_limit",
]
