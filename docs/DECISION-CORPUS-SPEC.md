# Decision corpus spec — replay at n=1, pooled failure intelligence at n=many

- **Status:** Phase 1 implemented (COORD-50); Phases 2–6 proposed and **blocked on ADR
  ratification** (see §11 and §12)
- **Date:** 2026-07-25
- **Strategy anchors:** DOGFOOD-7 (open-core boundary), DOGFOOD-10
  ([`SWITCHBOARD-BACKEND-MOAT.md`](SWITCHBOARD-BACKEND-MOAT.md) §5–§7)
- **Revives / replaces:** RECON-8 (event replay + dispatch simulation, *parked* by ADR-0006),
  RECON-9 (coordination receipts, *parked*), BUG-15 (failure classes and signal schema),
  DISPATCH-7 (policy simulator, scorecards, regret analysis)
- **Related:** [ADR-0006](decisions/0006-control-plane-done-enough.md) (the subtraction rule this
  document must satisfy) · [`AUTOPILOT-COMPLETION-STATE-MACHINE.md`](AUTOPILOT-COMPLETION-STATE-MACHINE.md)
  (the classifier this records) · [`TALLY-SPEC.md`](TALLY-SPEC.md) (prices the outcome metric) ·
  [ADR-0008](decisions/0008-three-plane-separation.md)

---

## 1. What this decides

Switchboard already records *what it decided and why* — `reason_code`, `route`, `desired_role`,
fenced to an exact head SHA inside an immutable `switchboard.execution_assignment.v1`. That record
is forensic: it explains one decision after the fact and is never counted, compared, or replayed.

This document specifies the minimum durable artifact that makes the same data **adaptive**, and it
does so in a way that pays for itself at one tenant while being the exact substrate a
multi-tenant network effect requires. One record, written once, two halves:

> A **private half** — the full classifier input and output — powers offline replay and
> counterfactual evaluation for a single tenant. A **projected half** — a versioned allowlist of
> structural features with no code, names, or content — is the poolable unit that makes each new
> fleet inherit failure modes solved by every previous fleet.

The rule that makes both work: **the projection is materialized at write time, never derived at
export time.** The privacy boundary is then a table definition — diffable in a migration,
reviewable by a customer's security team, and impossible to widen without an approved schema
change.

## 2. Non-goals

- **No machine learning.** Per [`SWITCHBOARD-BACKEND-MOAT.md`](SWITCHBOARD-BACKEND-MOAT.md) §5:
  "The first scheduler moat is not ML. It is complete, trustworthy data plus explainable policy."
  Everything here is counting, contrast sets, and replay of a deterministic function.
- **No PTY scrollback retention.** Full session capture is expensive, sensitive, and low
  signal-density. The normalized snapshot is the higher-value substrate per byte because it is the
  actual input to the actual decision. (See §12 on the stale SIMPLIFY-9 deferral.)
- **No automatic policy mutation in Phases 1–4.** Nothing in the first four phases changes a
  routing or gating decision. Feedback is advisory, then gated by human approval (§10).
- **Not a new coordination mechanism.** Phase 1 adds *storage*, in the same category ADR-0006
  §Decision-1 already blesses: "Supporting ledgers (activity log, git_state) are storage, not
  mechanisms; they stay." See §11 for the full accounting.

## 3. The measured problem

These are facts from the current tree and local board databases, not projections.

**The classifier is pure, and its input is discarded.** `classify_completion(current_run,
snapshot)` is documented and implemented as "one deterministic, side-effect-free decision for an
exact-head snapshot" over a versioned `switchboard.completion_snapshot.v1`. `run_completion_tick`
hydrates that snapshot, classifies, and lets it fall out of scope. The only path that persists a
snapshot is the attention path, which embeds one in `attention_requests.context_json` — so the
full feature vector is retained **exclusively for cases where a human was already being pulled
in**, and discarded for the automated majority. That is inverted from what learning needs.

