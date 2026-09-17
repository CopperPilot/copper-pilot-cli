# CopperPilot CLI

CopperPilot in your terminal: a thin, open-source client for CopperPilot electronics design agent, the best AI for PCB Design.

## Install and run

```console
pip install copper-pilot-cli
copper-pilot
```

The first interactive launch opens a browser for CopperPilot device login.
With no path argument, the current directory is the workspace and a new chat
starts immediately. Any existing directory is valid.

```console
copper-pilot ./my-board
copper-pilot --resume
copper-pilot threads list
copper-pilot -n -m "Review this board" --json
```

Both `copper-pilot` and `copper-pilot-cli` run the same program.

## Testing

```console
pytest -m "not live"
pytest -m live
```

Live tests exercise the installed CLI, Textual composer, hosted Ask stream, and
Agent/local-tool round trip. They load the normal private credential store and
skip automatically when no CopperPilot credential is available.

## Configuration

- Production API: `https://copperpilot.ai`
- Development override: `COPPER_PILOT_API_URL`
- User state: `~/.copper-pilot/.state`
- Project artifacts: `.copperpilot/`

Credentials are stored in a private, atomically replaced `auth.json`; on POSIX
the parent directory is mode `0700` and the file is mode `0600`.

## LangChain

```python
from copper_pilot_cli.langchain import CopperPilotAgent

agent = CopperPilotAgent(workspace=".")
result = agent.invoke({"messages": [{"role": "user", "content": "Review this design"}]})
```

`CopperPilotAgent` is a `Runnable`, not a chat model. It represents a complete
hosted agent run, including local tool requests. Use `as_langgraph()` for graph
composition or `as_compiled_subagent()` for explicit delegation from an outer
Deep Agent. The compatible `deepagents` runtime is installed and pinned by this
package.

## Security boundary

The hosted agent may request local filesystem and shell tools. Reads and
searches run freely. Writes, edits, deletes, and shell commands are gated by
Manual approval by default. Auto permits only deterministic routine actions;
unknown actions still ask. YOLO must be selected explicitly.

Filesystem, search, edit, and shell execution use Deep Agents' native
`FilesystemMiddleware` tools and `LocalShellBackend`; Copper's adapter only
normalizes hosted tool names, approval decisions, and result envelopes.

Computer use, model/provider selection, local model execution, LangSmith
tracing, content telemetry, MCP/plugin controls, and server implementation code
are not part of this package.

See [UPSTREAM.md](UPSTREAM.md) and [NOTICE](NOTICE) for the retained Textual
client attribution.
