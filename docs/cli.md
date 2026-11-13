# CLI reference

`copper-pilot` and `copper-pilot-cli` are the same program.

```console
copper-pilot --help
copper-pilot auth --help
copper-pilot threads --help
copper-pilot mcp --help
copper-pilot acp --help
copper-pilot validate --help
copper-pilot update --help
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
| `-r`, `--resume [THREAD]` | Continue a thread. With no id, resume the most recent thread for this workspace. With `-n` and no `--message`, resume that thread's unfinished turn (see [Turn durability](#turn-durability)). |
| `-n`, `--non-interactive` | Run without the Textual UI. Requires `--message`, or `-r` to resume an unfinished turn. |
| `--quiet` | Hide status and reasoning lines in headless streaming output. |
| `--no-stream` | Print the final assistant message after the run instead of streaming. |
| `--json` | Emit JSON events (or a final JSON document with `--no-stream`). |
| `--mode {agent,plan,ask}` | Hosted chat mode. Default: `agent`. |
| `--effort {balanced,intelligence}` | Hosted effort. Default: `balanced`; Intelligence requires a paid plan. |
| `--model MODEL` | Intelligence model id (see below). Implies `--effort intelligence`; rejected with `--effort balanced`. |
| `-y`, `--auto-approve` | Auto approval: routine writes and known-safe shell still run; unknown actions prompt. |
| `--yolo` | Permit local side effects without prompting, except commands that reach outside the workspace. Must be set explicitly. |
| `--version` | Print the package version. |

Examples:

```console
copper-pilot ./my-board
copper-pilot --resume
copper-pilot -n -m "Review this board" --json
copper-pilot -n -r            # finish the most recent thread's unfinished turn
copper-pilot ./my-board --mode plan -m "Propose a power tree"
copper-pilot ./my-board --mode plan --effort intelligence -m "Survey and plan the power tree"
copper-pilot ./my-board --model gpt-6.1-sol -m "Review the USB-C front end"
```

Headless mode is also used when `--message` is set and stdout is not a TTY.
With `--json --no-stream`, the document includes `thread_id` so a later run can
pass `--resume`.

Each streamed `--json` line is one event:

```json
{"type": "text", "data": "…", "title": null, "turn_id": "…", "root_turn_id": "…",
 "resume_attempt": 0, "ts": 1793500000.123, "sequence": 12}
```

`type`, `data` and `title` are unchanged from earlier releases. `turn_id` is the
hosted attempt, `root_turn_id` the logical turn (the id of its first attempt),
`resume_attempt` the client's resume count (0 for the original attempt) and
`ts` epoch seconds. `sequence` and `server_resume_attempt` (the hosted
service's own retry counter) appear when the hosted frame carried them.
`resume` events report each resume:
`{"attempt", "reason", "delay_s", "will_retry", "root_turn_id", "previous_turn_id",
"error_type", "error_message", "manual_resume"}`.
`capacity_wait` events report the hosted model waiting for LLM provider
capacity (a healthy turn, not an error). Their fields also appear at the top
level of the line:

```json
{"type": "capacity_wait", "phase": "retrying", "model": "gemini-3.8-flash",
 "retry_in_s": 20.0, "waited_s": 45.0, "budget_s": 600.0, "attempt": 2,
 "message": "CopperPilot is under high demand. …", "data": {…},
 "title": "High demand — waiting to continue (next try in 20 s)",
 "turn_id": "…", "root_turn_id": "…", "resume_attempt": 0, "ts": 1793500000.123}
