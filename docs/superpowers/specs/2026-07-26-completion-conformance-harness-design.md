# Completion Conformance Harness

- **Status:** T1 landed; T2 Observe landed under `COORD-66`; T3 bounded full-loop
  orchestration landed under `COORD-67` (see
  [`docs/superpowers/plans/2026-07-26-completion-conformance-t2-observe.md`](../plans/2026-07-26-completion-conformance-t2-observe.md)
  and [`docs/COMPLETION-CONFORMANCE-OPERATOR.md`](../../COMPLETION-CONFORMANCE-OPERATOR.md))
- **Date:** 2026-07-26
- **Board:** `project=switchboard`
  - Deliverable: `completion-conformance-harness`
  - Task: `COORD-65` (T1 fixture tick) — Done
  - Task: `COORD-66` (T2 Observe harness) — harness-side cut landed; obedient sandbox
    workflow + live wiring are follow-on work (see the T2 plan doc)
  - Task: `COORD-67` (T3 Full) — curated, isolated, concurrency-capped, and
    protected by a hard spend envelope
- **Related:** [AUTOPILOT-COMPLETION-STATE-MACHINE.md](../../AUTOPILOT-COMPLETION-STATE-MACHINE.md),
  [COMPLETION-LIFECYCLE-PIPELINE.md](../../COMPLETION-LIFECYCLE-PIPELINE.md),
  ADR-0008 three-plane separation

## Name

**Completion Conformance Harness** (short: **conformance**).

- Package / CLI: `completion_conformance` / `python -m completion_conformance`
- Scenario pack dir: `conformance/scenarios/`
- Switchboard project (Tier-2+): `conformance` (never `switchboard` / `atlas` live boards)
- Sandbox GitHub repo (Tier-2+): org-owned throwaway, e.g. `6th-Element-Labs/switchboard-conformance`

Alternate names considered and rejected: “scenario lab” (vague), “CI fuzzer” (implies
randomness), “shadow Autopilot” (implies a second daemon).

## Problem

We need proof that the completion state machine and Autopilot orchestration stay
aligned across the many ways a PR can look: CI red/green/pending/missing, draft,
conflict, review requested, merge-queue dequeue, head move, closed-under-runner, etc.

Live dogfood cannot be that proof: it creates blocked/wedged tasks and contends for
hosts. Pure unit tests alone miss real GitHub payload shapes and concurrency.

## Decision

One **scenario vocabulary** (`scenario.json`) drives three tiers. The only synthetic
thing is *why* evidence is unhappy. Downstream machinery is as real as the tier allows.

| Tier | Name | Synthetic | Real | When |
|---|---|---|---|---|
| **T1** | Fixture tick | Entire PR world | `classify` + `plan_effect` + `run_completion_tick` | Every PR / merge gate |
| **T2** | Observe | Failure reason via `scenario.json` workflow | GitHub checks, protection, queue, webhooks, Autopilot decision up to assignment | Post-merge or nightly matrix |
| **T3** | Full loop | Same scenario files | T2 + DHCP `start_task` + runner boot + push re-entry | Curated dozen nightly |

**Build order:** T1 first → T2 Observe → T3 Full. Same scenario schema throughout.

## Non-goals

- Testing real product code under test (ActionEngine, etc.)
- Running conformance on the live `switchboard` board or canonical product repo
- Asserting intermediate routes mid-flight on real clocks (flaky by design)
- Replacing DHCP dogfood observation (runners stay operator-watched separately)
- A second Autopilot daemon or forked completion codebase

## Scenario contract

Every conformance PR (T2/T3) or fixture (T1) is identified by a stable `id` and carries:

```json
{
  "schema": "switchboard.completion_conformance.scenario.v1",
  "id": "draft-red-ci-product",
  "expect": {
    "terminal": "blocked",
    "reason_code": "required_exact_head_ci_failed",
    "role_sequence": ["remediation"]
  },
  "world": {
    "draft": true,
    "ci": "fail",
    "ci_context": "Switchboard CI / VM gate",
    "ci_attribution": "product",
    "review": "passed",
    "mergeable": true,
    "merge_state_status": "BLOCKED",
    "queue": "none",
    "runner": { "live": true, "role": "review_merge", "head": "same" }
  },
  "timing": {
    "ci_delay_seconds": 0,
    "never_report": false,
    "hang_seconds": 0,
    "close_pr_while_runner_live": false,
    "move_head_during_assessment": false
  }
}
```

Rules:

