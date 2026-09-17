"""CopperPilot CLI entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx
from langchain_core.messages import HumanMessage
from rich.console import Console
from rich.prompt import Confirm

from copper_pilot_cli._version import __version__
from copper_pilot_cli.copper_app import CopperPilotApp
from copper_pilot_cli.copper_auth import (
    AuthenticationError,
    DeviceLogin,
    clear_credential,
    load_credential,
    validate_credential,
)
from copper_pilot_cli.copper_graph import create_copper_graph
from copper_pilot_cli.copper_preferences import saved_approval_mode, shell_auto_allow_settings
from copper_pilot_cli.copper_protocol import CopperMode, EventKind
from copper_pilot_cli.copper_tools import ApprovalMode, LocalToolBroker
from copper_pilot_cli.copper_workspace import (
    canonical_workspace,
    discover_workspace,
    remember_workspace,
)
from copper_pilot_cli.diagnostics import configure_diagnostics
from copper_pilot_cli.langchain import CopperPilotAgent
from copper_pilot_cli.sessions import delete_thread, generate_thread_id, list_threads

console = Console()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="copper-pilot")
    result.add_argument("workspace", nargs="?", default=".")
    result.add_argument("-m", "--message")
    result.add_argument("-r", "--resume", nargs="?", const="recent")
    result.add_argument("-n", "--non-interactive", action="store_true")
    result.add_argument("--quiet", action="store_true")
    result.add_argument("--no-stream", action="store_true")
    result.add_argument("--json", action="store_true")
    result.add_argument("--mode", choices=[item.value for item in CopperMode], default="agent")
    result.add_argument("-y", "--auto-approve", action="store_true")
    result.add_argument("--yolo", action="store_true")
    result.add_argument("--version", action="version", version=__version__)
    result.set_defaults(command=None)
    return result


def command_parser(command: str) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog=f"copper-pilot {command}")
    result.set_defaults(workspace=".", message=None, command=command)
    if command == "auth":
        result.add_argument(
            "action",
            nargs="?",
            choices=["login", "logout", "status"],
            default="status",
        )
        return result
    thread_sub = result.add_subparsers(dest="thread_action", required=True)
    listing = thread_sub.add_parser("list")
    listing.add_argument("--all", action="store_true")
    listing.add_argument("--limit", type=int, default=20)
    deleting = thread_sub.add_parser("delete")
    deleting.add_argument("thread_id")
    deleting.add_argument("-y", "--yes", action="store_true")
    return result


async def _auth_command(action: str) -> int:
    if action == "logout":
        console.print(
            "Logged out." if clear_credential() else "Already logged out.",
            markup=False,
        )
        return 0
    if action == "status":
        credential = load_credential()
        console.print(
            f"Authenticated to {credential.base_url}."
            if credential
            else "Not authenticated. Run `copper-pilot auth login`.",
            markup=False,
        )
        return 0 if credential else 1
    console.print("Opening CopperPilot login in your browser…", markup=False)
    credential = await DeviceLogin(client_version=__version__).login()
    console.print(f"Authenticated to {credential.base_url}.", markup=False)
    return 0


async def _threads_command(args: argparse.Namespace, workspace: Path) -> int:
    if args.thread_action == "delete":
        if not args.yes:
            if not sys.stdin.isatty():
                console.print(
                    "Use --yes to confirm deletion in non-interactive mode.",
                    markup=False,
                )
                return 2
            if not Confirm.ask(f"Delete chat {args.thread_id}?"):
                return 0
        deleted = await delete_thread(args.thread_id)
        console.print("Deleted." if deleted else "Thread not found.", markup=False)
        return 0 if deleted else 1
    rows = await list_threads(
        limit=args.limit,
        include_message_count=True,
        cwd=None if args.all else str(workspace),
    )
    for row in rows:
        console.print(
            f"{row['thread_id']}  {row.get('updated_at') or ''}  "
            f"{row.get('message_count', 0)} messages  {row.get('initial_prompt') or ''}",
            markup=False,
            highlight=False,
        )
    return 0


async def _credential() -> Any:
    credential = load_credential()
    if credential is not None:
        try:
            if await validate_credential(credential):
                return credential
        except httpx.HTTPError as exc:
            raise AuthenticationError(f"Could not reach CopperPilot: {exc}") from exc
        clear_credential()
        console.print("Your CopperPilot login expired; signing in again.", markup=False)
    if not sys.stdin.isatty():
        raise AuthenticationError("Login required; run `copper-pilot auth login` interactively.")
    console.print("Opening CopperPilot login in your browser…", markup=False)
    return await DeviceLogin(client_version=__version__).login()


async def _resolve_thread(resume: str | None, workspace: Path) -> str:
    if resume and resume != "recent":
        return resume
    if resume == "recent":
        rows = await list_threads(limit=1, cwd=str(workspace))
        if rows:
            return rows[0]["thread_id"]
    return generate_thread_id()


async def _headless(
    runtime: Any,
    workspace: Path,
    thread_id: str,
    message: str,
    args: argparse.Namespace,
) -> int:
    config = runtime.config(thread_id, workspace)
    events: list[dict[str, Any]] = []
    failed = False
    async for item in runtime.astream(
        {"messages": [HumanMessage(content=message)], "copper_mode": args.mode},
        config,
    ):
        _namespace, stream_mode, data = item
        if stream_mode != "custom" or not isinstance(data, dict):
            continue
        events.append(data)
        kind = str(data.get("type") or "").removeprefix("copper.")
        payload = data.get("data")
        if kind == EventKind.ERROR.value:
            failed = True
        elif kind == EventKind.FINAL.value and isinstance(payload, dict):
            failed = failed or payload.get("success") is False or bool(payload.get("error"))
        if args.no_stream:
            continue
        if args.json:
            print(json.dumps({"type": kind, "data": payload, "title": data.get("title")}))
        elif kind == EventKind.TEXT.value:
            print(str(payload), end="", flush=True)
        elif not args.quiet and kind in {
            EventKind.STATUS.value,
            EventKind.REASONING.value,
        }:
            console.print(f"\n{payload}", style="dim", markup=False, highlight=False)

    state = await runtime.aget_state(config)
    messages = (getattr(state, "values", {}) or {}).get("messages", [])
    assistant_content = str(messages[-1].content) if messages else ""
    if args.no_stream:
        if args.json:
            print(
                json.dumps(
                    {
                        "messages": [{"role": "assistant", "content": assistant_content}],
                        "events": events,
                    }
                )
            )
        else:
            console.print(assistant_content, markup=False, highlight=False)
    elif not args.json:
        print()
    return 1 if failed else 0


async def async_main(args: argparse.Namespace) -> int:
    workspace = canonical_workspace(args.workspace)
    if args.command == "auth":
        return await _auth_command(args.action)
    if args.command == "threads":
        return await _threads_command(args, workspace)
    credential = await _credential()
    context = discover_workspace(workspace)
    remember_workspace(context)
    approval = (
        ApprovalMode.YOLO
        if args.yolo
        else ApprovalMode.AUTO
        if args.auto_approve
        else saved_approval_mode()
    )
    normal_shell, dangerous_shell = shell_auto_allow_settings()
    broker = LocalToolBroker(
        workspace,
        mode=approval,
        always_allow_shell_commands=normal_shell,
        always_allow_dangerous_shell_commands=dangerous_shell,
    )
    thread_id = await _resolve_thread(args.resume, workspace)
    agent = CopperPilotAgent(
        workspace,
        credential=credential,
        conversation_id=thread_id,
        mode=CopperMode(args.mode),
        tool_broker=broker,
    )
    if args.non_interactive or (args.message and not sys.stdout.isatty()):
        if not args.message:
            raise ValueError("--message is required in non-interactive mode.")
        async with create_copper_graph(agent) as runtime:
            return await _headless(runtime, workspace, thread_id, args.message, args)
    async with create_copper_graph(agent) as runtime:
        app = CopperPilotApp(
            runtime,
            broker,
            workspace,
            thread_id,
            mode=CopperMode(args.mode),
            initial_message=args.message,
        )
        await app.run_async()
    return 0


def cli_main() -> None:
    """Run the CopperPilot command."""
    configure_diagnostics()
    argv = sys.argv[1:]
    args = (
        command_parser(argv[0]).parse_args(argv[1:])
        if argv and argv[0] in {"auth", "threads"}
        else parser().parse_args(argv)
    )
    try:
        raise SystemExit(asyncio.run(async_main(args)))
    except (AuthenticationError, FileNotFoundError, NotADirectoryError, ValueError) as exc:
        console.print(f"copper-pilot: {exc}", style="red", markup=False, highlight=False)
        raise SystemExit(2) from exc
    except (RuntimeError, httpx.HTTPError) as exc:
        console.print(f"copper-pilot: {exc}", style="red", markup=False, highlight=False)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        raise SystemExit(130) from None
