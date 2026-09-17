# Security policy

## Supported versions

The latest published version on [PyPI](https://pypi.org/project/copper-pilot-cli/)
is supported.

## Reporting a vulnerability

This CLI can run local filesystem and shell tools on the machine where it is
installed. Report vulnerabilities privately so they can be fixed before they
are public.

Use [GitHub private vulnerability reporting](https://github.com/CopperPilot/copper-pilot-cli/security/advisories/new).

Include the affected version (`copper-pilot --version`), the impact, and a
high-level description of how to trigger the issue.

Do **not** file a public GitHub issue for a security report.
Do **not** include API keys, `~/.copper-pilot/.state/auth.json`, or private
design files.

We will acknowledge the report and follow up with a fix or mitigation timeline.

## Local tool approval

Reads and searches run freely. Writes, edits, deletes, and shell commands
require Manual approval by default. `--auto-approve` still prompts for unknown
actions. `--yolo` must be selected explicitly and permits local side effects
without prompting.

See the [CLI reference](docs/cli.md#approval-modes) for the flags.
