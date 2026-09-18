# CLI reference

`copper-pilot` and `copper-pilot-cli` are the same program.

```console
copper-pilot --help
copper-pilot auth --help
copper-pilot threads --help
copper-pilot mcp --help
```

Python 3.12 or newer is required. Install with `pip install copper-pilot-cli`.

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
| `-y`, `--auto-approve` | Auto approval: routine writes and known-safe shell still run; unknown actions prompt. |
| `--yolo` | Permit local side effects without prompting. Must be set explicitly. |
| `--version` | Print the package version. |

Examples:

```console
copper-pilot ./my-board
copper-pilot --resume
copper-pilot -n -m "Review this board" --json
copper-pilot ./my-board --mode plan -m "Propose a power tree"
```

Headless mode is also used when `--message` is set and stdout is not a TTY.
With `--json --no-stream`, the document includes `thread_id` so a later run can
pass `--resume`.

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
| `ask` | `--mode ask`. Auto approval. Optional `thread_id`. |
| `plan` | `--mode plan`. Auto approval. |
| `run` | `--mode agent`. `approval` is `auto` (default) or `yolo`. |
| `resume` | Continue `thread_id`. Same approval as `run`. |
| `threads_list` | List threads for this workspace (`limit`, `all_workspaces`). |

Workspace is the tool argument, else `CLAUDE_PROJECT_DIR`, else the process
current directory. Manual approval is TUI-only. See [harness.md](harness.md).

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
