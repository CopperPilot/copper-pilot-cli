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
`--yolo` only when the user asked to skip local approval.

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
| `ask` | Read-oriented specialist (`--mode ask`) |
| `plan` | Plan without applying (`--mode plan`) |
| `run` | Mutating agent turn (`--mode agent`) |
| `resume` | Continue `thread_id` (`mode` defaults to `agent`; pass `mode="ask"` to keep it read-only) |
| `threads_list` | List threads for this workspace |

Workspace is the tool `workspace` argument, else `CLAUDE_PROJECT_DIR`, else the
process current directory.

## Approval

| Mode | Where | Behavior |
|------|-------|----------|
| Manual | Interactive CLI / TUI only | Prompt before gated writes and shell |
| Auto | Harness default (`-y`, MCP `approval=auto`) | Routine writes and known-safe shell |
| YOLO | Explicit `--yolo` or MCP `approval=yolo` | All local side effects. Never saved |

MCP cannot show the TUI, so Manual is not available there. `ask` and `plan`
always use Auto. `run` and `resume` default to Auto.

Missing credentials: MCP tools return `login_required` and tell the user to run
`copper-pilot auth login`. The server does not open a browser.

See [docs/cli.md](cli.md) for the full command reference.