**The reason_code timeline is overwritten as it is produced.** `completion_runs` is one row per
task with `ON CONFLICT(task_id) DO UPDATE SET ... reason_code=excluded.reason_code`. The
append-only companion, `task_execution_completion_phases` (migration `0074`), has **no
`reason_code` column** at all — only `phase`, `outcome`, `evidence_json`, `failure_json`. So for
the completion path the code is not merely uncounted; its history is destroyed on write.

**The vocabulary has no owner.** `reason_code` is a free string emitted by at least nine
subsystems (completion state machine, coordination, placement, runner preclaim, provider
credentials, provider capacity, AI admission, SCM, project execution policy) with well over a
hundred distinct values and no registry, no enum, and no test asserting an emitted code is known.
`required_ci_cancelled` and `required_ci_canceled` both exist, which will silently split cohorts.

**The corpus does not exist yet.** Across the five local board databases
(`maxwell`, `helm`, `switchboard`, `helm-layers`, `vulkan`, all at migration 0110):
`task_execution_completion_phases` 0 rows, `review_verdicts` 0, `preflight_runs` 0,
`wake_intents` 0, `attention_requests` 0. Count of persisted `completion_snapshot.v1` bodies
paired with a human decision: **zero**. The richest history is 206 `activity` rows on
`switchboard` spanning three days.

**The cost of the gap is already measured.** The last ~30 commits contain eight completion-
classifier corrections — SIMPLIFY-23, SIMPLIFY-24, SIMPLIFY-27, SIMPLIFY-28, BUG-169, BUG-171,
BUG-172, BUG-175 — each shipped against hand-built fixtures, each carrying regression risk against
cases fixed days earlier. Every one was a production snapshot held in memory and discarded.

**Counting alone would not have caught the motivating incident.** On 2026-07-24, PR #810 was an
open **draft** with genuinely red VM and Playwright checks, where `required_exact_head_ci_failed` →
remediation was the *correct* call; PR #811 was also an open draft, green with review passed, where
the correct call was mark-ready. Two drafts, opposite correct verdicts. A histogram over
`reason_code` prints `required_exact_head_ci_failed: 3` — precisely the signal the operator already
had, which was insufficient. What made it diagnosable was **conditional concentration**: the code
clustering on a feature value (`pr_draft=true`, `contexts_hydrated=false`) far above its base rate.
That is a contrast-set calculation over snapshot features, and it requires the features.

The mechanism is structural and will recur: `scripts/switchboard_pr_gate.py` honours
`SWITCHBOARD_CI_SKIP_DRAFTS=1`, so drafts receive no gate post, so required contexts are absent on
drafts — the same seam SIMPLIFY-24 patched from the `required_ci_hydration_missing` side.

### 3.1 Why now, and not six months ago

RECON-8 was parked by ADR-0006 as "speculative," and that judgement was correct **for the
substrate that existed then**: generic board-event replay, reconstructing derived state from a
heterogeneous activity log, with no pure function to replay against. SIMPLIFY-23 changed the
premise by making completion classification a pure function over a versioned snapshot. Replay
against a pure function with persisted inputs is not speculative; it is a loop. This document is
the defence ADR-0006 §Consequences demands ("every parked without a defender becomes a deletion"),
on a substrate that now makes the capability cheap.

## 4. Schemas

Four artifacts. Two are specifications with no runtime cost; two are tables.

### 4.1 `switchboard.reason_code.v1` — the registry

A frozen, versioned catalogue. Every code an authority may emit is declared once:

| Field | Meaning |
|---|---|
| `code` | Stable identifier. Additive-only; codes are API and are never renamed in place. |
| `family` | Coarse grouping (`required_ci`, `review`, `merge_queue`, `mergeability`, `placement`, `provider`, `runner`, `terminal`). |
| `subsystem` | The single authority permitted to emit it. |
| `expected` | `transient` (normal, self-clearing) or `anomalous` (should be rare; concentration is a signal). |
| `resolver` | Who can actually clear it: `agent`, `human`, `infra`, `provider`. |
| `poolable` | Whether this code may appear in an exported projection. Default true; codes carrying environment specifics are excluded. |

