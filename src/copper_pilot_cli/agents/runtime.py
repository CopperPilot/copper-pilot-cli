"""Live agent sessions of one CLI process, and one bring-your-own-agent turn.

The chat graph (``copper_graph``) calls ``stream_byo_turn`` instead of the
hosted stream when the thread's provider is Claude or Codex. Events come
back as ``CopperEvent``s, so the graph records the transcript in
``sessions.db`` and every front end renders them unchanged.

Each chat keeps one live agent session while the process runs. A session is
replaced (and the new one gets a handoff) when the chat switches provider or
model options, when the agent process died, or after a restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import yaml
from langchain_core.messages import BaseMessage, HumanMessage

from copper_pilot_cli.agents import PROVIDER_LABELS, AgentSelection, Provider
from copper_pilot_cli.agents import errors as agent_errors
from copper_pilot_cli.agents.handoff import build_handoff, seeded_prompt
from copper_pilot_cli.agents.session import (
    AGENT_PLAN_KIND,
    PLAN_READY_KIND,
    AgentSession,
    TurnContext,
)
from copper_pilot_cli.agents.tool_proxy import ToolProxy
from copper_pilot_cli.agents.tools_session import ToolSessionClient, ToolSessionError
from copper_pilot_cli.copper_auth import DeviceCredential
from copper_pilot_cli.copper_protocol import (
    CopperEvent,
    CopperMode,
    EventKind,
    PlanContext,
    ToolRequest,
)
from copper_pilot_cli.copper_tools import LocalToolBroker

logger = logging.getLogger(__name__)

SessionFactory = Callable[..., AgentSession]
ToolSessionFactory = Callable[..., ToolSessionClient]


def default_factories() -> dict[Provider, SessionFactory]:
    from copper_pilot_cli.agents.claude_driver import make_claude_session
    from copper_pilot_cli.agents.codex_driver import make_codex_session

    return {Provider.CLAUDE: make_claude_session, Provider.CODEX: make_codex_session}


class BridgeBroker:
    """Answers the tool socket's bridge requests under the chat's live approval rules.

    The chat's own broker serves the hosted chat socket; this one mirrors its
    mode, prompts and shell settings and adds compare-and-swap writes.
    """

    def __init__(self, parent: Callable[[], LocalToolBroker]) -> None:
        self._parent = parent
        self._broker: LocalToolBroker | None = None
        self._key: tuple[Any, ...] | None = None

    def current(self) -> LocalToolBroker:
        parent = self._parent()
        key = (
            parent.workspace,
            parent.mode,
            parent.always_allow_shell_commands,
            parent.always_allow_dangerous_shell_commands,
            parent.has_approver,
        )
        if self._broker is None or key != self._key:
            self._broker = LocalToolBroker(
                parent.workspace,
                mode=parent.mode,
                approve=parent.approve if parent.has_approver else None,
                ask_user=parent.ask_user,
                always_allow_shell_commands=parent.always_allow_shell_commands,
                always_allow_dangerous_shell_commands=parent.always_allow_dangerous_shell_commands,
                cas_writes=True,
                # Design files written by the agent's CopperPilot edit tools
                # (inside the workspace) are routine under Auto, like a subagent's.
                routine_kicad_writes=True,
            )
            self._key = key
        return self._broker

    async def execute(self, request: ToolRequest) -> Any:
        return await self.current().execute(request)


class AgentSessions:
    """One live agent session per chat."""

    def __init__(
        self,
        *,
        credential: Callable[[], DeviceCredential | None],
        broker: Callable[[], LocalToolBroker],
        factories: dict[Provider, SessionFactory] | None = None,
        tool_session_factory: ToolSessionFactory = ToolSessionClient,
    ) -> None:
        self._credential = credential
        self._broker = broker
        self._factories = factories
        self._tool_session_factory = tool_session_factory
        self._sessions: dict[str, AgentSession] = {}
        self._tools: dict[str, tuple[ToolSessionClient, ToolProxy]] = {}
        self._active: AgentSession | None = None

    def factories(self) -> dict[Provider, SessionFactory]:
        if self._factories is None:
            self._factories = default_factories()
        return self._factories

    async def session_for(
        self, *, thread_id: str, selection: AgentSelection, workspace: Path
    ) -> tuple[AgentSession, bool]:
        """The chat's live session for ``selection`` and whether it is new."""
        current = self._sessions.get(thread_id)
        if current is not None:
            same = (
                current.provider is selection.provider
                and current.selection.options() == selection.options()
                and current.alive
                and getattr(current, "workspace", workspace) == workspace
            )
            if same:
                return current, False
            self._sessions.pop(thread_id, None)
            await _close_quietly(current)
        credential = self._credential()
        if credential is None:
            raise agent_errors.AgentError(
                agent_errors.AUTH_REQUIRED,
                "CopperPilot login is required for CopperPilot tools. Run `copper-pilot auth "
                "login`.",
                retryable=False,
            )
        factory = self.factories().get(selection.provider)
        if factory is None:
            raise agent_errors.AgentError(
                agent_errors.UNKNOWN, f"Unsupported provider {selection.provider.value!r}."
            )
        bridge = BridgeBroker(self._broker)
        tools = self._tool_session_factory(
            credential,
            workspace=workspace,
            conversation_id=thread_id,
            executor=bridge.execute,
            agent={"provider": selection.provider.value, "model": selection.model or ""},
            turn_end=True,
        )
        proxy = ToolProxy(tools, workspace=workspace, broker=self._broker())
        session = factory(selection, workspace=workspace, proxy=proxy, broker=self._broker)
        try:
            await session.start()
        except ToolSessionError as exc:
            await _close_quietly(session)
            await tools.aclose()
            raise _tool_error(exc) from exc
        except BaseException:
            await _close_quietly(session)
            await tools.aclose()
            raise
        # Close the tool socket with the session.
        original_close = session.aclose

        async def close() -> None:
            try:
                await original_close()
            finally:
                self._tools.pop(thread_id, None)
                await tools.aclose()

        session.aclose = close
        self._sessions[thread_id] = session
        self._tools[thread_id] = (tools, proxy)
        return session, True

    async def compact(self, thread_id: str) -> str:
        """The agent's own compaction for the chat's live session."""
        session = self._sessions.get(thread_id)
        if session is None or not session.alive:
            return (
                "No live agent session: the next turn starts a fresh session seeded with a "
                "capped handoff, so there is nothing to compact."
            )
        return await session.compact()

    def has_live(self, thread_id: str) -> bool:
        session = self._sessions.get(thread_id)
        return session is not None and session.alive

    async def discard(self, thread_id: str) -> None:
        session = self._sessions.pop(thread_id, None)
        if session is not None:
            await _close_quietly(session)

    async def interrupt(self) -> None:
        session = self._active
        if session is not None:
            with contextlib.suppress(Exception):
                await session.interrupt()

    async def aclose(self) -> None:
        sessions, self._sessions = list(self._sessions.values()), {}
        await asyncio.gather(*(_close_quietly(item) for item in sessions))

    async def stream_turn(
        self,
        *,
        thread_id: str,
        selection: AgentSelection,
        workspace: Path,
        messages: list[BaseMessage],
        mode: CopperMode,
        turn_id: str,
        plan: PlanContext | None = None,
    ) -> AsyncIterator[CopperEvent]:
        """One turn of the user's agent; a fresh session gets the capped handoff."""
        if not messages or not isinstance(messages[-1], HumanMessage):
            raise agent_errors.AgentError(agent_errors.PROTOCOL, "Nothing to send.")
        content = messages[-1].content
        prompt = content if isinstance(content, str) else str(content)
        if plan is not None:
            prompt = build_prompt(plan, prompt)
        session, fresh = await self.session_for(
            thread_id=thread_id, selection=selection, workspace=workspace
        )
        handoff = None
        if fresh:
            handoff = build_handoff(
                messages[:-1],
                user_text=prompt,
                provider_label=PROVIDER_LABELS[selection.provider],
                thread_id=thread_id,
                model_context_window=session.context_window,
            )
        if handoff is not None:
            logger.info(
                "Seeding a new %s session with a handoff (items=%s, omitted=%s)",
                selection.provider.value,
                handoff.selected,
                handoff.omitted,
            )
            yield CopperEvent(
                EventKind.STATUS,
                f"Handing the chat to {PROVIDER_LABELS[selection.provider]} "
                f"({handoff.selected} earlier items)",
            )
        context = TurnContext(
            workspace=workspace,
            broker=self._broker(),
            mode=mode,
            thread_id=thread_id,
            turn_id=turn_id,
        )
        self._active = session
        tools, proxy = self._tools[thread_id]
        proxy.mode = mode
        final: list[CopperEvent] = []
        try:
            async for event in session.run_turn(seeded_prompt(handoff, prompt), context):
                data = event.data if isinstance(event.data, dict) else {}
                if event.kind is EventKind.STATUS_EVENT and data.get("kind") == AGENT_PLAN_KIND:
                    relative, name = save_agent_plan(
                        workspace, str(data.get("plan") or ""), selection.provider
                    )
                    yield CopperEvent(
                        EventKind.STATUS_EVENT,
                        {"kind": PLAN_READY_KIND, "file_path": relative, "name": name},
                    )
                    continue
                if event.kind is EventKind.FINAL:
                    # The turn's end marker comes after the server's turn_end note.
                    final.append(event)
                    continue
                yield event
            note = await tools.send_turn_end(turn_id)
            if note:
                yield CopperEvent(EventKind.STATUS, note)
            for event in final:
                yield event
        except ToolSessionError as exc:
            raise _tool_error(exc) from exc
        except agent_errors.AgentError as exc:
            # The failed turn may still have staged edits: let the server apply them.
            await tools.send_turn_end(turn_id)
            if exc.kind in {agent_errors.PROCESS_EXITED, agent_errors.NOT_INSTALLED}:
                await self.discard(thread_id)
            raise
        finally:
            self._active = None
            if not session.alive:
                await self.discard(thread_id)


