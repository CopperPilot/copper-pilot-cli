# CLI reference

`copper-pilot` and `copper-pilot-cli` are the same program.

```console
copper-pilot --help
copper-pilot auth --help
copper-pilot threads --help
copper-pilot mcp --help
copper-pilot acp --help
copper-pilot validate --help
```

Python 3.11 or newer is required. Install with `pip install copper-pilot-cli`.

## Chat

```console
copper-pilot [workspace]
```

With no path, the current directory is the workspace. The first interactive
launch opens a browser for CopperPilot device login. Any existing directory is
valid.

| Flag | Meaning |
|------|---------|
| `-m`, `--message TEXT` | Send this message. In a TTY this seeds the composer; with `-n` it is required. |
| `-r`, `--resume [THREAD]` | Continue a thread. With no id, resume the most recent thread for this workspace. |
| `-n`, `--non-interactive` | Run without the Textual UI. Requires `--message`. |
| `--quiet` | Hide status and reasoning lines in headless streaming output. |
| `--no-stream` | Print the final assistant message after the run instead of streaming. |
| `--json` | Emit JSON events (or a final JSON document with `--no-stream`). |
| `--mode {agent,plan,ask}` | Hosted chat mode. Default: `agent`. |
| `--effort {balanced,intelligence}` | Hosted effort. Default: `balanced`; Intelligence requires a paid plan. |
| `--model MODEL` | Intelligence model id (see below). Implies `--effort intelligence`; rejected with `--effort balanced`. |
| `-y`, `--auto-approve` | Auto approval: routine writes and known-safe shell still run; unknown actions prompt. |
| `--yolo` | Permit local side effects without prompting. Must be set explicitly. |
| `--version` | Print the package version. |

Examples:

```console
copper-pilot ./my-board
copper-pilot --resume
copper-pilot -n -m "Review this board" --json
copper-pilot ./my-board --mode plan -m "Propose a power tree"
copper-pilot ./my-board --mode plan --effort intelligence -m "Survey and plan the power tree"
copper-pilot ./my-board --model gpt-6.1-sol -m "Review the USB-C front end"
```

Headless mode is also used when `--message` is set and stdout is not a TTY.
With `--json --no-stream`, the document includes `thread_id` so a later run can
pass `--resume`.

## Validate

```console
copper-pilot validate [workspace] [--profile dev|release] [--gate stage|full|strict]
                      [--until STAGE] [--only ID,...] [--skip ID,...] [--json] [--junit PATH]
copper-pilot validate list|graph [workspace]
copper-pilot validate explain CHECK_ID [workspace]
```

Runs a stage-gated graph of read-only reviewers: `kicad-cli` checks (ERC,
DRC, parity, rules, Gerbers) and hosted Agent-mode reviewers scoped to one
concern each, in the order a hardware engineer reviews (intake, architecture,
parts, schematic, netlist-to-layout, placement, routing, DFM/DFA, release).
It never modifies design files. Outputs land in `.copperpilot/validate/latest/`:
`report.json`, `REPORT.md`, and `next_actions.json` (only the earliest failing
stage, worst first). Uses the stored credential; never opens a browser.

| Code | Meaning |
|------|---------|
| 0 | PASS: every selected check is PASS or WARN |
| 1 | FAIL: at least one blocking FAIL or ERROR |
| 2 | Usage or config error: bad flags, invalid YAML, a dependency cycle |
| 3 | INCOMPLETE: no failures, but checks were SKIPPED because of `--gate`, `--until` or `--only` |
| 4 | Environment: not logged in, or a usage-limit block |

```console
copper-pilot validate ./my-board --json --gate stage
copper-pilot validate ./my-board --until S3 --emit-fix-prompt
copper-pilot validate ./my-board --profile release --gate full --junit validate.xml
```

Flags, config, the default graph and the fix loop: [validate.md](validate.md).

## MCP

```console
copper-pilot mcp
```

Serves a stdio MCP conductor for Claude Code, Cursor, Codex, and other MCP
hosts. Uses the stored device-login credential. Does not open a browser. Do not
write to stdout; the protocol owns it.

| Tool | Role |
|------|------|
| `status` | Login state and discovered schematic/PCB paths. No hosted call. |
| `models` | Intelligence model ids, labels, prices, and the Balanced model. No hosted turn. |
| `ask` | `--mode ask`. Auto approval. Optional `thread_id`, `effort`, `model`. |
| `plan` | `--mode plan`. Auto approval. Optional `effort`, `model`. |
| `run` | `--mode agent`. Optional `effort`, `model`; `approval` is `auto` (default) or `yolo`. |
| `resume` | Continue `thread_id`. Optional `mode`, `effort`, `model`, and the same approval as `run`. |
| `threads_list` | List threads for this workspace (`limit`, `all_workspaces`). |
| `validate` | Read-only stage-gated review; returns `verdict`, `exit_code`, `report_path`, `next_actions`. |

