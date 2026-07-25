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
