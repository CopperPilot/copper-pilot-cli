from __future__ import annotations

import asyncio
import base64

import pytest
from langchain_core.messages import ToolMessage

from copper_pilot_cli.copper_protocol import ToolRequest
from copper_pilot_cli.copper_tools import (
    ApprovalMode,
    LocalToolBroker,
    ToolRejected,
    dangerous_shell_command,
    routine_action,
)


def tool(identifier: str, name: str, **arguments) -> ToolRequest:
    return ToolRequest(tool_call_id=identifier, tool_name=name, arguments=arguments)


@pytest.mark.asyncio
async def test_reads_do_not_prompt(tmp_path) -> None:
    (tmp_path / "hello.txt").write_text("first\nsecond")
    broker = LocalToolBroker(tmp_path)
    result = await broker.execute(tool("1", "read_file", file_path="hello.txt"))
    assert result["result"] == "@@ lines 1-2 of 2 @@\nfirst\nsecond"


@pytest.mark.asyncio
async def test_copper_one_based_read_offset_maps_to_native_offset(tmp_path) -> None:
    (tmp_path / "hello.txt").write_text("first\nsecond\nthird")
    broker = LocalToolBroker(tmp_path)
    result = await broker.execute(
        tool("offset", "read_file", file_path="hello.txt", offset=2, limit=1)
    )
    assert result["result"] == "@@ lines 2-2 of 3 | next offset 2 @@\nsecond"


@pytest.mark.asyncio
async def test_glob_and_grep_honor_requested_path(tmp_path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "inside.txt").write_text("copper")
    (tmp_path / "outside.txt").write_text("copper")
    broker = LocalToolBroker(tmp_path)

    globbed = await broker.execute(tool("glob", "glob", path="nested", pattern="**/*"))
    searched = await broker.execute(
        tool("grep", "grep", path="nested", pattern="copper", glob="*.txt")
    )

    assert globbed["result"] == "['/nested/inside.txt']"
    assert searched["result"] == "/nested/inside.txt"
    assert "outside.txt" not in searched["result"]


@pytest.mark.asyncio
async def test_manual_write_rejects_without_approval(tmp_path) -> None:
    broker = LocalToolBroker(tmp_path)
    with pytest.raises(ToolRejected):
        await broker.execute(tool("1", "write", file_path="hello.txt", content="hello"))
    assert not (tmp_path / "hello.txt").exists()


@pytest.mark.asyncio
async def test_auto_write_uses_native_tool_and_deduplicates(tmp_path) -> None:
    path = tmp_path / "hello.txt"
    path.write_text("old")
    broker = LocalToolBroker(tmp_path, mode=ApprovalMode.AUTO)
    request = tool("same-call", "write", file_path="hello.txt", content="new")
    first = await broker.execute(request)
    path.write_text("changed after result")
    second = await broker.execute(request)
    assert first == second
    assert path.read_text() == "changed after result"
    assert first["result"] == "Updated file /hello.txt"


@pytest.mark.asyncio
async def test_tool_result_deduplication_is_scoped_to_one_turn(tmp_path) -> None:
    broker = LocalToolBroker(tmp_path, mode=ApprovalMode.AUTO)
    first = tool("reused", "write", file_path="value.txt", content="first")
    second = tool("reused", "write", file_path="value.txt", content="second")

    broker.begin_turn()
    await broker.execute(first)
    assert (tmp_path / "value.txt").read_text() == "first"
    broker.begin_turn()
    await broker.execute(second)
    assert (tmp_path / "value.txt").read_text() == "second"


@pytest.mark.asyncio
async def test_concurrent_duplicate_tool_calls_execute_once(tmp_path, monkeypatch) -> None:
    broker = LocalToolBroker(tmp_path)
    calls = 0

    async def execute_once(_request):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return {"result": "once"}

    monkeypatch.setattr(broker, "_execute_once", execute_once)
    request = tool("duplicate", "read_file", file_path="unused")
    first, second = await asyncio.gather(broker.execute(request), broker.execute(request))
    assert first == second == {"result": "once"}
    assert calls == 1


