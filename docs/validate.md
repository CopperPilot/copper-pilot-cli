# Validate

`copper-pilot validate` runs a stage-gated graph of narrow, read-only reviewers
over a KiCad workspace. Deterministic checks call `kicad-cli`; the others are
hosted CopperPilot Ask turns, each scoped to one concern. It never modifies
schematic, PCB, library or project files: reviewers work on a read-only
snapshot behind a broker that only permits writes under a scratch folder, and
any change to the snapshot turns that check into an ERROR.

```console
copper-pilot validate [workspace] [flags]
copper-pilot validate list    [workspace] [--config PATH]
copper-pilot validate graph   [workspace] [--config PATH]
copper-pilot validate explain CHECK_ID [workspace] [--config PATH]
```

Validate is non-interactive. It uses the stored device-login credential and
never opens a browser. Builtin-only selections (`--only sch.erc`) need no
login.

## Flags

| Flag | Meaning |
|------|---------|
| `--config PATH` | Graph file layered on the packaged default and `.copperpilot/validate.yaml`. |
| `--context FILE ...` | Extra reviewer context (requirements, fab checklist, review prompt). Appended to every agent prompt under "Reference material". |
| `--profile dev\|release` | Severity policy. `dev` fails on P0/P1 at medium confidence; `release` fails on P0–P2 at any confidence. |
| `--gate stage\|full\|strict` | `stage` (default) finishes the current stage then stops on a blocking failure; `full` runs everything not blocked by a failed prerequisite; `strict` stops after the first failing wave. |
| `--until STAGE` | Last stage to run, e.g. `S3`. Later checks are SKIPPED. |
| `--only ID[,ID...]` | Run these checks plus their hard prerequisites. Others are SKIPPED. |
| `--skip ID[,ID...]` | Skip checks. Dependents still run. |
| `--effort balanced\|intelligence` | Default effort for agent checks; a per-check `effort:` wins. |
| `--concurrency N` | Concurrent hosted turns (default 3). |
| `--no-cache` | Ignore cached results. |
| `--repeat N` | Consensus runs for every agent check (overrides the graph). |
| `--json` | Stream newline-delimited JSON events to stdout. |
| `--junit PATH` | Also write JUnit XML. |
| `--out DIR` | Output directory (default `.copperpilot/validate/`). |
| `--emit-fix-prompt` | Also print a ready message for `copper-pilot -n --mode agent -y -m "..."`. |

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | PASS: every selected check is PASS or WARN. |
| 1 | FAIL: at least one blocking FAIL or ERROR. |
| 2 | Usage or config error: bad flags, invalid YAML, a dependency cycle, unknown check or stage. |
| 3 | INCOMPLETE: no failures, but checks were SKIPPED because of `--gate`, `--until` or `--only`. |
| 4 | Environment: not logged in, or a non-retryable usage-limit block. |
| 130 | Interrupted. A partial report marked `"interrupted": true` is written. |

A missing or unsupported `kicad-cli` makes `intake.tools` FAIL, everything
else SKIPPED, and the run exits 1 with the cause first in `next_actions`.

## Outputs

Each run writes `.copperpilot/validate/runs/<UTC-timestamp>-<short-hash>/`, and
`.copperpilot/validate/latest` points at it.

| File | Contents |
|------|----------|
| `report.json` | `{schema: "copper-pilot.validate/v1", run_id, verdict, profile, gate, design_hash, cli_version, kicad_version, started, finished, interrupted, exit_code, results: [CheckResult]}`. |
| `REPORT.md` | A table of every check (status, P0–P3 counts, notes), then each finding with Where / Why / Evidence / Rule / Action and the reviewer thread IDs. |
| `next_actions.json` | `{stage, stage_title, actions: [≤15, worst first], more, checks_blocked_downstream, rerun}`. Only the earliest failing stage, only grounded findings from blocking checks. |
| `junit.xml` | With `--junit`. One testcase per check: FAIL → failure, ERROR → error, SKIPPED → skipped (naming the blocker), WARN → passed with system-out. |
| `checks/<id>/` | Builtin reports (`erc.json`, `drc.json`, `netlist.xml`, Gerbers), and the prompt and raw reply of every agent slice. |

`CheckResult` is `{check_id, stage, title, status, gate, findings[], summary,
blocked_by[], threads[], cache_key, cached, seconds, error}`. Finding IDs are
`<check_id>/<nnn>`.

### Finding

Every agent reviewer must end its reply with one fenced `json` block:

```json
{"findings": [
  {"severity": "P0|P1|P2|P3", "title": "one line", "detail": "what and why",
   "locator": {"sheet": "/power/", "refs": ["U3"], "nets": ["+3V3"], "layer": "F.Cu",
               "x_mm": 41.2, "y_mm": 18.0},
   "evidence": "datasheet §8.2 Table 5 / report line / calculation",
   "rule": "standard, skill rule or fab limit", "suggested_action": "the fix, not applied",
   "confidence": "high|medium|low"}
 ],
 "summary": "one or two sentences"}
```

