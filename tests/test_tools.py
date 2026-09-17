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
