# Plan: Completion Conformance T2 Observe

- **Date:** 2026-07-26
- **Board:** `project=switchboard`, task `COORD-66`
- **Design:** [`2026-07-26-completion-conformance-harness-design.md`](../specs/2026-07-26-completion-conformance-harness-design.md)
- **Precedes:** T3 Full loop (not scoped here)
- **Status:** Landed (harness-side cut only — see scope note)

## Scope

Land the T2 Observe harness code + operator docs so a human can run real-sandbox observe
ticks without spending capacity (no `start_task` call, no Connect wake, no runner boot).
Explicitly a **harness-side cut**: no dry-run flag was added to `start_task` itself. The
observe/production boundary is entirely in which `CompletionEffectAdapters` get injected
into `run_completion_tick`.

Out of scope for this slice (tracked as T2 follow-on / T3 precondition):

- The obedient GitHub Actions workflow that makes the sandbox repo actually report
  `"Switchboard Conformance / scenario"` check conclusions per `scenario.json`.
  `github_sandbox.py` opens the PR; nothing in this repo authors that workflow yet.
- Wiring a live sandbox PR to a `conformance`-board task's `git_state` (needed before
  `run_observe_tick_live` has anything real to hydrate).
- A CLI subcommand that drives a full N-scenario live matrix end-to-end (`open PRs` →
  `poll` → `score` → `reap`). `github_sandbox.py`, `runner.py`, and `reaper.py` are the
  pieces; wiring them into one `matrix-run` command is future work once the workflow above
  exists to test against.

## What landed

```
tests/conformance/
  _shared.py                        # NEW — extracted from T1's test so T1/T2 share one
                                     #   scenario loader/validator, snapshot builder, and
                                     #   hermetic storage-patch context manager
  test_completion_conformance.py    # REFACTORED to use _shared (behavior unchanged —
                                     #   verified identical PASS/COVERAGE output)
  test_observe_conformance.py       # NEW — T2 unit + integration-style tests (BUG-181:
                                     #   runs standalone via `python <file>`)
  observe/
    __init__.py
    adapters.py                     # ObserveEffectAdapters
    scoreboard.py                   # build_row / classify_human / summarize
    assignment_preview.py           # preview_execution_assignment
    runner.py                       # run_observe_tick (fixture + live dispatch)
    reaper.py                       # gh cleanup for one run_id
    coverage.py                     # axis coverage report (T1's idea, reused)

scripts/completion_conformance/
  __init__.py
  __main__.py
  cli.py                            # validate / observe-fixture / matrix-seed / reaper
  github_sandbox.py                 # open sandbox branches/PRs (CLI-only, gh-backed)

docs/COMPLETION-CONFORMANCE-OPERATOR.md   # NEW — operator guide for both tiers
```

## Design decisions

1. **Reuse, don't fork.** `run_observe_tick_fixture` and `run_observe_tick_live` both call
   `switchboard.application.completion_driver.run_completion_tick` unmodified — the only
   thing T2 injects is `ObserveEffectAdapters().as_completion_adapters()` in place of
   `production_effect_adapters(...)`. The classifier, planner, and driver code paths are
   identical to what T1 and production dogfood exercise.
2. **The "stop" is the adapter, not a flag.** `start_task` and the `gh` mutation helpers in
   `production_effect_adapters` are simply never referenced by the observe path. There is
   no `dry_run=` parameter threaded through `task_execution.start_task` — per the task's
   constraint, this is a harness-side cut only.
3. **Fixture mode for T2 reuses T1's hermetic patches** (`_shared.hermetic_completion_patches`)
   so `observe-fixture` and `test_observe_conformance.py` have the same zero-network,
   zero-capacity guarantee as T1, and can run in the same merge-gate CI sweep
   (`scripts/switchboard_ci.sh` auto-discovers both `test_*.py` files).
4. **Live mode writes to the `conformance` project's own database normally** (decision
   records, completion_run projection) — only the *external* mutating effects are cut.
   This matches the design doc: "record planned effect + execution-assignment contract...
   then stop." Isolation against the live product boards is enforced in code
   (`FORBIDDEN_LIVE_PROJECTS` in `runner.py`), not left to operator discipline.
5. **`matrix-seed --emit` asks the real classifier**, not a human guess, what the current
   decision is for an uncovered axis cell, then writes that as a draft scenario. This keeps
   the "declare outcomes, don't invent bugs" rule from the design doc even for
   generator-authored scenarios; a human still reviews before merging a draft into the
   catalog.

## Verification

```
python3.11 tests/conformance/test_completion_conformance.py   # T1 — unchanged: 3 passed
python3.11 tests/conformance/test_observe_conformance.py      # T2 — 11 unit/integration tests
python3.11 -m scripts.completion_conformance validate
python3.11 -m scripts.completion_conformance observe-fixture
python3.11 -m scripts.completion_conformance matrix-seed --emit /tmp/... # spot-checked, not committed
```

See `docs/COMPLETION-CONFORMANCE-OPERATOR.md` for exact commands and live-mode
prerequisites once the sandbox repo's workflow + a `conformance`-board task exist.
