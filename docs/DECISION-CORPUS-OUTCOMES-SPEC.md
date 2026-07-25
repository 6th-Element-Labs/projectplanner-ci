# Decision corpus, part 2 — close the loop: outcomes, execution join, failing-check identity

Addendum to [DECISION-CORPUS-SPEC.md](DECISION-CORPUS-SPEC.md), which remains authoritative
for the registry, feature allowlist, ledger and signature schemas. That spec shipped the
hard half. This one specifies the three writers it scoped and left as column defaults.

## 1. What this decides

The corpus records **what was decided and why**. It does not yet record **what happened
next**. Every `decision_records` row is written `outcome='open'` and stays there, so the
ledger can prove a loop occurred but never that any attempt accomplished anything.

This is not a schema gap. `decision_records` already carries, verbatim:

```
# outcome, backfilled by reconcile / webhook
outcome            TEXT NOT NULL DEFAULT 'open',
head_advanced      INTEGER NOT NULL DEFAULT 0,
generations_spent  INTEGER NOT NULL DEFAULT 0,
merged_sha         TEXT NOT NULL DEFAULT '',
human_intervened   INTEGER NOT NULL DEFAULT 0,
human_action       TEXT NOT NULL DEFAULT '',
```

The comment names the missing component. The backfill was never built, so all six are
frozen at their defaults. **No migration is required for §3.1 or §3.2.**

## 2. The measured problem

Live evidence, CO-20 on 2026-07-25, 21 episodes across four heads
(`list_decision_episodes(task_id="CO-20")`):

| head | sequence |
|---|---|
| `8a79c572` | `missing_executed_test_run` (68 ticks) → `exact_head_pr_missing` (23) |
| `d150fd1b` | hydration_missing → ci_pending → **`required_exact_head_ci_failed` → remediation, seven separate episodes** (1, 2, 10, 1, 34, 6, 1, 20, 3 ticks), twice interrupted by `exact_head_pr_missing` |
| `c11fadf5` | ci_pending → `review_required` → `exact_head_pr_missing` |
| `a894afc0` | ci_pending → `review_required` |

**Every one of the 21 reads `outcome=open`, `generations_spent=0`, `head_advanced=False`** —
including the seventeen that were definitively superseded when the head moved on. The
corpus can therefore show that remediation was dispatched seven times against one head,
and cannot show that all seven achieved nothing.

Two further observations from the same window, which set the scope below:

**The feature projection is already strong.** That episode carried
`runner_head_matches_exact_head: False` with `runner_live: True` — the stale-runner
condition an operator spent a significant part of a session rediscovering by hand through
`list_runner_sessions`. It was in the log the whole time. The projection is not the weak
part; the outcome and the join are.

**The failing check is discarded.** `required_exact_head_ci_failed` says CI failed and not
what failed. The failing suite (`tests/test_bug174_repair_claim.py`) was only identified by
cloning the head and running the suite locally. Seven remediation attempts each received
the same contentless signal. This is the same shape as two other defects found the same
day — the merge-authorization gate computing `blocked[0]["code"]` and dropping it, and the
Fleet dock discarding `github_error: <exc>` for a hardcoded guess. **Recomputing what you
already had is the recurring bug in this system.**

## 3. Scope

### 3.1 Close the outcome (no migration)

Backfill the six existing columns from hooks that already fire.

| Trigger | Existing hook | Write |
|---|---|---|
| PR merges | `git.pr_merged` webhook | `outcome='merged'`, `merged_sha` on every open episode for that task |
| Head advances | new `head_sha` observed for a task | prior open episodes → `outcome='superseded'`, `head_advanced=1`, `generations_spent += 1` |
| Task reaches terminal Done | reconcile | open episodes → `outcome='done'` |
| Claim revoked / operator edit | `revoke_claim`, `update_task` | `human_intervened=1`, `human_action=<verb>` |
| Episode exceeds the retry budget | completion driver | `outcome='abandoned'` |

Vocabulary: `open | merged | done | superseded | abandoned | human_resolved`. Register it
in `switchboard.reason_code.v1`'s sibling position so an unregistered outcome is surfaced,
never silently counted — same rule the parent spec applies to reason codes.

**Backfill is idempotent and append-safe.** It only ever moves a row out of `open`; it
never rewrites `reason_code`, `features_json` or `snapshot_hash`. The episode stays the
immutable unit.

### 3.2 Join episode → execution (no migration)

