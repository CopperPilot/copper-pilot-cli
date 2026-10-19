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
`--model <id>` picks the Intelligence model (and implies Intelligence) while
model selection is on; it is currently off, see
[cli.md](cli.md#effort-intelligence-and-models).

For sign-off, run `copper-pilot validate --json`, read
`.copperpilot/validate/latest/next_actions.json`, fix with `--mode agent -y`
citing the finding IDs, and re-run. See [validate.md](validate.md).

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
| `models` | Intelligence model catalog for the `model` argument; no hosted turn |
| `ask` | Read-oriented specialist (`--mode ask`); optional `effort`, `model` |
| `plan` | Plan without applying (`--mode plan`); optional `effort`, `model` |
| `run` | Mutating agent turn (`--mode agent`); optional `effort`, `model` |
| `resume` | Continue `thread_id` (`mode` defaults to `agent`; optional `effort`, `model`) |
| `threads_list` | List threads for this workspace |
| `validate` | Read-only stage-gated review graph (`copper-pilot validate`); returns `next_actions` |

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
option selects Balanced or Intelligence, and the `model` option (ACP category
`model`, so T3 Code shows it in its own model picker) selects the Intelligence
model. If no credential is stored the server answers `auth_required` and
offers a terminal login method that runs `copper-pilot auth login`.

### Other ACP clients

Launch `copper-pilot acp` on stdio. The server implements `initialize`,
`authenticate`, `session/new`, `session/load`, `session/set_mode`,
`session/set_config_option`, `session/prompt`, and `session/cancel`.
`session/load` takes an existing CopperPilot thread id (see
`copper-pilot threads list`) and replays its messages and tool calls.

`initialize` advertises one terminal auth method whose arguments are
`auth login`. Clients that run it as a standalone command get
`copper-pilot auth login`; clients that append it to the agent command, such as
T3 Code, get `copper-pilot acp auth login`, which the CLI treats the same way.

`initialize` advertises `promptCapabilities.image`. Each `image` prompt block
is written to `.copperpilot/attachments/` in the workspace and passed to the
hosted agent as a path-first attachment, the same way the TUI sends pasted
images. The query also lists the saved paths, because clients such as T3 Code
mention their own copy outside the workspace. A prompt with images but no text
asks the agent to analyze them.

Hosted text, reasoning, and status arrive as agent message and thought chunks;
hosted tool requests and results arrive as `tool_call` and `tool_call_update`.
A hosted error ends the prompt with a JSON-RPC error whose `details` carry the
public message.

Further session updates:

- `usage_update` after each hosted usage report. `used` is the prompt tokens
  of the latest turn and `size` is `0`, because the hosted service does not
  report the model's context window; the raw hosted counts are in `_meta`.
  T3 Code shows the count alone when the size is unknown.
- `plan` whenever the hosted agent updates its todo list (`todo_list` status
  events) or writes a `.copperpilot/plans/<name>.plan.md` artifact. Plan todos
  map `open`, `running`, and `done` onto `pending`, `in_progress`, and
  `completed`, the same statuses the Electron client's plan card uses.
- Intelligence survey workers are `think` tool calls titled `Survey: <task>`
  with `in_progress`, `completed`, or `failed` status and the worker's output
  or error as content, mirroring the Electron client's subagent card.
- `available_commands_update` once per session advertising `/effort` and
  `/model`. A prompt of `/effort balanced`, `/effort intelligence`, or
  `/model <id>` is answered locally with a `config_option_update` and a
  confirmation; `/effort` or `/model` alone reports the current value (and
  `/model` lists the ids).
- `model_fallback` status events (the selected model failed before any
  output and the server switched to a Gemini model) are relayed as an agent
  message carrying the server's copy, because ACP has no system-message type.

### Effort and model as ACP config options

> Model selection is currently off (`ENABLE_MODEL_SELECTOR = False`): only the
> `mode` and `effort` options and the `/effort` and `/usage` commands are
> offered, a `model` config change is rejected as an unknown option, and
> Intelligence runs on Gemini 4 Argon. The rest of this section, the `/model`
> command above, and the MCP `models` tool apply when it is on.

`session/new` and `session/load` return three select options: `mode`
(category `mode`), `effort` (category `thought_level`), and `model` (category
`model`). The `model` option lists the Intelligence catalog (fetched from the
server once per process, else bundled) and its current value is always the
model the next turn will run on: the Balanced model (Gemini 3.8 Flash) while
effort is Balanced, otherwise the selected Intelligence model (Gemini 4 Argon
until changed). The two options stay consistent on every
`session/set_config_option`:

| Change | Result |
|--------|--------|
| `model` = any Intelligence model while Balanced | Effort becomes Intelligence (paid plan); the response carries both values |
| `model` = the Balanced model while Balanced | Stays Balanced; nothing paid is requested |
| `effort` = `balanced` | The model option shows the Balanced model; the Intelligence choice is remembered |
| `effort` = `intelligence` | The remembered model (default Gemini 4 Argon) is shown again |
| unknown model id | `invalid_params` listing the known ids |

Free accounts get the server's 402 "Upgrade to a paid plan to use
Intelligence." as the turn error, exactly as with `/effort intelligence`. Both
values persist in the thread checkpoint, so `session/load` restores them.

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
until the server exits. Hosted clarifying questions use ACP form elicitation
(`elicitation/create`) when the client advertised
`clientCapabilities.elicitation.form`: options become an enum field and free
text a string field, and a declined or cancelled form counts as Skip. This
matters for hands-off clients: T3 Code in Full access auto-selects the first
option of a permission prompt, but always shows a form to the user. Clients
without form elicitation get option questions as permission choices plus Skip
and a rejection pointing to the TUI for free text. Either way, a question that
is still unanswered after 100 seconds is skipped with a note telling the model
to ask again in its reply, because the hosted service closes a turn whose tool
result takes longer than two minutes.

`effort` is `balanced` (default) or `intelligence`. Intelligence requires a
paid plan. It may run server-owned survey subagents; Plan turns survey before
the parent writes a plan. `model` is an Intelligence model id from the
`models` tool; passing one selects Intelligence on that model, and `resume`
without `effort` or `model` keeps what the thread last used. Results include
`model` (the model requested) and `answered_model` (the model that answered
after any server fallback, also reported through progress and the digest).
Progress reports compact worker transitions and the
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