Workspace is the tool argument, else `CLAUDE_PROJECT_DIR`, else the process
current directory. Manual approval is TUI-only. Passing `model` selects
Intelligence on that model; the result carries `model` (requested) and
`answered_model` (after any server fallback). See [harness.md](harness.md).

## ACP

```console
pip install "copper-pilot-cli[acp]"
copper-pilot acp
```

Serves the Agent Client Protocol over stdio for editors such as Zed. The
`acp` extra is optional; without it the command prints an install hint and
exits with status 2. `copper-pilot acp auth login` is accepted as an alias for
`copper-pilot auth login`, because some ACP clients (T3 Code) run the
advertised login method by appending its arguments to the agent command. One ACP session is one CopperPilot thread, and the
session `cwd` is the workspace. Uses the stored device-login credential and
advertises a terminal auth method that runs `copper-pilot auth login`.

| ACP feature | CopperPilot behavior |
|-------------|----------------------|
| Session modes | `ask`, `plan`, `agent` (default `agent`) |
| Config options | `mode`, `effort` (`balanced` default, `intelligence` paid), and `model` (category `model`; the Intelligence catalog) |
| `session/load` | Resumes an existing thread id and replays its history |
| `session/request_permission` | Auto approval; non-routine writes and shell prompt the editor |
| `elicitation/create` | Clarifying questions (options or free text) when the client advertises form elicitation |
| `session/cancel` | Cancels the hosted turn and local tools |
| `usage_update` | Prompt tokens of the latest hosted turn; window size is unknown (0) |
| `plan` | Hosted todo list events and `.copperpilot/plans/*.plan.md` todos |
| `available_commands_update` | `/effort balanced\|intelligence` and `/model [id]`, answered locally |

"Allow all for this session" switches that session to YOLO until the server
exits; it is never saved. Clarifying questions use ACP form elicitation when
the client supports it (T3 Code does): options become an enum, free text a
string field. Without form elicitation, option questions are shown as
permission choices plus Skip and free-text questions are declined with a
message to continue in the TUI. A question nobody answers within 100 seconds
is skipped with a note asking the model to repeat it in its reply, because the
hosted service drops a turn whose tool result takes longer than two minutes.
Intelligence survey workers appear as `think` tool calls named
`Survey: <task>` whose status and output track the worker.
See [harness.md](harness.md).

## Effort, Intelligence, and models

Balanced is the default. Intelligence is available on paid plans and lets the
hosted parent run one to four survey subagents. Intelligence Plan turns survey
before creating or revising a plan. Workers are server-owned, survey-only, and
appear in the TUI through the existing Deep Agents task rows; they do not open
nested chats or execute through the local tool broker.

Balanced always runs on the server's Balanced model (Google Gemini 3.8 Flash);
there is no model choice on a free account. Intelligence runs on one model
from the catalog, Claude Opus 5.5 by default:

| Id | Model |
|----|-------|
| `claude-opus-5-5` | Claude Opus 5.5 (Intelligence default) |
| `claude-sonnet-5-5` | Claude Sonnet 5.5 |
| `gpt-6-astra` | OpenAI GPT-6 Astra |
| `gpt-6.1-sol` | OpenAI GPT-6.1 Sol |
| `gemini-4-argon` | Google Gemini 4 Argon |
| `gemini-3.8-flash` | Google Gemini 3.8 Flash (Balanced model) |

The list is fetched from `GET /api/copperpilot/chat/models` once per process
and falls back to this bundled copy when the deployment does not publish it.
Choosing a model selects Intelligence, the same rule as the desktop picker;
`--effort balanced` together with `--model` is an error. The model is sent
only on Intelligence turns and the server resolves unknown ids to the default.

The interactive `/effort` and `/model [id]` commands change effort and model
for the current thread; `/context` shows the model the next turn will use.
Resuming a thread restores both; a new thread starts Balanced. When the
selected model is unavailable the server falls back to a Gemini model and
sends a `model_fallback` status event; the TUI posts its message in the
transcript, headless mode prints it in yellow, and `--json` retains the raw
`status_event`. In headless text mode, worker state changes print as compact
lines.

Interactive clarification can offer suggested answers, custom text, or Skip
and waits without a client timeout. Headless and MCP calls cannot present that
UI, so an `ask_user_question` tool request is rejected with a clear error
instead of guessing or auto-skipping.

## Auth

```console
copper-pilot auth [login|logout|status]
```