Enforced by a test that asserts every literal reason code reachable in the emitting modules is
registered, and that no two codes differ only by spelling. This registry is also the public
artifact: it is the natural content of **BUG-15** ("define failure classes and signal schema"), and
it is what makes §6's privacy contract auditable.

### 4.2 `switchboard.decision_features.v1` — the allowlist

The exhaustive set of fields derivable from a snapshot that may cross a tenant boundary. All are
boolean, small enum, or bucketed integer. All describe the *shape* of the snapshot, never its
content.

```
board_status                    enum
has_pr                          bool
pr_state                        enum   open | closed | merged
pr_draft                        bool
pr_mergeable_state              enum
board_pr_identity_matches       bool
required_contexts_count         bucket 0 | 1 | 2-3 | 4-6 | 7+
contexts_fully_hydrated         bool
any_required_context_failed     bool
any_required_context_pending    bool
any_required_context_cancelled  bool
review_verdict_present          bool
review_verdict_status           enum
review_verdict_stale            bool    (invalidated_by_head_sha non-null)
open_blocking_findings_count    bucket 0 | 1 | 2-3 | 4+
merge_queue_state               enum
work_session_present            bool
work_session_preflight_state    enum
runner_live                     bool
runner_role_matches_desired     bool
runner_head_matches_exact_head  bool
has_merge_provenance            bool
generation_bucket               bucket 1 | 2 | 3-5 | 6+
```

Twenty-three fields. Note that this set is **sufficient to have caught the draft incident**:
`pr_draft=true` co-occurring with `contexts_fully_hydrated=false` is the signature.

Deliberately excluded from the poolable tier: task titles and descriptions, repository and branch
names, **status context names** (these leak internal tooling inventory and are only mildly useful),
head SHAs, PR numbers and URLs, agent and host identity, and any raw count that reveals throughput.
Runtime/provider class is valuable for learning but commercially sensitive; it belongs to an
explicit opt-in tier, not the default projection.

### 4.3 `switchboard.decision_record.v1` — the ledger

Append-only. One row per **decision episode**, not per tick (see §5).

```sql
-- 0117_decision_records
CREATE TABLE IF NOT EXISTS decision_records (
  record_id            TEXT PRIMARY KEY,
  project              TEXT NOT NULL,
  -- identity / fencing
  task_id              TEXT NOT NULL,
  pr_number            INTEGER NOT NULL DEFAULT 0,
  head_sha             TEXT NOT NULL DEFAULT '',
  generation           INTEGER NOT NULL DEFAULT 0,
  fence_epoch          INTEGER NOT NULL DEFAULT 0,
  -- private half: replay substrate, never exported
  snapshot_hash        TEXT NOT NULL,
  snapshot_json        TEXT NOT NULL DEFAULT '{}',
  decision_json        TEXT NOT NULL DEFAULT '{}',
  classifier_version   TEXT NOT NULL,
  -- projected half: materialized at write time, export-safe
  reason_code          TEXT NOT NULL DEFAULT '',
  route                TEXT NOT NULL DEFAULT '',
  desired_role         TEXT NOT NULL DEFAULT '',
  features_json        TEXT NOT NULL DEFAULT '{}',
  features_version     TEXT NOT NULL,
  -- off-policy hygiene (§7)
  advice_version       TEXT,            -- NULL = control arm, no advice was live
  -- episode accounting
  tick_count           INTEGER NOT NULL DEFAULT 1,
  first_seen_at        REAL NOT NULL,
  last_seen_at         REAL NOT NULL,
  -- outcome, backfilled by reconcile / webhook
  outcome              TEXT NOT NULL DEFAULT 'open',  -- open|converged|abandoned|escalated
  head_advanced        INTEGER NOT NULL DEFAULT 0,
  generations_spent    INTEGER NOT NULL DEFAULT 0,
  merged_sha           TEXT NOT NULL DEFAULT '',
  human_intervened     INTEGER NOT NULL DEFAULT 0,
  human_action         TEXT NOT NULL DEFAULT '',
  UNIQUE(project, task_id, snapshot_hash)
);

-- 0118_ix_decision_records_projection
CREATE INDEX IF NOT EXISTS ix_decision_records_projection
  ON decision_records(project, reason_code, first_seen_at DESC);

-- 0119_ix_decision_records_convergence
CREATE INDEX IF NOT EXISTS ix_decision_records_convergence
  ON decision_records(project, task_id, head_sha, generation);
```