Severity: P0 = will not work, unsafe, or the fab will reject it; P1 =
reliability, yield or assembly risk; P2 = an expert reviewer would flag it;
P3 = polish. A reply without a valid block gets up to two repair turns in the
same thread; if it is still invalid the check is an ERROR.

### Check status

- **FAIL**: the check is blocking and has at least one grounded finding whose
  severity is in the profile's `fail_on` at confidence ≥
  `min_confidence_to_fail`.
- **WARN**: there are other findings. Advisory checks never go past WARN.
- **PASS**: no findings.
- **ERROR**: the check could not produce a trustworthy answer (kicad-cli
  failure, invalid reply, modified snapshot, hosted error). ERROR never counts
  as passing and is never cached.
- **SKIPPED**: a hard prerequisite failed, a gate stopped the run, or the
  selection excluded it. `blocked_by` names the cause.

### Grounding

Every `refs`, `nets` and `layer` in a finding is checked against the design
index (netlist export, project file, PCB layer table). A finding that names
something that does not exist is marked `grounded: false`, prefixed
`[UNGROUNDED: …]`, and cannot fail a check or appear in `next_actions`.

## `--json` events

Newline-delimited objects in the chat `--json` envelope style:

```json
{"type": "validate.start", "data": {"run_id": "...", "checks": [...], "stages": [...]}}
{"type": "validate.check", "data": {"check_id": "sch.erc", "stage": "S3", "status": "running"}}
{"type": "validate.result", "data": {"check_id": "sch.erc", "status": "FAIL", ...}}
{"type": "copper.retry", "data": {"check_id": "arch.review", "stage": "scheduled", ...}}
{"type": "validate.end", "data": {"verdict": "FAIL", "report": "...", "next_actions": "...", "exit_code": 1}}
```

Without `--json`, one progress line per result goes to stderr and the verdict
line plus report paths go to stdout.

## Fix loop

```console
copper-pilot validate --json --gate stage
# read .copperpilot/validate/latest/next_actions.json
copper-pilot validate --emit-fix-prompt      # prints the fix message
copper-pilot -n --mode agent -y -m "<that message>"
copper-pilot validate --until S3             # the `rerun` field
```

The fix message says: fix only these items, cite each finding ID in its
change, do not loosen design rules or exclude violations, then re-run validate
for that stage.

## Execution model

- Stages run in order. Inside a stage, checks run in dependency waves
  (`requires` + `after`). In each wave builtin checks run first, then agent
  checks run concurrently up to `--concurrency`.
- A check whose hard prerequisite is FAIL, ERROR (with
  `error_blocks_dependents`) or gate-SKIPPED is SKIPPED with `blocked_by`
  naming it.
- **Snapshot.** Before the first agent check the workspace is copied to a
  temporary snapshot (excluding `.git`, `.copperpilot`, `*-backups`, lock
  files and caches) with every file read-only and one writable
  `.validate-scratch/` folder. Reviewers run as
  `CopperPilotAgent(snapshot, mode=ask)` behind a Manual broker whose policy
  approves reads, approves writes and edits only under the scratch folder,
  approves shell only when `dangerous_shell_command()` is false, and rejects
  deletes and unknown tools. `ask_user_question` is rejected with a message
  telling the model to state its assumptions. After each agent check the
  snapshot is re-hashed; any change outside scratch is an ERROR.
- **Fan-out.** `per_part` groups non-DNP parts by LCSC, else MPN, else
  value+footprint so identical parts are reviewed once; `per_sheet` is one
  item per sheet; `per_netclass` one per net class. `batch` sets items per
  hosted turn and slices run concurrently.
- **Consensus.** `repeat: N` runs N independent threads and keeps findings
  reported by a majority, matched by locator (refs/nets/sheet, else a
  normalized title); the worst severity wins.
- **Durability.** Hosted turns run through the same checkpointed graph as
  chat, so retries, backoff and `copper.retry` events apply. A usage-limit
  error (`http_402`, or `429` with a `block_reason`) makes that check an
  ERROR; when it is not retryable the run stops scheduling agent checks, marks
  the rest SKIPPED (`blocked_by: ["usage limit: weekly"]`), writes the
  reports, and exits 4. Builtin checks still run.
- **Cache.** `.copperpilot/validate/cache/<key>.json`, where the key hashes the
  check spec, preamble, limits, profile, `--context` file hashes, the
  `--repeat`/`--effort` overrides, and the check's `inputs` (`sch` = all
  sheets, `pcb`, `pro`, `libs` = `.kicad_sym` + `.kicad_mod` + lib tables,
  `intent` = `.copperpilot/README.md`, `PLANS.md`, `plans/*.md`, `all` = the
  whole tree). A PCB-only change re-runs only PCB-dependent checks. `intake.*`
  checks always run.
- **Threads.** Reviewer threads are named `validate-<run>-<check>-<slice>-<n>`
  and are created against the snapshot path, so `copper-pilot threads list`
  only shows them with `--all`.

