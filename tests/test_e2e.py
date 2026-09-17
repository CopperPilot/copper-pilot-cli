from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from textual.widgets import TextArea

from copper_pilot_cli.copper_app import CopperPilotApp
from copper_pilot_cli.copper_auth import DeviceCredential, DeviceLogin
from copper_pilot_cli.copper_graph import create_copper_graph
from copper_pilot_cli.copper_tools import LocalToolBroker
from copper_pilot_cli.copper_widgets import AssistantMessage, ToolCallMessage
from copper_pilot_cli.langchain import CopperPilotAgent
from copper_pilot_cli.sessions import list_threads


@pytest.mark.asyncio
async def test_login_stream_tool_checkpoint_and_resume(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("COPPER_PILOT_CLI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("copper_pilot_cli.sessions._db_path", None)

    def auth_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"flow_token": "flow-token", "expires_in_seconds": 30},
            )
        return httpx.Response(
            200,
            json={"status": "ready", "api_key": "cf_live_e2e"},
        )

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(auth_handler)

    def mocked_client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mocked_client)
    credential = await DeviceLogin("https://example.test").login(open_browser=False)

    workspace = tmp_path / "board"
    workspace.mkdir()
    (workspace / "board.kicad_sch").write_text("(kicad_sch)")

    class Socket:
        def __init__(self) -> None:
            self.frames = [
                json.dumps(
                    {
                        "type": "event",
                        "chat_response": {
                            "reasoning_update": "Reading the schematic",
                            "tool_request": {
                                "tool_call_id": "read-1",
                                "tool_name": "read_file",
                                "arguments": {"file_path": "board.kicad_sch"},
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "final",
                        "chat_response": {
                            "response": "The schematic is valid.",
                            "is_final": True,
                        },
                    }
                ),
            ]
            self.sent: list[dict] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def send(self, value: str) -> None:
            self.sent.append(json.loads(value))

        async def recv(self) -> str:
            return self.frames.pop(0)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.frames:
                raise StopAsyncIteration
            return self.frames.pop(0)

    socket = Socket()
    monkeypatch.setattr("copper_pilot_cli.copper_protocol.connect", lambda *a, **k: socket)

    agent = CopperPilotAgent(
        workspace,
        credential=credential,
        conversation_id="e2e-thread",
        tool_broker=LocalToolBroker(workspace),
    )
    async with create_copper_graph(agent) as runtime:
        config = runtime.config("e2e-thread", workspace)
        _ = [
            item
            async for item in runtime.astream(
                {"messages": [HumanMessage(content="Review")]},
                config,
            )
        ]
        state = await runtime.aget_state(config)
        tool_messages = [
            message for message in state.values["messages"] if isinstance(message, ToolMessage)
        ]
        assert tool_messages[0].status == "success"
        assert "@@ lines 1-1 of 1 @@" in str(tool_messages[0].content)
        assert isinstance(state.values["messages"][-1], AIMessage)
        assert state.values["messages"][-1].content == "The schematic is valid."

    assert socket.sent[1]["tool_call_id"] == "read-1"
    assert socket.sent[1]["result"]["result"].endswith("(kicad_sch)")
    rows = await list_threads(cwd=str(workspace), include_message_count=True)
    assert rows[0]["thread_id"] == "e2e-thread"


@pytest.mark.asyncio
async def test_composer_to_protocol_tool_and_rendering_with_hostile_text(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("COPPER_PILOT_CLI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("copper_pilot_cli.sessions._db_path", None)
    workspace = tmp_path / "board"
    workspace.mkdir()
    malformed = "[/ETH_TRD2_N]"
    (workspace / "net.txt").write_text(malformed)

    class Socket:
        def __init__(self) -> None:
            self.frames = [
                json.dumps(
                    {
                        "type": "event",
                        "chat_response": {
                            "reasoning_update": malformed,
                            "tool_request": {
                                "tool_call_id": "read-hostile",
                                "tool_name": "read_file",
                                "arguments": {"file_path": "net.txt"},
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "final",
                        "chat_response": {
                            "response": f"Observed {malformed}",
                            "success": True,
                            "is_final": True,
                        },
                    }
                ),
            ]
            self.sent: list[dict] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def send(self, value: str) -> None:
            self.sent.append(json.loads(value))

        async def recv(self) -> str:
            return self.frames.pop(0)

    socket = Socket()
    monkeypatch.setattr("copper_pilot_cli.copper_protocol.connect", lambda *a, **k: socket)
    credential = DeviceCredential(
        api_key="cf_live_e2e",
        fingerprint="test-device",
        base_url="https://example.test",
    )
    broker = LocalToolBroker(workspace)
    agent = CopperPilotAgent(
        workspace,
        credential=credential,
        conversation_id="tui-e2e-thread",
        tool_broker=broker,
    )

    async with create_copper_graph(agent) as runtime:
        app = CopperPilotApp(runtime, broker, workspace, "tui-e2e-thread")
        async with app.run_test(size=(120, 40)) as pilot:
            area = app.query_one(TextArea)
            area.text = "Inspect net.txt"
            area.focus()
            await pilot.press("enter")
            async with asyncio.timeout(5):
                while not list(app.query(AssistantMessage)) or app._turn_task is not None:
                    await pilot.pause()
                    await asyncio.sleep(0)
            await pilot.pause()

            assert malformed in list(app.query(AssistantMessage))[-1].content
            assert malformed in list(app.query(ToolCallMessage))[-1].output
            assert socket.sent[1]["tool_call_id"] == "read-hostile"
