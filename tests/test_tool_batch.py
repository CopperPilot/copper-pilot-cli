from __future__ import annotations

import asyncio
import time

import pytest

from copper_pilot_cli.copper_protocol import ToolRequest
from copper_pilot_cli.tool_batch import (
    ToolClass,
    ToolPlan,
    classify_tool,
    parallel_tools_enabled,
    plan_dependencies,
    read_only_shell_command,
    run_tool_batch,
)


def tool(call_id: str, name: str, **arguments) -> ToolRequest:
    return ToolRequest(tool_call_id=call_id, tool_name=name, arguments=arguments)


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git log --oneline -5",
        "git diff HEAD~1 -- board.kicad_sch",
        "git show HEAD:README.md",
        "git ls-files",
        "git rev-parse HEAD",
        "git branch --list",
        "git branch",
        "ls -la hardware",
        "cat README.md",
        "head -n 20 board.kicad_sch",
        "wc -l *.kicad_sch",
        "grep -rn VCC .",
        "rg footprint",
        "find . -name '*.kicad_pro'",
        "pwd",
    ],
)
def test_read_only_shell_commands_are_recognized(command: str) -> None:
    assert read_only_shell_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "",
        "make",
        "rm -rf build",
        "git commit -m x",
        "git checkout main",
        "git branch feature",
        "git diff --output=patch.diff",
        "git -c core.pager=sh status",
        "find . -delete",
        "find . -exec rm {} ;",
        "rg --pre ./script pattern",
        "tail -f log.txt",
        "cat a > b",
        "ls; rm x",
        "ls && touch x",
        "echo $(whoami)",
        "cat `ls`",
        "ls | xargs rm",
        "ls\nrm x",
        "FOO=1 ls",
        "cat 'unterminated",
    ],
)
def test_mutating_or_unprovable_shell_commands_are_not_read_only(command: str) -> None:
    assert not read_only_shell_command(command)


def test_classify_tools() -> None:
    workspace = "/work"
    assert classify_tool(tool("1", "read_file", file_path="a.txt"), workspace) == ToolPlan(
        ToolClass.READ, "/work/a.txt"
    )
    assert classify_tool(tool("2", "read_binary_file", file_path="/work/./img.png")) == ToolPlan(
        ToolClass.READ, "/work/img.png"
    )
    assert classify_tool(tool("3", "grep", pattern="x")).kind is ToolClass.READ
    assert classify_tool(tool("4", "ls", path=".")).path is None
    assert classify_tool(tool("5", "write", file_path="b/../a.txt"), workspace) == ToolPlan(
        ToolClass.MUTATING, "/work/a.txt"
    )
    assert classify_tool(tool("6", "edit", file_path="a.txt"), workspace).kind is (
        ToolClass.MUTATING
    )
    assert classify_tool(tool("7", "delete_file", file_path="a.txt")).kind is ToolClass.MUTATING
    assert classify_tool(tool("8", "write")).kind is ToolClass.EXCLUSIVE
    assert classify_tool(tool("9", "bash", command="git status")).kind is ToolClass.READ
    assert classify_tool(tool("10", "bash", command="make flash")).kind is ToolClass.EXCLUSIVE
    assert classify_tool(tool("11", "ask_user_question", question="?")).kind is (
        ToolClass.EXCLUSIVE
    )
    assert classify_tool(tool("12", "computer_click")).kind is ToolClass.EXCLUSIVE


def test_dependencies_follow_model_order() -> None:
    plans = [
        ToolPlan(ToolClass.READ, "/a"),  # 0
        ToolPlan(ToolClass.MUTATING, "/a"),  # 1 waits for 0
        ToolPlan(ToolClass.READ, "/b"),  # 2 free
        ToolPlan(ToolClass.MUTATING, "/a"),  # 3 waits for 0, 1
        ToolPlan(ToolClass.READ, "/a"),  # 4 waits for 1, 3
        ToolPlan(ToolClass.READ),  # 5 directory scope: waits for writers 1, 3
        ToolPlan(ToolClass.EXCLUSIVE),  # 6 waits for all
        ToolPlan(ToolClass.READ, "/c"),  # 7 waits for 6
    ]
    assert plan_dependencies(plans) == [
        set(),
        {0},
        set(),
        {0, 1},
        {1, 3},
        {1, 3},
        {0, 1, 2, 3, 4, 5},
        {6},
    ]


def test_parallel_tools_env_toggle(monkeypatch) -> None:
    monkeypatch.delenv("COPPER_PILOT_PARALLEL_TOOLS", raising=False)
    assert parallel_tools_enabled()
    monkeypatch.setenv("COPPER_PILOT_PARALLEL_TOOLS", "0")
    assert not parallel_tools_enabled()