- **Declare outcomes; do not invent real bugs.** The sandbox workflow reads `world` /
  `timing` and obeys (exit non-zero, hang, silence, pass) in ~10s without flake.
- **`expect.terminal`** ∈ `merged` | `blocked` | `human` | `reconcile_done`. Landing on
  `human` when expected is a **pass**, not a failure (`expected_human` vs
  `unexpected_human` on the scoreboard).
- **Do not assert mid-flight routes** on T2/T3. Assert terminal + role sequence +
  wall-clock bound. T1 may assert the immediate `effect` / `route` because there is no clock.
- Nasty states are **timing fields**, not separate scenario languages.

Initial catalog seeds from the regression matrix in
`docs/AUTOPILOT-COMPLETION-STATE-MACHINE.md` (~25 rows), then grows by generators over
axes (draft × CI × mergeability × review × runner).

## Tier details

### T1 — Fixture tick (merge gate)

- Load scenario → build snapshot (or inject hydrator) → `run_completion_tick` with
  **mocked effect adapters** (no host, no `gh`).
- Assert: `route`, `effect`, `reason_code`, desired role; idempotent second tick.
- Fast, free, deterministic. Answers: “is orchestration aligned with the classifier?”

### T2 — Observe (sandbox, cheap matrix)

- Open throwaway branches/PRs on the conformance repo; each contains `scenario.json`.
- One obedient workflow produces real check conclusions for configured contexts.
- Real webhooks hit a dedicated Switchboard project `conformance` with Autopilot armed.
- **Cut before capacity spend:** record planned effect + execution-assignment contract
  (role, head, reason_code, findings), then **stop** — no Connect wake / runner boot.
- Answers: “does the orchestrator see real GitHub shapes and decide correctly?”

### T3 — Full loop (curated nightly)

- Same scenarios; allow DHCP + runner + push + re-enter CI.
- Cap concurrency and scenario count (e.g. dozen). Never sixty Full loops against the
  shared fleet without an explicit capacity budget.
- Answers: “does the whole stack survive contact with reality?”

### Concurrency suite (separate)

- Not mixed into the serial truth table. One harness mode opens N PRs at once and
  asserts admission throttle, no double-boot, no raced coordinator ticks.
- Motivation: contention outages (poisoned-runner wedge, merge wedge) are invisible to
  one-at-a-time tests.

## Scoreboard (primary deliverable)

Output is a table, not a single green tick:

| Column | Meaning |
|---|---|
| `scenario_id` | Stable id |
| `expected_terminal` | From scenario |
| `actual_terminal` | Observed |
| `expected_human` / `unexpected_human` | Classification when terminal is human |
| `role_sequence` | Roles Autopilot requested |
| `reason_codes` | Sequence of classifier reasons |
| `duration_s` | Wall clock to terminal or timeout |
| `status` | `pass` / `fail` / `timeout` |

Rows worth reading: **timeout** and **unexpected_human** — states where a person pays.

## Isolation rules

1. Conformance never writes to `switchboard` or `atlas` live boards.
2. Conformance never opens PRs against the product canonical repo.
3. T2/T3 use a dedicated GitHub app / webhook path or clearly namespaced project binding.
4. Scenario workflows must not require product secrets beyond the sandbox token.

## Relationship to existing code

- Reuse: `classify_completion`, `plan_effect`, `run_completion_tick`,
  `build_completion_snapshot`, regression matrix text as catalog seed.
- Do not fork completion logic into the harness. Harness is a client of the same modules.
- Existing tests (`test_bug164_completion_driver.py`, `test_simplify23_completion_classifier.py`)
  remain; T1 absorbs and systematizes them into the scenario catalog over time.

## Success criteria

1. T1 catalog covers the documented regression matrix; merge-gated.
2. One shared `scenario.v1` schema used by T1 fixtures and T2/T3 PRs.
3. T2 Observe scoreboard runs without booting runners.
4. T3 Full is curated, budgeted, and isolated to project `conformance`.
5. Operator can add a case by adding one scenario file (and, for T2/T3, opening a branch).

## Open points (resolve before T2 implementation)

- Exact sandbox repo name and whether merge queue is enabled on day one.
- Observe cut API: dry-run flag on `start_task` vs harness-side stop after decision record.
- Wall-clock `N` defaults per terminal class.

## Implementation plan (next doc)

After this design is approved: write
`docs/superpowers/plans/2026-07-26-completion-conformance-harness.md` starting with
**T1 only** (schema + catalog seed + pytest + optional CLI scoreboard printer). T2/T3
follow as separate plan slices.