Migration numbers are indicative; 0116 is currently the highest applied and the implementer takes
the next free block to avoid collision.

### 4.4 `switchboard.decision_signature.v1` — the portable unit

The thing that pools. Derived, never hand-written:

```json
{
  "schema": "switchboard.decision_signature.v1",
  "reason_code": "required_ci_hydration_missing",
  "predicate": { "pr_draft": true, "contexts_fully_hydrated": false },
  "observed_resolution": "mark_ready_then_rehydrate",
  "evidence": {
    "episodes": 7,
    "resolution_agreement": 1.0,
    "feature_base_rate": 0.19,
    "feature_rate_within_code": 0.86,
    "lift": 4.5,
    "donor_configurations": 3
  },
  "confidence": "high"
}
```

Effectively a lint rule for orchestration decisions. It references only registry codes and
allowlisted features, so it is portable by construction and contains nothing a customer would
object to sharing.

## 5. Count episodes, not ticks

This is the detail most likely to be got wrong, and it determines whether every downstream number
is meaningful.

The completion driver ticks repeatedly against an unchanged head. A task stuck for two hundred
ticks would contribute two hundred identical rows and dominate every count, making the statistics a
measure of **polling frequency** rather than of the world. The `UNIQUE(project, task_id,
snapshot_hash)` constraint collapses identical consecutive observations into one episode with
`tick_count` incremented and `last_seen_at` advanced.

This is simultaneously the storage-control mechanism. A hydrated snapshot is plausibly 5–50 KB of
JSON; the projected half is ~200 bytes. Given the box is a t4g.micro that has already wedged itself
once on disk and memory (HARDEN-32), retention is not optional:

- **Projected half: retained indefinitely.** It is tiny and it is the corpus.
- **Snapshot bodies: retained 90 days by default**, then dropped, *except* for episodes that are
  escalated, non-convergent, or human-resolved — the demonstrations, which are the scarcest and
  densest labels in the system and are kept indefinitely.

A compaction step in the existing `background_jobs.py` catalogue enforces this. Storage is
therefore bounded by episode count, not tick rate.

## 6. The projection boundary as a contract

Opt-in pooling is the entire network effect, and pooling only happens if a security reviewer can be
satisfied. That makes the boundary a commercial artifact, not just hygiene.

1. **Materialized, not derived.** `features_json` is written by one function against
   `decision_features.v1` at the moment of classification. Export reads columns; it never touches
   `snapshot_json`. Nobody under deadline can widen the projection by reaching for one more field,
   because the widening is a migration.
2. **Export is a projection query with an explicit column list**, asserted by test to exclude
   `snapshot_json`, `decision_json`, `head_sha`, `merged_sha`, `pr_number`, and `task_id`.
3. **The one-pager already exists.** §4.1 plus §4.2 *is* the disclosure document: these
   twenty-three enum fields and this frozen code vocabulary leave your environment, nothing else
   does, and here is the test that proves it.
