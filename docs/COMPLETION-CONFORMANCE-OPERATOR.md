# Completion Conformance — operator guide (T1 Fixture, T2 Observe, T3 Full)

Companion to the design doc
[`docs/superpowers/specs/2026-07-26-completion-conformance-harness-design.md`](superpowers/specs/2026-07-26-completion-conformance-harness-design.md)
and the plan
[`docs/superpowers/plans/2026-07-26-completion-conformance-t2-observe.md`](superpowers/plans/2026-07-26-completion-conformance-t2-observe.md).
This doc is the "how do I actually run it" reference; the design doc is the "why does it
exist" reference.

## What T1 and T2 are

| Tier | What's synthetic | What's real | Spends capacity? |
|---|---|---|---|
| **T1** Fixture tick | Entire PR world (`scenario.json`) | `classify_completion` + `plan_effect` + `run_completion_tick` | No |
| **T2** Observe | Failure reason via `scenario.json`, on a real sandbox PR | GitHub checks/review/queue, real hydration, Autopilot decision, up to the assignment | **No** — this is the whole point |
| **T3** Full loop | Same scenarios | T2 + `start_task` + runner boot + push re-entry | Yes, curated and hard-budgeted |

T2's contract: hydrate a **real** GitHub PR on the sandbox `conformance` project, run it
through the same production classifier/planner/driver T1 uses, then **stop** before any
mutating effect reaches `task_execution.start_task` or `gh`. The stop is enforced in code
(`tests/conformance/observe/adapters.py::ObserveEffectAdapters`), not by a flag on
`start_task` — no dry-run mode was added to `start_task` itself.

## Layout

```
tests/conformance/
  _shared.py                 # scenario load/validate, snapshot builder, hermetic patches
                              #   (shared by T1 and T2 fixture mode; not a test itself)
  scenario.schema.json        # scenario.v1 JSON Schema
  scenarios/*.json             # the seed catalog (T1 + T2 fixture mode both read these)
  test_completion_conformance.py   # T1 — run standalone or via pytest
  test_observe_conformance.py      # T2 — run standalone or via pytest (BUG-181)
  observe/
    adapters.py               # ObserveEffectAdapters — capture, never call out
    scoreboard.py             # scoreboard row + expected_human/unexpected_human
    assignment_preview.py     # what start_task *would* have received, from a plan
    runner.py                 # run_observe_tick — fixture mode + live mode
    reaper.py                 # gh cleanup for one run_id
    coverage.py               # axis coverage (same idea as T1)

scripts/completion_conformance/
  cli.py                      # validate / observe-fixture / matrix-seed / reaper
  github_sandbox.py           # opens sandbox branches/PRs for a live matrix run (CLI-only)
```

## Fixture mode (default — hermetic, merge-gate safe)

No network, no `gh`, no capacity. Same guarantee as T1.

```bash
# Run the standalone test files directly (what CI does — see scripts/switchboard_ci.sh,
# which auto-discovers every test_*.py and runs it with `python <file>`).
python3.11 tests/conformance/test_completion_conformance.py
python3.11 tests/conformance/test_observe_conformance.py

# Or via the operator CLI, against the same seed catalog:
python3.11 -m scripts.completion_conformance validate
python3.11 -m scripts.completion_conformance observe-fixture
```

`observe-fixture` prints one scoreboard row (JSON) per scenario plus a summary line. Exit
code is non-zero if any row is `fail` or `timeout`.

## Growing the catalog: `matrix-seed`

`matrix-seed` reports axis cells (draft × CI × mergeability × review × queue × runner role)
that no scenario file covers yet:

```bash
python3.11 -m scripts.completion_conformance matrix-seed
```

With `--emit DIR`, it **asks the real classifier** what it decides for each undefined
cell's world (via `classify_completion` + `plan_effect`, the same pure functions
`run_completion_tick` calls) and writes a draft `scenario.json` per cell. This declares an
*outcome*, it does not invent a bug — per the design doc's scenario-contract rule. Review
every generated file before adding it to the merge-gated `scenarios/` catalog; the
generator has no opinion on whether the current behavior for a cell is actually correct,
only on what it currently is.

```bash
python3.11 -m scripts.completion_conformance matrix-seed --emit /tmp/conformance-draft
```

## Live mode (CLI only — never merge-gate CI)

Live mode points the **real** `completion_driver.hydrate_completion_snapshot` at a task on
the sandbox Switchboard project (`conformance`), so it sees actual GitHub PR/check/review/
queue state, while every mutating effect (`start_remediation`, `enqueue`, `mark_ready`, ...)
is still cut by `ObserveEffectAdapters`. It is gated behind an explicit environment flag so
CI stays hermetic even if a live test were accidentally imported:

```bash
export CONFORMANCE_LIVE=1
python3.11 - <<'PY'
from tests.conformance.observe.runner import run_observe_tick_live

outcome = run_observe_tick_live(
    "CONF-1",                # task on the conformance board tracking the sandbox PR
    project="conformance",   # never "switchboard" / "atlas" — enforced in code
    actor="you",
    agent_id="you",
)
print(outcome["tick"]["decision"])
print(outcome["receipts"])   # what would have been dispatched -- nothing was
PY
```

`run_observe_tick_live` (and `run_observe_tick(task_id=...)`) refuse to run at all unless
`CONFORMANCE_LIVE=1` is set, and refuse `project="switchboard"` / `"atlas"` even with the
flag set — isolation rule #1 from the design doc, enforced in code rather than left to
operator discipline.

**Prerequisites for a real live run (tracked as follow-on work, not yet exercised in this
change):**

1. Switchboard project `conformance` exists and is bound to
   `github_repo=6th-Element-Labs/switchboard-conformance` (already done — see COORD-66).
