# Upstream provenance

The terminal presentation layer began as a source fork of:

- Project: Deep Agents Code
- Repository: <https://github.com/langchain-ai/deepagents>
- Package: `deepagents-code==0.1.69`
- Revision: `1d3232c0852c47af09119edea10eeec887e4f0da`
- License: MIT

CopperPilot replaces the upstream model-provider, local-agent server, and
RemoteGraph runtime with a client for the hosted CopperPilot agent. Upstream
copyright and license notices must remain in distributions.

When synchronizing upstream:

1. Fetch the pinned/new revision separately.
2. Review security and terminal compatibility changes.
3. Port only presentation, session, input, approval, and update changes that
   apply to a hosted-agent client.
4. Do not reintroduce model providers, LangSmith tracing, MCP/plugin controls,
   sandboxes, local agent construction, or computer use.

The curated presentation port includes the tool lifecycle row, bounded output
preview behavior, deterministic Auto policy, operation-specific approval
previews, and approval keyboard flow derived from the pinned
`deepagents_code.tui.widgets.messages`, `tool_widgets`, `tool_renderers`,
`approval`, and `deepagents_code.auto_mode` modules. Execution is not vendored:
it is delegated to the pinned `deepagents` package's `FilesystemMiddleware` and
`LocalShellBackend`.

Only dependency-light leaf code is vendored under
`copper_pilot_cli._upstream.dcode_0_1_69`. Its `PROVENANCE.json` records the
upstream path and hash. CopperPilot-owned presentation adapters reproduce dcode's
loading, reasoning-collapse, successful-tool grouping, and inline-approval
contracts without importing dcode's provider/runtime application stack.

Selection auto-copy and completion-key routing are adapted from
`deepagents_code.clipboard`, `deepagents_code.app`, and the upstream
`ChatInput`/autocomplete widgets. Only the detached-widget Textual guard is
retained from upstream's private patch set. Consecutive successful reasoning
and tool activity uses CopperPilot's desktop `Worked for N` lifecycle; this is an
intentional presentation difference from dcode's separate reasoning and tool
summaries.
