# ADR-0006 — Done enough: freeze the control plane, one provenance model, a subtraction rule, a kill list

- **Status:** Accepted — H1 cuts shipped (CONSOL-1…4, PRs #177/#178/#183/#189); as-built
  divergences recorded here (CONSOL-5, 2026-07-11). Operator may declare *done enough* once
  this amendment is merged.
- **Date:** 2026-07-08 (amended 2026-07-11)
- **Author:** consolidation session (Claude Code / Opus), synthesizing several independent
  analysis threads that all converged on the same conclusion — this document exists so no
  thread has to re-derive it again.
- **Relates to:** [ADR-0005](0005-store-module-decomposition.md) (supersedes its remaining
  scope) · [ADR-0003](0003-work-provenance-and-reconciliation.md) (the provenance inversion
  this ADR consolidates) · SESSION-12 (the PR chokepoint) · RECON-11 (orphan sweep) ·
  HARDEN-32/-40/-42 · BUG-28 · board deliverable *"Consolidate the control plane."*

---

## Context — the numbers, then the diagnosis

**The numbers (two weeks, this repo):** 251 commits across 7 concurrent inward-facing epics
(HARDEN 27, RECON 12, DELIVERABLES 10, SESSION 7, ACCESS 6, REPO 5, ARCH 5) — ~18 commits/day
into the coordination layer itself. Product commits live elsewhere.

**The hard symptoms — not vibes:**
- **#143 and #144 built the same orphan-sweep feature on the same day.** The coordination
  system failed to coordinate its own construction.
- **ADR-0005's 17-step decomposition plan was invalidated step-by-step by the real dependency
  graph** (ARCH-5 discovered the `_conn` cycle; ARCH-6 hit a 21-function wall). A plan that
  doesn't survive contact with the code is itself a small Rube Goldberg machine.
- **Most of the week's pain was features built-but-never-turned-on** (the Helm webhook, the
  Node 20 gate, timer units running as root, RECON-11's token, SESSION-11's flag) — built
  twice, left dark, or sequenced backwards. A capability surplus with a coherence deficit.
- **HARDEN-32:** the layer grew heavy enough to wedge its own 911 MB host. The metabolism
  eating itself, made literal.

**The diagnosis (the core idea):** this is not a feature problem and not any agent's mistake.
It is the **natural equilibrium of a fleet of agents told to improve their own coordination
tool.** Every SESSION/RECON/HARDEN task is locally justifiable — each makes agents marginally
safer or faster — so no agent ever has a reason to stop. There is no internal force that says
"enough." **Therefore the stop condition cannot come from a better plan or a smarter gate; it
must be imposed from outside, by the operator.** This ADR is that stop condition, written down.