## Config (`validate.yaml`)

The packaged graph ships in the wheel. A project extends it from
`.copperpilot/validate.yaml`; `--config` layers one more file on top. With
`extends: default`, `checks` and `stages` merge by `id` (existing ids update
only the fields given, new ids append) and `policy`, `limits` and `reviewer`
deep-merge. Without `extends`, the file replaces the graph entirely.

```yaml
version: 1
extends: default

stages:
  - {id: S0, title: Intake, question: "Can the design be read, and is its intent known?"}

policy:
  default_profile: dev
  profiles:
    dev:     {fail_on: [P0, P1],     min_confidence_to_fail: medium}
    release: {fail_on: [P0, P1, P2], min_confidence_to_fail: low}
  error_blocks_dependents: true

limits:
  fab: jlcpcb
  jlcpcb: {min_clearance_mm: 0.127, min_track_mm: 0.127, min_via_diameter_mm: 0.45,
           min_drill_mm: 0.30, min_copper_edge_mm: 0.30, min_silk_text_height_mm: 1.0}

reviewer:
  preamble: |            # prepended to every agent check
    You are one read-only reviewer on a panel ...
  effort: balanced
  timeout_s: 1800

checks:
  - id: parts.footprints
    stage: S2
    title: Footprints match datasheets and LCSC package
    kind: agent             # agent | builtin
    scope: per_part         # project | per_part | per_sheet | per_netclass
    batch: 4                # items per hosted turn when fanned out
    requires: [intake.project]   # hard: SKIPPED if any of these fail or error
    after: []                    # soft: ordering only
    gate: blocking          # blocking | advisory
    inputs: [sch, pcb, libs]     # cache key inputs: sch|pcb|pro|libs|intent|all
    repeat: 1               # >1 = majority consensus
    effort: intelligence    # optional per-check effort
    prompt: |
      For each part below ... {items}

  - id: sch.erc
    stage: S3
    kind: builtin
    builtin: erc            # tools | project | intent | bom_fields | erc | parity | rules | drc | outputs | repo
    requires: [intake.project]

  # overrides (only with extends: default)
  - {id: pcb.silkscreen, enabled: false}
  - {id: sch.ratings, repeat: 3, effort: intelligence}
```

Validation rules, each exiting 2 with the offending check named: unique ids;
every stage, `requires` and `after` reference exists; no check depends on a
later stage; no cycles; agent checks have a `prompt`; builtin checks name a
registered builtin; a `{items}` placeholder is present when `scope` is not
`project`.

## Default graph

| Stage | Builtin checks (run first) | Agent checks |
|-------|----------------------------|--------------|
| S0 Intake | `intake.tools` (kicad-cli 9/10), `intake.project` (netlist export; parts, nets, sheets, layers indexed), `intake.intent` (advisory: project index and plan present) | — |
| S1 Architecture | — | `arch.review` (block diagram, power tree and budget, protection, clocks, boot path; intelligence effort) |
| S2 Parts | `parts.bom_fields` (Value, Footprint, MPN, LCSC `C#####`, Datasheet; DNP and mechanical parts exempt) | `parts.lcsc`, `parts.symbols`, `parts.footprints` (per part) |
| S3 Schematic | `sch.erc` | `sch.conventions` (per sheet), `sch.circuits` (per sheet; requires `arch.review`, `parts.symbols`, `sch.erc`), `sch.ratings` |
| S4 Netlist→layout | `pcb.parity` (schematic parity + unconnected items), `pcb.rules` (board rules and net classes ≥ fab limits) | — |
| S5 Placement | — | `pcb.placement` (requires `pcb.parity`, `parts.footprints`) |
| S6 Routing SI/PI | — | `pcb.power_routing`, `pcb.signal_integrity` (per net class) |
| S7 DFM/DFA | `pcb.drc` | `dfm.fab`, `dfa.assembly`, `pcb.silkscreen` (advisory) |
| S8 Release | `release.outputs` (Gerbers + drill; outline, drill and copper-layer counts), `release.repo` (no absolute user paths; project-local libraries) | `release.cpl` |

Builtin severities: ERC/DRC error → P1, warning → P2; parity error → P0,
parity warning (field or value drift) → P1; unconnected items → P0; a board
rule or net class below the fab limit → P1; a board rule left at 0 → P2.
ERC/DRC violations are grouped by type: one finding per type with a count and
up to 50 refs.

## MCP and Python

The MCP server exposes `validate(workspace?, profile?, gate?, until?, only?,
effort?, no_cache?)`, which returns `{ok, verdict, exit_code, report_path,
report_markdown, next_actions}` and sends per-check progress notifications.
The reviewer approval policy is fixed regardless of the host's mode.

```python
from copper_pilot_cli.validate import validate

report = validate("esp32", profile="release")
assert report.verdict == "PASS", report.next_actions
```

`validate()` returns a `ValidationReport` with `verdict`, `exit_code`,
`run_dir`, `report`, `next_actions`, `results`, `paths` and `findings`.
`avalidate()` is the async form.
