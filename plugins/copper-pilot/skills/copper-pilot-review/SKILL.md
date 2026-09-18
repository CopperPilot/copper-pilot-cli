---
name: copper-pilot-review
description: Review a schematic or PCB with CopperPilot. Use when the user asks to review a board, check ERC/DRC, inspect a net or reference, or get electronics feedback.
allowed-tools: Read mcp__plugin_copper-pilot_copper-pilot__status mcp__plugin_copper-pilot_copper-pilot__ask mcp__plugin_copper-pilot_copper-pilot__plan mcp__plugin_copper-pilot_copper-pilot__run mcp__plugin_copper-pilot_copper-pilot__resume mcp__plugin_copper-pilot_copper-pilot__threads_list
context: fork
agent: copper-pilot
background: false
---

# CopperPilot review

Delegate this request to CopperPilot. Do not invent electrical findings from KiCad source text.

Request:

$ARGUMENTS

If `$ARGUMENTS` is empty, use the user's latest message.

Steps:

1. Call `status` if login or schematic/PCB paths are unknown.
2. Prefer `ask` unless the user asked for a schematic or PCB change. Use `plan` for a plan and `run` only when they asked to apply a change.
3. Do not Edit or Write KiCad files.
4. If a tool returns `login_required`, tell the user to run `copper-pilot auth login`.
5. Summarize the specialist memo and quote `thread_id` so the parent can `resume`.