4. **Aggregate-only egress.** Signatures export with episode counts above a floor (`episodes >= 5`,
   `donor_configurations >= 2`), never single observations, so no exported row is traceable to one
   tenant's single event.

This is why the registry is load-bearing for the *business case* and not merely for aggregation.
Without it, pooling is unsellable, and without pooling the network effect never starts.

## 7. Off-policy hygiene, and why it is also the experiment

The moment learned advice reaches a runner — via `execution_assignment.acceptance_findings`, which
is already an immutable list field delivered to every runner, or via the `working_agreement` meta
override merged at request time — snapshots stop being independent observations of the world and
start reflecting our own influence. Statistics then measure the advice.

Two provisions, both cheap now and impossible to retrofit:

- **`advice_version` stamped on every record**, `NULL` meaning no advice was live.
- **A control arm**: a configured fraction of dispatches runs with advice suppressed.

The control arm does double duty. It is required for statistical validity *and* it is the
experimental apparatus that upgrades §8's central claim from "we modelled a saving" to "we measured
a saving against a control." One mechanism, two jobs — which is why it must be in the schema from
the first row, before there is anything to be advised about.

## 8. How compounding is measured

The claim to be demonstrated is not "the system learns." It is **"each additional fleet makes every
other fleet cheaper,"** and it has to be graphed from real data.

### 8.1 The metrics

**Inherited coverage** (the network-effect number): of the distinct failure modes a fleet
encounters in its first 90 days, what fraction were already present in the catalogue at high
confidence? Plotted against the number of distinct **donor configurations** pooled. This is
buyer-legible: *"you inherit 60% of your failure modes already solved, and that rises with every
fleet that joins."*

**Wasted generations, priced** (the CFO number): generations spent on a task where `head_sha` did
not advance. Tally already computes cost per verified outcome, so this converts directly into
currency, and the second line reads *"cost per verified outcome falls as pooled configurations
grow."*

Both require the baseline that only Phase 1 can start accruing, which is the scheduling argument in
§9.

### 8.2 Leave-one-board-out protocol

Run offline against the corpus; mutates nothing.

1. Freeze the corpus at time `T`. Treat each board as one configuration.
2. For held-out board `C`, walk its episodes chronologically.
3. Derive the catalogue from donor boards using **only records timestamped strictly before the
   current replay point.** Respect time ordering or lookahead leakage fabricates the curve.
4. For each non-convergence episode on `C`, ask whether a matching signature existed above
   confidence threshold *before the episode began*, and how many generations it would have saved.
5. Repeat for donor sets of size 1, 2, … to produce points on the curve.

Because the classifier is pure and inputs are persisted, this is a loop over rows and can be re-run
free of charge on every classifier change.

### 8.3 Honest limits, disclosed rather than discovered

- **Correlated donors.** `helm`, `switchboard`, and `maxwell` are run by one operator and share
  human habits, CI conventions, and partly the same repo constitution. That correlation *inflates*
  apparent transfer. The defensible early claim is transfer **across repositories and domains**,
  not yet across organisations.
- **Counterfactual, not observation.** "Would have been caught" is a model until the §7 control arm
  supplies an experimental anchor.
- **Two or three boards is a two-point curve** — directional evidence, not proof. It is still the
  difference between an asserted moat and a measured one in a diligence conversation.

## 9. Why this compounds on diversity, not volume

The honest concession first: **volume is the dimension where Switchboard loses.** A busy fleet
produces a few thousand decisions a year. GitHub holds merge provenance for the entire planet; if
Microsoft emitted agent-decision telemetry joined to outcomes, they would have six orders of
magnitude more within a week. Any strategy resting on "we have the data" is dead on arrival.

The learnable object is not how people write code. It is **under which control-plane configurations
agent work fails to land.** The conditioning variables are orchestration facts: which CI provider
and its API quirks (SIMPLIFY-24 exists because the GitHub Pulls REST endpoint omits status
contexts), branch-protection topology, required context sets, draft conventions and whether the
gate posts on drafts, merge queue versus direct merge, monorepo versus multi-repo, runtime and model
mix, review policy.

