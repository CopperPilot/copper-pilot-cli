### Summary

What changed and why.

### Test plan

- [ ] `make lint`
- [ ] `make test`
- [ ] Live test if this touches auth, the TUI, or the hosted stream (`pytest -m live`)

### Checklist

- [ ] Tests added or updated
- [ ] Docs updated (README, `docs/cli.md`, or `CONTRIBUTING.md`) when behavior changes
- [ ] No new hosted-server, model-picker, computer-use, MCP host/client, sandbox, or LangSmith surface. A thin `copper-pilot mcp` server that delegates to the hosted agent is allowed.