| Action | Meaning |
|--------|---------|
| `status` | Print the authenticated API origin, or exit `1` if logged out. Default. |
| `login` | Open a browser for device login and store credentials. |
| `logout` | Delete the stored credential. |

Credentials are written to `~/.copper-pilot/.state/auth.json`. On POSIX the
parent directory is mode `0700` and the file is mode `0600`. Override the home
directory with `COPPER_PILOT_CLI_HOME`.

Non-interactive runs fail with a login error if no valid credential is stored.
Run `copper-pilot auth login` from a TTY first.

## Threads

```console
copper-pilot threads list [--all] [--limit N]
copper-pilot threads delete THREAD_ID [-y]
```

`list` shows threads for the current workspace unless `--all` is set.
`delete` prompts unless `-y` / `--yes` is passed; non-interactive delete
without `--yes` exits `2`.

```console
copper-pilot threads list
copper-pilot threads list --all --limit 50
copper-pilot threads delete <thread_id> --yes
```

## Approval modes

The hosted agent may request local filesystem and shell tools. Reads and
searches run freely. Writes, edits, deletes, and shell commands are gated:

| Mode | How to select | Behavior |
|------|----------------|----------|
| Manual | Default | Prompt before side effects. |
| Auto | `-y` / `--auto-approve`, or a saved Auto preference | Permit deterministic routine actions; still prompt for unknown actions. |
| YOLO | `--yolo` | Permit local side effects without prompting. |

`--yolo` wins over `--auto-approve`. Saved preferences never enable YOLO.

## Turn durability

Every front-end (TUI, headless, MCP, ACP) runs a hosted turn through the same
checkpointed graph node, which mirrors the desktop client's chat manager:

- A stream that drops before the final frame (socket close, timeout, 5xx, 429)
  or a reconnect that the server refuses at the websocket handshake (HTTP 503
  while a backend restarts, 429, other 5xx) is retried up to four times with
  5 s, 10 s, 20 s, and 30 s backoff. The retry
  re-prompts the hosted agent with the partial transcript and
  `Resume where we left off: <original message>` so it continues rather than
  restarting. Deterministic failures (4xx other than 429, invalid or oversized
  requests, plan refusals, auth) fail immediately.
- Retry progress is visible: the TUI status bar and a notification, a yellow
  line in headless text mode, MCP progress, and ACP agent thoughts
  (`Retrying (1/3) in 5s: …`). `--json` emits `copper.retry` events.
- The websocket close code and reason are carried into the error message
  (`close code 1011 received: …`) and the log file.
- Whatever a failed turn produced before the error (assistant text, tool calls
  and results) is still checkpointed with a hidden
  `[Turn interrupted before completion: …]` note, so a later "continue" has the
  prior work in context.
- There is no idle timeout between frames once a turn is streaming; websocket
  keepalive still detects dead peers, and long hosted reasoning or subagent
  runs no longer trip a client-side limit. The first frame must still arrive
  within 60 s.
- Shell commands run in the foreground for the requested timeout (default
  25 s) and then move to the background unless the request sets
  `kill_on_timeout` or `background: false`. After the final frame the client
  resumes the agent with their output. Commands still running are awaited
  first. Commands that already exited while the model was still working are
  delivered too, so a search that finishes mid-turn is not dropped. Output is
  capped at 5 MB and the whole process tree is killed on cancel.

## Usage limits

The hosted service owns weekly and window usage. The CLI does not show raw
token balances, sell credits, or complete checkout.

Inside the interactive app, `/tokens` prints the current window as a
percentage, the next refresh time, and any additional-usage state.
`/tokens json` prints the member-safe API payload instead.

A blocked turn stays readable in `--json` output. An error event uses `data`
as an object:

```json
{
  "type": "error",
  "data": {
    "message": "Usage limit reached.",
    "code": "http_402",
    "status": 402,
    "block_reason": "weekly",
    "can_purchase_overage": true,
    "retryable": false
  }
}
```

`block_reason` is `window`, `weekly`, `cost_cap`, `busy`, or `billing` when
the server sends one. A `busy` block and an ordinary rate limit use status
`429` with `retryable: true`. `can_purchase_overage` only says whether the
billing page can offer additional usage. The Textual UI prints `message`.

## Configuration

| Setting | Location |
|---------|----------|
| Production API | `https://copperpilot.ai` |
| Development API | `COPPER_PILOT_API_URL` |
| CLI home | `COPPER_PILOT_CLI_HOME` or `~/.copper-pilot` |
| User state | `~/.copper-pilot/.state` |
| Project artifacts | `.copperpilot/` in the workspace |

## LangChain

`CopperPilotAgent` is a LangChain `Runnable`, not a chat model. It represents
one hosted agent run, including local tool requests. See
[`examples/001_esp32`](../examples/001_esp32) and the README.