2. The sandbox repo has a webhook wired to the `conformance` project so a completion tick
   has something durable to hydrate against.
3. A task exists on the `conformance` board whose `git_state` points at the opened sandbox
   PR (`scripts/completion_conformance/github_sandbox.py` opens the PR; wiring it to a task
   is a T2 follow-on, not built in this cut).

## Opening a sandbox scenario matrix (CLI-only, never in tests)

`scripts/completion_conformance/github_sandbox.py` commits `scenario.json` to a fresh
branch (`conformance/{run_id}/{scenario_id}`) and opens a PR via `gh`, against **the sandbox
repo only** — it never defaults to, or accepts, the product canonical repo. It is not
imported by any test and never runs in CI.

```python
from scripts.completion_conformance.github_sandbox import open_scenario_prs
from tests.conformance._shared import load_scenarios
from pathlib import Path

opened = open_scenario_prs(
    load_scenarios(Path("tests/conformance/scenarios")),
    repo="6th-Element-Labs/switchboard-conformance",
    run_id="2026-07-26-nightly-1",
    checkout_dir="/path/to/a/clean/clone/of/switchboard-conformance",
)
```

### Required-status-context bound (read before configuring branch protection)

The sandbox repo's branch protection is expected to require **exactly one** status
context: `"Switchboard Conformance / scenario"`. One obedient GitHub Actions workflow
(not built in this cut — a T2 follow-on) reads the checked-out `scenario.json`
(`world.ci` / `timing`) and reports that single context as pass / fail / pending-forever
(hang) / never-reporting, depending on the scenario.

**Limitation:** `timing.never_report` (a check that is requested but never posts a
conclusion) is only interesting to the merge queue / classifier if that context is in the
**required** list. If an operator widens branch protection to require additional contexts
beyond `"Switchboard Conformance / scenario"`, `never_report` scenarios targeting the
*other* contexts will silently not exercise the timeout path the design doc calls out.
Keep the sandbox repo to the single required context unless a scenario explicitly needs to
prove multi-context behavior.

## Cleaning up after a live run: `reaper`

```bash
python3.11 -m scripts.completion_conformance reaper \
  --run-id 2026-07-26-nightly-1 \
  --repo 6th-Element-Labs/switchboard-conformance \
  --task-id CONF-1 --task-id CONF-2 \
  --dry-run          # drop this flag to actually close PRs / delete branches
```

- Finds every open PR whose branch matches `conformance/{run_id}/*`, closes it, and deletes
  the branch (`gh pr close --delete-branch`).
- If `gh` is not on `PATH`, it does not fail — it prints what it *would* have searched for
  and deleted (`gh_available: false` in its JSON report) so a dry environment can still
  show the plan.
- Task archival goes through the Switchboard MCP `archive_task` tool, which this
  in-process script cannot call directly. Pass `archive_task=` (a callable) when calling
  `tests.conformance.observe.reaper.reap` from an MCP-aware caller; otherwise the CLI
  reports a `to_archive` list of task ids for an operator/agent to archive afterwards.

## T3 Full nightly contract

T3 is the external-port orchestration in `tests/conformance/full/runner.py`. The scheduled
sandbox job binds its ports to existing Switchboard surfaces:

- `start` calls only `start_task` on project `conformance`, carrying the run id and scenario;
- `observe` reads Task Execution terminal, role sequence, generations, reason codes, and
  attributed spend;
- `stop_run` asks the capacity plane to fence/stop every live runner tagged with the run id.

The curated pack is `tests/conformance/full/curated.json` (11 scenarios; the implementation
rejects more than 12). Runs are restricted to project `conformance` and repository
`6th-Element-Labs/switchboard-conformance`, with at most two concurrent boots. Schedule the
caller nightly outside the working day; the repository merge gate remains hermetic.

The spend envelope is a kill switch, not a warning. Configure `max_spend_usd` before launch.
Every observation adds newly attributed spend atomically. Crossing the cap aborts the run and
invokes `stop_run`; wall-clock expiry does the same. The run scoreboard adds `generation_count`
and `spend_usd` per scenario, and summarizes timeout, unexpected-human, and over-generation
rows. Do not accept an unattended run whose stop port is unbound.

T3 asserts only terminal state and the complete role sequence. It never grades intermediate
routes on a live clock.

## Isolation rules (enforced in code where practical)

1. Conformance never writes to `switchboard` or `atlas` live boards — `run_observe_tick_live`
   raises if `project` is one of those.
2. Conformance never opens PRs against the product canonical repo — `github_sandbox.py`
   takes `repo` as a required argument with no product default.
3. T2/T3 use a dedicated GitHub app / webhook path or a clearly namespaced project binding
   (`conformance`), never the product project's webhook.
4. `observe-fixture` and `matrix-seed` never touch the network — see
   `scripts/ci_hermeticity_lint.py`, which both test files pass.

## Gold decision catalog (optional diagnostic)

`tests/conformance/gold_decisions.py` replays the **gold** slice of the completion decision catalog (`tests/conformance/scenarios/gold/`) and fails when Autopilot routing drifts from the state-machine scar documented in `docs/AUTOPILOT-COMPLETION-STATE-MACHINE.md`.

This suite is **optional** — a local/diagnostic check, **not** merge-gated. `scripts/switchboard_ci.sh` only auto-discovers files named `test_*.py` or `*_test.py`, so `gold_decisions.py` is not run in Switchboard CI. Run it manually when you change completion routing or the state machine:

```bash
python3.11 tests/conformance/gold_decisions.py
```

Regenerate fixtures after changing axes or priorities:

```bash
python3.11 scripts/completion_conformance/build_gold_catalog.py
```

See `tests/conformance/GOLD_COVERAGE.md` for coverage counts and priority assumptions.