What we are *not* saying: that the layer failed. When activated it demonstrably works — this
week it caught real board↔git drift, self-healed orphaned tasks, correctly gated live PRs, and
grew its own antibody (SESSION-12's "search before you build" at the PR chokepoint). It doesn't
need to be elegant. It needs to be **stable and legible so people stop rebuilding it.**

---

## Decision 1 — The provenance model (the one page)

This is the master blueprint for the single question all six-plus mechanisms answer: *"did
this work happen, and is the board honest about it?"* One mechanism per layer. Anything else
is redundant by definition.

**Done ownership:** only **canonical-repo merge provenance** (or verifier-stamped
`offline_evidence` for non-code work) marks work Done. No agent, gate, or human status flip
self-declares Done. (Unchanged from ADR-0003; restated here as the root invariant.)

| Layer | The one mechanism | What it absorbs / retires |
|---|---|---|
| **Realtime** | GitHub webhook: `pr_opened` → In Review; `pr_merged` → Done — with retry-on-transient-lock (HARDEN-42, shipped) | — |
| **Backstop** (reconcile) | Exactly **two** recovery paths: **(a)** open-PR backstop — discovers open PRs whose `pr_opened` was dropped and advances their tasks (BUG-28, shipped CONSOL-1 / PR #177); **(b)** merged-PR **orphan sweep** (RECON-11) — scans recent canonical merges and stamps any referenced task | Retired reconcile's **PR-evidence hydration** and **default-branch backfill** paths (CONSOL-1 / #177, CONSOL-2 / #178; see as-built divergences below) |
| **Prevention** | One shared **"is this task backed?"** definition (`store.pr_backed_by_process`) — called from `merge_gate` (readiness/work-session hygiene + backed check) and the claim gate (traceability enforcement at the SESSION-12 CI chokepoint) | Retired duplicate "backed?" logic; the two gates remain distinct call sites with different surrounding checks (CONSOL-3 / PR #183; see as-built divergences below) |

Supporting ledgers (activity log, git_state) are storage, not mechanisms; they stay. Everything
not in this table is either parked or deleted (Decision 3).

## Decision 2 — The subtraction rule

> **No new coordination mechanism — gate, provenance path, tracker, ledger, monitor, or
> workflow engine — without deleting an overlapping one.**

Enforced as **review policy at the SESSION-12 PR chokepoint** (the reviewer/gate asks: "what
does this diff subtract?"). Deliberately *not* automated with a new detector — building a
mechanism to prevent mechanisms is the joke writing itself. Promote to an automated check only
if the policy is actually violated.

## Decision 3 — The kill list

| Mechanism | Verdict | Action | Status (as-built) |
|---|---|---|---|
| reconcile: PR-evidence hydration | redundant (merged flows) | **Retire into orphan sweep** | **Done** — CONSOL-2 / PR #178. `pr_number` now derived once at write time in `_upsert_git_state`; no reconcile scrape path. |
| reconcile: default-branch backfill | redundant (merged commits are merged PRs) | **Retire into orphan sweep** | **Done** — CONSOL-1 / PR #177. Push-handler and reconcile callers removed. |
| open-PR gap | the one real hole (BUG-28, hit live on PR #164) | **Build the open-PR backstop** — net −1 with the two retirements above | **Done** — CONSOL-1 / PR #177. |
| `merge_gate` + claim gate | two implementations of one question | **One shared "backed?" definition, two call sites** | **Done** — CONSOL-3 / PR #183. `pr_backed_by_process` is the single source of truth for "backed"; gates are not one merged function. |
| RECON-9 coordination receipts | unproven vs. activity log + reconcile | **Parked**: zero further investment; delete after the Helm sprint unless real usage defends it | **Parked** — no further investment. |
| RECON-8 event replay | speculative | **Parked**, same terms | **Parked** — no further investment. |
| RECON-10 DBOS evaluation | the "7th mechanism" instinct with a framework attached | **Decision recorded: NO adoption.** The evaluation is complete and the answer is no. | **Done** — decision recorded. |
| Session policy profiles (5 × ~20 fields) | config theater — reality is "code needs a clean branch + tests; everything else doesn't" | **Collapse to 2**: `code_strict` + default | **Done with divergence** — CONSOL-4 / PR #189 collapsed to **3**: `code_strict`, `docs_review` (default), `offline_evidence`. `ui_preview` and `no_repo` retired with aliases → `docs_review`. |
| HARDEN epic | 27 commits hardening scaffolding | **Frozen.** Only HARDEN-32 (the box) survives as an open item — it is infrastructure, not mechanism. HARDEN-40's public CI sandbox helps here by moving CI load off the box. | **Frozen** — HARDEN-32 remains open infrastructure work. |
| ARCH-6…17 (37-module decomposition) | invalidated by the dependency graph; maximum conflict surface on the fleet's hottest file | **Retired.** ARCH-1…5 outcomes stand (db/ package + 7 leaf stores; store.py 15,817 → 14,382). **Moratorium (policy) on net-new store.py growth.** Optional future extraction limited to the two genuinely clean leaf clusters (`side_effects`, `runner`) — only if store.py pain recurs in practice. | **Done** — ARCH-6…17 retired on board; moratorium is policy. |
| `mark_task_default_branch_commit` | automated backfill path retired | **Delete automated callers** | **Done with divergence** — automated callers removed (CONSOL-1); the low-level primitive kept as a **dormant manual/bootstrap escape hatch** with no automated caller. |

## As-built divergences (CONSOL-5)

The H1 code cuts (CONSOL-1…4) matched the ADR's intent but diverged in four places worth
recording so the plan-of-record stops lying:

1. **Write-time `pr_number` derivation, not a full hydration retirement story.** CONSOL-2
   removed reconcile's PR-evidence hydration scrape, but kept deriving `pr_number` from `pr_url`
   once at write time in `_upsert_git_state`. The kill-list wording implied a cleaner
   retirement; the as-built model is "no reconcile scrape, derive at write."

2. **Gates share only the "backed?" sub-question.** CONSOL-3 unified `store.pr_backed_by_process`
   as the single definition of whether a task is backed. `merge_gate` still adds
   readiness/work-session hygiene checks; the claim gate still enforces traceability at the CI
   chokepoint. They are not one merged gate — they share one sub-definition.

3. **Policy profiles collapsed 5→3, not 5→2.** CONSOL-4 found `offline_evidence` is
   load-bearing for non-PR verifier completion. `ui_preview` and `no_repo` were the genuine
   theater; they were retired with aliases to `docs_review`.

4. **`mark_task_default_branch_commit` kept as a dormant primitive.** CONSOL-1 removed all
   automated default-branch backfill callers, but left this manual/bootstrap repair function in
   `store.py` with no automated caller — a deliberate escape hatch for pre-flow commits.

### Activation-audit outcome — CI gate resilience

During cut #3 (#189), the VM gate's external-CI-mirror path was found to hard-fail every PR
when the mirror could not dispatch (HTTP 422 input mismatch) or when mirror machinery raised
(e.g. transient `database is locked` on the contended box). External CI is evidence-only and
must not be the sole source of truth.

**Fix shipped:** PRs #196 and #197. Any external-CI failure that produces **no test verdict**
(returned error dict *or* raised exception) now returns `unavailable` and **falls back to the
local suite** (`run_switchboard_gate`). Only a genuine test failure (`failure_class=test`, suite
ran and was red) still hard-fails the gate. This is an activation-audit win: a built-but-dark
mirror path that was failing the fleet is now resilient.

## Decision 4 — The counterweight: fleet default is product

The missing force that let the equilibrium form is product-pull. So: **the fleet's default
assignment is Helm.** Coordination-layer work is by exception (subtraction rule applies) —
not the default lane.

The horizons, so this document is also the master plan of record:

1. **H1 — Stabilize (this ADR):** freeze → this model → execute the kill list → BUG-28
   backstop → resolve HARDEN-32. Exit: the operator declares *done enough*, and a legitimate
   PR merges with zero manual re-trigger ceremony. **Code cuts complete (CONSOL-1…4); this
   amendment closes the paper record (CONSOL-5). HARDEN-32 remains the open infrastructure
   item.**
2. **H2 — Prove on Helm:** the whole fleet ships chartplotter work through the frozen tooling
   for a sprint. Real usage — not planning — is the judge of every surviving gate; whatever
   isn't earning its keep dies under the subtraction rule.
3. **H3 — Automate the operator out:** the COORD epic (already scoped, COORD-1…13). Exit is
   literally COORD-11 — *"coordinator runs a project slice without Steve as dispatcher."*
4. **H4 — Productize:** open-core release (ADAPTER-11, DOGFOOD-8, ACCESS-5/6/9, TALLY-4/5,
   public CI go-live): an external team can install Switchboard, connect a fleet, and pay.

## Consequences

### SIMPLIFY-17 accounting — one execution clock

Phase 1 removes three autonomous stop authorities: the Agent Host claim/idle reaper, the
review-steward acknowledgement-timeout replacement, and terminal-task cleanup. One authority
replaces them: expiry of the renewable runner heartbeat lease. Wake intents use the same model
and always receive a deadline. Lease enforcement now defaults on.
Lease enforcement is unconditional after the SIMPLIFY-16 observation proof;
SIMPLIFY-11 deleted the rollout flag and final compatibility branch.

- Kill mechanisms deleted: **3**
- Kill mechanisms added: **1**
- Net authority reduction: **2**

- Some redundancy that occasionally caught real issues is deliberately removed; the two-path
  backstop plus prevention layer is judged sufficient, and the Helm sprint is the test.
- Parked ≠ deleted: RECON-8/9 code stays but receives zero investment; the post-sprint review
  deletes or defends it. Every "parked" without a defender becomes a deletion.
- `store.py` remains ~14.4k lines and cohesive **on purpose**. Its documented seams (ADR-0005's
  map) remain valid if it ever needs splitting; nobody decomposes the hot file mid-flight.
- ADR-0005's remaining scope (ARCH-6…17) is superseded; its docs get a superseded banner rather
  than deletion — the per-function map stays useful as a reference.

## Alternatives rejected

- **Keep executing the 37-module plan.** Invalidated twice by the dependency graph; maximum
  merge-conflict surface on the fleet's hottest file for a win invisible until fully done.
- **Adopt DBOS / a durable-workflow engine.** Bolting a framework onto a system already too
  elaborate to hold in one head — the exact instinct this ADR exists to stop.
- **Build an enforcement mechanism for the subtraction rule.** See Decision 2; policy first.
- **A "consolidation framework" with its own tracker sprawl.** One deliverable on the board
  tracks all of this; that is the whole apparatus.