`decision_records` already carries `host_id`, `generation` and `fence_epoch`; runner
sessions carry the same triple plus `execution_id`. The join is therefore possible today
and simply is not exposed.

- Add `execution_id` to the **projected** half so the join is direct rather than inferred.
- Surface it in `list_decision_episodes` output.

This is what turns "routed to remediation" into "routed to remediation, which produced
run_e9d5a081, which pushed nothing and expired" — the question an operator actually asks.

### 3.3 Carry the failing check identity (features only)

`_required_ci_decision` iterates the failing required contexts to reach its decision and
then discards their identity. Retain it in `features_json`:

- `failing_contexts`: names of the required contexts that failed (e.g. `["Switchboard CI / VM gate"]`)
- `failing_check_url`: the run URL already present on the status row
- `failing_check_summary`: the status `description`, truncated

Feature-only, so it is covered by the parent spec's allowlist discipline and export rules.
It changes no routing. It exists so a remediation runner — and a human — receives *which*
check failed rather than *that* CI failed.

## 4. Non-goals

- **Full execution transcripts.** That is SIMPLIFY-9. The parent spec deliberately bounds
  snapshot bodies to 90 days on a small box; transcripts are a separate storage decision.
- **Any change to routing.** This spec adds no decision behaviour. If a route changes as a
  result of implementing it, that is a defect in the implementation.
- **Backfilling historical episodes.** Existing rows stay `open`. The corpus is append-only
  and honest about when instrumentation began.

## 5. Why this is the leverage point

The expensive half is built: typed, fenced, justified decision records with a retained
snapshot and a versioned classifier, which means decisions are already replayable. What is
missing is the **label**. Without an outcome, the corpus is a diary; with one, every
episode becomes a training pair of (situation → decision → result), and the questions that
currently require a human become queries:

- which reason codes never converge, and after how many generations
- which routes actually resolve their reason code versus merely re-observe it
- whether a classifier change would have produced a better outcome, by replaying retained
  snapshots against the new version — the parent spec's stated end state

§3.1 alone is the difference between *"remediation ran seven times"* and *"remediation ran
seven times and resolved nothing, because the signal it received named no failing check."*

## 6. Acceptance

- No episode remains `open` once its task reaches a terminal state or its head advances.
- Replaying the 2026-07-25 CO-20 window shows the seventeen superseded episodes as
  `superseded` with `head_advanced=1`, and non-zero `generations_spent`.
- `list_decision_episodes` returns `execution_id`, and it resolves to a real runner session
  for at least one episode per dispatched route.
- A `required_exact_head_ci_failed` episode names its failing context(s) in `features_json`.
- `get_reason_code_counts` can express "reason codes that never reach a terminal outcome."
- Outcome vocabulary is registered; an unregistered outcome is surfaced, not silently counted.
- Regression tests over a synthetic window covering merge, supersede, human intervention and
  abandonment, plus idempotent re-backfill.

## 6a. As built (COORD-51) — three places the implementation is narrower than §3

**§3.3 diagnostics are stored in `features_json` but stripped on export.** §3.3 says the
failing check identity is "covered by the parent spec's allowlist discipline and export
rules". It is not: `EXPORT_COLUMNS` reads `features_json` wholesale, and parent spec §4.2
explicitly excludes **status context names** ("these leak internal tooling inventory") and
PR/check URLs from the poolable tier. Storing them there unchanged would have widened the
commercial disclosure document by a side door. As built, the three keys live in
`features_json` where a reader looks, are declared in `features.DIAGNOSTIC_FIELDS`, and
`export_projection` strips them — so the exported projection is still exactly the
twenty-three enum fields §6.3 promises. `execution_id` is a private-half column for the
same reason. Both are asserted by test.

**The head-advance hook lives in the episode writer, not in a git-state webhook.** §3.1
names the trigger "new `head_sha` observed for a task". The completion driver is the only
producer of episodes, so an episode arriving at a different head *is* that observation —
and it commits in the same transaction as the insert instead of depending on a webhook
delivery. A head going from a SHA to empty is a hydration regression, not an advance, and
is deliberately not counted as one.

**Every closer degrades visibly on a DB without the corpus table.** The closers run inside
the merge, Done, and revoke transactions. Since the corpus "carries no authority: it gates
nothing, routes nothing", it must not be able to fail a merge webhook on a DB whose
additive migration has not run — losing merge provenance would be far worse than an
unrecorded outcome. Absence returns `skipped: true, reason: "decision_records_absent"`;
every other storage fault still propagates.