async def collect(requests, handler, **kwargs):
    return [item async for item in run_tool_batch(requests, handler, **kwargs)]


@pytest.mark.asyncio
async def test_reads_overlap_and_complete_out_of_order() -> None:
    delays = {"slow": 0.3, "medium": 0.2, "fast": 0.1}
    running = 0
    peak = 0

    async def handler(request):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(delays[request.tool_call_id])
        running -= 1
        return {"result": request.tool_call_id}

    requests = [tool(name, "read_file", file_path=f"{name}.txt") for name in delays]
    started = time.perf_counter()
    outcomes = await collect(requests, handler)
    elapsed = time.perf_counter() - started
    assert [request.tool_call_id for request, _ in outcomes] == ["fast", "medium", "slow"]
    assert all(result == {"result": request.tool_call_id} for request, result in outcomes)
    assert peak == 3
    assert elapsed < 0.5


@pytest.mark.asyncio
async def test_read_concurrency_is_capped() -> None:
    running = 0
    peak = 0

    async def handler(_request):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        return {"result": "ok"}

    requests = [tool(str(index), "read_file", file_path=f"{index}.txt") for index in range(6)]
    await collect(requests, handler, max_concurrent_reads=2)
    assert peak == 2


@pytest.mark.asyncio
async def test_same_file_writes_run_in_model_order() -> None:
    log: list[str] = []

    async def handler(request):
        log.append(f"start {request.tool_call_id}")
        # The first write is the slowest; it must still finish first.
        await asyncio.sleep(0.05 if request.tool_call_id == "w1" else 0)
        log.append(f"end {request.tool_call_id}")
        return {"result": "ok"}

    requests = [
        tool("w1", "write", file_path="a.txt", content="1"),
        tool("w2", "edit", file_path="./a.txt", old_string="1", new_string="2"),
        tool("w3", "write", file_path="a.txt", content="3"),
    ]
    await collect(requests, handler, workspace="/work")
    assert log == ["start w1", "end w1", "start w2", "end w2", "start w3", "end w3"]


@pytest.mark.asyncio
async def test_writes_to_different_files_overlap() -> None:
    running = 0
    peak = 0

    async def handler(_request):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        return {"result": "ok"}

    requests = [
        tool("a", "write", file_path="a.txt", content=""),
        tool("b", "write", file_path="b.txt", content=""),
    ]
    await collect(requests, handler)
    assert peak == 2


@pytest.mark.asyncio
async def test_exclusive_shell_runs_alone() -> None:
    log: list[str] = []

    async def handler(request):
        log.append(f"start {request.tool_call_id}")
        await asyncio.sleep(0.01)
        log.append(f"end {request.tool_call_id}")
        return {"result": "ok"}

    requests = [
        tool("r1", "read_file", file_path="a.txt"),
        tool("sh", "bash", command="make build"),
        tool("r2", "read_file", file_path="b.txt"),
    ]
    await collect(requests, handler)
    assert log.index("end r1") < log.index("start sh")
    assert log.index("end sh") < log.index("start r2")


@pytest.mark.asyncio
async def test_failing_tool_does_not_lose_other_results() -> None:
    async def handler(request):
        if request.tool_call_id == "bad":
            raise RuntimeError("disk on fire")
        await asyncio.sleep(0.01)
        return {"result": request.tool_call_id}

    requests = [
        tool("good-1", "read_file", file_path="a.txt"),
        tool("bad", "read_file", file_path="b.txt"),
        tool("good-2", "read_file", file_path="c.txt"),
    ]
    outcomes = dict(
        (request.tool_call_id, outcome) for request, outcome in await collect(requests, handler)
    )
    assert outcomes["good-1"] == {"result": "good-1"}
    assert outcomes["good-2"] == {"result": "good-2"}
    assert isinstance(outcomes["bad"], RuntimeError)


@pytest.mark.asyncio
async def test_closing_the_batch_cancels_unfinished_members() -> None:
    cancelled: list[str] = []

    async def handler(request):
        try:
            await asyncio.sleep(0 if request.tool_call_id == "fast" else 10)
        except asyncio.CancelledError:
            cancelled.append(request.tool_call_id)
            raise
        return {"result": request.tool_call_id}

    requests = [
        tool("fast", "read_file", file_path="a.txt"),
        tool("slow-1", "read_file", file_path="b.txt"),
        tool("slow-2", "write", file_path="b.txt", content=""),
    ]
    batch = run_tool_batch(requests, handler)
    first = await anext(batch)
    assert first[0].tool_call_id == "fast"
    await batch.aclose()
    assert cancelled == ["slow-1"]
