"""``copper-pilot mcp --tools-session``: CopperPilot's tools for your own agent over stdio MCP.

Two modes:

* ``--relay <address>`` (how the TUI launches it for Codex): a thin stdio
  server that forwards to the TUI process, which owns the chat's tool socket,
  approval prompts and subagent cards. The relay token comes from the
  environment, never argv.
* standalone (any MCP host): opens its own tool socket with the stored
  CopperPilot login. Reads run freely; writes and shell follow ``--approval``
  (``auto`` by default, ``yolo`` when asked). Nobody can answer a prompt
  here, so what Auto would ask about is refused, as in ``copper-pilot mcp``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from copper_pilot_cli.agents.mcp_server import ProxyBackend, relay_backend_from_env, serve_stdio
from copper_pilot_cli.agents.tool_proxy import ToolProxy
from copper_pilot_cli.agents.tools_session import ToolSessionClient
from copper_pilot_cli.copper_auth import AuthenticationError, load_credential
from copper_pilot_cli.copper_preferences import shell_auto_allow_settings
from copper_pilot_cli.copper_tools import ApprovalMode, LocalToolBroker
from copper_pilot_cli.copper_workspace import canonical_workspace


def run_tools_session(args: argparse.Namespace) -> int:
    if args.relay:
        asyncio.run(serve_stdio(relay_backend_from_env(args.relay)))
        return 0
    try:
        credential = load_credential()
    except AuthenticationError:
        credential = None
    if credential is None:
        print(
            "copper-pilot: CopperPilot login is required; run `copper-pilot auth login`.",
            file=sys.stderr,
        )
        return 2
    try:
        workspace = canonical_workspace(args.workspace or ".")
    except (FileNotFoundError, NotADirectoryError) as exc:
        print(f"copper-pilot: {exc}", file=sys.stderr)
        return 2
    normal_shell, dangerous_shell = shell_auto_allow_settings()
    broker = LocalToolBroker(
        workspace,
        mode=ApprovalMode.YOLO if args.approval == "yolo" else ApprovalMode.AUTO,
        always_allow_shell_commands=normal_shell,
        always_allow_dangerous_shell_commands=dangerous_shell,
        cas_writes=True,
    )
    session = ToolSessionClient(
        credential,
        workspace=workspace,
        conversation_id=args.conversation_id or str(uuid.uuid4()),
        executor=broker.execute,
        agent={"provider": args.provider or "mcp", "model": args.model or ""},
    )
    proxy = ToolProxy(session, workspace=workspace, broker=broker)

    async def main() -> None:
        try:
            await serve_stdio(ProxyBackend(proxy))
        finally:
            await session.aclose()

    asyncio.run(main())
    return 0


__all__ = ["run_tools_session"]
