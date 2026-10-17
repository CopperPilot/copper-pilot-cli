---
name: copper-pilot-review
description: Review a schematic or PCB with CopperPilot via the CLI. Use when the user asks to review a board, check ERC/DRC, inspect a net or reference, or get electronics feedback from CopperPilot.
license: MIT
compatibility: Requires copper-pilot-cli on PATH and a CopperPilot login (`copper-pilot auth login`).
---

# CopperPilot review (CLI)

You are the conductor. CopperPilot is the electronics agent. Do not invent ERC, DRC, stackup, or footprint judgments from KiCad source text when CopperPilot is available.

## Setup

1. The workspace is the project directory (the folder that contains `.kicad_pro` / `.kicad_sch` / `.kicad_pcb`, or the current working directory).
2. If a command fails with a login error, tell the user to run `copper-pilot auth login` in a terminal and stop.

## How to call CopperPilot

Read-only review (default):

```console
copper-pilot -n -m "$ARGUMENTS" --json --no-stream --mode ask
```

If `$ARGUMENTS` is empty, use the user's latest request as the message.

Propose a plan without applying it:

```console
copper-pilot -n -m "$ARGUMENTS" --json --no-stream --mode plan
```

Apply a change the user asked for (Auto: routine writes only):

```console
copper-pilot -n -m "$ARGUMENTS" --json --no-stream --mode agent -y
```

The JSON document includes `thread_id`. To continue the same chat:

```console
copper-pilot -n -m "$ARGUMENTS" --json --no-stream --resume THREAD_ID
```

## Validate

Before sign-off, or when the user asks whether the board is ready, run the
read-only stage-gated review graph instead of a free-form review:

```console
copper-pilot validate --json --gate stage
```

Then read `.copperpilot/validate/latest/next_actions.json`. It lists only the
earliest failing stage's issues, worst first, each with a finding ID, evidence,
rule and suggested action. Fix them with CopperPilot, citing the IDs:

```console
copper-pilot -n --mode agent -y -m "$(copper-pilot validate --emit-fix-prompt | sed -n '/^Fix only/,$p')"
```

Re-run `copper-pilot validate --until <stage>` (the `rerun` field) until that
stage passes, then run the full graph again. Exit codes: 0 pass, 1 fail, 2
config error, 3 incomplete, 4 not logged in or usage limit. `validate` never
edits design files; do not loosen design rules or exclude violations to make a
check pass.

## Rules

- Do not Edit or Write KiCad files yourself while a CopperPilot run is in flight.
- Prefer `--mode ask` unless the user asked for a schematic or PCB change.
- Do not pass `--yolo` unless the user explicitly asked to skip local approval.
- Summarize CopperPilot's `messages[0].content` for the user. Quote `thread_id` so they can resume.
- You may still read firmware, docs, and git yourself. Leave schematic and PCB side effects to CopperPilot.
