# ADR-0010 — CI concurrency: many agents land PRs without blocking each other

- **Status:** Accepted program charter (2026-07-12). Umbrella over the CI-concurrency work;
  execution tracked on `project=switchboard`, deliverable `ci-concurrency` (HARDEN lane).
- **Owner:** Platform / CI
- **Relates to:** [ADR-0007](0007-application-shell-cleanup.md) (Decision 2 ratchet — **retired**),
  [ADR-0009](0009-microservices-modernization.md) (ARCH-MS extraction reduces code-level conflicts),
  [ci-public-sandbox-rollout], [concurrent-load-slo-ratchet].

## Context — why the fleet blocks itself

A fleet of coding agents opens PRs in parallel against one `master` and one required check
(`Switchboard CI / VM gate`, posted by `scripts/switchboard_pr_gate.py`, run off-box). Three
shared resources turn that parallelism into failures:

1. **Shared hot files.** The retired exact-match size ratchet (`test_size_ratchet.py`) forced
   *every* file-adding PR to compare-and-swap one global integer against a moving `master`.
   Guaranteed conflicts. (Retired 2026-07-12, ADR-0007 Decision 2.)
2. **A moving base + a slow gate.** The gate runs on the PR's *merge ref* (branch + current
   `master`) and takes ~15 min on a contended box. `master` advances every few minutes, so by the
   time the gate finishes the merge ref no longer exists — GitHub reports "no merge ref" and the
   PR can't merge. Observed live: NARRATE-13 took **4 merge cycles** and still couldn't land.
3. **A single slow runner.** One box serializes every agent's gate and is slow, widening the
   window in which `master` moves.

Underneath, this is a distributed **lost-update / write-write conflict**: N agents compare-and-swap
against one shared value, verified by a slow step. A room of humans pushing this fast would hit the
same wall. The fix is not "try harder" — it is to remove contention at every layer, make
verification fast and attributable, and serialize only the *final merge* via a queue that tests the
*future* state.

## Decision — a layered program (seven levers)

No single change suffices; each lever removes one class of contention. Status as of this ADR:

### Lever 1 — Kill shared hot files (commutative CI config) · *started*
CI config a PR must edit is a global counter. Make every such check per-diff or per-file.
- **Done:** retire the exact-match size ratchet (ADR-0007 Decision 2; `test_size_ratchet.py`
  deleted). Growth is redirected by ADR-0007 Decision 3 + review, not a counter.
- **Next:** a **per-PR diff guard** — CI fails only if *this PR's own diff* adds net lines to a
  monolith (`store.py` / `app.py` / `mcp_server.py`) without a `MONOLITH-TOUCH:` justification in
  the PR body. Extractions that move lines out always pass. Two PRs touching different files never
  conflict because neither edits a shared line.
- **Rule:** append-y artifacts (changelogs, ledgers, snapshots) use one-file-per-PR *changeset
  fragments* merged by a bot, never a single shared doc. Lockfiles regenerate deterministically.

### Lever 2 — Serialize merges with a queue that tests the future state · *partial*
- **Done:** GitHub **auto-merge enabled** (`allow_auto_merge: true`). Agents `gh pr merge --auto
  --squash` and walk away; GitHub lands the PR when the required check passes.
- **Prerequisite before enabling the native merge queue:** the merge queue tests a `merge_group`
  ref, but `switchboard_pr_gate.py` posts `Switchboard CI / VM gate` only on **PR head commits**
  (it has explicit "PR #N has no merge ref" logic). Enabling the queue as-is would **hang** — the
  required check never arrives for the `merge_group`. Task: make the gate handle `merge_group`
  events (run the suite against the queue's temp ref and post the status on its SHA). Only then
  flip on the queue. Speculative batching (Bors/Zuul-style) is a later optimization.

### Lever 3 — Fast, horizontally-scalable gate · *design*
The ~15-min single-box gate is the amplifier; every minute widens the race window.
- Ephemeral **parallel runners** (one per PR) off the prod box, so N gates run at once and CI stops
  competing with serving traffic.
- **Test-impact analysis:** run only the tests affected by the diff, not all ~140 files.
- Sharding + caching (warm venvs, dep/artifact cache).

### Lever 4 — Self-healing so agents never hand-hold · *design (high ROI)*
- **Auto-rebase bot** (Mergify/Kodiak, or a homegrown scheduled workflow) keeps every open PR
  rebased on `master` and re-triggers the gate. Combined with auto-merge (Lever 2) this delivers
  most of a merge queue's benefit *without* the `merge_group` wiring — it directly kills the manual
  "master moved, re-merge by hand" loop this session hit ~6×.
- Auto-retry transient failures (network, SQLite lock, flake) instead of failing the PR.
- Conflict-resolver for known-safe files (lockfiles, generated files, changeset fragments).

### Lever 5 — Reduce conflicts in the *code*, not just CI · *ongoing (ADR-0009)*
Two agents editing `store.py` (15k lines) conflict regardless of CI. The **ARCH-MS extraction
program** (split monoliths into modules) is itself a CI-concurrency fix: smaller files, fewer
collisions. Plus CODEOWNERS / module boundaries and short-lived branches.

### Lever 6 — Use the switchboard as the coordination layer · *design (bespoke edge)*
The board already partitions work and has `claim_files` advisory locks — most teams lack this.
- Dispatch agents to **orthogonal modules** so PRs rarely touch the same files.
- Enforce `claim_files` so two agents don't edit one file concurrently.
- A switchboard **merge-coordinator** that lands PRs in dependency order and applies backpressure
  when the gate is saturated (a bespoke queue if GitHub's cross-repo topology doesn't fit).

### Lever 7 — Attribution + observability · *design*
- Every PR's CI run must be **attributable to that PR** with a direct link to its failing test.
  (Today the mirror rewrites SHAs, so a failed run can't be traced to its PR — a blocker in itself.)
- Dashboards: gate queue depth, p95 gate latency, flaky-test rate. Flaky *required* checks block
  the whole fleet — hunt and quarantine them.

## Sequencing

**Levers 1–4 reach "agents fly around and land PRs without touching each other" with a small amount
of work; 5–7 make it durable at scale.**

```
DONE: ratchet retired (L1) · auto-merge on (L2)
1. Auto-rebase bot (L4)              — kills the manual re-merge loop; pairs with auto-merge
2. Diff-guard replaces the ratchet (L1) — commutative growth check for when the fleet parallelizes
3. merge_group gate wiring → native merge queue (L2) — the durable serialization
4. Off-box parallel runners + test-impact analysis (L3) — throughput multiplier
5. Continue ARCH-MS extraction (L5)  — structurally fewer conflicts over time
6. Switchboard merge-coordinator + attribution (L6/7) — bespoke, durable edge
```

## Consequences

- Retiring the ratchet trades "auto-tighten on shrink" for fleet throughput; the extraction
  metric (ADR-0009 Decision 5 #4) replaces it as the anti-bloat signal.
- The one-time gate override used to land the ratchet retirement (disable `enforce_admins`, merge,
  re-enable) is an operator escape hatch, not a pattern — the program's point is that no future PR
  needs it.
- Enabling the native merge queue **before** the `merge_group` gate wiring would wedge the whole
  fleet; the sequencing above makes that dependency explicit.

## Alternatives rejected

- **Keep the exact-match ratchet.** A shared counter every concurrent PR must CAS is
  fundamentally incompatible with a parallel fleet. Retired.
- **Just tell agents to merge faster / retry.** Fighting a structural lost-update race by hand
  does not converge (proven this session).
- **Native merge queue, enabled immediately.** Hangs on the missing `merge_group` gate status.
