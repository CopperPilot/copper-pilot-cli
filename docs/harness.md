# Use CopperPilot from Claude Code, Cursor, or Codex

CopperPilot stays the electronics agent. The outer tool (Claude Code, Cursor,
Codex, or a script) only **delegates**. Do not hand-edit KiCad files while a
CopperPilot run is in flight.

Install the CLI and log in once from a terminal:

```console
pip install copper-pilot-cli
copper-pilot auth login
```

## Option 1: shell out to the CLI

Copy [`skills/copper-pilot-review/SKILL.md`](../skills/copper-pilot-review/SKILL.md)
into the skill location your harness reads:

| Harness | Skill location |
|---------|----------------|
| Claude Code | `~/.claude/skills/copper-pilot-review/SKILL.md` or the project's `.claude/skills/` |
| Cursor | `.cursor/skills/copper-pilot-review/SKILL.md` or project rules that point at the file |
| Codex | `AGENTS.md` or the project's skill directory, depending on your Codex setup |

Then ask the outer agent to review the board. It should run:

```console
copper-pilot -n -m "Review this board" --json --no-stream --mode ask
```

`--json --no-stream` prints one JSON document. `thread_id` is included so the
next turn can pass `--resume THREAD_ID`.

Use `--mode plan` for a plan, `--mode agent -y` for Auto local writes, and
`--yolo` only when the user asked to skip local approval. Add
`--effort intelligence` for a paid Intelligence turn; Balanced is the default.

## Option 2: MCP conductor (`copper-pilot mcp`)

The same process as the CLI, same stored credential. Stdio JSON-RPC; do not
write logs to stdout.

### Claude Code

```console
claude mcp add copper-pilot -- copper-pilot mcp
```

Or install the in-repo plugin (starts the same server automatically):

```text
/plugin marketplace add CopperPilot/copper-pilot-cli
/plugin install copper-pilot
```

Local checkout:

```console
claude --plugin-dir plugins/copper-pilot
```

The plugin also registers `@copper-pilot` and `/copper-pilot-review`.

### Cursor

Add a stdio server in `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "copper-pilot": {
      "command": "copper-pilot",
      "args": ["mcp"]
    }
  }
}
```

### Codex and other MCP hosts

Point the host at `copper-pilot mcp` on stdio. Tools:

| Tool | Role |
|------|------|
| `status` | Login state and discovered schematic/PCB paths |
| `ask` | Read-oriented specialist (`--mode ask`); optional `effort` |
| `plan` | Plan without applying (`--mode plan`); optional `effort` |
| `run` | Mutating agent turn (`--mode agent`); optional `effort` |
| `resume` | Continue `thread_id` (`mode` defaults to `agent`; optional `effort`) |
| `threads_list` | List threads for this workspace |

Workspace is the tool `workspace` argument, else `CLAUDE_PROJECT_DIR`, else the
process current directory.

## Option 3: ACP server (`copper-pilot acp`)

The Agent Client Protocol (ACP) lets editors drive an agent over stdio
JSON-RPC with streaming messages, tool calls, and permission prompts. The SDK
is an optional extra:

```console
pip install "copper-pilot-cli[acp]"
```

### Zed

Add an external agent to Zed `settings.json`:

```json
{
  "agent_servers": {
    "CopperPilot": {
      "command": "copper-pilot",
      "args": ["acp"]
    }
  }
}
```

Open the agent panel, pick CopperPilot, and chat. The project root is the
workspace. The mode picker switches `ask`, `plan`, and `agent`; the `effort`
option selects Balanced or Intelligence. If no credential is stored the server
answers `auth_required` and offers a terminal login method that runs
`copper-pilot auth login`.

### Other ACP clients

Launch `copper-pilot acp` on stdio. The server implements `initialize`,
`authenticate`, `session/new`, `session/load`, `session/set_mode`,
`session/set_config_option`, `session/prompt`, and `session/cancel`.
`session/load` takes an existing CopperPilot thread id (see
`copper-pilot threads list`) and replays its messages and tool calls.

Hosted text, reasoning, and status arrive as agent message and thought chunks;
hosted tool requests and results arrive as `tool_call` and `tool_call_update`.
A hosted error ends the prompt with a JSON-RPC error whose `details` carry the
public message.

## Approval

| Mode | Where | Behavior |
|------|-------|----------|
| Manual | Interactive CLI / TUI only | Prompt before gated writes and shell |
| Auto | Harness default (`-y`, MCP `approval=auto`, ACP sessions) | Routine writes and known-safe shell |
| YOLO | Explicit `--yolo`, MCP `approval=yolo`, or ACP "Allow all for this session" | All local side effects. Never saved |

MCP cannot show the TUI, so Manual is not available there. `ask` and `plan`
always use Auto. `run` and `resume` default to Auto.

ACP sessions start in Auto. Anything Auto would have prompted for in the TUI
is sent to the editor as `session/request_permission` with Allow, Allow all
for this session, and Reject. Choosing "Allow all" puts that session in YOLO
until the server exits. Hosted clarifying questions that offer options are
presented the same way, with a Skip choice; free-text questions are declined
with a pointer to the TUI.

`effort` is `balanced` (default) or `intelligence`. Intelligence requires a
paid plan. It may run server-owned survey subagents; Plan turns survey before
the parent writes a plan. Progress reports compact worker transitions and the
result digest omits full worker output. If the hosted agent asks a clarification,
MCP returns a clear rejection because it cannot present the interactive options
and Skip UI; continue the returned `thread_id` in the terminal TUI to answer.

Missing credentials: MCP tools return `login_required` and tell the user to run
`copper-pilot auth login`. The server does not open a browser.

A hosted usage block sets `ok` and `success` to false. `error` remains the
server's public message. The matching digest entry keeps the structured
`data` object (`status`, `code`, `block_reason`, `can_purchase_overage`, and
`retryable` when the server sends them). Checkout stays on the CopperPilot
billing page. See [docs/cli.md](cli.md).