Catalogue coverage is therefore a function of how many **distinct configurations** have been
observed paired with verified outcomes. That reframes the competition:

- **GitHub** has planetary volume, observes essentially one configuration from the inside, and has
  no agent decision layer — outcomes without decisions, so no attribution.
- **Cursor / Anthropic** see sessions across many configurations but hold no authoritative outcome;
  they cannot tell whether work landed under a fenced decision.
- **Switchboard** is the only place configuration diversity, the decision, and provenance-verified
  outcome co-occur — a consequence of being model-agnostic and multi-runtime, which is existing
  positioning rather than a new bet.

The economics follow. The marginal value of another PR from an existing fleet decays quickly, since
that configuration's failure modes are already known. The marginal value of a *new* fleet is high,
because it usually arrives with an unobserved configuration. **Diminishing returns to volume,
increasing returns to tenants** — the shape of a network effect on tenant count, and the reason
volume-rich incumbents do not automatically win.

What makes the data clean enough to support any of this is the expensive machinery already built:
exact-head fencing, the immutable `execution_assignment`, and Done requiring merge provenance rather
than self-report. A competitor with 1000× the volume and no fencing holds *confounded* data — they
cannot attribute outcome to decision. That is an execution moat, not a data moat, and it should be
described as such.

## 10. The ratchet

When a signature reaches high confidence and its resolution is deterministic, it is promoted out of
the statistical catalogue and into the classifier itself: propose → human approve → apply, recorded
as a `decisions` row with a `decision_key` so it is auditable and revertible. This reuses the
existing `propose_deliverable_breakdown` / `approve_deliverable_breakdown` shape rather than
inventing an approval path.

Consequences worth stating plainly. Each fleet's failures permanently improve the shipped product
for every fleet, including those that never pooled anything. The improvement survives a customer
opting out, or pooling being switched off entirely. And it maps onto the open-core structure already
in [`PRODUCT_ROADMAP.md`](PRODUCT_ROADMAP.md): the open spec and adapters carry the vocabulary and
contribute signatures; the hosted tier holds the catalogue and the calibration.

No aggregate ever silently mutates a gate. That constraint is not negotiable in a codebase built on
fail-closed provenance.

## 11. ADR-0006 subtraction accounting

ADR-0006 §Decision-2 is binding: *"No new coordination mechanism — gate, provenance path, tracker,
ledger, monitor, or workflow engine — without deleting an overlapping one."* This document proposes
a ledger, so it owes an accounting in the format ADR-0006 §Consequences uses.

**Category argument for Phase 1.** `decision_records` carries no authority: it gates nothing,
routes nothing, and cannot block or unblock work. It is storage in exactly the sense ADR-0006
already blesses — "supporting ledgers (activity log, git_state) are storage, not mechanisms; they
stay." Phase 1 adds zero authority.

**Mechanisms retired:**

| Mechanism | Verdict | Action |
|---|---|---|
| RECON-8 event replay (`test_event_replay.py`, `replay_verify_batch`) | Parked since ADR-0006; aimed at generic board-event replay, a substrate that never justified it | **Retire.** Superseded by replay against the pure classifier — the same intent on the substrate SIMPLIFY-23 created. |
| RECON-9 coordination receipts (`coordination_receipts.py`, `receipt_projection_batch`) | Parked, "unproven vs. activity log + reconcile"; no defender has emerged | **Delete.** ADR-0006's own terms: every parked mechanism without a defender becomes a deletion. |
| `get_preflight_calibration` bespoke recommender | A second, narrower predicted-vs-actual engine whose recommendations nothing consumes | **Absorb** into one signature calibration path. One calibration mechanism, not two. |

