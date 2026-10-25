Changelog
=========


(unreleased)
------------

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


