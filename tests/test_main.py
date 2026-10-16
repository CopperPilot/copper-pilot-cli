from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from copper_pilot_cli.copper_auth import AuthenticationError
from copper_pilot_cli.copper_main import (
    ACP_INSTALL_HINT,
    _headless,
    cli_main,
    command_parser,
    load_acp_runner,
    normalize_argv,
    parser,
)


def test_chat_cli_defaults_to_current_directory_and_fresh_thread() -> None:
    args = parser().parse_args([])
    assert args.workspace == "."
    assert args.resume is None
    assert args.mode == "agent"
    assert args.effort is None
    assert args.yolo is False
    assert args.auto_approve is False


def test_headless_flags_are_retained() -> None:
    args = parser().parse_args(
        [
            "board",
            "-n",
            "-m",
            "Review",
            "--json",
            "--no-stream",
            "--effort",
            "intelligence",
        ]
    )
    assert args.workspace == "board"
    assert args.non_interactive
    assert args.message == "Review"
    assert args.json
    assert args.no_stream
    assert args.effort == "intelligence"


def test_auth_defaults_to_status() -> None:
    assert command_parser("auth").parse_args([]).action == "status"


def test_thread_delete_requires_explicit_yes_flag() -> None:
    args = command_parser("threads").parse_args(["delete", "thread", "--yes"])
    assert args.thread_id == "thread"
    assert args.yes


def test_mcp_subcommand_has_no_positional_workspace() -> None:
    args = command_parser("mcp").parse_args([])
    assert args.command == "mcp"
    assert args.workspace == "."


def test_mcp_help_describes_stdio_server() -> None:
    help_text = command_parser("mcp").format_help()
    assert "stdio MCP server" in help_text


def test_acp_subcommand_has_no_positional_workspace() -> None:
    args = command_parser("acp").parse_args([])
    assert args.command == "acp"
    assert args.workspace == "."


def test_acp_help_describes_stdio_server() -> None:
    help_text = command_parser("acp").format_help()
    assert "stdio ACP" in help_text


def test_load_acp_runner_returns_the_server_entrypoint() -> None:
    from copper_pilot_cli.copper_acp import run_acp

    assert load_acp_runner() is run_acp


def test_load_acp_runner_explains_missing_sdk(monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "acp" or name.startswith("acp."):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "copper_pilot_cli.copper_acp", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ModuleNotFoundError, match="copper-pilot-cli\\[acp\\]"):
        load_acp_runner()


def test_load_acp_runner_reraises_unrelated_import_errors(monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "copper_pilot_cli.copper_acp":
            raise ModuleNotFoundError("No module named 'missing_dep'", name="missing_dep")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "copper_pilot_cli.copper_acp", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ModuleNotFoundError, match="missing_dep"):
        load_acp_runner()