**Mechanisms added:** one signature calibration path (absorbing preflight calibration, net zero);
one non-convergence circuit breaker whose only action is *stop dispatching and file attention* —
fail-closed, adding no capability to proceed.

- Mechanisms deleted: **3**
- Mechanisms added: **2** (one of which is a merge of an existing one)
- Net authority change: **−1**, and the one addition is strictly stop-authority

**Decision-4 compliance.** ADR-0006 makes product the fleet's default lane and coordination-layer
work the exception. This is not coordination-layer improvement; it is **H4 Productize** — the
commercial asset described in [`SWITCHBOARD-BACKEND-MOAT.md`](SWITCHBOARD-BACKEND-MOAT.md) §5–§7,
whose implementation was assigned to RECON-8 and DISPATCH-7 and never built.

Ratification: this needs to land as **ADR-0020** or as an amendment to ADR-0006's kill list. The
spec should not be implemented past Phase 1 until that record exists.

## 12. Phasing

Each phase is independently valuable, and no phase requires redesigning an earlier one, because the
projection boundary and `advice_version` are present from the first row.

**Phase 1 — Start the clock. — SHIPPED (COORD-50).** Registry, feature allowlist,
`decision_records` with both halves, `advice_version` stamped `NULL`, episode dedupe, retention
compaction. *Value at n=1: none directly — but the baseline is only obtainable prospectively, and
every week without it is a week of unrecoverable history.* This is the gate on everything else and
shipped in its dumbest working form.

| Artifact | Where it landed |
|---|---|
| `switchboard.reason_code.v1` | `src/switchboard/domain/decisions/reason_codes.py` |
| `switchboard.decision_features.v1` | `src/switchboard/domain/decisions/features.py` |
| `switchboard.decision_record.v1` | migrations `0117`–`0119`, `src/switchboard/storage/repositories/decision_records.py` |
| Write path | `run_completion_tick` appends one episode per tick, on automated ticks too |
| Retention | `compact_decision_snapshots` in the `background_jobs.py` catalogue |
| Counts | `get_reason_code_counts` / `list_decision_episodes` MCP tools |
| Proof | `tests/test_coord50_decision_corpus.py` |

Three deviations from the text above, all deliberate:

1. **`snapshot_hash` hashes the decision-relevant projection, not the raw snapshot body.** The
   hydrated snapshot embeds the whole task row and the session-health probe, both of which carry
   timestamps that move on every tick. Hashing the body would make every tick unique and defeat §5
   entirely. The identity is the classified verdict, the exact-head fence, and the materialized
   feature vector — anything that would change the decision changes the hash.
2. **`deliverable_id` and `host_id` are private-half columns**, added so counts can be scoped as
   the task required. They are named in `PRIVATE_COLUMNS` and asserted absent from every export,
   for the same reason head SHAs are: they identify the environment.
3. **Unregistered codes are surfaced by the count query**, not by a column on the ledger. The table
   stays as specified; `count_reason_code_episodes` returns `registered: false` and an explicit
   `unregistered_reason_codes` list. `switchboard.reason_code.v1` currently owns the completion
   authority's vocabulary; §15 Q3 leaves the other emitting subsystems open.

`§4.1`'s spelling rule is enforced by folding, not by editing the classifier: `required_ci_canceled`
and `required_ci_cancelled` are both still emitted by `_COORD_CI`, and `canonical_reason_code`
collapses them onto one registered cohort at the corpus boundary.

Phase 1 adds no authority — it gates nothing and routes nothing — so it does not consume the §11
subtraction budget. **Phase 2 onward must not start until ADR-0020 (or the ADR-0006 amendment)
exists and §11's three retirements actually ship.**

**Phase 2 — Replay harness.** Re-run the current classifier over the corpus; diff decisions against
what was recorded; surface every changed verdict for review. *Value at n=1: pays off on the very
next classifier change, and there have been eight in two weeks.* Retires RECON-8.

