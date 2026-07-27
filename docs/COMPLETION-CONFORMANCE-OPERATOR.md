# Completion Conformance — operator guide (T1 Fixture + T2 Observe)

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
| **T3** Full loop (not built yet) | Same scenarios | T2 + `start_task` + runner boot + push re-entry | Yes, curated/budgeted |

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
  _evidence.py                # COORD-78 world.evidence axis — runs the REAL merge_gate
                              #   with only DB/GitHub seams stubbed
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
    burst.py                  # concurrent burst runner + invariant scoreboard

scripts/completion_conformance/
  cli.py                      # validate / observe-fixture / matrix-seed / reaper
  github_sandbox.py           # opens sandbox branches/PRs for a live matrix run (CLI-only)
```

## Concurrency burst (COORD-68)

The burst suite is deliberately separate from the serial scenario scoreboard. The serial
matrix asks whether one decision is correct; the burst asks whether many decisions can
run together without duplicate capacity admission, raced coordinator ownership, leaked
runner rows, or a wedged host.

Run the hermetic Observe-mode proof (40 cases by default):

```bash
python3.11 tests/conformance/test_burst_conformance.py
python3.11 -m scripts.completion_conformance burst-fixture \
  --run-id local-burst --count 40 --concurrency 40
```

The burst scoreboard reports:

- boots requested, admitted, and completed;
- duplicate-boot and raced-coordinator-tick counts;
- orphaned runners, including the stricter failed-dispatch/live-runner count;
- wall-clock drain time; and
- whether a fresh post-burst canary boot succeeded.

Observe mode is the default and fails if any boot is admitted. The injected full-loop
runner refuses to start without both a positive capacity budget and
`operator_watching=True`; it is not a scheduled or unattended mode.

For real sandbox PR creation, use
`scripts.completion_conformance.github_sandbox.open_scenario_prs_concurrently`. It requires
one clean checkout path per scenario because concurrent branch creation cannot safely
share a git index. Branches retain the T2 `conformance/{run_id}/{scenario_id}` convention,
so the existing `reaper` command cleans a partially failed burst.

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

## The evidence axis: `world.evidence` (COORD-78)

The six axes above are all PR-world. The **board-evidence** family — the one the
last three completion outages came from — lives in an optional `world.evidence`
block:

| axis | values |
|---|---|
| `executed_test_run` | `valid` · `missing` · `near_miss_key` · `failed` · `stale_head` |
| `external_ci` | `green_exact_head` · `green_other_head` · `failed` · `pending` · `none` |
| `work_session` | `present` · `missing` · `borrowed` |
| `review_verdict` | `pass` · `missing` · `stale` · `open_findings` · `not_passed` |

```json
"world": {
  "draft": false, "ci": "pass", "review": "passed", "...": "...",
  "evidence": {
    "executed_test_run": "missing",
    "external_ci": "green_exact_head",
    "work_session": "present",
    "review_verdict": "pass"
  }
}
```

Two rules make this an axis rather than a label:

1. **The findings come from the real gate.** `_shared.build_snapshot` runs
   `switchboard.application.commands.merge_gate` for every scenario and passes
   its *actual* result into `build_completion_snapshot`. Only DB and GitHub
   seams are stubbed, at `merge_gate`'s own `_store_facade` indirection layer.
   Findings are **never** hand-built: `_merge_gate_finding` splats its `details`
   onto the finding, and two shipped-dead consumers (COORD-61, BUG-182) came
   from tests that hand-built the nested shape the gate never emits. Before
   COORD-78 this argument was the literal `{"findings": []}`.
2. **Omitting the block is an assertion, not a gap.** A scenario with no
   `evidence` gets the world derived by `_evidence.default_evidence` — clean
   evidence, with `external_ci` following `world.ci` so a red-CI scenario never
   claims a green receipt. That is why the 27 pre-COORD-78 scenarios still
   validate unchanged while now being graded against real gate output.

`world.evidence` only decides an outcome when the PR world is otherwise green:
`_finding_decision` runs after draft, exact-head CI and review in
`_classify_completion_base`. That is deliberate — it is exactly the state
COORD-57 sat in for 50 attempts. Write evidence scenarios on a clean PR world.

Per-axis coverage prints on every T1 run as `EVIDENCE_COVERAGE` lines (scored
over the seeds *and* the gold catalog), summarised as
`EVIDENCE_COVERAGE SUMMARY undefined=N`.

The three incident shapes are merge-gated T1 seeds, not gold-only:

- `coord57_evidence_derived_from_ci_receipt` — no agent-recorded run, green CI
  receipt on the exact head → derives, non-blocking, routes `review_merge`
  (ENFORCE-16 / PR #955);
- `coord57_evidence_missing_no_ci_receipt` — the negative control, without
  which "derives" is indistinguishable from "never demanded it";
- `co21_evidence_near_miss_key` — five suites recorded under `executed_tests`;
  asserts the decision *names* the near-miss key, which the reason code alone
  did not during the outage.

`tests/test_coord78_conformance_evidence_axis.py` guards the contract itself
(real gate, real finding shape, axis wired, schema matches the code).

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