def save_agent_plan(workspace: Path, plan: str, provider: Provider) -> tuple[str, str]:
    """Save a Plan-mode plan as ``.copperpilot/plans/<name>.plan.md`` (CopperPilot's format)."""
    lines = [line.strip().lstrip("#").strip() for line in plan.splitlines() if line.strip()]
    title = (lines[0] if lines else "Agent plan")[:80]
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48] or "agent-plan"
    directory = workspace / ".copperpilot" / "plans"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{slug}.plan.md"
    counter = 2
    while path.exists():
        path = directory / f"{slug}-{counter}.plan.md"
        counter += 1
    overview = next((line for line in lines[1:] if line), "")[:300]
    frontmatter = yaml.safe_dump(
        {
            "name": title,
            "overview": overview,
            "overall_status": "open",
            "source": provider.value,
            "todos": [],
        },
        sort_keys=False,
        allow_unicode=True,
    )
    path.write_text(f"---\n{frontmatter}---\n\n{plan.strip()}\n", encoding="utf-8")
    return path.relative_to(workspace).as_posix(), title


def set_plan_status(workspace: Path, relative: str, status: str) -> None:
    """``overall_status`` of a saved plan (open → running → done)."""
    path = workspace / relative
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    updated = re.sub(
        r"^overall_status: .*$", f"overall_status: {status}", text, count=1, flags=re.MULTILINE
    )
    if updated != text:
        path.write_text(updated, encoding="utf-8")


def build_prompt(plan: PlanContext, prompt: str) -> str:
    """An Agent-mode turn that builds an accepted plan carries the plan itself."""
    body = (plan.body or "").strip()
    return f"{prompt}\n\n[Accepted plan: {plan.file_path}]\n{body}"


def _tool_error(exc: ToolSessionError) -> agent_errors.AgentError:
    if exc.status == 401:
        return agent_errors.AgentError(
            agent_errors.AUTH_REQUIRED,
            "Your CopperPilot login expired. Run `copper-pilot auth login`, then try again.",
            retryable=False,
            status=401,
        )
    return agent_errors.tool_socket(str(exc), retryable=exc.retryable, status=exc.status)


async def _close_quietly(session: AgentSession) -> None:
    try:
        await session.aclose()
    except Exception:  # noqa: BLE001 - shutting down
        logger.debug("Closing an agent session failed", exc_info=True)


__all__ = ["AgentSessions", "BridgeBroker", "default_factories"]
