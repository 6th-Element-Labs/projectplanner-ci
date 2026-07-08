# ADR-0006 — Done enough: freeze the control plane, one provenance model, a subtraction rule, a kill list

- **Status:** Proposed — **accepted the moment the operator declares "done enough."** That is
  deliberate: the stop condition must be imposed from outside (see Context); this ADR is the
  written form of that imposition.
- **Date:** 2026-07-08
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
| **Backstop** (reconcile) | Exactly **two** recovery paths: **(a)** open-PR backstop — discovers open PRs whose `pr_opened` was dropped and advances their tasks (BUG-28, *to build; the one sanctioned addition, paired with two deletions below*); **(b)** merged-PR **orphan sweep** (RECON-11) — scans recent canonical merges and stamps any referenced task | Retires reconcile's **PR-evidence hydration** and **default-branch backfill** paths (the sweep is a superset for all merged flows — verified; it is *not* a superset for open PRs, hence (a)) |
| **Prevention** | One **PR-coverage function** — "is this PR backed by an active claim / Work Session / In-Review-or-Done state" — called two ways: cooperatively (`merge_gate`, the agent asks) and enforced (claim gate at the SESSION-12 CI chokepoint) | Retires the two separate implementations of the same coverage question |

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

| Mechanism | Verdict | Action |
|---|---|---|
| reconcile: PR-evidence hydration | redundant (merged flows) | **Retire into orphan sweep** |
| reconcile: default-branch backfill | redundant (merged commits are merged PRs) | **Retire into orphan sweep** |
| open-PR gap | the one real hole (BUG-28, hit live on PR #164) | **Build the open-PR backstop** — net −1 with the two retirements above |
| `merge_gate` + claim gate | two implementations of one question | **One coverage function, two call sites** |
| RECON-9 coordination receipts | unproven vs. activity log + reconcile | **Parked**: zero further investment; delete after the Helm sprint unless real usage defends it |
| RECON-8 event replay | speculative | **Parked**, same terms |
| RECON-10 DBOS evaluation | the "7th mechanism" instinct with a framework attached | **Decision recorded: NO adoption.** The evaluation is complete and the answer is no. |
| Session policy profiles (5 × ~20 fields) | config theater — reality is "code needs a clean branch + tests; everything else doesn't" | **Collapse to 2**: `code_strict` + default |
| HARDEN epic | 27 commits hardening scaffolding | **Frozen.** Only HARDEN-32 (the box) survives as an open item — it is infrastructure, not mechanism. HARDEN-40's public CI sandbox helps here by moving CI load off the box. |
| ARCH-6…17 (37-module decomposition) | invalidated by the dependency graph; maximum conflict surface on the fleet's hottest file | **Retired.** ARCH-1…5 outcomes stand (db/ package + 7 leaf stores; store.py 15,817 → 14,382). **Moratorium (policy) on net-new store.py growth.** Optional future extraction limited to the two genuinely clean leaf clusters (`side_effects`, `runner`) — only if store.py pain recurs in practice. |

## Decision 4 — The counterweight: fleet default is product

The missing force that let the equilibrium form is product-pull. So: **the fleet's default
assignment is Helm.** Coordination-layer work is by exception (subtraction rule applies) —
not the default lane.

The horizons, so this document is also the master plan of record:

1. **H1 — Stabilize (this ADR):** freeze → this model → execute the kill list → BUG-28
   backstop → resolve HARDEN-32. Exit: the operator declares *done enough*, and a legitimate
   PR merges with zero manual re-trigger ceremony.
2. **H2 — Prove on Helm:** the whole fleet ships chartplotter work through the frozen tooling
   for a sprint. Real usage — not planning — is the judge of every surviving gate; whatever
   isn't earning its keep dies under the subtraction rule.
3. **H3 — Automate the operator out:** the COORD epic (already scoped, COORD-1…13). Exit is
   literally COORD-11 — *"coordinator runs a project slice without Steve as dispatcher."*
4. **H4 — Productize:** open-core release (ADAPTER-11, DOGFOOD-8, ACCESS-5/6/9, TALLY-4/5,
   public CI go-live): an external team can install Switchboard, connect a fleet, and pay.

## Consequences

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