```

`phase` is `queued`, `retrying` or `resumed` (the wait is over).

### Side questions (`/btw`)

```text
/btw what is it doing right now?
/btw
```

`/btw <question>` asks about the session without interrupting the running
turn. The answer appears in a panel above the prompt. It is drawn from the
thread so far plus a live snapshot of the running turn: the request, the
current status, this turn's tool calls, survey workers, and the reply so far.

- The side question runs on its own hosted Ask stream with a new conversation
  id. It always uses Balanced effort, and every tool request is refused, so it
  cannot read files, run commands, or change anything. If the model asks for
  a tool anyway, the answer ends with a note that nothing was executed.
- The question and answer are never saved to the thread, never shown in the
  transcript, and never sent to the main turn. The newest five answered side
  questions are replayed to the next one.
- Each side question uses hosted usage like a short Ask turn. A usage limit or
  error is shown in the panel, not in the transcript. At most three can be in
  flight at once.
- `/btw` with no question reopens the panel on the latest answer. If you have
  not asked one yet, it shows the live snapshot without a hosted call.
- History is in memory only, up to 20 entries. `x`, `/clear`, `/cwd`, and
  resuming another thread clear it, and it is gone when the CLI exits.
- If the panel is closed when an answer arrives, a notification says it is
  ready. The panel never takes focus from an approval prompt.
- The panel shows at most the first 60 lines (8,000 characters) of an answer,
  so moving between long answers stays fast; `c` copies the full answer.

| Key (panel focused) | Action |
|---------------------|--------|
| `Esc`, `Enter`, `Space` | Close the panel and return to the prompt |
| `←` / `→`, `Shift+←` / `Shift+→`, `[` / `]` | Older / newer side answer |
| `↑` / `↓`, `PageUp` / `PageDown`, `Home` / `End` | Scroll the answer |
| `c` | Copy the raw answer |
| `x` | Clear side-question history |
| Any other character | Return to the prompt and type it |

With the prompt focused, the first `Esc` closes an open panel. Only an `Esc`
with no panel open stops the running turn. `/btw` is available in the
interactive app only; headless, MCP, and ACP runs do not offer it.

## Update

```console
copper-pilot update --check
copper-pilot update
```

The CLI performs a throttled, best-effort update check once a day. It writes a
single message to stderr only when a newer release declares a feature note, so
JSON and regular stdout output stay unchanged. `update --check` only reports
the current and latest versions. `update` asks for confirmation, then delegates
to pipx or the active Python environment's pip; use `--yes` in non-interactive
automation.

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
| `available_commands_update` | `/effort balanced\|intelligence`, `/model [id]`, and `/usage`, answered locally; `/resume` resumes the unfinished turn |
| Stop reason `max_turn_requests` | Automatic resumes ran out; the message says how to continue, and the next "continue" prompt resumes the turn |
| Prompt error `-32003` | A usage limit refused the turn; see below |

"Allow all for this session" switches that session to YOLO until the server
exits; it is never saved. Commands that reach outside the workspace still ask
each time (see [Outside the workspace](#outside-the-workspace)). Clarifying questions use ACP form elicitation when
the client supports it (T3 Code does): options become an enum, free text a
string field. Without form elicitation, option questions are shown as
permission choices plus Skip and free-text questions are declined with a
message to continue in the TUI. A question nobody answers within 100 seconds
is skipped with a note asking the model to repeat it in its reply, because the
hosted service drops a turn whose tool result takes longer than two minutes.
Intelligence survey workers appear as `think` tool calls named
`Survey: <task>` whose status and output track the worker.
See [harness.md](harness.md).

A turn refused by a usage limit fails with JSON-RPC error `-32003` (the code
xAI's Grok ACP agent uses for the same condition). The message names the
spent limit and when it lifts in local time, for example `CopperPilot usage
limit reached: the current usage window is used up. It resets at 3:20 AM (in
2h 23m).` `data` repeats it for clients that render a usage-limit notice:

```json
{
  "details": "CopperPilot usage limit reached: ...",
  "class": "usage_limit",
  "blockReason": "window",
  "resetAt": "2026-10-19T21:50:00Z",
  "canPurchaseOverage": false,
  "retryable": false
}
```

When the hosted error carries no reset time (a refused handshake), the server
reads it from the token-limits endpoint. A team cost cap, billing, or busy
block has no reset time and keeps the hosted wording. `/usage` prints the
current window, weekly balance, and reset times without a hosted turn.

## Effort, Intelligence, and models

Balanced is the default. Intelligence is available on paid plans and lets the
hosted parent run one to four survey subagents. Intelligence Plan turns survey
before creating or revising a plan. Workers are server-owned, survey-only, and
appear in the TUI through the existing Deep Agents task rows; they do not open
nested chats or execute through the local tool broker.

> **Model selection is currently off.** `ENABLE_MODEL_SELECTOR` in
> `src/copper_pilot_cli/copper_models.py` is `False`: every Intelligence turn
> sends the model `auto` (the server picks; Gemini 4 Carbon today), no surface names a model, and `--model`, `/model`,
> the MCP `models` tool and `model` argument, and the ACP `model` config option
> are not offered. A fallback notice reads "Switched this chat to a backup
> model." The rest of this section describes the behaviour when it is `True`.

Balanced always runs on the server's Balanced model (Google Gemini 4 Carbon);
there is no model choice on a free account. Intelligence runs on `auto` by
default, which lets the server pick (Gemini 4 Carbon today), or on one model
from the catalog (the server's catalog order and default win when it is
reachable):

| Id | Model |
|----|-------|
| `auto` | Auto: the server's Intelligence default (Intelligence default) |
| `gemini-4-carbon` | Google Gemini 4 Carbon (Balanced model) |
| `gemini-4-argon` | Google Gemini 4 Argon |
| `gemini-3.8-flash` | Google Gemini 3.8 Flash |
| `claude-opus-5-5` | Claude Opus 5.5 |
| `claude-sonnet-5-5` | Claude Sonnet 5.5 |
| `gpt-6-astra` | OpenAI GPT-6 Astra |
| `gpt-6.1-sol` | OpenAI GPT-6.1 Sol |

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
copper-pilot threads compact-db [--dry-run] [-y]
```