Also worth recording: retaining the failing-check identity had to be made **independent of
context presentation order**. Taking "the first failing context" broke the 57,624-state
permutation-invariance model in `tests/test_bug172_completion_classifier_model.py` — the
same class of source-ordering defect as COORD-49. The identity is ordered by context name.

---

## 7. Part 3 — feed the corpus back into the loop

Sections 3.1–3.3 make the corpus answer questions **we** ask. This section makes it answer
the question **the agent** should have asked. It is specified here because it depends
entirely on §3.1: without outcomes there is nothing worth telling a runner.

### 7.1 The measured problem

A remediation runner receives exactly this today (CO-20, live, verbatim):

```json
{"reason_code": "required_exact_head_ci_failed", "route": "remediation",
 "acceptance_findings": [], "generation": 1, "exact_head_sha": "d150fd1b…"}
```

`acceptance_findings` is empty and there is no history field. Runner #7 was handed
precisely what runner #1 was handed.

That reframes the CO-20 loop. By the seventh dispatch the classifier was routing
*correctly* — CI genuinely was failing. The loop persisted because **every attempt was, from
the agent's perspective, the first attempt.** Seven agents each independently concluded
"CI failed, inspect CI," and none knew the previous six had already done exactly that and
changed nothing. This is amnesia, not misclassification, and no retry budget fixes it:
attempt seven has no reason to behave differently from attempt one.

### 7.2 What to add

Attach a bounded prior-attempt summary to `switchboard.execution_assignment.v1`:

```json
"prior_attempts": {
  "schema": "switchboard.prior_attempts.v1",
  "attempt_number": 7,
  "same_reason_code_on_this_head": 6,
  "reason_code": "required_exact_head_ci_failed",
  "routes_tried": ["remediation"],
  "outcomes": ["superseded", "superseded", "superseded", "superseded", "superseded", "superseded"],
  "head_advanced_since_first": false,
  "generations_spent": 0,
  "recent_execution_ids": ["run_fadc7ab2", "run_e9d5a081"]
}
```

Derived entirely from `decision_records` once §3.1 and §3.2 land. No new storage.

**Bounded by construction.** A summary, never a transcript dump: counts, the distinct
routes tried, the outcome vector, and at most a few execution ids. A task with two hundred
episodes must not produce a two-hundred-entry payload — cap the vectors and report totals.

### 7.3 The behavioural contract

The assignment is already declared lifecycle authority ("fail closed before claiming if
task_id, assignment_id, execution_id, generation, desired_role or exact_head_sha
disagrees"). Extend that contract:

> When `attempt_number > 1` and `head_advanced_since_first` is false, the previous
> approach demonstrably did not move the work. Do not repeat it. Either take a
> structurally different approach or escalate with a stated reason.

This is the whole point. A worker that knows it is attempt seven of an unmoved head should
escalate; a worker that thinks it is attempt one will re-diagnose CI forever.

### 7.4 Non-goals

- **Transcripts of prior attempts.** SIMPLIFY-9, and it would blow the payload budget.
  Counts and outcomes are sufficient to change behaviour; reasoning is not required.
- **Deciding for the agent.** This adds context, not a route. The classifier still owns
  routing.

### 7.5 Acceptance

- A second dispatch for the same reason_code and head carries `attempt_number: 2` with a
  non-empty `outcomes` vector.
- The payload is bounded: a task with 200 episodes yields a summary of fixed maximum size.
- Replaying the 2026-07-25 CO-20 window, the seventh remediation dispatch reports
  `attempt_number: 7`, `same_reason_code_on_this_head: 6`, `head_advanced_since_first: false`.
- A first-ever dispatch omits `prior_attempts` entirely rather than sending a zeroed object.
- Regression test asserting the cap holds and that the field is absent on attempt one.

### 7.6 Prerequisite, and a warning

Requires §3.1 outcomes to be meaningful — an `outcomes` vector of six `open` values tells a
runner nothing.

**It also requires the reason codes to be true.** `exact_head_pr_missing` fired 92 ticks
across four tasks in this window and was largely wrong — a symptom of DIRTY pull requests,
not a missing PR (BUG-182). Feeding a mislabelled history to a worker is worse than feeding
it nothing: it will confidently act on a false pattern. Fix the vocabulary before wiring it
into the loop.
