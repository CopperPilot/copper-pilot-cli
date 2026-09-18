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

## Rules

- Do not Edit or Write KiCad files yourself while a CopperPilot run is in flight.
- Prefer `--mode ask` unless the user asked for a schematic or PCB change.
- Do not pass `--yolo` unless the user explicitly asked to skip local approval.
- Summarize CopperPilot's `messages[0].content` for the user. Quote `thread_id` so they can resume.
- You may still read firmware, docs, and git yourself. Leave schematic and PCB side effects to CopperPilot.