def test_cli_main_acp_runs_server(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(sys, "argv", ["copper-pilot", "acp"])
    monkeypatch.setattr(
        "copper_pilot_cli.copper_main.load_acp_runner", lambda: lambda: calls.append("run")
    )
    monkeypatch.setattr("copper_pilot_cli.copper_main.configure_diagnostics", lambda: None)
    cli_main()
    assert calls == ["run"]


def test_cli_main_acp_exits_2_when_sdk_missing(monkeypatch, capsys) -> None:
    def missing():
        raise ModuleNotFoundError(ACP_INSTALL_HINT, name="acp")

    monkeypatch.setattr(sys, "argv", ["copper-pilot", "acp"])
    monkeypatch.setattr("copper_pilot_cli.copper_main.load_acp_runner", missing)
    monkeypatch.setattr("copper_pilot_cli.copper_main.configure_diagnostics", lambda: None)
    with pytest.raises(SystemExit) as exc:
        cli_main()
    assert exc.value.code == 2
    assert "copper-pilot-cli[acp]" in capsys.readouterr().err


def test_cli_main_acp_keyboard_interrupt_exits_130(monkeypatch) -> None:
    def interrupted():
        raise KeyboardInterrupt

    monkeypatch.setattr(sys, "argv", ["copper-pilot", "acp"])
    monkeypatch.setattr("copper_pilot_cli.copper_main.load_acp_runner", lambda: interrupted)
    monkeypatch.setattr("copper_pilot_cli.copper_main.configure_diagnostics", lambda: None)
    with pytest.raises(SystemExit) as exc:
        cli_main()
    assert exc.value.code == 130


def test_parser_help_describes_workspace_and_approval() -> None:
    help_text = parser().format_help()
    assert "Workspace directory" in help_text
    assert "--yolo" in help_text
    assert "--auto-approve" in help_text
    assert "--effort" in help_text
    assert "copper-pilot auth" in help_text
    assert "list|delete" in help_text
    assert "copper-pilot mcp" in help_text


@pytest.mark.asyncio
async def test_headless_returns_failure_for_unsuccessful_final(tmp_path, capsys) -> None:
    class Runtime:
        def config(self, thread_id, workspace):
            return {"thread_id": thread_id, "workspace": workspace}

        async def astream(self, _values, _config):
            yield (
                (),
                "custom",
                {
                    "type": "copper.final",
                    "data": {"success": False, "error": "hosted failure"},
                },
            )

        async def aget_state(self, _config):
            return SimpleNamespace(values={"messages": [AIMessage(content="")]})

    args = parser().parse_args([str(tmp_path), "-n", "-m", "Review", "--json"])
    result = await _headless(Runtime(), tmp_path, "thread", "Review", args)
    assert result == 1
    assert '"success": false' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_no_stream_headless_uses_checkpointed_runtime_state(tmp_path, capsys) -> None:
    class Runtime:
        streamed = False

        def config(self, thread_id, workspace):
            return {"thread_id": thread_id, "workspace": workspace}

        async def astream(self, _values, _config):
            self.streamed = True
            yield (
                (),
                "custom",
                {"type": "copper.final", "data": {"success": True}},
            )

        async def aget_state(self, _config):
            return SimpleNamespace(
                values={"messages": [AIMessage(content="checkpointed response [/ETH_TRD2_N]")]}
            )

    runtime = Runtime()
    args = parser().parse_args([str(tmp_path), "-n", "-m", "Review", "--json", "--no-stream"])
    result = await _headless(runtime, tmp_path, "thread", "Review", args)
    output = capsys.readouterr().out
    assert result == 0
    assert runtime.streamed is True
    assert "checkpointed response" in output
    assert "[/ETH_TRD2_N]" in output
    assert '"thread_id": "thread"' in output


def test_normalize_argv_maps_acp_auth_onto_auth() -> None:
    # T3 Code appends the login method's args to the agent command.
    assert normalize_argv(["acp", "auth", "login"]) == ["auth", "login"]
    assert normalize_argv(["acp", "auth"]) == ["auth"]


def test_normalize_argv_leaves_other_commands_alone() -> None:
    for argv in (["acp"], ["auth", "login"], [], ["mcp"], ["acp", "--help"], ["."]):
        assert normalize_argv(argv) == argv


def test_acp_help_mentions_the_auth_passthrough() -> None:
    assert "copper-pilot acp auth login" in command_parser("acp").format_help()


def test_cli_main_acp_auth_login_runs_the_device_login(monkeypatch) -> None:
    seen: list[str] = []

    async def fake_async_main(args) -> int:
        seen.append(f"{args.command}:{args.action}")
        return 0

    monkeypatch.setattr(sys, "argv", ["copper-pilot", "acp", "auth", "login"])
    monkeypatch.setattr("copper_pilot_cli.copper_main.async_main", fake_async_main)
    with pytest.raises(SystemExit) as excinfo:
        cli_main()
    assert excinfo.value.code == 0
    assert seen == ["auth:login"]


def test_cli_main_acp_auth_login_failure_exits_nonzero(monkeypatch) -> None:
    async def failing(args) -> int:
        raise AuthenticationError("Device login expired or was rejected.")

    monkeypatch.setattr(sys, "argv", ["copper-pilot", "acp", "auth", "login"])
    monkeypatch.setattr("copper_pilot_cli.copper_main.async_main", failing)
    with pytest.raises(SystemExit) as excinfo:
        cli_main()
    assert excinfo.value.code == 2
