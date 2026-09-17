"""Credential-aware end-to-end tests against the hosted CopperPilot service."""

from __future__ import annotations

import asyncio
import json
import sys
import uuid

import pytest
from langchain_core.messages import HumanMessage
from textual.widgets import TextArea

from copper_pilot_cli.copper_app import CopperPilotApp
from copper_pilot_cli.copper_auth import DeviceCredential, load_credential, validate_credential
from copper_pilot_cli.copper_graph import create_copper_graph
from copper_pilot_cli.copper_protocol import CopperMode, EventKind
from copper_pilot_cli.copper_tools import ApprovalMode, LocalToolBroker
from copper_pilot_cli.copper_widgets import AssistantMessage
from copper_pilot_cli.langchain import CopperPilotAgent


@pytest.fixture(scope="module")
def live_credential() -> DeviceCredential:
    credential = load_credential()
    if credential is None:
        pytest.skip("CopperPilot credentials were not found")
    return credential


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_tui_composer_renders_hosted_response(
    live_credential: DeviceCredential,
    tmp_path,
) -> None:
    thread_id = f"live-tui-{uuid.uuid4()}"
    broker = LocalToolBroker(tmp_path)
    agent = CopperPilotAgent(
        tmp_path,
        credential=live_credential,
        conversation_id=thread_id,
        mode=CopperMode.ASK,
        tool_broker=broker,
    )
    async with create_copper_graph(agent) as runtime:
        app = CopperPilotApp(runtime, broker, tmp_path, thread_id, mode=CopperMode.ASK)
        async with app.run_test(size=(120, 40)) as pilot:
            area = app.query_one(TextArea)
            area.text = "Hello, reply briefly."
            area.focus()
            await pilot.press("enter")
            async with asyncio.timeout(120):
                while not list(app.query(AssistantMessage)) or app._turn_task is not None:
                    await pilot.pause()
                    await asyncio.sleep(0.05)
            await pilot.pause()
            assert list(app.query(AssistantMessage))[-1].content.strip()


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_cli_headless_json_chat(
    live_credential: DeviceCredential,
    tmp_path,
) -> None:
    assert live_credential
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "copper_pilot_cli",
        str(tmp_path),
        "--mode",
        "ask",
        "--message",
        "Hello, reply briefly.",
        "--non-interactive",
        "--json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    async with asyncio.timeout(120):
        stdout, stderr = await process.communicate()

    assert process.returncode == 0, stderr.decode(errors="replace")
    events = [
        json.loads(line) for line in stdout.decode().splitlines() if line.strip().startswith("{")
    ]
    assert any(event["type"] == "text" and event["data"] for event in events)
    assert events[-1]["type"] == "final"
    assert "initial_file_tree" not in events[-1]["data"]


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_ask_chat_streams_text_and_final(
    live_credential: DeviceCredential,
    tmp_path,
) -> None:
    assert await validate_credential(live_credential)
    agent = CopperPilotAgent(
        tmp_path,
        credential=live_credential,
        conversation_id=f"live-ask-{uuid.uuid4()}",
        mode=CopperMode.ASK,
    )

    async with asyncio.timeout(120):
        events = [
            event
            async for event in agent.astream_events_raw(
                {"messages": [HumanMessage(content="Hello, briefly introduce yourself.")]}
            )
        ]

    assert not [event for event in events if event.kind is EventKind.ERROR]
    assert any(event.kind is EventKind.REASONING for event in events)
    assert any(event.kind is EventKind.TEXT and str(event.data).strip() for event in events)
    assert events[-1].kind is EventKind.FINAL
    assert events[-1].data["success"] is not False


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_agent_chat_round_trips_local_tool_results(
    live_credential: DeviceCredential,
    tmp_path,
) -> None:
    marker = f"COPPER-E2E-{uuid.uuid4().hex[:12]}"
    (tmp_path / "board-summary.txt").write_text(
        f"Project code: {marker}\nBoard: credential-aware CLI integration fixture\n"
    )
    broker = LocalToolBroker(tmp_path, mode=ApprovalMode.AUTO)
    agent = CopperPilotAgent(
        tmp_path,
        credential=live_credential,
        conversation_id=f"live-agent-{uuid.uuid4()}",
        mode=CopperMode.AGENT,
        tool_broker=broker,
    )

    async with asyncio.timeout(180):
        events = [
            event
            async for event in agent.astream_events_raw(
                {
                    "messages": [
                        HumanMessage(content="What project code is recorded in board-summary.txt?")
                    ]
                }
            )
        ]

    requests = {
        event.data.tool_call_id: event for event in events if event.kind is EventKind.TOOL_REQUEST
    }
    results = {
        event.data["tool_call_id"]: event for event in events if event.kind is EventKind.TOOL_RESULT
    }
    response = "".join(str(event.data) for event in events if event.kind is EventKind.TEXT)

    assert requests, "The hosted agent did not request a local tool"
    assert requests.keys() <= results.keys()
    assert marker in response
    assert events[-1].kind is EventKind.FINAL
    assert events[-1].data["success"] is not False
