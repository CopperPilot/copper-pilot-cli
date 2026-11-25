"""Shared fakes for the bring-your-own-agent tests: a fake tools socket server."""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from typing import Any

from websockets.asyncio.server import serve

from copper_pilot_cli.copper_auth import DeviceCredential

CANARY = f"CP-CANARY-{uuid.uuid4().hex}"
# The server ends the guide with a per-session canary comment.
GUIDE = f"Use copperpilot_subagent for edits. Read before you write.\n<!-- cp-session:{CANARY} -->"
TOOLS = [
    {
        "name": "query_design",
        "description": "Query the KiCad design.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
        "read_only": True,
    },
    {
        "name": "run_erc",
        "description": "Run ERC.",
        "input_schema": {"type": "object", "properties": {}},
        "read_only": True,
    },
    {
        "name": "place_symbol",
        "description": "Place a symbol.",
        "input_schema": {"type": "object", "properties": {}},
        "read_only": False,
    },
]


class FakeToolsServer:
    """Speaks the server side of ``/api/copperpilot/tools/ws`` (SPEC protocol v1)."""

    def __init__(self) -> None:
        self.starts: list[dict[str, Any]] = []
        self.frames: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.bridge_results: list[dict[str, Any]] = []
        self.port = 0
        self.refuse_with: dict[str, Any] | None = None
        # Drop the first connection right after ready, with a server_restarting error.
        self.restart_once = False
        # ``query_design`` first asks the client to read this file over the bridge.
        self.bridge_read: str | None = None
        self.bridge_write: dict[str, Any] | None = None
        self.connections = 0
        # What the server answers a ``turn_end`` with; ``turn_end_silent`` never answers.
        self.turn_end_note: str | None = None
        self.turn_end_silent = False

    @property
    def credential(self) -> DeviceCredential:
        return DeviceCredential(
            api_key="device-key", fingerprint="fp", base_url=f"http://127.0.0.1:{self.port}"
        )

    async def handler(self, socket: Any) -> None:
        self.connections += 1
        request = getattr(socket, "request", None)
        if request is not None:
            self.headers.append(dict(request.headers))
        start = json.loads(await socket.recv())
        self.starts.append(start)
        if self.refuse_with is not None:
            await socket.send(json.dumps({"type": "error", **self.refuse_with}))
            await socket.close()
            return
        await socket.send(
            json.dumps(
                {
                    "type": "tool_session_ready",
                    "protocol_version": 1,
                    "session_id": start["session_id"],
                    "tools": TOOLS,
                    "guide": {"title": "CopperPilot guide", "text": GUIDE},
                    "limits": {"max_concurrent_calls": 4, "calls_per_minute": 60},
                }
            )
        )
        pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        restart = self.restart_once
        self.restart_once = False

        async def serve_call(frame: dict[str, Any]) -> None:
            text = f"result of {frame['name']}"
            if frame["name"] == "query_design" and (self.bridge_read or self.bridge_write):
                bridge_id = f"bridge-{uuid.uuid4().hex[:6]}"
                future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
                pending[bridge_id] = future
                request: dict[str, Any] = {
                    "type": "tool_request",
                    "tool_call_id": bridge_id,
                    "parent_call_id": frame["call_id"],
                }
                if self.bridge_write is not None:
                    request.update(self.bridge_write)
                else:
                    request.update(
                        {"tool_name": "read_file", "arguments": {"file_path": self.bridge_read}}
                    )
                await socket.send(json.dumps(request))
                answer = await future
                self.bridge_results.append(answer)
                text = json.dumps(answer["result"])
            await socket.send(
                json.dumps({"type": "tool_progress", "call_id": frame["call_id"], "message": "50%"})
            )
            await socket.send(
                json.dumps(
                    {
                        "type": "tool_call_result",
                        "call_id": frame["call_id"],
                        "ok": True,
                        "content": [{"type": "text", "text": text}],
                        "usage": {"copperpilot_tokens": 12},
                    }
                )
            )

        try:
            async for raw in socket:
                frame = json.loads(raw)
                self.frames.append(frame)
                if frame.get("type") == "tool_call":
                    if restart:
                        restart = False
                        await socket.send(
                            json.dumps(
                                {
                                    "type": "error",
                                    "code": "server_restarting",
                                    "retryable": True,
                                    "public_message": "Restarting",
                                }
                            )
                        )
                        await socket.close()
                        return
                    asyncio.create_task(serve_call(frame))
                elif frame.get("type") == "turn_end":
                    if not self.turn_end_silent:
                        await socket.send(
                            json.dumps(
                                {
                                    "type": "turn_end_result",
                                    "turn_id": frame["turn_id"],
                                    "applied": self.turn_end_note is not None,
                                    "note": self.turn_end_note,
                                }
                            )
                        )
                elif frame.get("type") == "tool_result":
                    future = pending.pop(frame["tool_call_id"], None)
                    if future is not None:
                        future.set_result(frame)
        except Exception:  # noqa: BLE001 - the client went away
            return

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [frame for frame in self.frames if frame.get("type") == kind]


@asynccontextmanager
async def tools_server():
    fake = FakeToolsServer()
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        fake.port = next(iter(server.sockets)).getsockname()[1]
        yield fake