`list` shows threads for the current workspace unless `--all` is set.
`delete` prompts unless `-y` / `--yes` is passed; non-interactive delete
without `--yes` exits `2`.

Each chat keeps only its newest checkpoints (plus its first), and each turn
prunes older ones. `compact-db` shrinks a database written by an older CLI:
it prunes every chat at once, stores the replay log in its compact form, then
runs `VACUUM`. It refuses to run while a chat is open (TUI, ACP or MCP; on
POSIX), and refuses to `VACUUM` unless the free space next to the database is
at least the database's size and twice its live data. `--dry-run` reports what
would be removed and changes nothing. It prompts unless `-y` / `--yes` is
passed.

```console
copper-pilot threads list
copper-pilot threads list --all --limit 50
copper-pilot threads delete <thread_id> --yes
copper-pilot threads compact-db --dry-run
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

### Outside the workspace

Some shell commands reach beyond the project folder. They count as dangerous
shell commands: they need explicit approval unless you run in YOLO, or in Auto
with "always allow dangerous shell commands" turned on (the same switch as the
desktop app's "Always Allow Dangerous Shell Commands"):

- recursive searches rooted at `/`, a system top-level directory (`/usr`,
  `/etc`, `/home`, ...), or your home directory: `find /`, `grep -r ... ~`,
  `rg`, `fd`, `du`, `tree`, `ls -R`, and `locate`/`mdfind`
- paths under your home directory outside the workspace, such as
  `cat ~/other-project/x.kicad_sch` or `cp ~/Downloads/lib.kicad_sym .`.
  KiCad's library and settings folders, `~/.copper-pilot` and `TMPDIR` are
  allowed.
- `docker`, `podman`, `kubectl`, `sudo`, `ssh`, `scp`, and similar
- killing processes by name: `killall`, `pkill`, `kill $(pgrep ...)`

The CLI follows `&&`/`;`/`|` chains, `cd`, `$(...)` and `sh -c`. The TUI
prompt shows the reason and offers only yes or no. ACP clients get a
permission request titled "Outside the workspace: ..." with the reason and no
"Allow all for this session". Each approval covers that one call only. With
nobody to approve (headless, MCP) they are refused, unless dangerous shell
commands are auto-allowed as above.

With nobody to ask (headless `-n`, MCP), the command is refused and the agent
gets the tool error `blocked: outside the workspace (<reason>); ask the user
first`. This is a heuristic, not a sandbox: in-project commands with relative
paths (`kicad-cli sch erc board.kicad_sch`, `grep -rn VCC .`) are left alone.

## Turn durability

Every front-end (TUI, headless, MCP, ACP) runs a hosted turn through the same
checkpointed graph node. It implements the client side of the turn resume
protocol (v1). The hosted service keeps no conversation content and cancels a
turn when its socket closes, so the resume state lives only on this device.

- **Journal.** While a turn runs, the CLI records the original request, the
  assistant text streamed so far, and every tool call with its outcome
  (`completed`, `failed`, or `interrupted` when the outcome was lost; results
  clipped to 4 KB, at most 200 calls). It is rewritten to
  `~/.copper-pilot/.state/turns/<thread>.json` as the turn runs and kept in the
  thread's checkpoint when a turn ends unfinished. It is deleted when the turn
  completes or the thread is deleted.
- **Automatic resume.** When a stream drops (socket close, keepalive or idle
  timeout, 5xx, 429, a failed final, or a reconnect the server refuses with
  HTTP 503), the CLI resends the *original* request with a new `turn_id` and
  `request.resume` built from the journal (capability `turn_resume_v1`). The
  server continues from there; completed tools are not run again, and a
  repeated `tool_call_id` is answered from the local cache. Up to 8 resumes or
  10 minutes of recovery per logical turn, with 1, 2, 5, 10, 20, 30, 60, 60 s
  backoff (±20% jitter). Before each resume the CLI waits for
  `GET /api/heartbeat` to answer 200; that wait does not use up an attempt.
- **Server restarts and socket rotation.** A `final` with `resumable: true`
  (`server_restarting`, `socket_rotation`) resumes after 0–2 s and does not
  count against the error budget (the hard cap is 20 resumes per turn).
- **High demand (LLM rate limits).** While the hosted model waits for provider
  capacity the server sends `capacity_wait` events; the CLI shows "High
  demand — waiting to continue (next try in N s)" and the idle bound grows by
  the server's remaining wait budget, so the wait is never mistaken for a
  stall. When the server's wait budget runs out the turn ends with a
  resumable `provider_busy` final: the CLI waits its `retry_after_s` (plus
  up to 20% jitter, at least 15 s) and resumes with reason `provider_busy`. These resumes have
  their own budget, 6 in a row or 30 minutes per logical turn, and do not
  spend the error budget; any model output starts a new count. When they run
  out the turn stays resumable and says CopperPilot is under high demand
  rather than reporting an error.
- **Fail fast.** Authentication, usage limits, invalid or oversized requests,
  content-policy blocks and user cancellation are not retried.
- **Liveness.** The socket pings every 15 s and closes when no pong arrives
  within 20 s. A turn that receives no frame for 330 s (longer than the
  server's 300 s quiet timeout; `COPPER_PILOT_IDLE_TIMEOUT_SECONDS`, `0` waits
  forever) is closed with code 4000 and resumed. The first frame must arrive
  within 60 s. Frames keep being read while a local tool runs; a tool that
  finishes after its socket closed still lands in the journal.
- **Unfinished turns.** When automatic resumes run out, or the CLI is killed,
  quit or interrupted with Ctrl-C mid-turn, the turn stays resumable:
  - TUI: opening the thread shows the unfinished turn; type `/resume` (or a
    message starting with "continue", such as "Continue where you left off").
    `/resume` opens the thread picker when there is nothing to resume.
  - Headless: `copper-pilot -n -r [THREAD]` with no `--message` resumes it,
    and exits 2 when there is nothing to resume. A turn that stops unfinished
    prints the exact command.
  - ACP: the prompt ends with stop reason `max_turn_requests` and a message;
    the next "continue …" prompt or `/resume` resumes it.
  - Any front-end: a short message starting with `continue` or `resume`
    resumes the unfinished turn instead of starting a new one. Any other
    message starts a new turn; the unfinished turn's partial work stays in the
    transcript.
- **Visibility.** The TUI status bar and notifications, yellow lines in
  headless text mode, MCP progress and ACP agent thoughts show resumes
  (`Retrying (1/8) in 1s: …`) and high-demand waits (the TUI counts down).
  `--json` emits `retry`, `resume` and `capacity_wait` events. The log file
  (`~/.copper-pilot/.state/logs/copper-pilot.log`) records each drop (close
  code, reason, attempt, elapsed time), each resume start and success, each
  capacity wait and provider-busy pause, and each finished turn (wall time,
  attempts, capacity waits, tool counts), without content.
- **Rollback.** `COPPER_PILOT_TURN_RESUME=0` turns off structured resume for a
  hosted service without `turn_resume_v1`: retries then re-prompt with the
  partial transcript and `Resume where we left off: <original message>`.
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
    "retryable": false,
    "resets_at": "2026-10-21T02:56:47Z"
  }
}
```