@pytest.mark.asyncio
async def test_external_read_requires_explicit_approval(tmp_path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret")
    broker = LocalToolBroker(tmp_path)
    with pytest.raises(ToolRejected):
        await broker.execute(tool("1", "read_file", file_path=str(outside)))


@pytest.mark.asyncio
async def test_auto_never_approves_external_write(tmp_path) -> None:
    outside = tmp_path.parent / "outside-auto.txt"
    broker = LocalToolBroker(tmp_path, mode=ApprovalMode.AUTO)
    with pytest.raises(ToolRejected):
        await broker.execute(tool("1", "write", file_path=str(outside), content="must not write"))
    assert not outside.exists()


@pytest.mark.asyncio
async def test_binary_round_trip_in_yolo(tmp_path) -> None:
    broker = LocalToolBroker(tmp_path, mode=ApprovalMode.YOLO)
    await broker.execute(
        tool(
            "1",
            "write_binary_file",
            file_path="pixel.bin",
            data=base64.b64encode(b"pixel").decode(),
        )
    )
    result = await broker.execute(tool("2", "read_binary_file", file_path="pixel.bin"))
    assert result["result"].startswith("data:application/octet-stream;base64,")
    assert base64.b64decode(result["result"].partition(",")[2]) == b"pixel"
    assert result["byte_length"] == 5


def test_auto_shell_policy_is_narrow() -> None:
    assert routine_action("bash", {"command": "git status"})
    assert not routine_action("bash", {"command": "git reset --hard"})
    assert not routine_action("bash", {"command": "rm -rf build"})


def test_desktop_style_dangerous_shell_classification() -> None:
    assert not dangerous_shell_command("python -m pytest")
    assert dangerous_shell_command("rm -rf build")
    assert dangerous_shell_command("git status")


@pytest.mark.asyncio
async def test_auto_shell_settings_control_normal_and_dangerous_commands(tmp_path) -> None:
    prompts: list[str] = []

    async def approve(request):
        prompts.append(str(request.arguments["command"]))
        return False

    broker = LocalToolBroker(tmp_path, mode=ApprovalMode.AUTO, approve=approve)
    await broker._authorized(tool("normal", "bash", command="python -V"), "execute")
    assert prompts == []

    with pytest.raises(ToolRejected):
        await broker._authorized(tool("danger", "bash", command="rm -rf build"), "execute")
    assert prompts == ["rm -rf build"]

    broker.set_shell_auto_allow(normal=False, dangerous=True)
    await broker._authorized(tool("danger-allowed", "bash", command="rm -rf build"), "execute")
    with pytest.raises(ToolRejected):
        await broker._authorized(tool("normal-prompted", "bash", command="python -V"), "execute")


@pytest.mark.asyncio
async def test_bash_alias_preserves_native_execute_artifact(tmp_path) -> None:
    broker = LocalToolBroker(tmp_path, mode=ApprovalMode.YOLO)
    result = await broker.execute(tool("shell", "bash", command="exit 7"))
    assert result["exit_code"] == 7
    assert "Exit code: 7" in result["result"]


@pytest.mark.asyncio
async def test_native_validation_error_becomes_hosted_error(tmp_path) -> None:
    broker = LocalToolBroker(tmp_path, mode=ApprovalMode.YOLO)
    result = await broker.execute(
        tool("edit", "edit", file_path="missing.txt", old_string="x", new_string="y")
    )
    assert "error" in result
    assert "not found" in result["error"].lower()


@pytest.mark.asyncio
async def test_native_tool_message_conversion_preserves_multimodal_content(tmp_path) -> None:
    broker = LocalToolBroker(tmp_path)
    message = ToolMessage(
        content=[{"type": "text", "text": "preview"}],
        name="read_file",
        tool_call_id="media",
        status="success",
    )
    assert broker._message_result(message)["result"] == [{"type": "text", "text": "preview"}]


@pytest.mark.asyncio
async def test_delete_is_never_deterministically_auto_approved(tmp_path) -> None:
    path = tmp_path / "remove.txt"
    path.write_text("keep")
    broker = LocalToolBroker(tmp_path, mode=ApprovalMode.AUTO)
    with pytest.raises(ToolRejected):
        await broker.execute(tool("delete", "delete_file", file_path="remove.txt"))
    assert path.exists()


def test_auto_rejects_sensitive_and_dependency_writes(tmp_path) -> None:
    assert not routine_action("write", {"file_path": ".env"}, tmp_path)
    assert not routine_action("write", {"file_path": "pyproject.toml"}, tmp_path)
    assert not routine_action("edit", {"file_path": ".github/workflows/ci.yml"}, tmp_path)


@pytest.mark.asyncio
async def test_ask_user_has_no_client_timeout(tmp_path) -> None:
    async def answer(arguments):
        await asyncio.sleep(0.02)
        return {"status": "answered", "answer": arguments["question"], "source": "custom"}

    broker = LocalToolBroker(tmp_path, ask_user=answer)
    result = await broker.execute(
        ToolRequest(
            tool_call_id="ask",
            tool_name="ask_user_question",
            arguments={"question": "Continue?"},
            timeout_ms=1,
        )
    )
    assert result == {
        "status": "answered",
        "answer": "Continue?",
        "source": "custom",
    }


@pytest.mark.asyncio
async def test_noninteractive_ask_rejects_immediately(tmp_path) -> None:
    broker = LocalToolBroker(tmp_path)
    with pytest.raises(ToolRejected, match="headless and MCP"):
        await broker.execute(tool("ask", "ask_user_question", question="Continue?"))


def _home_workspace(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = home / "board"
    (home / "other").mkdir(parents=True)
    (home / "other" / "secret.kicad_sch").write_text("other project")
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return workspace


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [ApprovalMode.AUTO, ApprovalMode.YOLO])
async def test_outside_workspace_shell_prompts_in_auto_and_yolo(
    tmp_path, monkeypatch, mode
) -> None:
    workspace = _home_workspace(tmp_path, monkeypatch)
    requests = []

    async def approve(request):
        requests.append(request)
        return True

    broker = LocalToolBroker(workspace, mode=mode, approve=approve)
    result = await broker.execute(tool("1", "bash", command="cat ~/other/secret.kicad_sch"))

    assert "other project" in result["result"]
    assert len(requests) == 1
    assert requests[0].outside_workspace == "touches ~/other/secret.kicad_sch"
    assert requests[0].reason == "Outside the workspace: touches ~/other/secret.kicad_sch"
    # Approval is per call: the next one asks again and the mode is unchanged.
    await broker.execute(tool("2", "bash", command="cat ~/other/secret.kicad_sch"))
    assert len(requests) == 2
    assert broker.mode is mode


@pytest.mark.asyncio
async def test_outside_workspace_shell_rejected_by_user(tmp_path, monkeypatch) -> None:
    workspace = _home_workspace(tmp_path, monkeypatch)

    async def decline(_request):
        return False

    broker = LocalToolBroker(workspace, mode=ApprovalMode.YOLO, approve=decline)
    with pytest.raises(ToolRejected, match=r"outside the workspace \(`find` searches /\)"):
        await broker.execute(tool("1", "bash", command="find / -name kicad-cli"))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", list(ApprovalMode))
async def test_headless_rejects_outside_workspace_with_reason(tmp_path, monkeypatch, mode) -> None:
    workspace = _home_workspace(tmp_path, monkeypatch)
    broker = LocalToolBroker(workspace, mode=mode)
    assert broker.has_approver is False
    with pytest.raises(ToolRejected) as caught:
        await broker.execute(tool("1", "bash", command='grep -rn "VCC" / 2>/dev/null'))
    assert str(caught.value) == (
        "blocked: outside the workspace (`grep -r` searches /); ask the user first"
    )


@pytest.mark.asyncio
async def test_in_project_shell_still_runs_without_prompt_in_yolo(tmp_path, monkeypatch) -> None:
    workspace = _home_workspace(tmp_path, monkeypatch)
    (workspace / "board.kicad_sch").write_text("VCC")
    broker = LocalToolBroker(workspace, mode=ApprovalMode.YOLO)
    result = await broker.execute(tool("1", "bash", command="grep -rn VCC ."))
    assert "board.kicad_sch" in result["result"]


@pytest.mark.asyncio
async def test_kicad_purpose_read_returns_whole_file_raw(tmp_path) -> None:
    body = "(kicad_sch\n" + "".join(f"\t(wire {i})\n" for i in range(500)) + ")\n"
    (tmp_path / "board.kicad_sch").write_text(body)
    broker = LocalToolBroker(tmp_path)
    result = await broker.execute(
        tool("1", "read_file", file_path="board.kicad_sch", purpose="kicad_workspace_schematic")
    )
    assert result == {"result": body}


@pytest.mark.asyncio
async def test_plain_read_keeps_the_paged_window(tmp_path) -> None:
    (tmp_path / "notes.txt").write_text("".join(f"{i}\n" for i in range(150)))
    broker = LocalToolBroker(tmp_path)
    result = await broker.execute(tool("1", "read_file", file_path="notes.txt"))
    assert result["result"].startswith("@@ lines 1-100 of 150 | next offset 100 @@\n")


async def _page_through(broker, file_path, **extra):
    """Follow the CLI's pages like the server's bridge does."""
    import re

    pages, offset, n = [], None, 0
    while True:
        args = dict(extra, file_path=file_path)
        if offset is not None:
            args.update(offset=offset, limit=1_000_000)
        n += 1
        text = (await broker.execute(tool(f"p{n}", "read_file", **args)))["result"]
        head, _, body = text.partition("\n")
        match = re.match(r"@@ lines (\d+)-(\d+) of (\d+)", head)
        assert match, head
        pages.append(body)
        end, total = int(match.group(2)), int(match.group(3))
        if end >= total:
            return "\n".join(pages), n
        offset = end + 1


@pytest.mark.asyncio
async def test_oversized_purpose_read_comes_back_in_raw_pages(tmp_path, monkeypatch) -> None:
    from copper_pilot_cli import deepagents_tools

    monkeypatch.setattr(deepagents_tools, "_DIRECT_READ_MAX_BYTES", 2_000)
    body = "".join(f"(segment {i})\n" for i in range(300))
    (tmp_path / "big.kicad_pcb").write_text(body)
    broker = LocalToolBroker(tmp_path)
    text, pages = await _page_through(broker, "big.kicad_pcb", purpose="kicad_workspace_pcb")
    assert text == body
    assert pages > 1


@pytest.mark.asyncio
async def test_big_external_library_is_paged_not_lost(tmp_path, monkeypatch) -> None:
    """Connector_Generic.kicad_sym (6.7 MB) is over one frame; the native read
    could not see it outside the workspace and said 'not found'."""
    from copper_pilot_cli import deepagents_tools

    monkeypatch.setattr(deepagents_tools, "_DIRECT_READ_MAX_BYTES", 4_000)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    lib = tmp_path / "Connector_Generic.kicad_sym"
    body = "(kicad_symbol_lib\n" + '\t(symbol "Conn" (pin 1))\n' * 500 + ")\n"
    lib.write_text(body)
    broker = LocalToolBroker(workspace)
    text, pages = await _page_through(broker, str(lib), external=True, platform="linux")
    assert text == body
    assert pages > 1


@pytest.mark.asyncio
async def test_internal_read_honours_offset_and_limit(tmp_path) -> None:
    (tmp_path / "a.kicad_sch").write_text("l1\nl2\nl3\nl4")
    broker = LocalToolBroker(tmp_path)
    result = await broker.execute(
        tool(
            "1",
            "read_file",
            file_path="a.kicad_sch",
            purpose="kicad_workspace_schematic",
            offset=2,
            limit=2,
        )
    )
    assert result == {"result": "@@ lines 2-3 of 4 | next offset 3 @@\nl2\nl3"}


@pytest.mark.asyncio
async def test_binary_read_returns_every_byte_of_a_long_text_file(tmp_path) -> None:
    body = "".join(f"(line {i})\n" for i in range(5000)).encode()
    (tmp_path / "board.kicad_sch").write_bytes(body)
    broker = LocalToolBroker(tmp_path)
    result = await broker.execute(tool("1", "read_binary_file", file_path="board.kicad_sch"))
    assert base64.b64decode(result["result"].partition(",")[2]) == body
    assert result["byte_length"] == len(body)


@pytest.mark.asyncio
async def test_binary_read_keeps_unknown_binary_bytes(tmp_path) -> None:
    data = bytes(range(256)) * 4
    (tmp_path / "sheet.SchDoc").write_bytes(data)
    broker = LocalToolBroker(tmp_path)
    result = await broker.execute(tool("1", "read_binary_file", file_path="sheet.SchDoc"))
    assert base64.b64decode(result["result"].partition(",")[2]) == data


@pytest.mark.asyncio
async def test_empty_binary_read_is_zero_bytes(tmp_path) -> None:
    (tmp_path / "empty.kicad_pcb").write_bytes(b"")
    broker = LocalToolBroker(tmp_path)
    result = await broker.execute(tool("1", "read_binary_file", file_path="empty.kicad_pcb"))
    assert result["byte_length"] == 0
    assert result["result"].endswith(";base64,")


@pytest.mark.asyncio
async def test_external_kicad_library_reads_are_allowed(tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    lib = tmp_path / "stock" / "Device.kicad_sym"
    lib.parent.mkdir()
    lib.write_text("(kicad_symbol_lib\n" + "\t(symbol x)\n" * 300 + ")\n")
    broker = LocalToolBroker(workspace)
    text = await broker.execute(
        tool("1", "read_file", file_path=str(lib), external=True, platform="linux")
    )
    assert text == {"result": lib.read_text()}
    binary = await broker.execute(
        tool(
            "2",
            "read_binary_file",
            file_path=str(lib),
            external=True,
            platform="linux",
            purpose="kicad_symbol_library",
        )
    )
    assert base64.b64decode(binary["result"].partition(",")[2]) == lib.read_bytes()


@pytest.mark.asyncio
async def test_external_reads_stay_blocked_without_flag_or_for_other_files(tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    lib = tmp_path / "Device.kicad_sym"
    lib.write_text("(kicad_symbol_lib)")
    secret = tmp_path / "id_rsa"
    secret.write_text("secret")
    broker = LocalToolBroker(workspace, mode=ApprovalMode.YOLO)
    with pytest.raises(ToolRejected):
        await broker.execute(tool("1", "read_file", file_path=str(lib)))
    with pytest.raises(ToolRejected):
        await broker.execute(tool("2", "read_file", file_path=str(secret), external=True))
    with pytest.raises(ToolRejected):
        await broker.execute(
            tool("3", "write_file", file_path=str(lib), content="x", external=True)
        )
    assert lib.read_text() == "(kicad_symbol_lib)"
