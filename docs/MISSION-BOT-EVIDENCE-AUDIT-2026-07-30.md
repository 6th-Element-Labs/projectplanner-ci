# Mission Bot evidence-blindness audit — 2026-07-30

- **Status:** Evidence record. Closed for the v1 control path; the open items are carried by linked board tasks.
- **Scope:** Every path where Mission Bot cannot see evidence that exists, or cannot converge.
- **Evidence base for:** deliverable `mission-bot-v4` ([`MISSION-BOT-V4.md`](MISSION-BOT-V4.md))
- **Method:** 6 independent audit lenses over code + production state, each finding attacked by
  2 adversarial verifiers (refute-by-default, `real=false` when uncertain).
- **Production at audit time:** `b2ee3554` → `51b8e918`. Fixes landed as #1102 and #1103.

This document records what was found, what was *dis*proved, and what remains open. It exists
because the fixes are only half the value: the refutations say which parts of the system are
correctly built, and the open items are prerequisites for the v4 cutover.

## Why this audit happened

Overnight 2026-07-29/30, canary PRs sat green, exact-head-reviewed, and mergeable while
Mission Bot re-dispatched work at ~32-second intervals without ever arming merge. Two bugs had
been filed and fixed hours earlier (BUG-234, BUG-235), then both were rolled back on a
misdiagnosis (#1086), which reinstated the defect and wedged the fleet again. Re-landing them
(#1092) unwedged it and produced the first production proof of the ARM_MERGE path.

That sequence raised the real question: how many more places can Mission Bot fail to see
evidence it already holds? This audit answers it.

## The bug class

**Evidence blindness: evidence is judged by WHO or WHEN recorded it, rather than by WHAT it
proves about the exact commit.**

Every confirmed finding is an instance. The prior art names the same shape:

| Prior bug | Instance |
|---|---|
| BUG-234 clause 1 | Verdict compared by URL *spelling* (`api.github.com/...pulls/N` vs `github.com/...pull/N`) instead of PR identity |
| BUG-234 clause 2 | Playwright proof compared to the *newest* Work Session instead of validated on its own merits |
| BUG-235 | Arm failure reported as `node_id unavailable` — the real HTTP cause discarded |
| BUG-237 | Runner session refused because the *id* was reused, though the prior launch was dead |

The audit's contribution is showing the class is systemic rather than incidental, and
quantifying it against production.

## Confirmed findings

Severity is the verifiers' own: `blocks_merge_forever` means no dispatched role can clear it
without human action.

| # | Finding | Severity | Disposition |
|---|---|---|---|
| 1 | External CI evidence looked up by `task_id`, but the mirror/organic path keys runs by `source_sha` and leaves `task_id` NULL — **2066 of 2492 prod rows (83%)** | blocks | Fixed — BUG-239 / #1102 |
| 2 | Work Session preflight demanded on the newest session; an exact-head clean run sat on the implementation session | blocks | Fixed — BUG-240 / #1102 |
| 3 | Session-keyed gates read exactly ONE session (`ORDER BY updated_at DESC`); sibling evidence at the same exact head is invisible — **64 multi-session `(task, head)` prod pairs** | blocks | Fixed — BUG-241 / #1103 |
| 4 | Executed-test gate hard-failed `wrong_test_work_session` on a differing session id even when the run's own branch **and** head matched exactly | blocks | Fixed — BUG-241 / #1103 |
| 5 | Gate read `merged_payload`, so `complete_claim`'s `task_git_state.evidence` mirror — written for exactly this reader — was dead code | blocks | Fixed — BUG-241 / #1103 |
| 6 | Review escalations (`review_round_limit_reached`, `review_stalled_no_verdict`) routed to remediation runners that cannot clear them | blocks | Fixed — BUG-241 / #1103 |
| 7 | `facts.agent_requires_human` structurally unreachable — all three evidence sources absent from the hydrated snapshot | blocks | Fixed for v1 — BUG-241 / #1103; **v4 must re-prove:** QA-29 |
| 8 | `queue_failed` read a **successful arm** as a queue ejection (`mergeQueueEntry` is GraphQL-only, always absent under REST) | delays | Fixed — BUG-241 / #1103 |
| 9 | BUG-235's typed `GitHubPRFetchError` re-swallowed, so a 403/timeout became the definite verdict `github_pr_state_unavailable` | delays | Fixed — BUG-241 / #1103 |
| 10 | External-CI gate keyed on `task.git_state.head_sha` while every other head-fenced gate uses the live PR head | delays | Fixed — BUG-241 / #1103 |
| 11 | Autopilot-scope `task_id` compared through `_text`, an enum normalizer mapping `QA-24` → `qa_24` — the ADR-0008 **W2 stopped-scope fence never matched a real board id** | delays | Fixed for v1 — BUG-241 / #1103; **v4 must not reintroduce:** HARDEN-81 |
| 12 | Execution-publication branch binding never repaired across generations; both write paths swallow the refusal and write nothing | blocks | **OPEN — BUG-242** |
| 13 | UI/Playwright gate has the same single-session read surface in `_run_candidates` | delays | Partially addressed (resolver path already merit-based; fallback given the same rule) |

Two findings deserve emphasis because they were *silently dead safety mechanisms*, not
slowdowns:

- **#11** meant an operator pressing Stop did not fence the coordinator. It failed **open**,
  and no error was ever raised.
- **#7** meant that when an agent explicitly handed work to a human, Mission Bot never saw it
  and kept dispatching. `COORD-98` holds three valid server-stamped receipts that were ignored.

### Open item — and why it blocks v4

**BUG-242** is the only confirmed `blocks_merge_forever` finding not fixed. Publication rows
bind the branch of the first execution generation; a later generation pushes a new branch, and
`validate_event` forgives a head-only mismatch but never a branch mismatch. Both writers turn
the raised error into a returned dict and write nothing — so `task_git_state.head_sha` freezes
and the merge stamp is dropped. Three PRs merged on master 2026-07-27 still show
`merged_sha` NULL.

V4 makes verified canonical merge provenance the **only** path to `DONE` (acceptance criterion
6; rule W4) and keeps webhook/reconcile as an authoritative primitive. V4 also removes the
classifier fallback, so a mission whose provenance write is silently refused has no alternate
route and would hang harder than v1 does today. **Fix before cutover, not after.**

## What was disproved

Eight findings were killed with reasoning. These are load-bearing: they document parts of the
system that are correctly built, so a future audit need not re-litigate them.

- **Review-verdict URLs are safe by construction.** A finding claimed BUG-234 clause 1 survived
  in the merge-gate lookup. Refuted: `record()` validates with `same_pr_identity` and then
  overwrites `payload["pr_url"]` with the canonical spelling, so raw equality on read is correct
  *by design* — every row is byte-identical to `task_git_state.pr_url` at record time.
- **The `changed_files` oscillation does not exist.** Plausible on paper; the preflight
  machinery never writes that key, on any session, and prod confirms it is absent everywhere.
- **Orphaned Work Sessions are not a separate defect** — a re-description of finding #3 with the
  wrong fix location and an inflated blast radius. Session status only decides set membership.
- **The `OBSERVE_MERGED` infinite no-op** described the *retired* classifier; its live evidence
  was entirely pre-cutover, and the cited code takes an explicit failure branch.
- **`gates_green` using the unfiltered required-context list** is a real code asymmetry but
  cannot occur in this system's state: `required_status_contexts` comes only from repo topology.
- Three further findings about execution-publication fallback, `complete_claim`'s UI gate, and
  merge-webhook validation were refuted or reduced to already-known items.

## A fix that was rejected by policy

The audit recommended deriving executed-test proof from a green required status context on the
exact head. It was implemented, then **reverted before merge**.

The merge-group suite caught it:
`tests/conformance/scenarios/coord57_evidence_missing_no_ci_receipt.json` and
`co21_evidence_near_miss_key.json` both set `ci=pass` with the required context green,
`external_ci=none`, and expect `terminal=blocked`. That is deliberate committed policy — the
**#859 rule: never manufacture what the gate exists to demand.** Executed-test proof must come
from a recorded run, not a one-bit status projection.

Nothing in production needed the shortcut: QA-24's mirror receipt existed the whole time, and
finding #1 is what made it visible.

> **Rule for future work:** widening an evidence source is not automatically safe. Check
> `tests/conformance/scenarios/*.json` before relaxing any gate — those files are the committed
> policy, and the merge-group lane enforces them.

## Method and its limits

Six lenses ran in parallel — merge-gate evidence gates, the Mission Bot fact layer, loop
convergence, hydration/diagnostic-discard, empirical production forensics, and identity binding
across generations. Each finding was then attacked by two adversarial verifiers (one for code
correctness, one for production reproducibility), both instructed to refute and to default to
`real=false` under uncertainty. Only findings both verifiers confirmed are listed above.

Honest limits:

- **The second run's verifiers were rate-limited.** Roughly twenty items were dropped
  *unverified* rather than refuted. Most duplicate fixes already shipped, but a handful were
  never checked: no runtime bound on the reducer repeating an identical command, dispatches that
  produce zero wakes for hours, the `gh` subprocess having no timeout (a hung call stalls the
  coordinator tick), and the reconcile sweep discarding merge-stamp errors. **These are unknown,
  not clear.**
- **Lens output was capped** at six findings per lens before verification; the caps are recorded
  in the run coverage notes (2–5 truncated per lens).
- Prod journald retains only ~2h, so historical reconstruction relied on
  `decision_records` / `external_side_effects` / `completion_runs` plus GitHub timelines.
  (Mitigated since by #1095.)

## Bearing on Mission Bot v4

The audit independently confirms the v4 thesis. V4's subtraction ledger lists, as things to
remove, what this audit found empirically:

| V4 removes | Audit found |
|---|---|
| "evidence selection by newest Work Session" | Findings #2, #3, #4 — 64 affected prod pairs |
| "the possibility that a green PR waits because Mission Bot misunderstood its evidence" | The entire 2026-07-29/30 incident |
| "factory-selected remediation" / "factory-selected human escalation" | Finding #6 |
| "completion-route retry/convergence state" | Findings #8, #9 |

The fixes removed *instances*; v4 removes the *class*. Both were worth doing — and because v4
keeps merge-gate facts as advisory reads for the LLM, the gate fixes remain useful after cutover.

Three items are carried into the deliverable:

| Task | Role | Why |
|---|---|---|
| **BUG-242** | prerequisite, blocks | V4's only path to DONE runs through a write path that currently refuses silently |
| **QA-29** | acceptance | V4 elevates `agent_requires_human` to criterion 5; it was unreachable in v1 |
| **HARDEN-81** | acceptance | V4 deletes `completion_driver.py`; the W2 fence must be rewritten without the normalizer trap |

## References

- [`MISSION-BOT-V4.md`](MISSION-BOT-V4.md) — the design this evidence supports
- [`decisions/0008-three-plane-separation.md`](decisions/0008-three-plane-separation.md) — W1–W4 authority rules
- [`decisions/0022-one-fail-closed-ci-verdict.md`](decisions/0022-one-fail-closed-ci-verdict.md) — the single required verdict
- `tests/conformance/scenarios/` — committed gate policy; check before relaxing a gate
- PRs: #1092 (re-land), #1094, #1095, #1096, #1102 (BUG-239/240), #1103 (BUG-241)