`resets_at` is present for a `window` or `weekly` block when the server sends
the reset time. `block_reason` is `window`, `weekly`, `cost_cap`, `busy`, or `billing` when
the server sends one. A `busy` block and an ordinary rate limit use status
`429` with `retryable: true`. `can_purchase_overage` only says whether the
billing page can offer additional usage. The Textual UI prints `message`,
with a window or weekly reset time in local time (`It resets at 3:20 AM (in 2h 23m).`).

## Configuration

| Setting | Location |
|---------|----------|
| Production API | `https://copperpilot.ai` |
| Development API | `COPPER_PILOT_API_URL` |
| CLI home | `COPPER_PILOT_CLI_HOME` or `~/.copper-pilot` |
| Idle bound between frames | `COPPER_PILOT_IDLE_TIMEOUT_SECONDS` (default 330; `0` waits forever) |
| Structured turn resume | `COPPER_PILOT_TURN_RESUME` (default on; `0` uses the stitched-transcript fallback) |
| User state | `~/.copper-pilot/.state` |
| Project artifacts | `.copperpilot/` in the workspace |

## LangChain

`CopperPilotAgent` is a LangChain `Runnable`, not a chat model. It represents
one hosted agent run, including local tool requests. See
[`examples/001_esp32`](../examples/001_esp32) and the README.