**Phase 3 — Non-convergence breaker.** Same task, same reason code, generation and fence epoch
incrementing, `head_sha` unchanged, across N episodes → stop dispatching, file attention with the
evidence bundle. Fail-closed by construction. *Value at n=1: directly saves spend, and Tally prices
exactly how much.* Establishes the wasted-generations baseline.

**Phase 4 — Contrast sets and advisory feedback.** A `background_jobs.py` catalogue entry plus a
systemd timer, following the `reconcile_alerts_resumable` pattern; findings delivered through the
existing `reconcile_alerts` signature-dedupe path rather than new alerting. Advice injected into
`acceptance_findings` and the `working_agreement` override, advisory only, with the §7 control arm
live. *Value at n=1: the draft incident gets caught by the machine.*

**Phase 5 — Cross-board transfer study.** The §8.2 protocol over the accumulated corpus. Produces
the two curves. *Requires no clients.*

**Phase 6 — Pooling and the ratchet.** Export/import of projected records, catalogue service,
opt-in participation, and §10's promotion path. *Only meaningful at n≥2 organisations; deliberately
deferred, and cheap to reach because the boundary was designed in.*

## 13. Risks

**Goodhart on the vocabulary.** Once codes are counted and counts influence policy, anything that
emits a code has an incentive to emit a flattering one. Mitigation: codes in the counted set are
emitted **only** by the state machine from snapshot facts. A runner may never self-report a
reason code into the aggregated set.

**Self-confirming statistics.** Addressed by §7; the risk is real and the mitigation must be built
before advice exists, not after.

**Storage on a small box.** Addressed by §5. If episode volume exceeds projections, snapshot-body
retention shortens first; the projected half is small enough to be permanent under any plausible
load.

**Mechanism sprawl — the ADR-0006 failure mode.** This document is exactly the sort of locally
justifiable coordination improvement that ADR-0006 exists to stop. The honest defences are the
subtraction accounting in §11, the H4/productisation framing, and the hard rule that Phases 1–4
add no authority to proceed with anything. If §11's retirements do not actually ship, this proposal
has violated the rule it claims to satisfy and should be reverted.

**Weak early evidence.** §8.3. Do not let the curve be presented as stronger than a correlated
two-point measurement until the control arm and a second organisation exist.

## 14. Loose end found while specifying this

`get_execution_transcript` hard-codes `"complete": False` with `incomplete_reason` citing
"durable full-session capture lands with the single session transport (SIMPLIFY-9)"
(`src/switchboard/application/commands/task_execution.py:1090-1094`). SIMPLIFY-9 is recorded **Done**
on the board, merged via PR #681, with its acceptance battery in
`tests/test_simplify9_single_session_transport.py`. Either capture landed and the flag is now
lying, or the transport shipped without persistence and the deferral needs a new owner. A hardcoded
partiality notice citing a closed task is the first thing a technical diligence reader will trip
over in a document about data assets.

## 15. Open questions

1. **Confidence thresholds.** `get_preflight_calibration` uses `min_outcomes=3`. Is three episodes
   enough to promote a signature to advisory? To gate promotion into the classifier? These should be
   distinct thresholds and both are guesses until Phase 5 measures them.
2. **Configuration identity.** §9's argument counts *configurations*, not tenants. What defines one
   — repository, CI provider, branch-protection shape, or a hash of the required-context set? This
   determines the x-axis of the network-effect curve and deserves a deliberate answer.
3. **Coverage beyond completion.** Placement, provider capacity, and AI admission all emit reason
   codes into the same registry. Should they write `decision_records` too, or is the pure-function
   property of `classify_completion` the thing that makes this worthwhile, in which case the corpus
   should stay scoped to completion until other authorities are equally pure?
4. **Retention versus offline evidence.** Escalated episodes are kept indefinitely as
   demonstrations. Does that interact with any customer data-retention commitment, and should
   snapshot bodies be encrypted at rest given they contain PR metadata?
