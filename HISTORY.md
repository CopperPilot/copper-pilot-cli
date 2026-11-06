Changelog
=========


(unreleased)
------------

Fix
~~~
- Name what earlier attempts saw in a turn's final error. [AdityaNG,
  Claude Opus 5.5]

  During a hosted outage on 2026-10-30 (rea-hardware thin-laptop i12) every
  attempt of a turn failed differently (dropped sockets, close code 1012
  service restart, keepalive timeouts, HTTP 503 on reconnect, generic failed
  finals), but the turn error only carried the last attempt's generic
  "CopperPilot ran into an error. (... after N s ...)". The causes the
  client did see never reached the caller.

  When a turn fails after retries, its error now appends the earlier
  attempts' failure type and message, or says all attempts failed the same
  way.
- Do not double the period in the update notice. [AdityaNG, Claude Opus
  5.5]

  A feature note that ends in a period printed 'shortcuts.. Run:'.
- Close the side-question review findings. [Cursor, Naveen_Copperpilot]

  A side question that stops its own stream now reports the answer instead of a
  connection error, a dropped question no longer keeps its request running, and
  clearing the chat forgets the request the panel would show.
- Resume on a fresh hosted conversation when the service refuses it as
  too large. [AdityaNG, Claude Opus 5.5]

  validate arch.review on a large RK3566 tablet board ERRORed with "This
  conversation is too large to continue". The review was not over-sized:
  the hosted turn failed after 698 s of tool work (generic failed final),
  the in-turn retry from #39 resumed on the same conversation id, and the
  service refused that resume within 245 ms with 413 chat_request_too_large
  because it judges the size of the history it stores per conversation,
  not the request (a 190-byte repair prompt was refused the same way).

  The stitched resume already carries its own transcript, so when the
  service refuses a turn as too large (413 / chat_request_too_large, not
  the client's own PAYLOAD_TOO_LARGE), the turn now continues once on a
  fresh hosted conversation id. A second refusal still ends the turn.
- Retry a failed hosted final inside the turn and say what is known.
  [AdityaNG, Claude Opus 5.5]

  rea-hardware turns 5-30 min in ended with only "CopperPilot ran into an
  error." and lost their work. The hosted service sends that as a failed
  final (success false, zero tokens, no turn id); 5 of 6 sessions on
  2026-10-27 hit it after exactly 300 s of model silence (11 "Planning next
  moves" heartbeats), i.e. the hosted model-call cap.

  A failed final is now a retryable failed attempt (HOSTED_TURN_FAILED):
  the existing stitched resume re-sends the partial transcript with backoff,
  up to the usual 4 attempts, and the failed final is held back so a
  recovered turn is not reported as an error. A final with tos_blocked is
  not retried. When retries run out, the error names elapsed time, model,
  content-policy block and turn id when the service sends them. The hosted
  error copy echoed as text is kept out of the resume transcript.
- Wait until the loading spinner is removed after cancel. [Cursor,
  Naveen_Copperpilot]

  Scheduling the removal left the spinner mounted when the turn finished, so the interface still showed it as running.
- Finish turn cleanup when cancel lands during startup. [Cursor,
  Naveen_Copperpilot]

  A cancel while the spinner is still mounting skipped the turn's cleanup, so the turn never finished and the UI stayed busy.

Other
~~~~~
- Version inc. [AdityaNG]
- Merge pull request #48 from
  CopperPilot/feat/harness_tools_over_scripts_4_nov_2026. [Aditya]

  Tools over scripts: whole-file KiCad reads, read-only stock libraries, loud INCOMPLETE validate
- Test: read tests write their fixtures as bytes (Windows CRLF)
  [AdityaNG]

  On windows-latest, write_text turned the fixtures' newlines into CRLF and
  the whole-file / paged read tests compared them with LF strings. The read
  tools return the file's exact bytes (right for a CRLF checkout too), so
  the fixtures are now written as bytes and a test pins byte-exactness with
  a CRLF file.
- Merge remote-tracking branch 'origin/main' into
  feat/harness_tools_over_scripts_4_nov_2026. [AdityaNG]

  # Conflicts:
  #	tests/test_tools.py
- Merge pull request #49 from
  CopperPilot/fix/cli_checkpoint_growth_5_nov_2026. [Aditya]

  Sessions DB: compact event log, bounded checkpoints, threads compact-db
- Merge remote-tracking branch 'origin/feat/chat_compaction_4_nov_2026'
  into fix/cli_checkpoint_growth_5_nov_2026. [AdityaNG]
- Feat(sessions): threads compact-db shrinks a database from an older
  CLI. [AdityaNG]

  Pruning per turn stops growth, but space already used stays in the file.
  `copper-pilot threads compact-db [--dry-run] [-y]`:
  - prunes every CopperPilot thread to its first and newest checkpoints
    (threads another agent wrote are left alone)
  - rewrites copper_events in the remaining checkpoints and writes to the
    compact form
  - VACUUMs only when the free space next to the database is at least its
    size and twice its live data (the temporary copy goes there too);
    otherwise it stops after pruning with a clear message
  - never runs while a chat is open: chat runtimes hold a shared lock on
    .sessions.db.open.lock and compact-db needs it exclusively (POSIX; on
    Windows it relies on the prompt)
  - --dry-run reports what would go and changes nothing
- Fix(sessions): keep only the newest checkpoints of a thread.
  [AdityaNG]

  Each turn writes three checkpoints and each holds the whole state, so a
  thread's rows grew with the square of its turns and nothing pruned them.
  - after every turn and state update the runtime deletes the thread's
    older root-namespace checkpoints and their writes, keeping the first
    (its writes hold the initial prompt) and the newest KEEP_CHECKPOINTS (3)
  - nothing reads older ones: the CLI never time-travels and the turn
    journal is a sidecar file; LangGraph's SQLite saver has no prune API, so
    this deletes by checkpoint_id range under the saver's own lock
  - one prune frees at most 256 MiB of checkpoints, so a large thread from
    before is trimmed over a few turns instead of stalling one
  - a failed prune is logged and never fails the turn
- Fix(sessions): store copper_events in a compact replay form.
  [AdityaNG]

  Every checkpoint row stores the whole state, and copper_events kept every
  streamed frame (one per text chunk, plus tool arguments and results), so
  each turn re-stored the thread's whole event history.
  - drop text, tool_request and tool_result frames: the TUI never replays
    them (they are in messages), and ACP, headless and MCP read messages only
  - join consecutive reasoning chunks into one event, only where the TUI's
    TurnState accepts every chunk; the joined event keeps the last sequence,
    so replay renders the same text and ends in the same turn state
  - keep every other event (status, todo_list, subagent, final, error,
    compaction) unchanged; latest_todos and session_state read the same
  - a thread stored before this is rewritten to the compact form once, on
    its next turn (LangGraph Overwrite)
  Tests compare the TUI's replay of a raw log with its compact form.
- Merge pull request #47 from
  CopperPilot/feat/chat_compaction_4_nov_2026. [Aditya]

  Chat context v1: pinned state, /compact, /context, ACP usage size (CLI + ACP)
- Merge remote-tracking branch 'origin/main' into
  feat/chat_compaction_4_nov_2026. [AdityaNG]
- Fix(context): pin plans written to "/.copperpilot/..." on Windows.
  [AdityaNG]

  Deep Agents writes plans to workspace-rooted virtual paths. On Windows
  "/.copperpilot/plans/x.plan.md" has no drive, so Path.is_absolute() is
  False and joining it to the workspace landed on the drive root: the plan
  was never pinned (3 tests failed on windows-latest only).
- Merge pull request #46 from
  CopperPilot/feat/async_subagents_3_nov_2026. [Aditya]

  Subagent status in the turn journal, concurrent agent lanes, bare resume intents
- Merge main into feat/async_subagents_3_nov_2026. [AdityaNG]
- Merge pull request #45 from
  CopperPilot/feat/chat_resilience_resume_1_nov_2026. [Aditya]

  Chat drops: keepalive, journaled turn resume, -r/`/resume`, ACP continue
- Test: the late background process outlives the first turn on slow
  runners. [AdityaNG]

  It slept 0.4 s, so on a loaded runner (ubuntu 3.13 on #46) it finished
  before the first turn ended and no background_processes_waiting event was
  emitted. It now sleeps 3 s.
- Test: Windows-safe outside-workspace guard tests, wait for TUI focus.
  [AdityaNG]

  The shell-scope and tools tests compared against POSIX display paths
  ("/", "/usr", "~" for /home/maker). On Windows a drive-less root
  resolves to the current drive ("D:\\"), so HOME and the expected reasons
  now use the OS's absolute form; the tools tests expect the workspace
  drive's root. The ask-user TUI test waits for focus instead of asserting
  it right after one pause (flaky on windows-latest).
- Test: kill-by-name tests hold on Windows too. [AdityaNG]

  The PID-kill run test used a POSIX shell line; on Windows (cmd.exe) it
  runs the equivalent taskkill /PID. The classification tests use native
  paths from the OS temp dir instead of the shared /home/maker fixture, so
  they don't depend on the POSIX-path fix pending in #45.
- Fix(tools): ask before killing processes by name. [AdityaNG]

  The agent ran `killall -9 python3` to stop its own background job and
  killed every python3 of the user, the CLI itself included. Process
  kills by name (killall, pkill incl. -f, killall5, taskkill /IM or /FI,
  Stop-Process -Name, kill fed by pgrep/pidof/ps, kill -1, kill 0) now go
  through the outside-the-workspace path: asked in every approval mode,
  YOLO and Auto included, and refused in headless runs with the reason
  and what to do instead (kill the PID of the process you started).
  kill <PID> and taskkill /PID stay ordinary commands.
- Fix(tools): page big internal KiCad reads ourselves, outside the
  workspace too. [AdityaNG]

  A KiCad read too big for one frame fell back to Deep Agents' read_file,
  which sees only the workspace: stock libraries such as Connector_Generic
  (6.7 MB with JSON escaping) came back 'File not found', so placing a
  header from it failed (tool-usage arm 4). Internal KiCad reads now return
  the whole file raw when it fits, and otherwise raw frame-sized pages under
  an '@@ lines a-b of N | next offset b @@' header, honouring offset/limit;
  the server's bridge follows the pages.
- Workspace: never pick a project under .copperpilot. [AdityaNG]

  Discovery walked into .copperpilot, so a downloaded reference project or a
  staging copy could become the active design (fewest folders, then path
  order) and its files used up the 2000-file scan budget. Ignore the folder
  like the validate snapshot already does.
- Validate: name skipped checks in a loud INCOMPLETE line. [AdityaNG]

  A tool-usage baseline agent ran validate with --skip pcb.drc (and every
  other PCB stage), saw a clean-looking summary and reported "all
  verification stages passing" on a board with 556 DRC errors. Print one
  line, in the console summary and at the top of REPORT.md, that names each
  skipped check and says the run does not show the design passes. Exit codes
  are unchanged.
- Fix(tools): whole-file KiCad reads for the hosted bridge. [AdityaNG]

  - read_file with a KiCad workspace purpose returns the whole file raw (no
    100-line window), like the desktop harness; files too big for one frame
    fall back to the paged native read.
  - read_binary_file reads bytes from disk instead of Deep Agents' 2000-line
    text window, which cut text files silently and mangled unknown binaries.
  - External reads are allowed only when flagged and only for KiCad library
    files (.kicad_sym, .kicad_mod, 3D models); external writes stay blocked.
- Test(context): resume with a bare "Continue." (only bare resume
  intents resume since #46 b5ebd2f) [AdityaNG]
- Feat(context): never compact while subagent workers are queued,
  running or paused. [AdityaNG]

  A resumed turn restarts unfinished async subagents from
  .copperpilot/scratch/manifest.json and they need the uncompacted context.
  - auto-compaction is skipped (logged, nothing shown) while any manifest
    unit has status queued, running or paused
  - manual /compact (TUI and ACP) warns with the worker count and compacts
    only on a second /compact within 60 s
  - a missing, unreadable or malformed manifest counts as no active workers
- Fix(context): count inline base64 images at a fixed cost, never send
  them raw. [AdityaNG]

  The model gets an inline data:image/...;base64 URI (the schematic
  visualizer's <img>) as an image block, so chars/4 overstated it ~50x.
  Estimates (context_estimate, the live-measurement remainder, now
  pending_tokens) count each image as IMAGE_TOKENS = 1600. Compaction
  context entries, tool digest excerpts, the interrupted-turn entry, the
  pending query and earlier_user_messages replace image URIs with
  [image], and search_chat_history neither matches nor snippets base64.
- Fix(context): estimate counts only content after the measurement.
  [AdityaNG]

  A turn's stored context usage recorded measured_at = len(original), the
  messages before the turn, so the next estimate re-added the turn's own
  answer (already in next_turn_tokens) and, for a resumed turn, its
  "continue" messages. In cli-forced-2 the 339K-char answer of the resumed
  u3 added ~85K tokens (estimate 234204 vs next_turn_tokens 149173).

  measured_at is now the checkpoint length after the turn (state messages,
  including continue messages, plus the turn's additions). A mid-turn
  (live) measurement also stores the assistant text streamed after it as
  pending_chars. An interrupted turn's journal usage covers the messages
  present when it started. Tool messages are never counted.
- Fix(context): auto-compact a new turn that supersedes an interrupted
  one. [AdityaNG]

  A new message after an interrupted turn supersedes it (no longer
  resumable), so the never-mid-turn rule no longer blocks compaction. The
  pre-turn check now also runs there, after the interrupted turn's facts are
  merged, measured from its carried context usage (next_turn_tokens when the
  server sends it). The interrupted turn's work, which never reached the
  checkpoint, goes into the compaction context as one assistant entry with
  tool digests from its journal, and the pending message ends the context so
  the server's cut keeps it. Resume attempts (/resume, -r, "continue")
  still never compact, and session_state is still pinned after compaction.
- Test(context): history after interrupted turns: new message vs
  continue. [AdityaNG]

  Pins the #45 resume semantics behind the chat_context tap counts
  [0, 1, 1, 1]: a new message after interrupted turns sends every earlier
  user message, while a "continue …" message resumes the unfinished turn by
  resending its original request.
- Fix(plans): parse agent-written plans with unquoted colons;
  session_state never fails a turn. [AdityaNG]

  Found in the live headless story: the agent wrote a plan whose todo text had
  unquoted ': ' (invalid YAML), and every following turn crashed in
  parse_plan via session_state with yaml.ScannerError. /build in the TUI and
  ACP hit the same parser. Plain values are now quoted and re-parsed on a YAML
  error, and a failure to build session_state sends {version: 1} with a
  warning instead of failing the turn.
- Fix(context): keep interrupted turns' answers, todos, plan and size in
  session_state. [AdityaNG]

  An interrupted turn (Ctrl-C, SIGINT) cancels the graph node, so its
  messages and copper_events never reach the checkpoint; only its turn
  journal survives. The journal now also records the turn's latest todo list
  and context usage. The next turn lifts the facts out of the abandoned
  journal (ask_user_question answers incl. rejected/unanswered as skipped,
  plan files written, todos, context size), carries them in its own journal
  so consecutive interruptions chain, and stores them under
  copper_interrupted_facts once a turn returns. session_state merges them by
  transcript position and timestamp, deduplicated against the checkpoint.
- Feat(tui): /compact, context-aware /context and a ctx meter in the
  status bar. [AdityaNG]

  - status bar meter 'ctx 45% · 470K/1M' from stored usage, live
    context_usage events and finals; warn/high/critical colours
  - /compact [focus] runs manual compaction locally (refused mid-turn and
    while a turn is unfinished) and prints a notice
  - /context adds used/limit, auto-compacts-at and the compaction; counts the
    context via the count endpoint when there is no fresh measurement
  - printed notices for auto and in-turn compaction
- Feat(acp): /compact and /context commands, context usage_update with
  size. [AdityaNG]

  - advertise /compact (hint: optional focus) and /context; both answered
    locally without a hosted chat turn
  - usage_update{used: context_tokens, size: context_limit} from finals, live
    context_usage events (throttled to 2 s), compaction and session/load;
    billing token counts no longer drive the meter once context usage exists
  - compaction progress and results as agent message text
  - session/load notes a compacted chat
- Feat(context): pinned session state, compaction store and history
  search. [AdityaNG]

  Chat context protocol v1 on the CLI graph:
  - ChatRequest.session_state snapshotted once per logical turn (ask answers,
    latest todos, working plan, compaction summary and earlier user messages),
    journaled with the compaction cut so every resume resends it unchanged
  - copper_compaction / copper_compaction_superseded / copper_context_usage
    checkpoint keys; context after the cut only; history_omitted reported
  - auto-compaction before a new turn (never while a turn is unfinished),
    in-turn compaction from status events or the final frame
  - POST /analyze/compact and /analyze/context/count client with tool digests
  - search_chat_history routed by the broker to the local transcript;
    chat_history_search capability for checkpointed chats
  - catalog context_window and context_policy parsing
- Fix(diagnostics): keep numeric log args numeric when redacting.
  [AdityaNG]

  RedactingFormatter turned every argument into a string, so any %d / %.1f
  line written to the diagnostics file (e.g. "Chat turn finished ... wall=%.1fs")
  raised "TypeError: must be real number, not str". Numbers and None now pass
  through; everything else is still redacted, and mapping args are redacted
  per value.
- Fix(resume): only a bare resume intent resumes the unfinished turn.
  [AdityaNG]

  A message like "continue, but use 0603 resistors" used to resume and drop
  the instruction. Now only "continue", "resume", "/resume" and "continue
  where you left off" (case and punctuation ignored) resume; anything with
  extra text starts a new turn.
- Feat(protocol): run subagents' tool requests concurrently, one lane
  per agent. [AdityaNG]

  Tool requests tagged with agent_id ('main' or a subagent id) run on one
  lane per agent: an agent's requests keep their order, different agents
  run at the same time (they used to be awaited one by one, so parallel
  subagents queued behind each other). Results stream back as they finish
  and are all reported before the final; after a dropped socket the lanes
  still finish so their results reach the journal. Approval prompts stay
  one at a time (the broker's prompt lock). Untagged requests from older
  servers are still awaited inline.
- Feat(resume): journal subagent status and send it on resume.
  [AdityaNG]

  Each subagent chip (unit, status, scratch folder) is kept in the turn
  journal and returned in request.resume, matching the desktop client.
- Docs: outside-the-workspace shell approval. [AdityaNG, Claude Opus
  5.5]
- Tui: show the outside-the-workspace reason and drop the Auto option.
  [AdityaNG, Claude Opus 5.5]

  The approval menu adds a warning line with the reason and offers only yes
  and no; the Auto shortcut is ignored for these prompts.
- Acp: carry the outside-the-workspace reason on permission requests.
  [AdityaNG, Claude Opus 5.5]

  The request title is prefixed with "Outside the workspace:", the reason is
  in the tool-call content, and "Allow all for this session" is not offered,
  so a YOLO session still asks for each such command and a stray
  session-wide allow neither approves nor switches modes.
- Tools: ask before outside-the-workspace shell in every approval mode.
  [AdityaNG, Claude Opus 5.5]

  The broker classifies each execute request before the mode check. A flagged
  command is sent to the approver even in Auto and YOLO, with the reason on
  ApprovalRequest.outside_workspace, and approval is never cached. With no
  approver (headless -n, MCP) it is refused with the tool error
  "blocked: outside the workspace (<reason>); ask the user first". The
  validate reviewer, which has no human, declines these requests.
- Shell: keep the temp-dir allowance from covering a sandboxed HOME.
  [AdityaNG, Claude Opus 5.5]

  A TMPDIR that contains the home directory would have allowed every path
  under it, so it is skipped.
- Shell: classify agent commands that reach outside the workspace.
  [AdityaNG, Claude Opus 5.5]

  classify_outside_workspace(command, workspace_root, home) returns a short
  reason for recursive searches rooted at /, a system top-level dir or a home
  dir (find, grep -r, rg, fd, du, tree, ls -R), filesystem index searches,
  paths under the home dir outside the workspace (KiCad locations, the CLI
  home and TMPDIR excepted), and docker/podman/kubectl/sudo/ssh. It follows
  &&/||/;/| chains, cd, $(...), sh -c and common wrappers. Commands seen in
  long --yolo runs are flagged; relative and in-project commands are not.
- Graph: never resume a provider_busy pause before retry_after_s.
  [AdityaNG, Claude Opus 5.5]

  The jitter only adds up to 20% on top of the server's retry_after_s, so a
  resume never lands earlier than the server asked.
- Docs: high-demand waits, provider_busy resumes and the capacity_wait
  JSON event. [AdityaNG, Claude Opus 5.5]
- Acp: capacity waits and provider-busy pauses as progress thoughts.
  [AdityaNG, Claude Opus 5.5]

  capacity_wait events and provider-busy resume pauses become agent thought
  lines ("High demand — waiting to continue (next try in N s)", "continuing
  in N s"), which T3 Code shows as progress. A turn whose provider-busy
  budget ran out ends like other unfinished turns, with high-demand copy and
  the continue hint instead of the error wording.
- Tui: high-demand countdown for capacity waits and provider-busy
  pauses. [AdityaNG, Claude Opus 5.5]

  A capacity_wait shows "High demand — waiting to continue (next try in N s)"
  on the working line with a live countdown and one toast per turn; a
  provider-busy pause counts down "continuing in N s"; the next normal frame
  clears it. When the provider-busy budget runs out the turn shows the
  high-demand resume notice instead of an error.
- Headless: show capacity waits and provider-busy pauses; --json
  capacity_wait lines. [AdityaNG, Claude Opus 5.5]

  Text mode prints a yellow "High demand — waiting to continue (next try in
  N s)" line per wait and "continuing in N s" for a provider-busy pause; a
  turn whose provider-busy budget ran out prints the high-demand copy with the
  -n -r resume command. --json emits {"type": "capacity_wait", ...} with the
  event fields at the top level next to the turn ids and ts.
- Graph: wait out provider_busy finals on their own resume budget.
  [AdityaNG, Claude Opus 5.5]

  A provider_busy resumable final (the LLM provider stayed out of capacity for
  the server's whole wait budget) no longer resends at once into an exhausted
  quota. The turn waits retry_after_s (+/-20%, at least 15 s) and resumes from
  the journal with reason provider_busy. These resumes have their own budget,
  6 in a row or 30 minutes per logical turn, and spend none of the transport
  budget; any model progress starts a new streak. When the budget runs out
  the turn ends manually resumable with high-demand copy instead of an error.

  capacity_wait status events become copper.capacity_wait progress events
  with a one-line high-demand title, and INFO logs (ids and timings only)
  record each wait, pause, budget reset and stop. A resumable final's echoed
  error copy is no longer streamed as answer text.
- Protocol: capacity_wait extends the idle bound; provider_busy finals
  carry retry_after_s. [AdityaNG, Claude Opus 5.5]

  A capacity_wait status event means the hosted model is waiting for LLM
  provider capacity; the turn is healthy, so the next-frame bound grows by the
  longer of the announced retry delay and the server's remaining wait budget
  until a resumed or normal frame arrives. A provider_busy resumable final now
  passes retry_after_s through and classifies as its own error type.
- Test: wire-level resume after a drop mid tool result. [AdityaNG,
  Claude Opus 5.5]

  A real agent and protocol stack over fake sockets: the second
  chat_start resends the original query/context/effort with a new turn_id,
  turn_resume_v1 and the journal (tool completed with its clipped result,
  reason tool_result_undeliverable), and a repeated tool_call_id is
  answered from the broker cache instead of running again.
- Acp: let the graph pick app_restart or manual for a resumed turn.
  [AdityaNG, Claude Opus 5.5]
- Docs: turn durability, unfinished-turn resume, and --json turn fields.
  [AdityaNG, Claude Opus 5.5]
- Acp: end an unfinished turn cleanly; "continue" resumes its journal.
  [AdityaNG, Claude Opus 5.5]

  When automatic resumes run out, the prompt now ends with stop reason
  max_turn_requests and a message naming the last error and how to
  continue, instead of a JSON-RPC error. Usage-limit and auth failures
  keep their existing errors. The next "continue …" prompt (also inside a
  T3 Code <user_request> envelope) or /resume (advertised as a command)
  resumes the unfinished turn from its journal instead of sending
  "continue" as a new request; with no unfinished turn, "continue" is a
  normal prompt. Server restarts and socket rotations show a thought line.
- Tui: offer /resume for an unfinished turn. [AdityaNG, Claude Opus 5.5]

  Opening a thread whose last turn did not finish (crash, quit, Ctrl-C,
  exhausted auto-resumes) shows a notice with the query, the recorded tool
  calls and the last error category. /resume now continues that turn from
  its journal; with no unfinished turn it still opens the thread picker
  (/threads and Ctrl+R are unchanged). A "continue" message does the same
  through the graph. Resume progress updates the status line, and a turn
  that stops unfinished shows the same affordance inline.
- Headless: -n -r resumes the unfinished turn; JSON events carry turn
  ids. [AdityaNG, Claude Opus 5.5]

  - `copper-pilot -n -r [thread]` without --message resumes that thread's
    unfinished turn (crash, Ctrl-C, exhausted auto-resumes) with its
    journal; it exits 2 with a clear message when there is none. A turn
    that stops unfinished prints the exact command to resume it.
  - `--json` lines keep type/data/title and add turn_id, root_turn_id,
    resume_attempt (client resume count) and ts (epoch seconds), plus
    sequence and server_resume_attempt when the frame carried them.
    copper.resume events surface as type "resume" with attempt, reason,
    delay_s and will_retry.
- Graph: resume dropped turns from a client-side journal. [AdityaNG,
  Claude Opus 5.5]

  The copper_pilot node now keeps a journal of each logical turn (original
  request, assistant text, tool calls with completed/failed/interrupted
  outcomes) and resumes a dropped attempt by resending the ORIGINAL request
  with a new turn_id and request.resume, instead of the stitched transcript
  plus "Resume where we left off" prose. The prose path stays behind
  COPPER_PILOT_TURN_RESUME=0 for servers without turn_resume_v1.

  Retry policy (protocol v1): up to 8 automatic resumes or 10 min of
  recovery per logical turn, backoff 1,2,5,10,20,30,60,60 s with +-20%
  jitter, and a GET /api/heartbeat wait before each resume that does not
  use an attempt. A resumable final (server_restarting/socket_rotation)
  resumes after 0-2 s outside the error budget, capped at 20 resumes.
  Later attempts keep the broker's tool-result cache.

  The journal is rewritten to a sidecar file while the turn runs and kept
  in the checkpoint (copper_turn_journal) when the turn ends unfinished,
  so a crash, Ctrl-C or exhausted budget can be resumed with
  copper_resume input or a "continue" message. Events carry root_turn_id,
  turn_attempt and ts; copper.resume reports each resume. INFO logs record
  drops (close code, attempt, elapsed), resume start/success and turn
  completion (wall time, attempts, tool counts), without content.
- Protocol: keepalive, 330 s idle bound, and a reader that never stops.
  [AdityaNG, Claude Opus 5.5]

  Turn resume protocol v1, transport side:

  - ping_interval=15 / ping_timeout=20 on the chat socket; the idle bound
    drops to 330 s (longer than the server's 300 s quiet timeout) and an
    idle socket is closed with 4000 client_liveness_timeout.
  - A reader task drains the socket for the whole turn, so frames and the
    library's pong handling keep flowing while a long local tool runs.
    Frames are still processed in order, one tool step at a time.
  - A tool result whose socket is gone is still reported (delivered=false)
    instead of being dropped, including the rest of a running batch.
  - chat_start carries turn_id, request.resume, and advertises
    turn_resume_v1; final frames surface resumable/error_code.
  - Drops carry close code/reason and a resume reason; heartbeat() probes
    GET /api/heartbeat.
  - turn_journal.py holds the client-side journal and its sidecar store.
- Merge pull request #44 from CopperPilot/fix/turn-error-attempt-
  history. [Aditya]

  fix: name what earlier attempts saw in a turn's final error
- Validate: connector_edge reads role-named references like J_ETH (#43)
  [Aditya, Claude Opus 5.5]

  Agents often name connectors by role (J_ETH, J_USBA, J_HDMI) instead of
  J1, J2. The reference pattern required a digit right after the prefix, so
  those parts were never checked and the check passed with "0 external
  connector(s) checked" on a board whose USB-C and micro-HDMI face inward.
  Accept a separator after the prefix too; JMP_EMMC still doesn't match.
- Validate: ungated connector-edge and 3D-model checks; agent shell
  finds CLI tools (#42) [Aditya, Claude Opus 5.5]

  Add two deterministic, no-LLM builtin checks that run even after a stage
  gate halts the run (new `ungated: true` check field):

  - pcb.connector_edge (S5): external connectors (USB, RJ45, HDMI, DP, DVI,
    barrel/audio jacks, D-Sub) must sit on the board edge with the mating
    face pointing out. The face direction comes from the pad centroid to the
    Fab/courtyard body centre (or a "PCB Edge" drawing note), rotated into
    the board frame, and is ray-cast against Edge.Cuts. Inward, sideways or
    inboard connectors are P1, set-back ones P2. Headers, board-to-board,
    vertical and ambiguous parts are not judged.
  - pcb.models_3d (S2): every footprint (model ...) path must resolve
    (${KIPRJMOD}, ${KICADn_3DMODEL_DIR} for any version, KiCad's configured
    path variables, the system 3D directory next to kicad-cli, project-
    relative paths). Missing files are P1; bodies with no model are P2.

  parts.footprints no longer rates a missing 3D model as P3.

  Agent shell commands now append the CLI environment's scripts folder to
  PATH, so tools installed alongside the CLI by pipx or `uv tool` (e.g.
  easyeda2kicad) are runnable without shadowing the user's own python.
  easyeda2kicad is offered as the optional `easyeda` extra because it is
  AGPL-3.0, which the CI license gate rejects in the default install.
- Merge pull request #38 from naveen-forkd/feat/cli-self-update.
  [Aditya]

  Add a self-update command and one-line feature notices
- Merge main (with /btw) into feat/cli-self-update. [AdityaNG, Claude
  Opus 5.5]
- Merge pull request #36 from naveen-forkd/feat/btw-side-questions.
  [Aditya]

  Add /btw side questions that do not interrupt the running turn
- Docs: note that long btw answers are shortened in the panel. [Cursor,
  Naveen_Copperpilot]
- Perf: ask side answers to stay brief. [Cursor, Naveen_Copperpilot]
- Perf: cap the lines rendered in the btw panel. [Cursor,
  Naveen_Copperpilot]
- Perf: skip re-rendering an unchanged btw answer. [Cursor,
  Naveen_Copperpilot]
- Docs: document /btw side questions. [Cursor, Naveen_Copperpilot]
- Test: cover /btw panel, keys and turn isolation in the tui. [Cursor,
  Naveen_Copperpilot]
- Test: cover side question builder, runner and log. [Cursor,
  Naveen_Copperpilot]
- Test: refresh chrome snapshots for /btw. [Cursor, Naveen_Copperpilot]
- Feat: list /btw in slash commands and key help. [Cursor,
  Naveen_Copperpilot]
- Feat: wire /btw command into the app. [Cursor, Naveen_Copperpilot]
- Feat: let esc close the btw panel before stopping the turn. [Cursor,
  Naveen_Copperpilot]
- Feat: add btw panel widget. [Cursor, Naveen_Copperpilot]
- Feat: run side questions on a tool-less hosted stream. [Cursor,
  Naveen_Copperpilot]
- Feat: add side question log and request builder. [Cursor,
  Naveen_Copperpilot]
- Merge pull request #41 from CopperPilot/fix/resume-on-fresh-
  conversation. [Aditya]

  fix: resume on a fresh hosted conversation when the service refuses it as too large
- Merge pull request #39 from CopperPilot/fix/retry-hosted-failed-final.
  [Aditya]

  fix: retry a failed hosted final inside the turn and say what is known
- Test: prove batched reads overlap instead of timing them. [Cursor,
  Naveen_Copperpilot]

  A wall-clock cutoff of half a second failed when CI was only slightly slower than the ideal overlap, even though the reads still ran together.
- Test: cover self-update behavior and document the command. [Cursor,
  Naveen_Copperpilot]

  Record the check, confirmation, and notice rules so a release cannot announce more than one feature line.
- Feat: show feature update notices inside the app. [Cursor,
  Naveen_Copperpilot]

  Textual captures stderr while the interface is open, so show the one-line notice in the app and keep tests off the network.
- Feat: add a confirmed package-manager update command. [Cursor,
  Naveen_Copperpilot]

  Let users check or apply an update through pipx or the active environment, without replacing an editable checkout.
- Feat: resolve a one-line feature notice from PyPI. [Cursor,
  Naveen_Copperpilot]

  Keep update checks throttled and fail-open, and publish only a validated one-line feature note with each release.


1.0.0 (2026-10-27)
------------------

Fix
~~~
- Do not count echoed request fields toward the stream byte budget.
  [AdityaNG, Claude Opus 5.5]

  The hosted service echoes the request `query` (and, in the final frame,
  `context`) in every chat_response. A live probe showed the echo is ~97% of
  each frame. A validate arch.review prompt is ~76 KB, so a long review on a
  large board crossed the 64 MiB client budget after ~815 frames (2601 s)
  although the hosted output itself was ~4.4 MB, and the check ERRORed with
  "Chat exceeded the client stream byte budget."

  The budget now counts frame bytes minus the echoed request fields the
  client never reads, and the error names the counted size and frame count.
- Stop cutting long hosted answers at the 16 KB status limit. [AdityaNG,
  Claude Opus 5.5]

  Every hosted response frame went through sanitize_status, which caps
  status lines at 16 KB. The hosted side can send a whole answer as one
  response frame, so a validate review longer than 16 KB lost the end of
  its findings JSON. The reviewer then found no json block, sent a repair
  turn into the already huge thread, and the server refused it ("This
  conversation is too large to continue"), so arch.review ERRORed. Chat
  and ACP answers over 16 KB were cut the same way.

  Response text now gets a 1 MiB bound; status and reasoning lines keep
  16 KB. A validate check that fails with a hosted failed final also
  records the hosted turn id, as chat does.
- Surface stalled or failed hosted turns instead of hanging. [AdityaNG,
  Claude Opus 5.5]

  Three ways a long ACP turn could hang or hide its cause:

  - idle_frame_timeout was None, so a hosted server that kept the socket
    (and keepalive) up but stopped sending hung the prompt forever. It now
    defaults to 10 minutes (COPPER_PILOT_IDLE_TIMEOUT_SECONDS, 0 disables)
    and raises the existing retryable CHAT_IDLE_TIMEOUT with the silence
    length in the message. The hosted side heartbeats every 25 s.
  - A failed hosted final (success false) still waited, unbounded, for
    background shells and then ran a continuation turn. A runaway background
    command held the hosted error back until the caller timed out, and a
    continuation that succeeded still reported the stale error. A failed
    final now ends the turn and kills leftover background processes.
  - The hosted copy "CopperPilot ran into an error." carries no detail. The
    ACP error now names the hosted turn id, and the client logs the failing
    final frame's keys, so the server-side cause can be found.
- Guard the state lock with sys.platform so ty passes on Windows.
  [AdityaNG, Claude Opus 5.5]
- Stop concurrent sessions racing on recent-projects state. [AdityaNG,
  Claude Opus 5.5]

  Every process wrote recent-projects.json (and config.json) through the
  same fixed temp name, so parallel `copper-pilot acp` session starts moved
  or clobbered each other's temp file and session/new failed with
  FileNotFoundError on os.replace.

  Writes now go through a per-write mkstemp temp file, the recent-projects
  read-modify-write holds a POSIX flock so no entry is dropped, and the
  bookkeeping logs instead of raising so it can never fail a session.

Other
~~~~~
- Release: version 1.0.0 🚀 [AdityaNG]
- Merge pull request #37 from CopperPilot/fix/stream-byte-budget.
  [Aditya]

  fix: do not count echoed request fields toward the stream byte budget
- Merge pull request #35 from CopperPilot/fix/long-response-truncation.
  [Aditya]

  fix: stop cutting long hosted answers at the 16 KB status limit
- Merge pull request #33 from CopperPilot/fix/hosted-stream-stall.
  [Aditya]

  fix: surface stalled or failed hosted turns instead of hanging
- Merge pull request #34 from CopperPilot/fix/validate-hierarchical-
  nets. [Aditya]

  fix(validate): ground findings that cite label names of hierarchical nets
- Fix(validate): ground findings that cite label names of hierarchical
  nets. [AdityaNG, Claude Opus 5.5]

  KiCad names local and hierarchical label nets with their sheet path
  ("/RAIL_IO_3", "/power/RAIL_IO_3"), but reviewers cite the label
  ("RAIL_IO_3"). Grounding compared the cited name to the full net name
  only, so every real finding on such a board was marked UNGROUNDED and
  could never fail a blocking check (WARN / exit 3 instead of FAIL).

  A cited net now grounds when it equals a design net name or is a
  "/"-boundary suffix of one; invented names still do not.
- Merge pull request #32 from CopperPilot/fix/recent-projects-race.
  [Aditya]

  fix: stop concurrent ACP sessions racing on recent-projects state


0.1.11 (2026-10-25)
-------------------

Fix
~~~
- Do not block turn cancellation on loading-widget removal. [Cursor,
  Naveen_Copperpilot]

  Textual defers widget pruning to its message loop. Awaiting that removal during a cancelled turn could leave the turn task pending on Windows, so the cancellation cleanup test timed out.
- Require Pillow 13 so the security audit passes. [Cursor,
  Naveen_Copperpilot]

  Pillow 12.3.0 is the newest release the old pin allowed, and pip-audit fails the CI job on four advisories fixed in 13.0.0.
- Tell the type checker these widgets are Static. [Cursor,
  Naveen_Copperpilot]

  query() is typed as a generic widget, so ty rejected update() on the working line, the mode icon, and the input rules. CI stopped there before the tests ran.
- Retry a reconnect the server refuses with HTTP 503. [AdityaNG, Claude
  Fable 5.1]

  Every "server rejected WebSocket connection: HTTP 503" failure seen from
  T3 Code follows the same sequence: the hosted pod restarts mid-stream
  (no close frame), the stitched resume reconnects 5 s later while the
  backend is still coming back, and the load balancer answers the upgrade
  with 503. websockets raises InvalidStatus for that, which classify_error
  did not know, so the turn was typed "unknown", non-retryable, and the
  remaining attempt was never used.

  - classify InvalidStatus by HTTP status (5xx/429 retry; auth and other
    4xx fail fast) and InvalidHandshake without a status as a connection
    drop
  - include InvalidHandshake in the transport's pre-first-event retry
  - stretch the turn budget to four attempts (5/10/20/30 s) so it outlasts
    a typical pod restart
  - regression tests for the drop-then-503 sequence and the transport retry

Other
~~~~~
- Release: version 0.1.11 🚀 [AdityaNG]
- Merge pull request #31 from naveen-
  forkd/feat/cli_chat_chrome_2026_10_24. [Aditya]

  Redraw the CLI chat screen, add themes, and make typing faster
- Test: wait until a tool is active before cancelling. [Cursor,
  Naveen_Copperpilot]
- Test: wait for the loading widget to leave after cancel. [Cursor,
  Naveen_Copperpilot]

  Textual drops the widget on a later prune tick, so an immediate check still sees it on Windows.
- Test: run the suite in a UTF-8 locale. [Cursor, Naveen_Copperpilot]

  Windows CI has no UTF-8 locale, so the app drew the ASCII fallback and the Unicode snapshots failed. The suite now sets a UTF-8 locale, and a test still checks that a C locale or COPPERPILOT_ASCII selects the fallback.
- Test: cover theme switching and the input hot path.
  [Naveen_Copperpilot]

  Check the presets, /theme persistence, idle typing, the pruned file index, and streamed Markdown that keeps closed blocks.
- Perf: keep typing, file completion, and streaming off the hot path.
  [Naveen_Copperpilot]

  Skip completion repaints when the matches did not change, and apply a pending @ filter before tab or enter. Build the file list in the background, skip ignored folders, and refresh it when it is stale. Append assistant Markdown only while a block is still open, so a finished paragraph or code fence is not replaced.
- Perf: import deepagents the first time a tool runs.
  [Naveen_Copperpilot]

  Opening the UI was paying for the tool stack at import. Load it on the first tool call instead.
- Test: keep the saved theme out of the suite. [Naveen_Copperpilot]

  Point each test at a temporary CLI home and reset the shared palette. Live tests keep the real home so they can still load the device credential.
- Feat: let /theme switch presets and remember the choice.
  [Naveen_Copperpilot]

  /theme opens a picker or takes a name, writes it to config.json, and repaints the chrome that bakes colours into text.
- Feat: make near-black the default and add theme presets.
  [Naveen_Copperpilot]

  The warm stone background is now one preset. Black is the default, with warm, light, and high-contrast beside it. Snapshot colours read the active tokens, and the locale encoding is cached so glyph checks do not call locale.getencoding on every paint.
- Feat: redraw the prompt and bind shift+tab and ctrl+e.
  [Naveen_Copperpilot]

  Rules replace the rounded box, and one tip sits above them until the first message. shift+tab and ctrl+e were taken by focus movement and end-of-line, so those bindings now take priority and cycle mode and effort while the prompt is focused.
- Feat: replace the boxed header with a flat welcome and one status
  line. [Naveen_Copperpilot]

  Drop version, full path, and model from the header. Show the wordmark, repo, and quick start, and put mode and effort on the line under the prompt.
- Feat: add the plain-text layout for the chat chrome.
  [Naveen_Copperpilot]

  Welcome, status, slash rows, the working line, and tips are content the widgets render. Only commands, modes, and keys the CLI already has are included.
- Feat: draw the CopperPilot cube as a Braille mark.
  [Naveen_Copperpilot]

  Six rows of thirteen cells, one orange. The welcome omits it when the terminal is narrow, uncolored, or ASCII.
- Feat: switch the CLI theme to the warm orange palette.
  [Naveen_Copperpilot]

  One orange and warm neutrals, with the same layout in truecolor, 256-color, and NO_COLOR. Status green and red stay limited to tool results.
- Merge pull request #27 from
  CopperPilot/feat/disable_model_selector_19_oct_2026. [Aditya]

  feat: hide Intelligence model selection behind ENABLE_MODEL_SELECTOR (off)
- Feat: hide Intelligence model selection behind ENABLE_MODEL_SELECTOR
  (off) [AdityaNG, Claude Opus 5.5]

  Add ENABLE_MODEL_SELECTOR to copper_models, set to False. While it is off
  no surface names a model or offers a choice, and every Intelligence turn
  asks for the default model (Gemini 4 Argon), including threads saved with
  another model. Balanced still sends no model. No server change.

  - TUI: no /model command or completion, /context and /help omit the model.
  - Headless: --model is not registered.
  - MCP: no models tool, the model argument is dropped from the ask/plan/
    run/resume schemas, and results omit model/answered_model.
  - ACP: only mode and effort config options, no /model command, a model
    config change is rejected, and effort copy names no model.
  - Fallback notices use a generic "switched to a backup model" message.

  Flipping the constant to True restores the previous behaviour; the
  existing model-selection tests run with it on via a fixture.
- Merge pull request #28 from
  CopperPilot/feat/agent_latency_cli_19_oct_2026. [Aditya]

  Run tool_requests batches concurrently (parallel_tool_requests capability)
- Keep batched tool calls on one assistant message; test ACP overlap.
  [AdityaNG, Claude Opus 5.5]

  The transcript records a batch's calls on a single AIMessage so every
  call is followed by its result whatever the completion order. ACP already
  keys tool_call / tool_call_update by id; a test now covers three
  overlapping calls completing out of order.
- Execute tool_requests batches concurrently over the chat socket.
  [AdityaNG, Claude Opus 5.5]

  chat_start advertises the parallel_tool_requests capability (disable with
  COPPER_PILOT_PARALLEL_TOOLS=0). A batch frame yields one TOOL_REQUEST per
  call, runs them with the scheduler and sends each tool_result, matched by
  tool_call_id, as soon as it completes. Single tool_request frames are
  unchanged. The hosted attempt is now closed deterministically so a
  consumer that stops listening cancels running batch members, and the
  local broker serializes approval and question prompts.
- Add a scheduler for parallel tool-request batches. [AdityaNG, Claude
  Opus 5.5]

  Classifies each hosted tool request as READ, MUTATING (keyed by path) or
  EXCLUSIVE and runs a batch with model-order dependencies: reads overlap
  (capped at 8), same-path mutations run in order, shell commands that are
  not provably read-only run alone. Failures are reported per tool and
  closing the batch cancels unfinished members.
- Merge pull request #26 from CopperPilot/feat/acp-usage-limit-reset.
  [Aditya]

  feat(acp): explain usage limits with their reset time
- Merge remote-tracking branch 'origin/main' into feat/acp-usage-limit-
  reset. [AdityaNG]

  # Conflicts:
  #	docs/cli.md
  #	src/copper_pilot_cli/copper_acp.py
  #	tests/test_acp.py
- Merge pull request #25 from CopperPilot/t3/intelligence-mode-model-
  selection. [Aditya]

  feat: Intelligence model selection across TUI, CLI, MCP, and ACP
- Feat: default Intelligence to Gemini 4 Argon, Gemini models first.
  [AdityaNG, Claude Fable 5.1]

  Mirror copper-pilot-client #98: the bundled catalog now lists the Gemini
  models first and defaults Intelligence to gemini-4-argon. The server's
  catalog order and default still win whenever it is reachable.
- Feat: Intelligence model selection across TUI, CLI, MCP, and ACP.
  [AdityaNG, Claude Fable 5.1]

  Mirror the desktop client's Intelligence model picker (copper-pilot#97):
  Balanced always runs on the server's Balanced model; Intelligence turns
  may pick one model from the hosted catalog (Claude Opus 5.5 default).

  - copper_models: catalog fetched once per process from
    GET /api/copperpilot/chat/models with the bundled list as fallback,
    plus model_fallback status-event helpers.
  - ChatRequest.model is sent only on Intelligence turns; the thread
    checkpoint persists copper_model next to copper_effort.
  - CLI --model (implies --effort intelligence), TUI /model and /context,
    and model_fallback notices in the transcript and headless output.
  - MCP: model argument on ask/plan/run/resume, a models tool, and
    model/answered_model in results and the digest.
  - ACP: a model config option (category model) next to effort
    (thought_level); choosing a model selects Intelligence, Balanced pins
    the option to the Balanced model, /model slash command, and fallback
    notices as agent messages.
- Test(acp): accept a usage reset that falls after midnight. [AdityaNG,
  Claude Opus 5.5]
- Feat(acp): explain usage limits with their reset time. [AdityaNG,
  Claude Opus 5.5]

  A turn refused by the usage window or weekly limit reached ACP clients as a
  generic -32603 error whose text carried a raw UTC timestamp, or for a refused
  handshake only "HTTP 402".

  - Keep the reset time from the hosted error frame's detail
    (window_ends_at / week_ends_at) on ProtocolError and the error event.
  - ACP fails such a turn with code -32003 and a message naming the spent
    limit and its local reset time ("It resets at 3:20 AM (in 2h 23m).");
    data carries class=usage_limit, blockReason and resetAt. Without a reset
    time in the frame, the token-limits endpoint fills it in; a bare 402 the
    account view does not confirm (the Intelligence plan gate) is unchanged.
  - The graph keeps a refused handshake's HTTP status on its error event.
  - ACP drops the terminal "Attempt N failed" thought that repeated the error.
  - New ACP /usage command shows the window, weekly balance and reset times.
  - The TUI renders usage blocks with the same local reset time.
- Merge pull request #24 from CopperPilot/fix/retry-refused-handshake.
  [Aditya]

  fix: retry a reconnect the server refuses with HTTP 503


0.1.10 (2026-10-18)
-------------------

Fix
~~~
- Skip the killed-background resume test on Windows. [AdityaNG, Cursor]

  os.killpg is POSIX-only, and that failure was the Windows CI break.
- Resume the agent when a background shell already exited. [AdityaNG,
  Cursor]

  The continuation turn only ran while a background command was still alive, so a search that finished during the rest of the turn never woke the agent.

Other
~~~~~
- Release: version 0.1.10 🚀 [AdityaNG]
- Merge pull request #23 from CopperPilot/feat/acp-image-prompts.
  [Aditya]

  feat(acp): accept image prompts
- Feat(acp): accept image prompts. [AdityaNG, Cursor]

  T3 Code refused to start any ACP turn that carried an image because initialize never advertised promptCapabilities.image, and prompt() dropped image blocks anyway. Advertise image support, save each image block under .copperpilot/attachments, pass it as a path-first runtime attachment like the TUI does, and list the saved paths in the query so the agent reads the workspace copy.
- Merge pull request #22 from CopperPilot/fix/resume-finished-
  background-shell. [Aditya]

  fix: resume the agent when a background shell already exited
- Merge pull request #21 from CopperPilot/feat/validate. [Aditya]

  feat: copper-pilot validate, a stage-gated read-only review graph
- Fix(validate): default reviewers to agent mode and pass Windows CI.
  [AdityaNG, Cursor]

  Agent mode is the better default for hosted reviewers; the read-only broker still blocks design writes. Windows treated POSIX virtual paths and library URIs as relative, and os.kill(SIGTERM) aborted pytest before it could report.
- Fix(validate): enforce reviewer.timeout_s and treat SIGTERM like
  Ctrl-C. [AdityaNG, Claude Fable 5.1]

  A hosted architecture review on a 68-part board outlived an external
  timeout(1) wrapper; SIGTERM killed the process with no report, and the
  graph's reviewer.timeout_s had never been applied. Each reviewer slice now
  runs under asyncio.timeout(reviewer.timeout_s) and times out as an ERROR,
  and the CLI maps SIGTERM onto the same partial-report path as Ctrl-C.
- Fix(validate): scope release.repo to the project folder; tolerate
  markdown escapes in replies. [AdityaNG, Claude Fable 5.1]

  Running validate on a repository that keeps generated boards outside the
  KiCad project (video/build/tmp_pcbs) counted them as portability failures.
  The workspace walker now skips the same build/dist/node_modules folders as
  discover_workspace, and release.repo only scans the project folder.

  A live sch.ratings reviewer wrote '\*' inside JSON strings, which is not a
  JSON escape and cost a repair turn; invalid escapes are now dropped before
  the repair path is used.
- Feat(validate): explain, --context, Python validate() API, and live
  test. [AdityaNG, Claude Fable 5.1]

  Add copper-pilot validate explain CHECK_ID (config, rendered prompt, input
  hashes, last result), --context FILE ... (appended to every reviewer prompt
  under Reference material and folded into the cache key), the
  copper_pilot_cli.validate.validate()/avalidate() API returning a
  ValidationReport, and a pytest -m live end-to-end run of validate --until S2
  on examples/001_esp32.
- Feat(validate): --json events, --junit, --emit-fix-prompt, MCP
  validate tool, skill and docs. [AdityaNG, Claude Fable 5.1]

  Stream validate.start/check/result/end events (plus copper.retry pass-through)
  with --json, write JUnit XML with --junit (FAIL/ERROR -> failure/error,
  SKIPPED -> skipped naming the blocker, WARN -> system-out), and print a
  ready fix message for copper-pilot --mode agent with --emit-fix-prompt.
  Expose validate(workspace?, profile?, gate?, until?, only?, effort?,
  no_cache?) on the MCP server with per-check progress and the fixed read-only
  reviewer policy. Add a Validate section to the review skill and plugin,
  docs/validate.md, and the CLI reference with the exit-code table.
- Feat(validate): hosted agent reviewers with read-only broker,
  grounding and consensus. [AdityaNG, Claude Fable 5.1]

  Add the reviewer adapter: each agent check runs CopperPilot in Ask mode
  against a read-only snapshot of the workspace through a Manual broker whose
  policy approves reads, scratch-folder writes and non-dangerous shell only.
  The snapshot is re-hashed after every check; any change outside scratch is
  an ERROR. Replies must end with a fenced json findings block (two repair
  turns, then ERROR); findings are grounded against the design index and
  ungrounded ones can never fail a check. per_part, per_sheet and per_netclass
  checks fan out in batches, repeat: N takes a majority consensus, and a
  non-retryable usage-limit block stops scheduling agent checks and exits 4.
  default.yaml gains the agent checks for S1-S8 and the CLI gains --effort,
  --concurrency and --repeat.
- Feat(validate): engine, builtin checks, reports, and validate
  list|graph|run. [AdityaNG, Claude Fable 5.1]

  Add the copper-pilot validate subcommand: a stage-gated graph of read-only
  checks loaded from a packaged default.yaml and extended by
  .copperpilot/validate.yaml. This slice ships the engine (config models and
  graph validation, design snapshot/hash/index, kicad-cli wrappers, dependency
  waves, stage/full/strict gating, result cache) with the deterministic builtin
  checks (intake.tools, intake.project, intake.intent, parts.bom_fields,
  sch.erc, pcb.parity, pcb.rules, pcb.drc, release.outputs, release.repo) and
  writes report.json, REPORT.md and next_actions.json under
  .copperpilot/validate/runs/<id>/ with a latest pointer.
- Merge pull request #20 from CopperPilot/feat/acp-session-updates.
  [Aditya]

  feat(acp): usage, plan, subagent, elicitation, and /effort session updates
- Merge origin/main into feat/acp-session-updates. [AdityaNG, Claude
  Fable 5.1]
- Merge pull request #19 from CopperPilot/feat/client-turn-durability.
  [Aditya]

  feat: make hosted turns durable across transport drops
- Fix(background): normalize CRLF shell output on Windows. [AdityaNG,
  Claude Fable 5.1]
- Feat: make hosted turns durable across transport drops. [AdityaNG,
  Claude Fable 5.1]

  Port the desktop client's chat-manager resilience into the shared graph
  node so every front-end (TUI, headless, MCP, ACP) benefits:

  - Retry a dropped stream up to three times with 5/10/20 s backoff and a
    stitched "Resume where we left off" re-prompt carrying the partial
    transcript, so the hosted agent continues instead of restarting.
  - Classify failures like `_categorizeError`: drops, timeouts, 5xx and 429
    retry; 4xx, invalid/oversized requests, plan refusals and auth fail fast.
  - Capture the websocket close code and reason in the error message and log,
    including drops that happen while a tool result is being sent.
  - Checkpoint partial progress (text, tool calls, results) on a terminal
    failure or cancellation with a hidden interruption note, so a later
    manual "continue" has the prior work in context.
  - Surface retry progress as `copper.retry` events: ACP agent thoughts, TUI
    status/notification, headless yellow lines, MCP progress.
  - Honour the desktop foreground-to-background shell contract: commands
    outliving their timeout keep running, the turn continues, and their
    output is fed back in a continuation turn after the final frame.
  - Drop the 300 s idle-frame timeout; websocket keepalive still detects dead
    peers and long hosted reasoning no longer trips a client limit.
  - Fix the ACP cancel race where a server close after `session/cancel`
    surfaced as an error instead of `cancelled`.
- Fix(acp): read slash commands from the first prompt block. [AdityaNG,
  Claude Fable 5.1]

  T3 Code appends runtime context as a second text block, so /effort was
  reaching the hosted agent instead of being answered locally.
- Fix(acp): elicit option questions, unwrap T3 prompts, bound question
  waits. [AdityaNG, Claude Fable 5.1]

  T3 Code in Full access auto-selects the first option of a permission prompt,
  so clarifying questions with options now use form elicitation (enum) when the
  client supports it. /effort is recognised inside T3's <user_request> envelope.
  Questions nobody answers within 100 seconds are skipped with a note, because
  the hosted service drops a turn whose tool result takes longer than two minutes.
- Feat(acp): usage, plan, subagent, elicitation, and /effort updates.
  [AdityaNG, Claude Fable 5.1]

  Emit usage_update from hosted token counts so T3 Code's context meter works,
  project hosted todo_list events and .copperpilot/plans artifacts onto ACP plan
  entries, render Intelligence survey workers as think tool calls, collect
  free-text clarifications through ACP form elicitation when the client supports
  it, and advertise a locally handled /effort command.


0.1.9 (2026-10-16)
------------------
- Release: version 0.1.9 🚀 [AdityaNG]
- Merge pull request #18 from CopperPilot/feat/acp-server. [Aditya]

  feat: add `copper-pilot acp` Agent Client Protocol server
- Fix(acp): surface hosted turn failures in the JSON-RPC error message.
  [AdityaNG, Claude Fable 5.1]

  T3 Code showed a bare "Internal error" for a prompt the hosted service
  had refused with "Upgrade to a paid plan to use Intelligence." because the
  explanation only lived in the error's `data`, which T3 does not display.
  Carry the hosted message in the error `message` (keeping the code), map a
  refused websocket handshake to auth_required when it is 401/403, log every
  failed turn to the diagnostics log, and never let an unexpected exception
  escape to the SDK's generic handler.
- Fix(acp): accept `copper-pilot acp auth login` for clients that append
  login args. [AdityaNG, Claude Fable 5.1]

  T3 Code runs an ACP terminal auth method by appending the method's args to
  the configured agent command, so the advertised `auth login` was spawned as
  `copper-pilot acp auth login`. The acp parser rejected the extra arguments
  with exit code 2 and T3 reported "The provider login command did not finish
  successfully." Normalize `acp auth ...` onto `auth ...` before parsing and
  document the alias.
- Feat: add `copper-pilot acp` Agent Client Protocol server. [AdityaNG,
  Claude Fable 5.1]

  Serve the hosted CopperPilot agent over ACP stdio so editors such as Zed
  can drive it with streaming messages, tool calls, permission prompts,
  session modes (ask/plan/agent), an effort config option, session/load
  resume of existing threads, and session/cancel.

  - New copper_acp.py built directly on the agent-client-protocol SDK;
    approvals and clarifying questions map onto session/request_permission.
  - `acp` optional extra; the command prints an install hint without it.
  - CI license gate ignores agent-client-protocol (Apache-2.0 LICENSE file,
    no license metadata in the wheel).
  - Tests: tests/test_acp.py (100% coverage of copper_acp.py, including a
    real stdio JSON-RPC round trip), parser and entrypoint tests, and a
    release-gate check for the extra.
  - Docs: cli.md, harness.md (Zed setup), README.


0.1.8 (2026-10-14)
------------------

Fix
~~~
- Annotate async context managers for ty 0.0.87. [AdityaNG]

  CI resolves the unpinned ty dependency to 0.0.87, which rejects AsyncIterator returns on asynccontextmanager.
- Upgrade the pinned Deep Agents runtime to 0.7.19. [AdityaNG]

  Keep the exact runtime pin while picking up the filesystem and media-handling fixes from 0.7.14.
- Align the CLI with hosted usage limits. [AdityaNG]

  Keep blocked turns machine-readable, shrink oversized resumed chats to the desktop frame budget, and report the installed client version.

Other
~~~~~
- Release: version 0.1.8 🚀 [AdityaNG]
- Merge pull request #16 from CopperPilot/update/14_oct_2026. [Aditya]

  Add Intelligence effort, hosted survey workers, and modal keyboard focus
- Fix(tests/test_tui.py) [AdityaNG]
- Feat(src/copper_pilot_cli/copper_status_events.py) [AdityaNG]
- Merge pull request #3 from
  CopperPilot/dependabot/github_actions/softprops/action-gh-release-3.
  [Aditya]

  build(deps): bump softprops/action-gh-release from 2 to 3
- Build(deps): bump softprops/action-gh-release from 2 to 3.
  [dependabot[bot]]

  Bumps [softprops/action-gh-release](https://github.com/softprops/action-gh-release) from 2 to 3.
  - [Release notes](https://github.com/softprops/action-gh-release/releases)
  - [Changelog](https://github.com/softprops/action-gh-release/blob/master/CHANGELOG.md)
  - [Commits](https://github.com/softprops/action-gh-release/compare/v2...v3)

  ---
  updated-dependencies:
  - dependency-name: softprops/action-gh-release
    dependency-version: '3'
    dependency-type: direct:production
    update-type: version-update:semver-major
  ...
- Merge pull request #4 from CopperPilot/dependabot/pip/websockets-
  gte-15-and-lt-18. [Aditya]

  build(deps): update websockets requirement from <17,>=15 to >=15,<18
- Build(deps): update websockets requirement from <17,>=15 to >=15,<18.
  [dependabot[bot]]

  Updates the requirements on [websockets](https://github.com/python-websockets/websockets) to permit the latest version.
  - [Release notes](https://github.com/python-websockets/websockets/releases)
  - [Commits](https://github.com/python-websockets/websockets/compare/15.0...17.1)

  ---
  updated-dependencies:
  - dependency-name: websockets
    dependency-version: '17.1'
    dependency-type: direct:production
  ...
- Merge pull request #5 from CopperPilot/dependabot/pip/uuid-utils-
  gte-0.12-and-lt-2. [Aditya]

  build(deps): update uuid-utils requirement from <1,>=0.12 to >=0.12,<2
- Build(deps): update uuid-utils requirement from <1,>=0.12 to
  >=0.12,<2. [dependabot[bot]]

  Updates the requirements on [uuid-utils](https://github.com/aminalaee/uuid-utils) to permit the latest version.
  - [Release notes](https://github.com/aminalaee/uuid-utils/releases)
  - [Commits](https://github.com/aminalaee/uuid-utils/compare/0.12.0...1.0.0)

  ---
  updated-dependencies:
  - dependency-name: uuid-utils
    dependency-version: 1.0.0
    dependency-type: direct:production
  ...
- Merge pull request #2 from
  CopperPilot/dependabot/github_actions/actions/setup-python-7. [Aditya]

  build(deps): bump actions/setup-python from 5 to 7
- Build(deps): bump actions/setup-python from 5 to 7. [dependabot[bot]]

  Bumps [actions/setup-python](https://github.com/actions/setup-python) from 5 to 7.
  - [Release notes](https://github.com/actions/setup-python/releases)
  - [Commits](https://github.com/actions/setup-python/compare/v5...v7)

  ---
  updated-dependencies:
  - dependency-name: actions/setup-python
    dependency-version: '7'
    dependency-type: direct:production
    update-type: version-update:semver-major
  ...
- Merge pull request #13 from
  CopperPilot/update/deepagents_0_7_19_3_oct. [Aditya]

  Upgrade the pinned Deep Agents runtime to 0.7.19
- Merge pull request #1 from
  CopperPilot/dependabot/github_actions/actions/checkout-7. [Aditya]

  build(deps): bump actions/checkout from 4 to 7
- Build(deps): bump actions/checkout from 4 to 7. [dependabot[bot]]

  Bumps [actions/checkout](https://github.com/actions/checkout) from 4 to 7.
  - [Release notes](https://github.com/actions/checkout/releases)
  - [Changelog](https://github.com/actions/checkout/blob/main/CHANGELOG.md)
  - [Commits](https://github.com/actions/checkout/compare/v4...v7)

  ---
  updated-dependencies:
  - dependency-name: actions/checkout
    dependency-version: '7'
    dependency-type: direct:production
    update-type: version-update:semver-major
  ...
- Merge pull request #12 from
  CopperPilot/update/contract_alignment_3_oct. [Aditya]

  Align the CLI with hosted usage limits


0.1.7 (2026-10-03)
------------------
- Release: version 0.1.7 🚀 [AdityaNG]
- Merge pull request #11 from
  CopperPilot/feat/python_versions_support_expansion. [Aditya]

  Support Python 3.11 and future releases
- Ci: probe Python 3.15 weekly instead of on pull requests. [AdityaNG]

  Keep the upstream install failure visible without turning required pull request checks red.
- Feat(py11) [AdityaNG]
- Merge pull request #7 from CopperPilot/feat/example_002_cm5_camera.
  [Aditya]

  feat(examples/002_cm5_camera): add 3D pose audit and generated carrier
- Fix(examples/002_cm5_camera): render the README PCB as a dimetric 3D
  view. [AdityaNG, Cursor]

  Use an orthogonal high-quality camera with a slight isometric tilt so the
  hero PNG shows connector height instead of a flat top-down shot.
- Fix(examples/002_cm5_camera): skip library dirs relative to the
  workspace. [AdityaNG, Cursor]

  Absolute path parts named tmp (Linux /tmp pytest roots) were skipping the
  entire haystack, so libraries_ready failed on Ubuntu CI.
- Feat(examples/002_cm5_camera): add 3D pose audit and generated
  carrier. [AdityaNG, Cursor]

  Lock connector 3D offsets, Module1 pad orientation, and the generated
  KiCad project so the example reproduces NANO-C assembly parity.
- Feat(examples/002_cm5_camera/main.py): new example. [AdityaNG]


0.1.6 (2026-09-18)
------------------
- Release: version 0.1.6 🚀 [AdityaNG]
- Merge pull request #6 from CopperPilot/feat/plugins. [Aditya]

  feat: add MCP conductor and Claude Code plugin
- Clarify server restrictions in PR template. [Aditya]

  Updated checklist item to clarify server restrictions.
- Feat: add MCP conductor and Claude Code plugin. [AdityaNG, Cursor]

  Let Claude Code, Cursor, and Codex delegate electronics work to the hosted agent via copper-pilot mcp, a portable review skill, and an in-repo plugin.


0.1.5 (2026-09-17)
------------------
- Release: version 0.1.5 🚀 [AdityaNG]
- Fix(README.md) [AdityaNG]


0.1.4 (2026-09-17)
------------------
- Release: version 0.1.4 🚀 [AdityaNG]
- Fix(tests/test_release_gates.py) [AdityaNG]


0.1.3 (2026-09-17)
------------------
- Release: version 0.1.3 🚀 [AdityaNG]
- Docs. [AdityaNG]


0.1.2 (2026-09-17)
------------------
- Release: version 0.1.2 🚀 [AdityaNG]
- Ci(pyproject.toml) [AdityaNG]
- Fix(tests/test_tui.py) [AdityaNG]


0.1.1 (2026-09-17)
------------------
- Release: version 0.1.1 🚀 [AdityaNG]


0.1.0 (2026-09-17)
------------------
- Release: version 0.1.0 🚀 [AdityaNG]
- Fix(tests/test_release_gates.py) [AdityaNG]
- Test. [AdityaNG]
- Docs. [AdityaNG]
- Docs. [AdityaNG]
- Example(examples/001_esp32/) [AdityaNG]
- Makefile. [AdityaNG]
- Ci. [AdityaNG]
- Docs. [AdityaNG]
- Refactor(name) [AdityaNG]
- Init. [AdityaNG]


