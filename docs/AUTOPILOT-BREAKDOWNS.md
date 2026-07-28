# Autopilot Breakdown Log

**A living, append-only log of where autonomous execution actually breaks.**

## How to use this file

- **Append, don't rewrite.** Add new entries at the end of the relevant run section, or
  open a new run section. Do not renumber or delete existing breakdowns — a fixed one
  gets a `**STATUS: FIXED**` line and the PR/commit that fixed it, not a deletion.
- **One breakdown per heading.** Give it a severity, the verbatim error/event evidence,
  why it matters, and a suggested fix direction. Evidence beats narrative.
- **Record the good path too.** "This worked hands-off" is data; it tells us what not to
  re-litigate.
- **Do not fix a breakdown just because you logged it.** These runs are observations.
  If you repair something mid-run you destroy the evidence for everyone else.
- File real defects as BUG tasks via `submit_bug` and cross-reference the id here.

---


**Run date:** 2026-07-25
**Operator:** claude/DOGFOOD-19 (observer mode — no intervention after launch)
**Host:** `host/steve-mbp-co16` (Steves-MacBook-14-PRO.local)
**Tasks launched:** UI-63, CO-20, ADAPTER-27, CO-21 (all `runtime=codex`, `role=implementation`)
**Deliverable under test:** `deliverable-coordinator-mediated-dispatch-t0-t1` (Autopilot MVP), milestone `m8-simplified-autopilot-acceptance`

> Standing instruction for this run: **do not intervene**. Document breakdowns only.
> Nothing below has been repaired.

---

## Step 0 — Preconditions (PASS)

Recorded before launch, as required by the 2026-07-21 amendment.

| Field | Value |
|---|---|
| host_id | `host/steve-mbp-co16` |
| status | online, `stale=false`, heartbeat_ttl 60s |
| agent_host_version | 0.3.999 |
| enrollment_id | `hostenroll-b91248c4bfb4405f` (identity_generation 1) |
| public_key_fingerprint | `sha256:c95655d511048ea525b7784e2dfd2dfbfa121e072af482aeff92211ca15fced1` |
| runtime_profile hash | `sha256:d91aa4ef937a480748b6b04a1b938fac9ac5d71071954212bf404948684014bd` |
| local_auth | `chatgpt_personal`, available, `acct-bb4e660b7a9319ed` |
| runner_watch | true |
| capacity at launch | max 16, active 0, **headroom 16** |
| BUG-112 | Done (required precondition) |

Headroom was real capacity, not the BUG-111 masking mode (zero active sessions).

---

## BREAKDOWN 1 — `start_task` refuses an unregistered operator identity

**Severity:** low (UX / launcher ergonomics)
**Observed:**
```
start_task(task_id=UI-63, agent_id=claude/DOGFOOD-19)
-> error_code: start_refused
   start_error: agent_not_registered
   "agent_id is not currently registered/heartbeat-active."
```
**Why it matters:** the operator/launcher path is documented as "the same Connect door as the UI Start button," but it requires the caller to already be a live registered agent. An operator surface has no natural reason to be a heartbeat-active *worker*. Discoverability is poor — the error names the symptom, not the required flow.

**Not a blocker.** Resolved by registering. Recorded because it is friction on the exact path autopilot/operators use.

---

## BREAKDOWN 2 — `start_task` requires the caller to be registered *against that same task*

**Severity:** medium (operator/autopilot ergonomics)
**Observed:** after registering as `claude/DOGFOOD-19` bound to task `DOGFOOD-19`, launching a *different* task fails:
```
start_task(task_id=UI-63, agent_id=claude/DOGFOOD-19)
-> error_code: start_refused
   start_error: agent_registered_on_different_task
   "agent_id is live but not bound to this task."
```
**Consequence:** launching N tasks requires N `register_agent` calls, re-binding the operator identity to each task in turn. `prepare_agent_session(mode="launcher")` confirms this is the intended flow — its `first_calls` include a `register_agent` pinned to the target task before `start_task`.

**Why it matters:** an operator arming a deliverable of 10 tasks must re-register 10 times, and the operator identity ends up bound to whichever task was armed last — which is misleading provenance. The launcher mode declares `allowed_actions: [start_task, get_task_execution]` and `forbidden_actions: [claim_task, claim_next]`, i.e. it *knows* the caller is not a worker, yet still demands worker-style per-task binding.

**Suggested fix direction:** allow a launcher-mode principal to start any task in its project without per-task re-registration, or let `start_task` accept an explicit operator actor distinct from the worker `agent_id`.

---

## BREAKDOWN 3 — `start_task` dispatches a live runner for a task whose dependencies are unsatisfied ⚠️

**Severity:** HIGH — wastes real capacity and real provider quota
**Task:** CO-21

**Observed:** CO-21 depends on CO-20 and ADAPTER-27, neither of which was complete at launch:
```
dependency_state: ready=False, satisfied=False
  BLOCKING: CO-20      (In Review)
  BLOCKING: ADAPTER-27 (In Progress)
```
Despite this, the whole dispatch chain succeeded:
```
start_task -> action=started, wake-f83f84cb7b794a48, queue_position 1
board events: wake.requested -> wake.claimed -> direct_session.mcp_token_issued
              -> runner.session_registered -> wake.completed -> side_effect.verified
              -> work_session.created -> work_session.updated
MISSING EVENT: task.claimed
```
Resulting state:
```
lifecycle_phase: running
runner: run_90d2da81c3f6c349 on host/steve-mbp-co16, status=running
active_claims: []
board status: Not Started
```

**The defect:** dependency readiness is enforced at **claim** time, not at **dispatch** time. So Switchboard spawned a native Codex CLI process, issued it an MCP token, created a Work Session, and consumed a host slot — for a task the runner can never claim. The runner is live and idle-looping against unclaimable work, burning the operator's ChatGPT-personal quota.

**Contrast:** `claim_task` gets this right and refuses. The gate exists; it is simply downstream of the expensive operation.

**Suggested fix direction:** `start_task` should evaluate `dependency_state.satisfied` before requesting a wake and refuse with a typed error (`dependencies_unsatisfied`), consistent with how it already fails closed on capacity/runtime mismatch. Cheap check, expensive omission.

**Watch item:** whether this orphaned runner self-terminates, times out, or leaks the host slot — the BUG-111 failure mode (terminal runners still heartbeating and masking zero headroom). Being observed, not repaired.

---

## BREAKDOWN 4 — Branch-prefix does not match the launched runtime

**Severity:** low-medium (provenance / fleet attribution)
**Observed:** all four tasks were launched with `runtime=codex`, but the branches created are:

| Task | Branch | Expected prefix |
|---|---|---|
| UI-63 | `codex/UI-63-execution-readiness` | ✅ codex |
| CO-20 | `claude/CO-20-hybrid-placement` | ❌ claude |
| ADAPTER-27 | `claude/ADAPTER-27-workspace-materializer` | ❌ claude |

The host advertises only `work_modules: {codex: adapters.codex_local_worker:run}` and `local_auth.runtime=codex`, so all three should have produced `codex/` branches.

**Why it matters:** `pr_provenance_gate.py` decides fleet-vs-operator by branch prefix (`DEFAULT_FLEET_BRANCH_PREFIXES = ("cursor/", "codex/", "claude/", "agent/", "devin/")`). Both prefixes are in the fleet list so gating still works, but branch prefix is being used as a runtime/attribution signal and is now unreliable. Worth determining whether the worker self-names from something other than the dispatched runtime.

---

## OBSERVATION — the hands-off path does work

Not a breakdown; recording it because it is the thing the proof is meant to establish.

Within roughly five minutes of launch, with **zero operator interaction**:

- **CO-20**: launched → implemented → branch pushed → **PR #863 opened** → board status **In Review**
- **UI-63**: launched → implemented → branch pushed → **PR #864 opened**
- **ADAPTER-27**: launched → branch pushed, In Progress

Full event chain observed per task: `agent.registered` → `wake.requested` → `wake.claimed` → `direct_session.mcp_token_issued` → `runner.session_registered` → `work_session.created` → `task.claimed` → implementation → PR.

Both PRs show `Switchboard / claim gate: SUCCESS` — the claim-gate binding is working end to end.

---

## OPEN AT TIME OF WRITING

| PR | Task | VM gate | Playwright | Merge auth | Merge state |
|---|---|---|---|---|---|
| #863 | CO-20 | PENDING | PENDING | FAILURE | BLOCKED |
| #864 | UI-63 | PENDING | PENDING | FAILURE | BLOCKED |

`merge authorization: FAILURE` is **expected while CI is PENDING** (it fails closed on missing required contexts). The real test is whether it flips to SUCCESS once the VM gate and Playwright go green — that is the first genuine exercise of today's BUG-176/177 fix (`cdd6ec5d`, deployed to prod) in a fully hands-off run. **If it stays FAILURE after CI turns green, that is the next breakdown to capture.**

---

## PRIOR CONTEXT (fixed earlier today, for reviewer orientation)

These were repaired before this run and are *not* open issues:

1. **Fleet merge wedge** — since 2026-07-23 every agent PR was unmergeable while operator PRs were exempt (`Exempt: non-fleet (human/operator) PR`). Cause: `b21b9d2a` (#836) made `Switchboard / merge authorization` a required check, `956f0419` BUG-172 (#849) added `adversarial_self_review_forbidden` so a single agent could not produce a verdict, and merge_gate never resolved a Work Session by `task_id` while the CI gate supplies neither `work_session_id` nor `claim_id`.
2. **Fixes landed:** #856 (self-review fence removed), #859 (task-scoped Work Session resolution + preflight repair text), #857 (de-flaked `test_task_open_latency.py`). All deployed to prod at `cdd6ec5d`.
3. **BUG-177 as originally filed was wrong** — the preflight requirement is satisfiable via the BUG-159 `coordinator_unverifiable` path; the reporting agent had simply never run `preflight_work_session`. Corrected on the task and in the fix.
4. **DOGFOOD-19 itself was stale-Blocked** with all five dependencies Done and `blocking: []`. A prior attempt had been killed (`runner.kill_requested` → `kill_completed`). Cleared to Not Started before this run.

---

## BREAKDOWN 5 — Runners open **draft** PRs, which can never pass the merge gate ⚠️

**Severity:** HIGH — hard dead-end for hands-off completion
**Observed on:** CO-20 (#863), ADAPTER-27 (#865). UI-63 (#864) was not draft.

Merge-authorization status on both draft PRs, at a head whose CI is fully green:
```
Switchboard CI / VM gate:        SUCCESS
Switchboard UI / Playwright:     SUCCESS
Switchboard / claim gate:        SUCCESS
Switchboard / merge authorization: FAILURE -> "Draft PRs cannot pass the merge gate."
```
```
#865 draft=True  commits=2  claude/ADAPTER-27-workspace-materializer
#863 draft=True  commits=4  claude/CO-20-hybrid-placement
```

**The defect:** the worker opens its PR as a draft and nothing in the loop ever marks it
ready-for-review. The merge gate refuses drafts by design, so the PR is permanently
unmergeable. The agent responds by pushing *more commits* — CO-20 reached 4 commits
across 4 distinct head SHAs (`259812aa` → `6e70a520` → `0738fc94` → `8a79c572`) — because
the failing gate reads as "work not finished." It is not a code problem and more commits
can never fix it.

**Net effect:** an infinite remediation loop that burns provider quota and host slots
while the PR sits in a state the gate will never accept. This alone prevents the
Autopilot MVP acceptance from ever completing hands-off.

**Suggested fix direction:** either the worker marks the PR ready when it believes the
work is complete (`gh pr ready`), or the merge gate treats "draft" as a distinct,
*actionable* terminal signal that routes to a "mark ready" step rather than to generic
remediation. Today the agent cannot tell "your PR is a draft" apart from "your code is
wrong" — the retry policy is identical for both.

---

## BREAKDOWN 6 — Every remediation push invalidates the review verdict, and the worker never records a new one

**Severity:** HIGH
**Observed on:** UI-63 (#864), the one non-draft PR.
```
failure -> "Review required for current head 915c2ee2a92015e023ee381621918c35f511596a."
```
Review verdicts are exact-head bound (correctly — that is the anti-stale-proof property).
But each remediation push creates a new head, invalidating the prior verdict, and the
worker does not record a verdict for the new head. So the loop is:

> push → verdict invalid for new head → gate fails "review required" → agent treats it as
> a code problem → push again → …

**Suggested fix direction:** the completion loop must record an exact-head verdict as part
of *each* push cycle, or the gate must distinguish "no verdict yet for this head" (an
actionable step the worker can take) from "review found problems" (which needs code
changes). These are currently the same signal to the worker.

---

## BREAKDOWN 7 — Workers do not attach `executed_test_run` in completion-evidence form

**Severity:** MEDIUM-HIGH
**Observed on:** UI-63 (#864).
```
failure -> "Merge gate requires a passing executed test run with output/log hash."
```
The worker ran CI (the VM gate is SUCCESS off-box) but never recorded a
`switchboard.executed_test_run.v1` object with commands, completion timestamp and an
output hash where the gate reads it.

**Same failure class the coordinator-autopilot hit on COORD-47 earlier the same day** —
its own receipt read `reason_code: missing_executed_test_run`, `route: coordination_retry`,
`effect: none`, looping at generation 5. So this is not specific to these four tasks; it is
the standard way autopilot stalls.

**Note:** `merge_gate(evidence_json=...)` accepts the object directly, and
`update_work_session` silently drops an `executed_test_run` field (`updated:false`) — a
worker writing it to the Work Session would believe it succeeded while the gate still
sees nothing.

---

## BREAKDOWN 8 — Runner sessions accumulate on the host

**Severity:** MEDIUM (capacity leak; BUG-111 adjacent)
**Observed:** 4 tasks were launched. Host session count over the run:
```
at launch:      active_sessions=0  -> 4 after the four start_task calls
~30 min later:  active_sessions=6, available=10 (max 16)
```
Session count grew to 6 with no additional tasks started, and nothing has reached a
terminal state. Combined with BREAKDOWN 3 (CO-21's runner running against an unclaimable
task), this is the BUG-111 shape: terminal or useless runners continuing to heartbeat and
consuming headroom. Not yet at zero headroom, so not fatal in this run — recorded because
it trends the wrong way and the 2026-07-21 amendment requires the host slot to be
recovered without manual database repair.

---

## RUN SUMMARY (as of this entry)

**What worked, hands-off and unaided:** launch → claim → isolated worktree → implement →
push → PR open → **full CI green** (VM gate + Playwright SUCCESS on all three) → claim gate
SUCCESS. That is most of the lifecycle, working with zero operator input.

**Where it dead-ends:** every one of the three PRs is stuck at merge authorization, for
three *different* reasons — draft state (×2), missing exact-head verdict, missing executed
test-run evidence. None of them is a code-quality problem, and in every case the worker's
response is to push more commits, which cannot help.

**The common root:** the merge gate returns a single undifferentiated "failure" to the
worker, so process-state problems (draft PR, missing verdict, missing evidence) are
indistinguishable from "your code is broken." The worker's only lever is another commit.
Until the gate's typed reason codes are routed to distinct worker actions, hands-off
completion cannot close, no matter how good the implementation is.

**Not repaired.** Left in place for review, per the run's observer protocol.

---

## CORRECTION to BREAKDOWN 8 — runner growth was **not** a leak

**STATUS: WITHDRAWN.** The earlier entry called 6 concurrent runners a capacity leak on
4 launched tasks. `list_runner_sessions` shows that reading was wrong:

| Runner | Task | Role | Why |
|---|---|---|---|
| `coordinator-autopilot/3f4da0e93df4` | CO-20 | **remediation** | `reason_code: required_exact_head_ci_failed` |
| `coordinator-autopilot/7598d05c47f4` | UI-63 | **remediation** | same |
| `coordinator-autopilot/7598d05c47f4` | ADAPTER-27 | **remediation** | same |
| `claude/DOGFOOD-19` | CO-21 | implementation | genuine orphan (BREAKDOWN 3) |
| `agent/codex/bug-178` | BUG-178 | implementation | **expired cleanly**, PR #867 |
| `agent/codex/bug-179` | BUG-179 | implementation | autonomously filed + dispatched |

Three are legitimate **remediation** runners the coordinator dispatched after detecting
CI failure at the exact head. Two are autonomously-created BUG tasks. Only CO-21 is a
real orphan. Growth 4 → 6 is the system reacting correctly, not leaking.

**Lesson for future entries in this file:** a raw counter (`active_sessions`) is not
evidence of a leak. Resolve every session to its task, role and `reason_code` before
calling it one. This entry is left in place rather than deleted, as the header requires.

---

## OBSERVATION — autonomous bug intake and clean terminalization both work

**BUG-178** was created, dispatched, implemented and handed off with **no operator
involvement**, and its runner ended *correctly*:
```
completion_handoff:
  pr: #867   head_sha: 5fd70534…   git_diff_check: clean
  executed_test_run: { commands: [...5 commands...], exit_code: 0,
                       output_hash: sha256:667160f5…, status: success,
                       work_session_id: worksession-958275775b83403e }
lease_surrender: { reason: "completion_requested", lease_epoch: 2 }
terminalized_by: runner_lease_expiry
```
This matters for two reasons:
1. It **narrows BREAKDOWN 7**. Workers *can* produce a well-formed
   `switchboard.executed_test_run.v1` — BUG-178's is complete and correct. So the
   evidence gap on UI-63 is not "the runtime cannot do this"; it is specific to the
   remediation path. Worth re-scoping rather than treating as universal.
2. Host slot recovery works: the lease surrendered with a reason and terminalized on
   expiry, which is exactly what the 2026-07-21 amendment demands.

---

## BREAKDOWN 9 — reason codes are recorded but never aggregated (no learning loop) ⚠️

**Severity:** HIGH (product/strategic, not a runtime defect)

Switchboard records decisions *and their justification* with unusual rigor. Every runner
carries an immutable `switchboard.execution_assignment.v1`:
```
reason_code:    required_exact_head_ci_failed
route:          remediation
desired_role:   remediation
exact_head_sha: 259812aa…   generation: 1   fence_epoch: 1
```
plus `runner_lease_surrender.v1` (why a lease ended), `terminalized_by`, an idempotent
side-effect ledger with `payload_hash` and provider readback, exact-head review verdicts
that preserve `invalidated_by_head_sha` instead of deleting history, `merge.gate`
activity events, and a cursored per-task event stream.

**The gap:** nothing ever *counts* these. In this run the identical
`required_exact_head_ci_failed` fired on **3 of 3** PRs, and the true cause was not CI at
all — two PRs were drafts and one lacked an exact-head verdict. No surface aggregates
reason codes across tasks, so a systemic, single-cause stall reads as three unrelated
per-task retries. A human had to notice.

**Why it's the moat:** the expensive part (typed, fenced, justified decision records) is
already built. What is missing is cheap by comparison — count reason codes over a window,
per deliverable and per host, and alert when one dominates. That converts a forensic log
into a system that gets smarter as it runs.

**Suggested fix direction:** a `reason_code` rollup alongside the existing
`get_review_remediation_metrics` / `get_saturation_signals` / `get_plan_signals`, with a
"same reason_code on N tasks in window W" signal routed to the attention queue. Cheap,
and it would have caught this run's real problem in one glance.

---

## BREAKDOWN 10 — execution transcripts are incomplete, so *reasoning* is not retained

**Severity:** MEDIUM-HIGH (blocks the learning loop above)

`get_execution_transcript` documents that `complete` is **always false** with an explicit
`incomplete_reason` — full session capture is deferred to **SIMPLIFY-9**. Observed live:
`log_tail: ""` and `last_snapshot: {}` on every running session, with only a host-side
`stdout.log` path on disk.

**Consequence:** outcomes are durable but the agent's *reasoning* is not. We can prove
what a runner decided and what it produced, never why it chose that path or where it went
wrong. For the learning objective in BREAKDOWN 9 that is the missing input — reason codes
tell you a route was taken, transcripts would tell you whether it was the right one.

**Suggested fix direction:** land SIMPLIFY-9 session capture, and persist transcripts to
the same durable store as the assignment records so a post-hoc analyzer can join
`reason_code` → transcript → outcome.

---

## WHAT TO CAPTURE GOING FORWARD (product recommendations)

Concrete gaps this run exposed, ordered by leverage:

1. **Aggregate reason codes** (BREAKDOWN 9). Highest leverage, lowest cost. Would have
   diagnosed this entire run instantly.
2. **Make gate failures actionable per type.** The gate already emits typed codes
   (`draft_pr`, `review_required`, `missing_executed_test_run`), but the worker receives
   one undifferentiated "failure" and answers every one with another commit. Route
   process-state codes to process-state actions.
3. **Capture full transcripts** (SIMPLIFY-9) so reasoning is analyzable, not just outcomes.
4. **Record dependency readiness at dispatch**, not only at claim (BREAKDOWN 3).
5. **Emit a per-run economic summary** — runners spawned, quota consumed, commits pushed
   against unwinnable gates. This run burned three remediation runners on a problem no
   commit could fix; nothing surfaced that cost.

---

## BREAKDOWN 5 — ESCALATED: draft state is the single dominant blocker (3 of 4)

**Severity: HIGH → this is the headline finding of the run.**

The decisive case is **BUG-178 / PR #867** — the run that did everything right:
autonomously filed, implemented with no operator involvement, produced a complete
`switchboard.executed_test_run.v1` (5 commands, `exit_code: 0`, output hash, work-session
bound), surrendered its lease with an explicit reason, terminalized cleanly via
`runner_lease_expiry`. There is no evidence gap, no verdict gap, no CI gap.

It is blocked anyway:
```
#867  draft=True   Switchboard CI / VM gate: SUCCESS
      Switchboard / merge authorization: FAILURE
      -> "Draft PRs cannot pass the merge gate."
```

Full picture across the mature agent PRs:

| PR | Task | Draft | CI | Merge auth | Actual blocker |
|---|---|---|---|---|---|
| #863 | CO-20 | **yes** | SUCCESS | FAILURE | draft (4 commits pushed at it) |
| #865 | ADAPTER-27 | **yes** | SUCCESS | FAILURE | draft |
| #867 | BUG-178 | **yes** | SUCCESS | FAILURE | draft — *with flawless evidence* |
| #864 | UI-63 | no | SUCCESS | FAILURE | missing exact-head verdict + evidence |
| #868 | BUG-179 | no | pending | – | too new to judge |

**Three of four mature PRs are stuck on one word.** Not code quality, not CI, not the
BUG-176/177 merge-gate fix deployed earlier the same day (`cdd6ec5d`) — that fix works and
was verified live. The workers open drafts and nothing ever marks them ready.

**Why it compounds:** the gate returns a single undifferentiated `failure` to the worker,
so "your PR is a draft" is indistinguishable from "your code is wrong." The worker's only
lever is another commit. CO-20 pushed **4 commits across 4 head SHAs** plus consumed a
dedicated remediation runner, against a gate no commit can ever satisfy. Every one of those
pushes also invalidated its review verdict (BREAKDOWN 6), deepening the hole.

**This is the cheapest high-leverage fix available.** Either:
- the worker calls `gh pr ready` when it considers the work complete, or
- the merge gate routes `draft` to a distinct "mark ready" action instead of generic
  remediation.

Either change unblocks 3 of 4 PRs in this run. Nothing else in the log has this
leverage-to-cost ratio.

**Corollary for BREAKDOWN 7:** BUG-178 proves the evidence machinery works end to end.
Do not chase "workers cannot produce executed_test_run" as a general defect — scope it to
the remediation path, where UI-63 failed.

---

# Session 2 — after COORD-49 / CO-25 / ADAPTER-30 / COORD-50 / BUG-184 landed

Same protocol: observe, record, do not repair mid-run. All four fixes from session 1 are
merged AND deployed to prod (`a07af674`), so this section describes behaviour with them live.

## OBSERVATION — the session-1 fixes verifiably worked

- `required_exact_head_ci_failed` fell from dominant (3 of 3 PRs) to absent in the window.
- `draft_ready_to_mark_ready` began firing on the `review_merge` route — the branch that was
  structurally unreachable before COORD-49.
- `review_required` now routes to `review_merge` rather than remediation, i.e. the fix
  generalised past draft to the whole Class A family exactly as intended.
- BUG-184 stopped the completion tick crashing; episodes persist and the corpus fills.
- Two PRs merged fully hands-off on their own gates: #865 ADAPTER-27 (`0ec6988f`) and
  #864 UI-63 (`1772cab0`).

**Accuracy note on the day's merge count.** Six PRs reached master, but only those two were
hands-off. #886, #885 and #868 were **admin merges** by the operator, and #863's conflicts
were resolved by hand. Counting all six as autopilot successes would be wrong.

---

## BREAKDOWN 11 — nothing reaps a runner whose pinned head no longer exists ⚠️

**Severity:** HIGH. Filed as **BUG-186** (blocking). **Stalled the whole board twice in one day.**

A runner is dispatched with an immutable assignment pinning `exact_head_sha`, and is
instructed to fail closed on mismatch — which it correctly does. When the PR head then moves,
**nothing terminates it.** It stays alive, unable to act, holding the execution lease that
would allow a fresh dispatch at the real head.

```
Occurrence 1 (~05:00): five runners stale simultaneously
  CO-20       pinned 259812aa   PR head 8a79c572   alive 4.5h
  UI-63       pinned fb652f50   PR head 915c2ee2   alive 4.3h
  ADAPTER-27  pinned cf496b36   PR head e49b68a8   alive 4.3h
  CO-21       no head at all    orphan             alive 4.7h
Occurrence 2 (~07:00, after BUG-184 deployed): one runner, same shape
  CO-20  run_e9d5a081  pinned d150fd1b   PR head a894afc0   alive 2.8h
```

**Fingerprint:** a very high tick-to-episode ratio. Occurrence 2 showed `review_required`
at 584 ticks across 5 episodes (~117:1). The classifier decided correctly every tick;
nothing could act.

**Why it is systemic.** Any head change orphans the current runner, and head changes are
routine — remediation pushes, rebases, and *a sibling PR merging*, which forces every other
branch in the deliverable to rebase. #863 went stale twice for exactly that reason, once
when ADAPTER-27 merged and once when UI-63 merged. The more parallel work in a deliverable,
the more often this fires.

**It masks other fixes.** Occurrence 2 was initially misread as "BUG-184 did not work,"
because the board looked frozen immediately after a correct fix deployed. BUG-184 *had*
worked. A held lease is indistinguishable from a dead system from the outside. **Check
`runner_head_matches_exact_head` before concluding a classifier or gate fix failed.**

**The detection input already exists and is unused.** `decision_records.features_json`
carries `runner_head_matches_exact_head` and `runner_live` on every episode. Both
occurrences were fully described in the corpus while an operator rediscovered each by hand
via `list_runner_sessions`.

---

## BREAKDOWN 12 — the completion driver ticks tasks that no autopilot scope owns

**Severity:** MEDIUM (waste, and it disguises itself as a stall)

After the stale runner in occurrence 2 was killed, CO-20 had a free lease, the host had
16/16 headroom, the tick was healthy and the route was correct — and **nothing was
dispatched.** Cause:

```
get_autopilot(project-independent-execution-plane) -> scopes: []
```

No armed scope. The prior scope was **task-scoped to COORD-47** and completed when COORD-47
merged; nothing re-armed at the deliverable level. So the completion driver went on
classifying three tasks that no scope owned:

```
review_required   6 episodes / 604 ticks / 3 tasks   route=review_merge   resolver=agent
```

Correct decisions, indefinitely, with no actor to execute them.

**Why it matters beyond the wasted compute.** It is externally indistinguishable from a
stall: PRs sit, ticks climb, nothing moves — the same surface as BREAKDOWN 11 and the same
surface as a broken classifier. Diagnosing it costs a scope check that nothing prompts you
to make.

**Fix direction:** either the driver should not tick a task no scope owns (and should say
so), or an unowned-but-ready task should surface as an explicit "needs arming" state rather
than silently accumulating episodes. A task generating 604 ticks with `resolver: agent` and
no agent is a condition the system should name itself.

**Note this is arguably by design** — the Autopilot MVP's end state is "one operator action
arms a deliverable." The gap is that nothing reports the *absence* of that action, so an
unarmed deliverable and a broken one look identical.

---

## RUNNING TALLY OF THE ONE RECURRING DEFECT

Every one of these is the same shape: **a diagnostic is computed and then destroyed.**

| # | Where | What is discarded |
|---|---|---|
| 1 | merge-authorization gate | `blocked[0]["code"]`, dropped from the published status (COORD-49) |
| 2 | Fleet dock | server's `github_error: <exc>`, overwritten with "GitHub is unreachable" (BUG-185) |
| 3 | `_required_ci_decision` | names of the failing required contexts (COORD-51 §3.3) |
| 4 | coordinator receipt | exception *message* kept only as class name — `CompletionRunError` vs `unsupported completion state: assessing` (BUG-184) |
| 5 | decision episodes | `runner_head_matches_exact_head` computed into features, consumed by nothing (BUG-186) |

Five instances in one day. This is a convention or a lint, not five independent fixes.

# RUN 2 — 2026-07-25, intervention session

**Operator:** claude/COORD-50
**Host:** `host/steve-mbp-co16`
**Mode:** **INTERVENTION, not observation.** Unlike Run 1, repairs were made during this
run at the operator's instruction.

> ⚠️ **Evidence caveat.** This run violated Run 1's "do not fix what you log" rule *by
> instruction*. BREAKDOWN 11 was repaired and deployed mid-run, and six PRs were merged
> (three by admin bypass). Where a breakdown below is marked FIXED, the original failing
> state is no longer reproducible on prod — the quoted evidence is all that survives.
> Everything marked LIVE was still failing at the end of the run and was not touched.

**Starting state:** every agent PR blocked; the scoped completion coordinator reporting
`lifecycle: janitor_only` with an `action_census` of all zeros — zero dispatches, zero
reviews, zero remediations, for hours.

---

## BREAKDOWN 11 — the classifier emits a state its own store rejects, killing every review tick ⚠️

**Severity: CRITICAL. STATUS: FIXED — BUG-184, [PR #888](https://github.com/6th-Element-Labs/projectplanner/pull/888), master `a07af674`.**

`classify_completion` is the decision authority; `completion_runs` records what it decided.
Their vocabularies had silently diverged:

```
classifier can emit : assessing, blocked, ready_to_queue, reconciling, waiting, waiting_merge_queue
store accepts       : blocked, done, failed, implementing, ready_to_queue, reconciling, waiting, waiting_merge_queue

EMITTED BUT REJECTED: ['assessing']
```

`assessing` is the review branch — `review_required`, `review_verdict_stale`, and the review
findings route. It is the most common route in the system, and it was the one state the
store refused. Because `run_completion_tick` persists *before* it plans:

```python
decision = classify_completion(current, snapshot)
persisted = ensure_completion_run(...)   # ← CompletionRunError raised here
plan      = plan_effect(decision, snapshot, persisted)
result    = execute_effect(...)
```

…every review tick died at the persist step — before planning an effect, before fencing a
stale runner, before dispatching a review generation. Verbatim, from the coordinator's own
receipts, repeating every ~60s:

```json
{"status":"completion_tick_failed","error":"CompletionRunError","task_id":"CO-20"}
{"status":"completion_tick_failed","error":"CompletionRunError","task_id":"BUG-179"}
{"status":"completion_tick_failed","error":"CompletionRunError","task_id":"BUG-183"}
"lifecycle": {"status":"janitor_only","action_census":{ ...all zeros }}
```

Meanwhile #863, #868 and #885 sat blocked on "Review required" — waiting for a review the
autopilot could not record the decision to request.

**Why it went undetected:** the two enums landed two PRs apart — the store in #818
(SIMPLIFY-22), `assessing` in #820 (SIMPLIFY-23) — and no test asserted they agree. Routes
and phases were checked during this run and *do* agree; `assessing` was the only drifted
value, so this is one bug and not a family.

**Fix:** one line in the enum. The guard is the part that matters — a test reads every
`_decision` call out of the classifier's AST and asserts its states and routes are subsets
of what the store accepts, so a new branch is covered the moment it is written rather than
when someone remembers a fixture. 4 of the 8 new tests fail with the one-line change reverted.

---

## OBSERVATION — a *correct* fix can move traffic into an unwritable state

This is the most transferable lesson of the run, and it explains why the previous day's work
appeared to accomplish nothing.

COORD-49 ([PR #878](https://github.com/6th-Element-Labs/projectplanner/pull/878)) was
correct. Before it, a red `Switchboard / merge authorization` context was misrouted through
`_required_ci_decision` to `required_exact_head_ci_failed` → route `remediation` → state
`blocked`. **`blocked` is writable.** So the fleet burned remediation runners pointlessly,
but the tick survived and the system kept moving.

COORD-49 correctly stopped the misrouting and deferred to the typed findings, which sends
those cases to the review branch → state `assessing` → **unwritable**. A correct fix moved
traffic into the one state that could not be persisted, converting a wasteful-but-moving
system into a stopped one.

**Implication for this log:** "we fixed the thing the evidence pointed at and the fleet is
still stuck" is not proof the diagnosis was wrong. It can mean the fix was right and
uncovered a defect the previous bug was masking. Verify by checking whether the *error class*
changed, not whether the symptom cleared.

---

## OBSERVATION — cheap falsification beats confident architecture

Recorded because the operator paid for this lesson in wall-clock time.

Mid-run, this session produced a confident four-part architectural diagnosis: hydration
laundering GitHub failures into facts, a missing "unobservable" classifier outcome, the retry
budget having no durable home, and a server-side lease-reclaim path. It was coherent, it cited
real code, and it was **wrong about what was actually stalling the fleet.**

What killed it was a three-minute check — hydrating the three stuck tasks' real snapshots on
prod, read-only:

```
CO-20     pr=863   head=a894afc0  | review_required  | persist OK on the pr/head guard
BUG-179   pr=868   head=5e8c6149  | review_required  | persist OK on the pr/head guard
BUG-183   pr=885   head=0ddbcf2e  | review_required  | persist OK on the pr/head guard
```

None of them produced `exact_head_pr_missing`, which the whole plan was built around. That
falsification cost ten minutes and pointed straight at BREAKDOWN 11.

**Recommendation:** before any autopilot diagnosis is written up, hydrate the affected tasks'
live snapshots and classify them. It is read-only, it takes minutes, and it distinguishes a
plausible story from the actual failing line.

---

## CORRECTION — expired execution leases *do* self-heal; there is no reclaim gap

Logged so nobody builds the mechanism this session nearly proposed.

The claim was that an expired execution lease permanently blocks dispatch, because
`ux_active_execution_lease` keys only on `released_at IS NULL` and only an explicit release
sets it. The index part is right. The conclusion is wrong:
`coordination._acquire_execution_lease_in` walks the unreleased rows and expires any past
their TTL *before* inserting the new lease.

```python
for row in rows:
    expires_at = float(lease["claimed_at"]) + int(lease["ttl_seconds"])
    if expires_at > now and str(lease.get("lease_state") or "active") in {...}:
        return lease
    c.execute("UPDATE resource_leases SET released_at=?,lease_state='expired',"
              "fence_epoch=COALESCE(fence_epoch,0)+1 WHERE id=?", (now, lease["id"]))
```

Expiry self-heals lazily on the next dispatch attempt. A "reclaim expired leases past a grace
period" job would have sat idle forever. Credit to the reviewing agent who caught this — the
error came from reasoning off the index definition without reading the acquire path.

---

## BREAKDOWN 12 — runners with a null `execution_connection_id` can never be fenced ⚠️

**Severity: HIGH. STATUS: LIVE — BUG-187. This is the top blocker as of the end of this run.**

Only reachable once BREAKDOWN 11 was fixed; the persist crash was hiding it.

`fence_task_generation` demands all seven fields of the exact execution identity and fails
closed if any is empty:

```python
required = ("runner_session_id", "execution_id", "execution_connection_id",
            "generation", "fence_epoch", "role", "head_sha")
```

Connect-path runners carry no `execution_connection_id` — the completion hydrator sources it
only from `active_runner['metadata']['execution_connection_id']`, which is absent. CO-20's
live runner, verbatim:

```
runner_session_id      : run_e9d5a081ab73a7df
execution_id           : execlease-62d036501b6147ec9873
execution_connection_id: null          ← fails the guard
generation / fence     : 1 / 1
role / head_sha        : review_merge / d150fd1b…
```

Such a runner is **permanently unfenceable through completion**, so a stale generation can
never be replaced. Reproduced directly on prod after #888 deployed:

```
RAISED: TaskExecutionError
msg   : exact execution generation identity is required
```

**Related, worth fixing alongside:** fencing is sequenced as a prologue to the repair effect
inside `_execute_mutating_effect`, *after* five idempotency early-returns (verified replay,
issued-awaiting-readback, claim-in-flight, retry-backoff, retry-claim-lost). Any condition
that suppresses the repair also suppresses the fence — even though fencing is safe and
idempotent, and is precisely the thing that needs to happen for progress.

---

## BREAKDOWN 13 — hot retry loop against an unsatisfiable dependency

**Severity: MEDIUM. STATUS: LIVE — not separately filed; needs its own BUG if it is to be fixed independently of BREAKDOWN 12.**

With CO-20 wedged, its dependent is dispatched roughly twice a minute and refused every time.
Ten-minute window on prod, post-#888:

```
completion_tick_failed : 30  → all CO-20
dispatched             : 19  → all CO-21
  every one: {"action":"refused","reason":"Task dependencies are unsatisfied: CO-20."}
```

The refusal is correct — CO-25 added it. What is wrong is the cadence: nothing backs off, so
a single wedged task converts into unbounded dispatch attempts on everything downstream of it.
This is direct spend with a guaranteed-zero outcome, and it will scale with the depth of the
dependency graph behind any stuck task.

---

## BREAKDOWN 14 — the coordinator receipt discards the exception message ⚠️

**Severity: MEDIUM (diagnostic). STATUS: LIVE.**

The receipt records the exception *class* and throws away its text:

```json
{"status":"completion_tick_failed","error":"CompletionRunError","task_id":"CO-20"}
```

The discarded message was `unsupported completion state: assessing` — it named the exact bad
value. Preserving it would have made BREAKDOWN 11 a one-minute diagnosis. Instead it took
hours of log archaeology across two agents, and the message was never found in the journal at
all; the defect was located by reading the enums and reproducing offline.

This is the same disease as BREAKDOWN 9: the diagnostic exists for one moment and is destroyed
on write. Highest diagnostic-value-per-line fix in this log.

---

## BREAKDOWN 15 — the documented CI recovery path posts only one of two required contexts ⚠️

**Severity: HIGH (process). STATUS: SUPERSEDED by CI-17.**

`master` branch protection requires three contexts: `Switchboard CI / VM gate`,
`Switchboard UI / Playwright`, and `Switchboard / merge authorization`. There are two verify
paths and they run **different workflow files**:

| Path | Trigger | Workflow that runs | Contexts posted |
|---|---|---|---|
| `ci_verify_dispatch.dispatch_verify()` — *the documented manual recovery* | `repository_dispatch` on `projectplanner-ci` **main** | that repo's own `verify.yml` | VM gate **only** |
| mirror push — the normal path | push to `ci/**` on `projectplanner-ci` | the **canonical** repo's `verify.yml` | VM gate **and** Playwright |

Following the documented recovery therefore leaves a PR permanently `BLOCKED` with no error
anywhere, and `gh pr merge` **exits 0 printing nothing** while silently not enqueuing. Cost a
merge during this run before the divergence was spotted. `scripts/switchboard_pr_gate.py`
does not post the Playwright context at all, so `--once-open-prs` cannot repair it either.

**Workaround that works** (~5 min, then delete the branch):

```
git push <ci-repo> <HEAD_SHA>:refs/heads/ci/verify-<slug>-<sha8>
```

**Fix direction:** make the two `verify.yml` files agree, or make `dispatch_verify` target the
mirror path. Two copies of a required-context list drifted apart — the same class of defect as
BREAKDOWN 11, one layer out.

**Current resolution:** CI-17 deleted both workflow-selection paths. Canonical code is
mirrored to a disposable public branch, but only `projectplanner-ci`'s trusted
default-branch workflow can execute. It posts one required lifecycle,
`Switchboard CI / VM gate`; the claim timer posts only the advisory
`Switchboard / claim gate`. Head admission is bounded and the native merge group runs the
full suite. There is no `repository_dispatch`, mirrored workflow, PAT, or second required
context list to drift. See `docs/CI-STRATEGY.md`.

---

## BREAKDOWN 16 — one shared GitHub API budget; agents starve CI of it ⚠️

**Severity: MEDIUM. STATUS: LIVE.**

A CI verify run failed at its very first step:

```
verify  Post pending required status
  gh: API rate limit exceeded for user ID 176963715 (HTTP 403)
  ##[error]Process completed with exit code 1
```

That is the **same user ID** this session's own `gh` status-polling had been consuming. The
CI workflow's `PRIVATE_READ_TOKEN` and an operator/agent `gh` share one 5,000/hr pool, so
polling a PR's status can break the workflow that posts that status. Self-inflicted here, and
it will recur whenever the fleet, CI, and a human overlap.

**Likely the same root cause as the run's other unexplained 403s:** prod's
`merge_coordinator` crashed four times with `HTTP Error 403` at 01:33–01:38, exactly when
PR #879 opened, and that PR's CI trigger was never fired. It was initially written off as a
transient GitHub blip. A shared-budget capacity problem fits the evidence better.
**Not proven** — prod's token was not confirmed to be the same user ID.

**Fix direction:** separate tokens per consumer, or a budget guard. Also: agents should poll
with `until`-loops on 60–90s intervals, never tight retry.

---

## STATUS UPDATE — BREAKDOWN 9 (reason codes never aggregated)

**Partially addressed.** COORD-50 shipped Phase 1 of `docs/DECISION-CORPUS-SPEC.md`
([PR #879](https://github.com/6th-Element-Labs/projectplanner/pull/879), master `309eca24`):
a reason-code registry with an AST conformance test, a 23-field export-safe feature
projection, and `decision_records` as an append-only per-episode ledger that
`run_completion_tick` writes on every tick — including automated ones.

Two caveats, both honest:

- The episode write sits *before* the persist that BREAKDOWN 11 was crashing on, so these
  failures were being captured even while the tick died. That was luck of ordering.
- `get_reason_code_counts` was **not yet live on the prod MCP surface** at the end of this
  run. Until it is, the counting this breakdown asked for still requires an SSH session — which
  is exactly how BREAKDOWNS 12–14 were found. Deploying it turns that into one query.

## STATUS UPDATE — BREAKDOWN 6 (review verdicts invalidated by every push)

**Still LIVE, and now the next thing behind BREAKDOWN 12.** BUG-179 at the time of this run:
`verdict_count: 2`, `stale_verdict_count: 2`, `current_verdict: null`, `round 2 of 3`,
`waited_seconds` climbing past 8,000. Two verdicts were recorded and both were invalidated by
head moves; nothing re-recorded one for the current head.

BREAKDOWN 11's fix restores the *ability* to dispatch a reviewer for a new head. It does not
make verdicts survive a rebase, nor should it. Whether the loop closes faster than `master`
moves is **open and unmeasured** — `stale_verdict_count` is the number to watch.

---

---

## BREAKDOWN 17 — the Fleet dock freezes on a non-JSON error body and never recovers ⚠️

**Severity: MEDIUM. STATUS: LIVE — BUG-188.**

Found because the operator reported the dock saying it could not see GitHub while GitHub was
demonstrably fine. Backend checked at the same moment: `build_open_prs` returned
`unavailable: None` with 5 PRs, token at 4975/5000 core and 4745/5000 graphql. Thirty
minutes of real browser traffic:

```
GET /ixp/v1/open_prs     150 x 200,  1 x 401
GET /ixp/v1/deployments  150 x 200,  1 x 401
```

**One failure out of 151, and the dock was still showing it.**

`_loadFleetDock` parses both responses *before* it inspects `.ok`, and the whole block ends:

```js
prPayload = await pRes.json();
deploymentPayload = await dRes.json();
...
} catch (e) { this._fleetLoadBusy = false; return; }
```

The early return skips **both** the `_dockPrUnavailable` update and `_renderFleetDock`, so the
dock keeps whatever it last drew. The signature is never recomputed on the throw path, which
is precisely why no number of subsequent successes clears it.

The inline comment states the assumption that fails: *"401/5xx bodies are `{detail: …}` with
no `prs`/`unavailable`."* True for the app's own errors. **Not** true for a Caddy-level 502/503,
which returns HTML — and those happen routinely, because every merge to `master` hard-restarts
the fleet with no drain. Six PRs merged during this run; the single 401 is consistent with an
in-flight request dropped by one of those restarts.

**Do not conflate with two fixes that already landed and are correct:** #881 added the
unavailable flags to `_fleetSignature` so a message-only transition re-renders, and #886
replaced the blanket "GitHub is unreachable right now" with the server's real reason. This is
the remaining path where *neither* runs, because the function returned before reaching them.

**Fix direction:** treat a parse failure as an unavailable reason like any other rather than
aborting the render, and distinguish "response was unreadable" from "you are not authenticated."
A transient error should cost at most one wrong frame.

---

## OBSERVATION — a week-cached asset made a deleted bug look live

The operator was seeing wording that **no longer exists anywhere in `master`**: #886 removed
the string "GitHub is unreachable right now" that same evening, and a grep of the current tree
returns zero hits. Prod was serving `app.js?v=61`; the browser was still running the cached
`v=60`.

Static assets are `Cache-Control: public, max-age=604800` **keyed on the `?v=` query**, so a
returning browser runs week-old JavaScript until the pin changes *and* the page is hard-
refreshed. Both #881 and #886 bumped the pin correctly. The gap is that a user with the old
file cached keeps the old behaviour, including old copy for bugs that are already fixed.

**Why this belongs in a breakdown log:** it cost real diagnosis time and it is a trap that will
recur. When a UI defect is reported, **check whether the reported string still exists in the
tree before investigating the behaviour.** If it does not, the report is about cached code and
the first move is a hard refresh, not a bug hunt.

---

## BREAKDOWN 18 — `switchboard` fails its own project execution readiness gate ⚠️

**Severity: CRITICAL (configuration, not code). STATUS: LIVE.**
**Found by the session-2 agent (`codex`, PR #889 thread); independently verified here.**

`ensure_review_generation` → `start(plan)` → `task_execution.start_task(...)`, and `start_task`
now runs UI-63's project-execution readiness gate. For `switchboard` that gate fails:

```
readiness.passed : False
reason_code      : project_execution_policy_missing
   blocker: project_execution_policy_missing   no execution policy configured
   blocker: provider_selector_missing
   blocker: scm_connection_missing             SCM connection unavailable
```

The deliverable under test is *"project-independent execution plane for every Switchboard
project"* — its premise is that a project declares repo topology, SCM authorization and
execution policy as data, and then anything can run. UI-63 shipped the gate that enforces it
and CO-20 made placement mandatory on the same facts. **Both are correct.** Nobody ever
populated that data for the dogfood project itself, because until today nothing required it.
We built the door and never cut ourselves a key.

**Precision note, unresolved between the two logs:** the session-2 report lists
`project_not_available` as the third blocker; this session's probe returns
`provider_selector_missing`. Reconcile before configuring against the wrong field.

**CORRECTED — the "gate" is not a gate.** The readiness *check* genuinely fails (verified
above). But the traced refusal does **not** exist:

```
grep -rn "project_execution_not_ready" --include=*.py --include=*.js .   -> no matches, anywhere
grep -rn "readiness" src/switchboard/application/commands/connect_dispatch.py -> no matches
```

`get_project_execution_readiness` is consumed only by reporting surfaces —
`project_contract.py:167` and `projects.py:756`, both as a displayed
`execution_readiness` field — and by tests. **No dispatch path consults it.** UI-63 (#864)
shipped `static/js/settings.js` and test files; "expose" meant display it in Settings, not
enforce it.

So the session-2 claim that *"every autopilot dispatch is refused at the last step"* by this
gate is **not supported by the code**. The configuration gap is real and worth closing on its
own merits, but it is **not** currently blocking dispatch.

**What is actually observed blocking, verified by direct probe:**
- CO-20 dies at the fence — `TaskExecutionError: exact execution generation identity is required` (BUG-187)
- CO-21 dies at the dependency check — `Task dependencies are unsatisfied: CO-20`

**Honest limit on the evidence.** The readiness check is *verified failing*. It has **not** been observed
refusing a live dispatch, because every current task dies earlier — CO-20 at the fence
(BREAKDOWN 12), CO-21 at the dependency check. It is a confirmed *latent* blocker, not the
observed one. Recorded this way deliberately: overclaiming which gate is "the" blocker is the
mistake both agents made today, in opposite directions.

---

## THE JOINT MODEL — three gates in series, and why neither agent alone was right

The two independent investigations converged on different layers of one dispatch path. A task
moves only if the completion tick clears all three, in order:

| # | Gate | Fails how | Status |
|---|---|---|---|
| 1 | **Persist** the classified decision | `CompletionRunError: unsupported completion state: assessing` | **FIXED** — BUG-184 / #888 |
| 2 | **Fence** the stale runner | `TaskExecutionError: exact execution generation identity is required` | LIVE — BUG-187 |
| 3 | **Dispatch** via `start_task` | ~~readiness refusal~~ — **no such gate exists; claim retracted** | UNPROVEN |

Plus BUG-186 (nothing reaps a runner whose pinned head moved) as the safety net for when gate
2 is never attempted.

**Each agent claimed a single blocker and each was wrong — including the joint model's own
gate 3, which was retracted within the hour when the refusal string turned out not to exist.** This session argued that repairing
the fence would move CO-20 — it would then have hit gate 3. The session-2 agent argued
readiness was "the single blocker for the entire autopilot right now" — CO-20 never reaches
it. **Both fixes are required; neither is sufficient.**

That is the transferable lesson, and it is the third time today the same error shape appeared:
a correct local diagnosis presented as the whole cause. See also the COORD-49 observation above
— a correct fix that moved traffic into an unwritable state.

**Agreed order:** configure the project (gate 3, operator decision, no code) → BUG-187 (gate 2)
→ BUG-186 (safety net) → the two lints.

**Agreed proof, from the corpus rather than from inspection.** Baseline at the time of writing:

```
review_required        ep=10  ticks=700  tasks=4   ratio=70:1
exact_head_pr_missing  ep=14  ticks=270  tasks=5   ratio=19:1
total                  53 episodes / 1455 ticks
```

After the config and BUG-187, these must move or the model is wrong: `completion_tick_failed`
for CO-20 → 0; a `review_merge` runner actually starts; the `review_required` tick-to-episode
ratio falls from 70:1 toward ~1:1; CO-21's dependency refusals stop. A ratio near 1:1 means the
classifier is deciding *and something is acting*. A high ratio means correct decisions with no
actor — which is the fingerprint the session-2 agent identified, and the reason
`runner_head_matches_exact_head` in `decision_records.features_json` should become the detector
rather than staying computed-and-unused.

## RUN 2 SUMMARY

| # | Breakdown | Severity | Status |
|---|---|---|---|
| 11 | Classifier emits `assessing`; store rejects it | CRITICAL | **FIXED** (BUG-184 / #888) |
| 12 | Null `execution_connection_id` ⇒ unfenceable runner | HIGH | LIVE — BUG-187, top blocker |
| 13 | Hot retry loop on unsatisfiable dependency | MEDIUM | LIVE |
| 14 | Coordinator receipt discards exception message | MEDIUM | LIVE |
| 15 | Manual CI recovery posts 1 of 2 required contexts | HIGH | LIVE |
| 16 | Shared GitHub rate-limit budget | MEDIUM | LIVE |
| 17 | Fleet dock freezes on a non-JSON error body | MEDIUM | LIVE — BUG-188 |
| 18 | `switchboard` fails its own execution readiness check | MEDIUM | LIVE — config gap; **not** a dispatch gate (corrected) |
| 6 | Review verdict invalidated by every push | HIGH | LIVE (unchanged) |
| 9 | Reason codes never aggregated | HIGH | Partially fixed (COORD-50) |

**Net effect of the run:** the autopilot went from `janitor_only` with an all-zero action
census to 19 dispatches in 10 minutes. It is acting again. It still cannot finish CO-20
unaided, and it will not until BREAKDOWN 12 is repaired.

**The pattern across 11, 15 and — one layer out — 9:** three separate places where two copies
of a vocabulary drifted apart with no test asserting they agree. Two enums for completion
state, two `verify.yml` files for required contexts, and `reason_code` as free text emitted by
nine subsystems. Each was invisible until it wedged something.

---

# CORRECTION — 2026-07-25, appended when #890 was folded in

**The retraction above is wrong. The readiness gate is real, it was the blocker, and it is now fixed.**

`claude/COORD-50` retracted the readiness-gate claim after grepping and finding no
`project_execution_not_ready` anywhere. That grep ran against the **shared Dropbox checkout**,
which was parked on branch `revert/pr-881-fleet-dock` — a branch that predates #864. The code
was on `master` the whole time:

```
git grep -n "readiness is blocked" origin/master
origin/master:src/switchboard/application/commands/task_execution.py:668
```

The gate, verbatim from `task_execution.py:663-673`:

```python
readiness = get_project_execution_readiness(project)
if readiness.get("passed") is not True:
    raise TaskExecutionError(
        "start_refused",
        str(readiness.get("message") or "Project execution readiness is blocked."), ...)
```

That string is the exact `last_error` on all three failed CO-20 effects. Identity, not inference.

The `connect_dispatch.py:250` analysis in the retraction is **correct and irrelevant**: strict
resolution is indeed skipped for unconfigured projects, but that code is never reached, because
`task_execution.py` raises thirteen lines earlier.

**Causal chain, from commit timestamps and ledger `requested_at`:**

| time | event |
|---|---|
| 03:56Z `1772cab0` | UI-63 lands the readiness refusal. Latent — nothing reaches `start_task`, BUG-184 throws first. |
| 06:06Z `a07af674` | BUG-184 fix lands. The driver can now persist and proceed to execute. |
| 06:22Z | First effect reaches `start_task` all day. Refused. `last_error = "Project execution readiness is blocked."` |
| 07:42Z `3eea4234` | #891 scopes the gate to opted-in projects. Deployed 32s later. |
| 07:47Z | `effect-964856e75357065f` goes `failed -> verified`, `last_error -> null`, `started: true`. |

**Fixed by #891** (BUG-190) and **proven in production**. Two correct fixes, landed hours apart,
composing into a hard stop — neither wrong alone, and nothing tested the composition.

## BREAKDOWN — a shared checkout makes `grep` lie

Several agents share one Dropbox working copy. Whatever branch it happens to be on is what every
`grep` sees, so an agent can conclude with total confidence that code on `master` does not exist —
and then retract a correct finding on that basis. This cost hours today and nearly buried the real
root cause.

**Rule: diagnose against `git grep <pattern> origin/master`, never a bare `grep` of the working tree.**
Better: work in your own worktree ([[shared-checkout-use-worktrees]]).

## BREAKDOWN — the sixth and seventh discard

`last_error` sat in the external-effect ledger for eight hours while every tick reported
`effect_retry_backoff` with `result={}`. Separately, `start_task` returned `started: true` while
bound to a wake that was already `failed`, and the effect **verified**.

Fixed by #892 (BUG-189): all four suppressed-effect receipts now carry `last_error` /
`retry_count` / `resource`, a dispatch bound to a dead wake is a failure that names the wake and
its `failure_class`, and `completion_projection` gained `blocked_reason`.

## BREAKDOWN — nothing ever enqueues, because nothing is ever reviewed

`effect="enqueue"` is emitted from exactly one place: `state_machine.py:684`,
`exact_head_gates_passed`. Reaching it requires a recorded exact-head review verdict. The corpus
over this window:

| reason_code | episodes | ticks |
|---|---|---|
| `review_required` | 11 | **1000** |
| `exact_head_gates_passed` | 1 | **5** |

BUG-187/#890 itself: `verdict_count: 0`, `current_verdict_status: "missing"` — while GitHub's own
`Switchboard / merge authorization` reads SUCCESS. The board and GitHub disagree about whether the
PR is reviewed, and the board is what gates `enqueue`.

So PRs sit green and unqueued forever. The merge queue is not broken; it is empty because nothing
is ever handed to it.


---

# SESSION 3 — 2026-07-25 evening → 2026-07-26 morning (claude/DOGFOOD-19)

Autopilot's first genuinely productive window: seven tasks reached Done (UI-63, ADAPTER-27,
BUG-178/179/183, CO-20, CO-21) plus COORD-51 overnight. This section records the two failures
inside that window, with enough evidence that the fix can be specced without re-deriving anything.
**Operator direction: evidence only — no new tasks, no code changes filed from this section yet.**

## BREAKDOWN 20 — the evidence-grammar gap: right facts, wrong key, nobody told

**The single human assist in an otherwise autonomous CO-21 run, end to end:**

| time (UTC 07-25) | event |
|---|---|
| 09:29 | deliverable scope dispatches CO-21 (and ADAPTER-28) |
| 09:40 | In Progress; runner working in /private/tmp/co21-codex |
| 09:43 | branch pushed, PR #896 opens, In Review |
| 09:44:33 | review verdict `pass` recorded for exact head `41092162` (round 1) |
| 09:46 | runner completes session and exits cleanly |
| 09:49 | VM gate SUCCESS. Merge authorization: **FAILURE — missing_executed_test_run** |
| 09:50–09:51 | repair dispatch (attempt 2) runs ~90s, writes nothing, exits |
| 10:05 | human assist (below) |
| 10:16:00 | machine merges #896 → `d12267e9`; 10:16:30 board Done |

**What the runner recorded** (worksession-aa0ccd80bb504bbd hygiene, verbatim):

```json
"executed_tests": [
  {"command": "python test_co_fleet.py", "passed": true, "summary": "46 passed"},
  {"command": "./test_co_repo_cache.py", "passed": true, "summary": "8 passed"},
  {"command": "python tests/test_co9_hybrid_scheduler.py", "passed": true, "summary": "27 passed"},
  {"command": "python tests/test_bug91_runtime_config_contract.py", "passed": true, "summary": "15 passed"},
  {"command": "python tests/test_co4_graceful_drain.py", "passed": true, "summary": "10 passed"}]
```

It also recorded `baseline_findings` — a test failing on clean origin/master, checked and
attributed correctly. The *work* was professional. The runner even did diligence nobody asked for.

**What the gate reads:** a single `executed_test_run` object, schema
`switchboard.executed_test_run.v1`, with `commands`, a pass signal (`passed:true` or
`exit_code:0`), a completion timestamp, and an output hash under one of EIGHT exact key names
(`output_hash`, `output_sha256`, `stdout_sha256`, `stderr_sha256`, `log_hash`, `logs_hash`,
`artifact_hash`, `result_hash` — see `store.py:_executed_test_run_has_output_hash`).
`executed_tests` (plural, no schema, no hash) does not qualify. Same facts, wrong grammar.

**Why the repair failed:** attempt 2's assignment carried only
`reason_code: missing_executed_test_run` — no expected shape, no pointer to the near-miss.
It ran preflight in a fresh worktree, **forked a NEW work session**
(worksession-7e0e58113497497d, left active/orphaned — a gate reading the *bound* session can
never be satisfied by a forked one), wrote no evidence, and exited. Attempt 3 would have received
the identical brief.

**The assist (claude/DOGFOOD-19, ~10:05):** re-ran all five suites in a detached worktree at the
exact PR head; identical results (46/8/27/15/10, exit 0); sha256 over the combined log
(`9803d0a1…64fcf1d`); wrote a conforming `executed_test_run` onto the BOUND session with the
runner's original preserved beside it and an attribution note. Merge authorization went green on
the next evaluation; everything downstream (authorize → merge → Done) was autonomous.

**Root cause, precisely:** the contract exists only as a parser inside the gate. It is disclosed
nowhere a runner can see — not in the assignment, not in the claim response, not in the
`update_work_session` write path (which accepted the wrong key with a silent success), and not in
the refusal. Desktop agents pass this gate because they carry accumulated failure-memory; fleet
runners are born fresh and only get the prompt. **The gate assumes culture; runners only have the
contract — so the contract must be the interface.**

## Scoping state — asked 2026-07-26: "did we scope that fix, is it in COORD-51?"

**No, and deliberately.** COORD-51's freeze (agreed msgs 1369/1376) put "typed merge-close
(draft→ready / verdict / **evidence**)" in **L1** and restricted COORD-51/52 to L2 memory. The L1
layer was named in the freeze and **never filed as work** — that is the hole this breakdown fell
through.

Two further scoping facts:
- The `missing_artifact` amendment (features_json carries expected_key/expected_schema/
  found_near_miss) was appended to COORD-51 at ~10:00Z 07-25 — **after the builder had started**.
  #897 merged overnight WITHOUT it (`git show b4de4de1 | grep -c missing_artifact` → 0). The
  amendment text survives on the COORD-51/52 task descriptions and in the builder's mailbox
  (msg 1397, unacked as of this writing).
- The two write-path fixes discussed on 07-25 (reject-at-write; teach-at-refusal) were proposed
  in-session and **never filed** — held per operator direction, evidence first.

## PROPOSED FIX (not filed) — make evidence a typed tool, the Atlas pattern

Operator direction 2026-07-26: "we need to show them what they need to do — make it a typed tool
like we do in Atlas." Verified against the ActionEngine repo
(`actionengine/engine/services/atlas_context/contracts.py`): every service boundary is a pydantic
contract — `Field(..., min_length=1, description=…)`, constrained ranges, `json_schema_extra`
examples, and the header rule *"These contracts are immutable — services depend on them."* The
schema IS the interface; a malformed request cannot exist.

Applied here: a first-class MCP tool, e.g. `record_executed_test_run`, whose **input schema is
the evidence contract**:

- typed fields: `commands: list[str]` (min 1), `passed: bool` / `exit_code: int`,
  `output_sha256: str` (pattern-validated) — ONE canonical hash key, server maps to the gate's
  accepted set; `completed_at` stamped server-side, not trusted from the caller
- writes to BOTH read surfaces at once (work_session.hygiene AND claim evidence), erasing the
  split-surface trap documented 07-25 (completion reads evidence; merge authorization reads hygiene)
- returns the gate's verdict immediately — the runner learns "evidence accepted, merge
  authorization now lacks only X" in the same call, the desktop-agent feedback loop at machine speed
- the `missing_executed_test_run` refusal names this tool: "call record_executed_test_run" — the
  refusal becomes an instruction, not a diagnosis

Why this beats validating the free-form dict: a validator still lets the runner guess the
envelope; a typed tool makes the wrong key **unrepresentable**. It is also subtraction-shaped —
once runners use the tool, the eight-hash-key tolerance ladder and the near-miss detection become
legacy compatibility, not load-bearing logic.

Candidate follow-ons in the same pattern (evidence for the L1 layer, not filed): typed
`record_review_verdict` already exists and worked first try on CO-21 — proof the pattern holds;
`mark_pr_ready` and draft-state transitions are the remaining untyped L1 actions.

## BREAKDOWN 21 — the immortal zombie: dead runner, green heartbeat, 9 hours

ADAPTER-28's runner (run_f1a4357f86bc380c, dispatched 09:29 07-25) hit an OpenAI rate-limit
stream error at ~21:34 local and died at an interactive prompt:

```
■ stream disconnected before completion: Internal server error
  Approaching rate limits — Switch to gpt-5.4-mini for lower credit usage?
  › 1. Switch to gpt-5.4-mini   2. Keep current model   3. Keep current model (never show again)
```

**Evidence it was dead, not waiting** (2026-07-26 morning):
- PTY log: zero bytes appended since 21:34 (~9h); process 0% CPU, state Ss, no children
- Input injection through the host's own endpoint (`/runner/v1/sessions/…/inject`, valid minted
  ticket) returned `injected: true, bytes_written: 1` for both "3" and Enter — **and produced zero
  redraw**. A live TUI repaints on any keypress. Nobody was home.
- Meanwhile the host wrapper heartbeated faithfully the entire time (`heartbeat_ttl_s: 180`,
  renewed all night), so no lease expiry, no reaper, no escalation. It never created a Work
  Session — 9 hours of green liveness, zero progress. **Liveness and progress are different
  measurements and we only take one of them.**
- Resolution: `stop_task` 07-26 ~08:40Z; host acked; process gone in ~20s; coordinator re-dispatch
  expected on its normal cadence.

**Compounding find — stale secret after rotation (HARDEN-46):** the runner's PTY stream showed
its relay websocket in an endless connect→fail loop (46 straight attempts). The host process
predates yesterday's token rotation and still carries the OLD `PM_MCP_TOKEN` (env hash
`7938bfc3…` vs the shell's rotated `913887d0…` — how the inject 401 was diagnosed). Long-running
Agent Hosts are a stale-secret surface the rotation checklist does not cover; a host restart is
required to pick up rotated credentials.

**Classes to spec from this (not filed):** (a) progress watchdog — output-staleness beside
heartbeat, N hours of silent PTY on a live lease is an escalation; (b) headless codex must not be
able to block on interactive prompts (`--dangerously-bypass-approvals` does not cover provider
menus — config-level model-fallback or non-interactive mode); (c) HARDEN-46 rotation checklist
gains "restart Agent Hosts".

---

## BREAKDOWN 22 — host-bundle drift: the server sprints, the host stands still, the fleet dies quietly

**Severity: CRITICAL. STATUS: RECOVERED 2026-07-25 (bundles 0.4.0/0.4.1); systemic fix NOT filed.**

The server autodeploys on every merge to master. Agent Hosts update only when an operator cuts an
Ed25519-signed bundle by hand. Bundles stopped at 0.3.999 (Jul 24 15:56); the server then took
~15 merges in ~30 hours, including COORD-52 (#908), which added `prior_attempts` to the
execution-assignment wire contract. The Jul-24 host computed the old shape, BUG-168's
fail-closed exact-match check refused the new one, and **every Connect launch on the host died at
admission** (`execution_assignment_contract_mismatch`). The pre-ADAPTER-32 host also failed to
report those failures (TLS blips on `complete_wake` were swallowed), so wakes parked in
`claimed` — invisible to the pending loop, unretried, forever.

Operator experience: "the simple 3-layer boot model that worked for days just stopped; nothing we
did should have changed it." Something did — the wire contract moved with no host release. The
drift was invisible: no alarm compares the host's installed bundle against what master would ship,
even though the host reports its version in every heartbeat.

**Recovery that worked:** cut+install 0.4.0 from master; for each stranded task `complete_wake`
the parked wake, `release_resource` its surviving execution lease, `start_task`. Both stranded
tasks (COORD-48, ENFORCE-14) booted real runners within seconds of the final fix.

**Classes to spec from this (not filed):** (a) staleness alarm — host heartbeats its bundle
manifest hash; server compares against the manifest its own tree would produce and badges the
Fleet dock / Needs-you on drift, red when `src/switchboard/connect/` or `agent_host*.py` changed;
(b) one-command release script (`scripts/agent_host_release.sh`) wrapping keychain → build-bundle
→ verify → update; (c) contract-version negotiation so a host can refuse-and-report rather than
refuse-and-strand.

---

## BREAKDOWN 23 — the sweep kills the wake but not its lease; retries replay the corpse's error

**Severity: HIGH. STATUS: LIVE — not filed (operator: fix, don't file). Manual bypass proven.**

When `sweep_wake_intents` fails a claimed wake at its deadline, the wake goes terminal but its
**execution lease stays active** (`reserved`, TTL 7200s). `_acquire_execution_lease_in` coalesces
any new start for the same task+role onto that lease, which resolves to the swept wake, whose
stored result is `deadline_expired` — so `start_task`/`retry_task` return **the dead generation's
error as if it were the new attempt's refusal**, with no hint that a corpse is being replayed.
ENFORCE-14's remediation was undispatchable for what would have been 2 hours; nothing in the
refusal named the lease.

By contrast, a wake ended via `complete_wake` releases correctly (COORD-48 chained generations
1→2→3 cleanly). The asymmetry is exactly the sweep path.

**Manual bypass:** `list_active_resource_leases` → `release_resource(lease_id)` → start succeeds
immediately. **Fix direction:** the sweep must release the execution lease in the same
transaction that fails the wake, and a start refusal built from a terminal predecessor must say
so (`stale_generation_replayed`, naming the lease and wake), per the evidence-grammar rule of
BREAKDOWN 20.

---

## BREAKDOWN 24 — 0.4.0 required an Execution Context the server deliberately does not send

**Severity: HIGH. STATUS: FIXED — ADAPTER-33 / #917, live as bundle 0.4.1.**

ADAPTER-28 (#902) made the host demand a server-issued Execution Context for every Connect
launch. But `connect_dispatch.enqueue_task` resolves a context **only for projects with a
configured execution policy** — and switchboard has none *by design* (the restored COORD-47
contract; configuring it prematurely caused two prior fleet stops, see the CO-20/UI-63 notes in
that function). So minutes after the 0.4.0 rollout, every legacy wake refused with
`invalid_execution_identity` — a new hard requirement colliding with a server path that is still
the production path. Same-day irony: the host-side strictness was reviewed and merged hours
before it met real traffic shapes it had never seen in tests, because the tests only built
context-bearing wakes.

**Fix:** materialization, generation binding, and the workspace receipt apply exactly when a
context is present; context-less wakes launch from the host checkout as they always did, pinned
by `test_legacy_wake_without_context_launches_from_repo_root`. **Rule worth keeping:** when the
server keeps a legacy branch alive on purpose, the host must keep the matching branch alive too —
delete both in one change, never one side first.

---

## OBSERVATION — renaming a PR's branch closes the PR, silently and irreversibly

GitHub's branch-rename API (`/branches/{branch}/rename`) does not retarget open PRs from that
branch — it **closes them**, and a closed PR whose head ref no longer exists cannot be reopened.
Cost one PR cycle tonight (#910 → #912) while binding a branch to its board task. Rename first,
open the PR second; if a PR is already open, live with the name or open a fresh PR from a new
branch.

---

## BREAKDOWN 25 — Autopilot forgot DHCP (starved scopes + stole the caller's name)

**Severity: HIGH. STATUS: FIXED — this branch (COORD-64 admit + principal).**

Two regressions stacked on the ADR-0008 DHCP model ("scope says boot → `start_task` →
runner"):

1. **COORD-48 admit** required a configured execution policy *and* green readiness before
   ticking a project. Unconfigured dogfood boards with live Autopilot scopes got
   `candidate_count: 0` forever (DOGFOOD-25 ready, nothing started). BUG-190 already said
   readiness is opt-in for `start_task`; the daemon invented a second kill switch.
2. **Worker principal = caller** in `connect_dispatch` made the runner `register_agent` as
   the coordinator, task-bound. Next `start_task` for any other task refused
   `agent_registered_on_different_task` (six ENFORCE-14 remediation retries, zero runners).

**Fix (subtraction, not new machinery):** armed scope ⇒ admit; readiness refuses only
opted-in policies; mint `agent/<runtime>/<task-id>` as the worker principal always.

## BREAKDOWN 42 — `Blocked(route=remediation)` can never be re-selected: wrong `dependency_state.ready` field ⚠️

**Severity:** high (COORD-46 "fix" is a no-op in production)
**Code:**

- `build_dependency_state` sets `ready = (status == "Not Started") and not blocking`
  (`src/switchboard/domain/board/tasks.py`).
- `satisfied = not blocking` is the real dependency signal.
- `task_ready_for_dispatch` for `Blocked` requires `dependency_state.ready`
  (`src/switchboard/domain/completion/routing.py`).
- Therefore **any** `Blocked` task has `ready=False` forever, even with
  `satisfied=True` and `route=remediation`.

**Proof (this checkout):**

```
status       dep.ready  satisfied  task_ready_for_dispatch(route=remediation)
Not Started  True       True       True
In Progress  False      True       True   # special-cased; ignores ready
In Review    False      True       True   # special-cased; ignores ready
Blocked      False      True       False  # BUG: automatic remediation dead
```

**Why COORD-46 tests green:** `tests/test_coord46_route_aware_selection.py`
injects `dependency_state.ready=True` for Blocked rows — a state
`build_dependency_state` never produces. Contract tested ≠ contract shipped.

**Live symptom (COORD-57 / #936):** classifier projects remediation → board
`Blocked` → mission/daemon selection uses `task_ready_for_dispatch` → no
candidate → Autopilot looks "alive" on `diagnostic-integrity` but never
re-enters COORD-57. Matches activity: remediation `start_remediation` retries
exhausted earlier; afterward selection cannot pick it up again.

**Fix direction (not applied here):** for `ROUTE_KEYED_STATUSES`, gate on
`dependency_state.satisfied` (or `blocked_by_count==0`), never on `ready`.
Add a regression that builds dependency_state via `build_dependency_state`
for status=`Blocked` and asserts dispatchable when route=`remediation`.

**Not repaired.**


### Observe tick — dual Autopilot (2026-07-26 19:09 UTC)

- Queue empty; #940 still OPEN/CLEAN out. No new enqueue.


### Observe tick — dual Autopilot (2026-07-26 19:11 UTC)

- Queue empty; #940 still OPEN/CLEAN out. No new enqueue.


### Observe tick — dual Autopilot (2026-07-26 19:14 UTC)

- Queue empty; #940 still OPEN/CLEAN out. No new enqueue.


### Observe tick — dual Autopilot (2026-07-26 19:15 UTC)

- Queue empty; #940 still OPEN/CLEAN out. No new enqueue.

---

# SESSION 4 — 2026-07-27, fleet-down repair (claude/steve-desktop-operator)

**Run date:** 2026-07-27
**Operator:** claude/steve-desktop-operator (**intervention session** — the operator
directed repair, so this section is written *after* the fixes, per the "STATUS: FIXED"
convention. Breakdowns 49-52 and 54 were observed and deliberately **not** repaired.)
**Host:** `host/steve-mbp-co16`, bundles 0.4.2 → 0.4.8 over the session
**Presenting symptom:** no CLI runner would launch from either MCP or the UI, all day.

> Context for the reader: this session started from "CLI runners aren't working, launched
> from either MCP or UI". It was not one defect. It was **six**, stacked, each hidden
> behind the one in front of it. Every fix revealed the next. The order below is the
> order they surfaced, which is also the order a future debugger will hit them.

---

## BREAKDOWN 43 — the launch gate re-derives a contract it was told to echo

**Severity:** critical (every retry/remediation launch, fleet-wide)
**Observed:** every Connect wake for a task *with execution history* died on the host:

```
"reason": "runtime_launch_configuration_error",
"provider_error": "connect execution assignment disagrees with persisted lease:
                   execution_assignment_contract_mismatch",
"runner_registered": false, "pid": null
```

then rotted until `connect_claim_hold_expired` (90s), and Autopilot re-queued into the
same wall. ADAPTER-36 reached attempt 9 this way; COORD-57 reached 50.

**Root cause:** COORD-52 mints `prior_attempts` into the execution assignment **once, at
dispatch**, carried verbatim. The contract's own docstring states the rule: every
verification-path caller must **echo** the stored value, never re-derive it, because the
decision corpus is append-only and moves between dispatch and claim. The server claim
path obeys this (`storage/repositories/claims.py:186`). `adapters/agent_host.py`'s launch
gate did not — its re-derived `expected` had no `prior_attempts` key, and
`require_exact_execution_assignment` is exact dict equality, so it refused.

**Why it matters:** it presents as intermittent. A *first* attempt carries no history and
launches fine; only retries and remediations die. So the fleet looks "mostly working"
while every self-healing path is dead.

**Diagnostic-discard note:** the failure summary named the contract but not the differing
key. One line of "expected keys vs observed keys" would have ended this in a minute.

**STATUS: FIXED** — PR #961, `adapters/agent_host.py` now passes
`prior_attempts=execution_assignment.get("prior_attempts")`. Regression:
`tests/test_adr008_connect_prior_attempts_launch.py` (reproduces the live error pre-fix,
pins that tampering any *other* field still refuses, and that first-ever dispatches with
no key still launch).

---

## BREAKDOWN 44 — the signed bundle ships a facade without its imports

**Severity:** critical (any clean host install)
**Observed:** after cutting a bundle from clean master, *every* launch failed:

```
"provider_error": "No module named 'evidence_claims'"
"provider_error": "No module named 'push_verification'"
"provider_error": "No module named 'deliverable_gates'"
```

one module at a time, each install revealing the next missing one.

**Root cause:** `create_signed_bundle` copies `adapters/`, `src/switchboard/`, `db/`,
`constants.py`, and `store.py`. But `store.py` is a facade over the legacy repo-root
modules, and the Connect launch path imports it. The bundle shipped the facade without
its import closure. Bundle 0.4.2 had 319 root files; a clean rebuild produced 8.

**Why it matters:** this is the defect that proves the *real* problem (breakdown 45).
0.4.2 worked only because the missing files had been **hand-copied into the installed
release directory**. The packaging bug was invisible for as long as nobody rebuilt.

**STATUS: FIXED** — PR #961, the bundle spec now ships all repo-root modules plus the
`deliverable_gates` / `deliverable_closure` packages (8 files → 724).

---

## BREAKDOWN 45 — production ran code that existed nowhere in git ⚠️

**Severity:** critical (process, not code — and the root cause of this whole session)
**Observed:** yesterday's outage (2026-07-25, breakdown 22) was "fixed" by editing files
**directly inside the installed release directory** `~/.local/share/switchboard-agent-host/releases/0.4.2/`,
plus uncommitted changes left sitting in the shared Dropbox checkout. Neither was ever
committed. Diffing the installed 0.4.2 against master showed 20+ files differing.

The moment anything rebuilt from master — which this session did, to ship breakdown 43's
fix — **every un-landed fix silently reverted at once**. Breakdowns 44, 46, and 47 are all
"a hand-patch that was never landed, resurrecting."

**Why it matters:** it makes the fleet's behaviour unreproducible and undebuggable. The
host was running a build that no commit describes, so "does master work?" and "does the
fleet work?" had different answers, and nobody could tell which was authoritative. It also
guarantees the next clean rebuild re-breaks production.

**Fix direction (process):** never hand-edit a release directory. Land the fix, rebuild,
reinstall. Worth an automated check: on install, compare the bundle manifest hash against
the installed tree and refuse/warn on drift, so a hand-patched release announces itself.

**STATUS: FIXED (this instance)** — all hand-patches landed in PR #961; host reinstalled
from merged master as 0.4.8, so host and server are byte-identical again. The *class* is
not prevented — no drift check exists yet.

---

## BREAKDOWN 46 — the credential vocabulary disagrees with itself

**Severity:** critical (every Connect launch, once 45 reverted it)
**Observed:**

```
"provider_error": "connect generation binding refused: provider_connection_revoked"
```

on a credential that was demonstrably healthy and in use.

**Root cause:** `application/commands/execution_context.require_generation_binding`
accepted `revocation_state` in `{none, active, valid}`. The provider inventory and every
Connect execution context write **`not_revoked`**. So the validator rejected the emitter's
own word and read a healthy credential as revoked.

**Why it matters:** classic shared-vocabulary drift between emitter and validator — the
exact class COORD-57 exists to catch. A conformance test between the two would have caught
it at author time.

**STATUS: FIXED** — PR #961 admits `not_revoked`. (This fix had been hand-patched into
0.4.2 and never landed — see breakdown 45.)

---

## BREAKDOWN 47 — Autopilot cannot answer a trust prompt

**Severity:** high (silently converts a launch into a dead session)
**Observed:** operator opened a freshly-booted DOGFOOD-17 session and found Codex sitting
at its interactive workspace-trust TUI, waiting for a `1` or `2` keypress. The runner was
"running" and heartbeating; it just wasn't doing anything, and never would.

**Root cause (two layers):**
1. Codex prompts for trust **per exact cwd**. Parent-directory trust does not cover a new
   per-execution worktree, and every Connect launch creates one.
2. The seeding helper that was supposed to prevent this resolved `CODEX_HOME` from the
   environment **only**, and returned early when unset. launchd starts the Agent Host with
   a minimal environment that has no `CODEX_HOME`, so the helper silently no-opped in
   exactly the context it existed for.

**Why it matters:** "Autopilot just needs to boot" — a prompt that requires a human at the
keyboard is a total defeat of hands-off operation, and it is invisible from the control
plane: lease healthy, heartbeats green, zero progress.

**STATUS: FIXED** — PR #961 seeds exact-path trust before launch and defaults to
`~/.codex` when the env var is absent. Verified in the host log:
`[agent_host] seeded Codex trust for .../DOGFOOD-17/execlease-...`, and the rebooted
session came up with no prompt.

---

## BREAKDOWN 48 — the completion planner kills a remediation runner for doing its job ⚠️

**Severity:** critical (ADR-0008 C2 violation; the "long-running windows" killer)
**Observed:** remediation sessions launched, worked for minutes, then died. ADAPTER-36 sat
at attempt 9; COORD-57 reached attempt 50 with an empty merge queue.

**Root cause:** `domain/completion/effects.py::_fence` — *"A live generation may be kept
only if role AND exact head both match."* A remediation runner's entire purpose is to
**push a new commit**. The instant it pushes, the PR head advances past its pinned head,
the next completion tick fences it, and the host kills the PTY. A fresh generation is
dispatched, pushes, and meets the same fate. Two changes armed it: COORD-77 (#956) made
completion ticks continuous, and BUG-175's "kill surrendered runners even if heartbeat is
still fresh" made the host execute the kill instantly.

**Why it matters:** ADR-0008 C2 permits exactly two ways for a lease to become due — TTL
expiry, or the holder's own surrender at a role boundary — and states plainly that no task
status, claim status, coordinator, or steward may kill a process. A classifier fencing a
live, renewing, correct-role generation because the head moved is a **coordinator kill
wearing a lease costume**. It is also unnecessary: the stale-head *write* gate in claim
verification already fail-closes an obsolete completion. Process death adds no safety.

**STATUS: FIXED** — PR #961. Head drift alone no longer fences a live matching-role
remediation generation; it attaches and waits. `review_merge` still requires the exact
decided head (that role never advances the head itself), role mismatch still fences, and
terminal-task cleanup is unchanged. Regression added in
`tests/test_coord46_effect_planner.py::test_live_remediation_survives_advancing_its_own_head`.
**Not yet proven end-to-end live** — no runner has completed a full push→survive→review→merge
loop since the fix deployed.

---

## BREAKDOWN 49 — a failed launch is not allowed to say it failed

**Severity:** high (turns every launch failure into a 90-second stall)
**Observed:**

```
POST /txp/v1/complete_wake unavailable (RuntimeError: HTTP 403 /txp/v1/complete_wake:
  detail={"allowed": false,
          "error_code": "direct_task_completion_binding_denied",
          "reason_codes": ["direct_runner_not_found"]}); skipping
POST /txp/v1/complete_wake incomplete; retry 1/3
... retry 2/3 ...
POST /txp/v1/complete_wake exhausted retries; wake may remain claimed wake_id=wake-980e27bafc274136
```

**Root cause:** when a Connect launch fails **before** `register_runner_session` succeeds,
there is no bound runner — but the completion-binding gate requires one. So the host's
honest "this failed" report is refused, ADAPTER-32's retry loop exhausts, and the wake
sits claimed until `connect_claim_hold_expired` burns the full 90s hold.

**Why it matters:** it multiplies every other breakdown's cost by 90 seconds and makes the
board lie (`Starting`) about a launch that is already dead. Per ADR-0008 M2, delivery state
must be explicit and terminal; a host that cannot report a terminal failure violates that.

**Not repaired.** Fix direction: allow the claiming host to record a failed start for the
wake it claimed when `started=false` and no runner ever bound, without weakening the gate
for `started=true` completions.

---

## BREAKDOWN 50 — a failed effect is terminal to the operator, with no way out

**Severity:** high (operator cannot start a task, and is not told why)
**Observed:** after a failed launch, `start_task` and `retry_task` both refuse:

```
{"error": "effect is failed", "error_code": "start_refused",
 "start_error": "effect is failed", "failure_class": "failed_gate"}
```

forever. Observed on ADAPTER-36 and COORD-76.

**Root cause:** `storage/repositories/external_effects.py:97` — a `failed` effect is
terminal to ordinary `claim_external_effect` callers. Only the completion executor's
bounded compare-and-swap (`retry_external_effect`) may reissue it, and that never runs for
a task parked outside an active completion run.

**Why it matters:** two failures compound. The task is wedged, *and* the refusal names
neither the effect key nor the retry path — "effect is failed" is the whole message. The
operator has no move. (Same diagnostic-discard signature as breakdown 43.)

**Not repaired.** Fix direction: let an explicit operator `start_task` reclaim a failed
start-effect through the same audited CAS, and make the refusal name the `effect_key` and
the path out.

---

## BREAKDOWN 51 — closing a PR as "superseded" strands its task forever ⚠️

**Severity:** high (silent; blocks every downstream dependent)
**Observed:** COORD-66 sat at **In Review** indefinitely. `explain_task_block`:

```
"classifier": {"route": "wait", "state": "waiting",
               "reason_code": "recovered_incomplete_run",
               "planned_effect": "wait", "next_retry_at": null}
```

**Root cause:** its PR #927 was closed **unmerged** as "superseded — already incorporated";
the work had been absorbed as the first commit of PR #931, which **squash-merged**. A
squash discards the original SHA, so the task's exact head is not an ancestor of master and
no merge-provenance webhook can ever fire. The classifier parked it at `route=wait` with
**`next_retry_at=null`** — nothing will ever wake it again.

**Why it matters:** the work shipped; only the paperwork was lost. Meanwhile COORD-67 and
COORD-68 sat Not Started behind it as dependents — *two runners' worth of idle capacity for
a full day*. Nobody was told: no Needs-you, no finding, no retry. This is precisely the
livelock class COORD-77 was written to catch ("a stable decision's verified effect replays
as a no-op forever — task frozen, nobody told"); the trigger simply is not covered.

**Not repaired** (the *instance* was cleared: COORD-66 stamped Done via verifier offline
evidence pointing at squash `0ac1c415`; COORD-67/68 dispatched hands-off within minutes and
are running as of this entry). Fix direction: closing a PR as superseded should either
transfer provenance to the absorbing PR or park the task as a visible Needs-you — never
freeze it with no scheduled retry.

---

## BREAKDOWN 52 — the board asks for a session policy no host has ⚠️

**Severity:** critical (silently undispatchable tasks, fleet-wide)
**Observed:** COORD-78's wake sat pending and was never claimed. Placement refused **all
six** candidate hosts. For the one healthy host:

```
"host_id": "host/steve-mbp-co16",
"reason_codes": ["session_policy_not_supported"],
"physical_capacity": {"active_sessions": 1, "available_sessions": 15, "max_sessions": 16}
...
"eligible_host_count": 0, "reason_code": "no_eligible_persistent_capacity"
```

with the wake requesting `"session_policy": "docs_review"`.

**Root cause:** the switchboard board's defaults set **both** `default_profile` and
`code_task_default_profile` to `docs_review` (`storage/repositories/projects.py:294-296`,
noted in-code as a legacy fixture). Every Agent Host advertises only `code_strict`
(`adapters/agent_host.py:412`, `PM_HOST_SESSION_POLICIES` default). So **any task whose
description lacks an explicit `policy_profile:`/`session_profile:` tag is undispatchable by
construction.** COORD-76 and DOGFOOD-17 launched because they carry the tag; COORD-78 has
none.

**Why it matters:** it fails *silently* on both sides. Server-side the wake just never
places and expires; host-side `eligible_runtime` returns falsy and the host `continue`s
with **no entry in `refused`** — the tick reports `pending: 3, acted: [], refused: []`,
which reads as "nothing to do" rather than "I skipped three wakes I can never run."

**Not repaired.** Fix direction: reconcile the vocabulary at the board default rather than
tagging tasks one at a time, and make a policy-mismatch skip *recorded* host-side instead
of a silent `continue`.

---

## BREAKDOWN 53 — host presence reaches only one of the boards it serves

**Severity:** high (a whole board's Autopilot dies quietly)
**Observed:** atlas wakes DIST-1 and DIST-4 sat pending all day. `host_status(project=atlas)`
reported the host `stale: true`, `agent_host_version: 0.4.1` (26 hours out of date), while
`project=switchboard` showed it healthy and current. Placement refused it with
`host_unavailable`.

**Root cause:** the host polls wakes for every project in `_host_projects` (DOGFOOD-25
multi-project support) but posts `heartbeat_host` / `register_host` for `PM_PROJECT` only.
Every other served board therefore holds a permanently stale host row.

**Why it matters:** the board looks like it has no capacity when a healthy host is sitting
idle beside it, and nothing on either side reports the contradiction.

**STATUS: FIXED (pending merge)** — PR #973 posts presence per served project;
execution-policy authority stays bound to the primary project's response so per-board
policies cannot flap host config.

---

## BREAKDOWN 54 — the classifier acts on a resolved conflict

**Severity:** medium (wasted dispatch; masks true state)
**Observed:** COORD-78's classifier held `reason_code: pr_merge_conflict`, `route:
remediation`, and issued a remediation wake — while GitHub reported the PR
`mergeStateStatus: CLEAN` with both gates green and a passing review verdict. The conflict
had already been resolved by the working agent minutes earlier.

**Why it matters:** the stale decision spends capacity on a repair that is not needed, and
(combined with breakdown 52) the wake it dispatched could never be claimed anyway. It also
misreports the task's real state to any operator reading the classifier.

**Not repaired.** Fix direction: re-hydrate PR mergeability before acting on a
conflict-derived route, or expire conflict decisions on head change.

---

## THE GOOD PATH (recorded, per the file's own rule)

- **Lease survival now holds.** DOGFOOD-17's runner ran ~90 minutes continuously *through
  two signed-bundle swaps and host restarts*, renewing cleanly the whole time
  (`"renewed": true, "renew_deferred": false`). The lease-survival work is doing its job.
- **Dependency unblock → dispatch is genuinely hands-off.** Clearing COORD-66 freed
  COORD-67 and COORD-68; Autopilot dispatched both within minutes with no operator action,
  and both booted, registered, and are running as of this entry.
- **Trust seeding works.** A rebooted DOGFOOD-17 came up with no interactive prompt, with
  the seeding line visible in the host log.
- **The merge queue held under load.** Three PRs (#961, #946, #948) merged in one window;
  the group CI caught real reds rather than rubber-stamping.

## SESSION 4 SUMMARY

| # | Breakdown | Severity | Status |
|---|---|---|---|
| 43 | launch gate re-derives instead of echoing `prior_attempts` | critical | FIXED #961 |
| 44 | signed bundle ships `store.py` without its import closure | critical | FIXED #961 |
| 45 | production ran hand-patched code that existed nowhere in git | critical | FIXED (instance) |
| 46 | `not_revoked` rejected by the binding validator | critical | FIXED #961 |
| 47 | Codex trust prompt blocks the PTY; seeding no-ops under launchd | high | FIXED #961 |
| 48 | planner kills a live remediation runner for advancing the head | critical | FIXED #961 |
| 49 | failed launch cannot report failure (`complete_wake` 403) | high | open |
| 50 | failed start-effect is terminal to the operator | high | open |
| 51 | superseded-PR close strands its task forever | high | open |
| 52 | board default session policy no host advertises | critical | open |
| 53 | host presence reaches only `PM_PROJECT` | high | FIXED #973 |
| 54 | classifier acts on an already-resolved merge conflict | medium | open |

**Pattern of the day:** six of these (43, 44, 46, 47, 49, 52) are *silent* failures — the
control plane reported healthy or reported nothing while the fleet was dead. The single
highest-leverage change is not any one fix; it is that **a skip must be recorded**. Three
separate paths (`eligible_runtime` returning falsy, a policy mismatch, a
never-registered runner) drop work on the floor with no refusal, no finding, and no
counter. Breakdown 43's cause was named in an error message that omitted the one field
that differed. The diagnostic-discard pattern is still the dominant bug class on this board.

---

## BREAKDOWN 55 — an admin merge bypassed a red gate and put conflict markers on master ⚠️

**Severity:** critical (master red; operator-caused)
**Recorded against this session's own operator.** The gate worked; it was overridden.

**Observed:** PR #948 (COORD-76) was merged with `gh pr merge 948 --merge --admin`,
bypassing the merge queue. Its branch head `7183e28c` carried **committed conflict
markers**:

```
$ git show 7183e28c:tests/test_coord46_production_normalization.py | grep -n '^<<<<<<<\|^>>>>>>>'
475:<<<<<<< HEAD
482:>>>>>>> cf287c76 (fix(COORD-76): align CI suites with draft/wait observe contracts)
492:<<<<<<< HEAD
508:>>>>>>> cf287c76 (fix(COORD-76): align CI suites with draft/wait observe contracts)
```

Its `Switchboard CI / VM gate` status was **`failure`** at the time of the merge. Master
carried 4 conflict markers for ~6 minutes until emergency repair PR #972 ("PR #948 landed
unresolved conflict markers in tests/test_coord46_production_normalization.py").

**Why it matters:** three separate defences existed and all were skipped by one flag — the
VM gate (red, and correct), the merge queue's group CI (never ran), and the conflict-marker
scan. The bypass was requested by the operator to unblock a stalled board; the mistake was
executing it **without surfacing that the gate was red and what it was red about**. A
bypass is a legitimate escape hatch; a *silent* bypass is not.

**Second-order cost:** the resulting master churn (#972 plus the other merges) is what made
PR #963 go `DIRTY` and get ejected from the queue minutes later — see the observation below.

**Fix direction (process, and tooling):** when `--admin` is used, print the gate states
being overridden and refuse on a *red required* check unless a second explicit confirm is
given. Cheap, and it converts a silent override into an informed one. Worth noting the
board's own rule already covers this: ADR-0020's bar is CI + Playwright + queue, and
admin-merge routes around all three at once.

**Not repaired** (the instance was repaired by #972, not by this session).

---

### Observation — PR #963 (COORD-78) ejected from the merge queue (2026-07-27 ~03:46 UTC)

Not a defect in itself; recorded because it is the live test of two things that landed
today.

- Both required gates were **green** on head `c54b98fe`, review verdict pass, 7 test suites
  recorded passing, PR ready and not draft. It entered the queue at position 1.
- Master then advanced (#972 emergency repair plus the day's merges) and #963 became
  `mergeStateStatus: DIRTY` — a **genuine** conflict, not a flaky-CI eject.
- Note the distinction from COORD-76's fix, which covers *tip-green* ejects
  (`failed_checks` / `checks_timed_out`). A DIRTY eject is a real rebase, i.e. remediation
  work — which makes this the first live opportunity to prove **breakdown 48's fix**:
  a remediation runner that pushes a rebase must now survive its own push instead of being
  fenced the instant the head moves.

Watching, not repairing. Outcome to be appended.

---

### BREAKDOWN 55 — addendum: the bypass cost two emergency repairs, not one

Appended after further observation. The `--admin` merge of PR #948 required **two**
follow-up repairs to master, not just the conflict-marker cleanup:

1. **PR #972** — "remove conflict markers merged by PR 948". Four markers in
   `tests/test_coord46_production_normalization.py`.
2. **PR #974** — "restore completion conformance after PR 948", whose stated root cause is
   that *"PR #948 ... overwrote the newer expectation"* of **the ADR-0008 matching-role
   remediation contract**, and which also had to bound a reconciliation livelock in the
   convergence ladder.

The second is the sharper lesson. PR #948's branch predated breakdown 48's fix (landed in
#961 barely an hour earlier). Because it was merged with `--merge --admin` — no squash, no
merge-queue group build, no rebase onto current master — **it silently reverted a contract
that had just been established**, along with its gold catalog expectation. The merge queue
exists precisely to build each entry against current tip; bypassing it converted a stale
branch into a regression on master.

**Combined cost of one `--admin` flag:** master red with syntax-breaking conflict markers,
one just-landed ADR-0008 contract regressed, two emergency PRs, and a livelock that had to
be separately bounded.

---

## BREAKDOWN 56 — a stale-base PR fails conformance and nothing tells it to rebase

**Severity:** medium (wasted CI, misread as a real defect)
**Observed:** PR #975 (COORD-67, authored hands-off by an Autopilot runner minutes earlier)
came back red:

```
== FAILED: 2 of 573 Python test file(s) ==
FAIL  remediation_fences_stale_head_matching_role:
        effect 'attach_and_wait' != 'start_remediation'
FAIL  pr_merged_reconcile: FAIL_livelock
```

Run against **current master** the same two suites are green:

```
Completion gold decisions: 36 passed, 0 failed (of 36 gold scenarios)
Convergence conformance: 40 passed, 0 failed (of 40 scenarios, budget 12 ticks)
```

**Root cause:** the PR is based on a master that predates #961 (the matching-role
remediation contract) and #974 (the regenerated gold catalog, where the scenario was
renamed `remediation_fences_stale_head_matching_role` →
`remediation_attaches_stale_head_matching_role` with the new expected effect). The branch
therefore carries the *old* gold expectation while the code under test has the *new*
behaviour. PR #973 is red for the same reason.

**Why it matters:** the red is indistinguishable, from the board's point of view, from a
genuine defect — the completion classifier will route it to remediation and spend a runner
on a "bug" whose entire fix is `git rebase`. On a day when a contract changes, every
in-flight PR becomes a false positive at once. Nothing in the failure output says
"your base is behind"; a reader has to independently run the suite against master to find
out, which is what happened here.

**Fix direction:** when a required-check failure is confined to conformance/gold suites and
the same suites pass on the merge-base's tip, classify as `base_stale` and route to a
rebase, not a remediation of the task's own code. The signal is cheap — the merge queue
already builds against tip; the classifier just never compares.

**Not repaired.**

---

### Observation — the conformance harness earned its keep (2026-07-27)

Recorded because it is the counter-example to most of this file.

The gold catalog **caught a real contract regression in production CI**: when PR #948's
stale branch overwrote the ADR-0008 matching-role remediation expectation, the gold suite
failed and PR #974 was raised to restore it. That is exactly what COORD-71 built it for —
*"so future regressions fail merge CI, not live boards."* It worked, on its first real
adversarial event, against a regression introduced by the operator.

### Observation — first clean hands-off loop since the repairs (2026-07-27 ~03:46-03:55 UTC)

COORD-67 and COORD-68 ran the **full loop with zero operator action**: dependency cleared
(COORD-66 stamped Done) → Autopilot dispatched both within minutes → both booted with no
trust prompt → worked ~9 minutes → pushed branches → opened PRs #975 and #976 → called
`complete_claim` → surrendered the lease (fence_epoch 1→2) → capacity reaper stopped the
process → tasks exposed **In Review**.

The `LEASE-EXPIRED` events this produced are the **correct** ADR-0008 C3 terminal path, not
a failure. Worth stating plainly because they are indistinguishable in the host log from
breakdown 21's immortal-zombie expiry and from breakdown 48's wrongful fence — three very
different events, one message. A `reason` that distinguished *surrendered-after-completion*
from *TTL-expired* from *fenced-by-coordinator* would make this log a lot shorter.

**Still open at time of writing:** the fence fix (breakdown 48) has **not** yet been proven
end-to-end by a live Autopilot remediation runner. PR #963's rebase — the obvious candidate
— was pushed by an interactive agent session, not a Connect runner, so it does not count as
proof.

---

## BREAKDOWN 57 — the dead runner's task claim outlives it and blocks its own remediation ⚠️

**Severity:** critical (remediation cannot be dispatched; retries forever)
**Observed live on COORD-67 and COORD-68**, both simultaneously, immediately after the
successful hands-off loop recorded above.

The completion classifier does the **right** thing:

```
"classifier": {"route": "remediation", "planned_effect": "start_remediation",
               "reason_code": "required_exact_head_ci_failed", "state": "blocked"}
```

and the effect fails, over and over:

```
"external_effects": [{"effect_type": "completion_effect", "resource": "start_remediation",
                      "status": "failed", "retry_count": 6,
                      "last_error": "Another agent owns this task.",
                      "next_retry_at": null}]
"claim_refusals": [ 5 × {"kind": "side_effect.failed",
                         "message": "Another agent owns this task."} ]
```

**Root cause:** the implementation runner's **task claim survives its own completion**.

```
"active_claims": [{"claim_id": "taskclaim-c7da4880105341a9",
                   "agent_id": "agent/codex/coord-67",
                   "principal_id": "direct-session/run_bf392438d0cc6d02",
                   "status": "active",
                   "claimed_at": 1785124095, "expires_at": 1785127695}]
```

`run_bf392438d0cc6d02` is the runner that **already completed, surrendered its lease
(fence_epoch 1→2), and was reaped**. Its claim keeps `status='active'` on a one-hour TTL.
`start_task` refuses any caller that is not the claim owner
(`application/commands/task_execution.py:751-760` →
`coordination.task_start_ownership` → `_active_task_claim_in`, which matches on
`status='active' AND expires_at > now`). The completion owner is not
`agent/codex/coord-67`, so **every** remediation dispatch is refused for up to an hour.

**Why it matters:** this is the failure the whole day's work was meant to eliminate — a
task that cannot get a runner. It is worse than the earlier ones because *nothing is
broken*: the classifier is right, the host is healthy, capacity is free (15 of 16 slots),
the fix from breakdown 48 is deployed. The work is simply un-dispatchable because a dead
agent still owns the task. The retry loop burns a dispatch every ~35-100s and records
`next_retry_at: null`, so it also lands in breakdown 50's terminal-effect wedge.

**Fix direction:** `complete_claim` already surrenders the execution lease at the role
boundary; it must release the **task claim** in the same transaction. Failing that, the
completion owner must be recognised as an authorised successor for a task whose claim
holder's execution generation is terminal — a dead generation's claim is not ownership.

**Not repaired** (observed under standing instruction not to intervene).

---

## BREAKDOWN 58 — the dependency healer overwrites `Blocked(route=remediation)` ⚠️

**Severity:** critical (the janitor mutates work state; ADR-0008 W3)
**Observed:** COORD-67's activity feed, six times in a row:

```
{"actor": "switchboard/mission-status", "kind": "task.dependency_status_healed",
 "text": {"previous_status": "Blocked", "status": "Not Started",
          "reason": "all_dependencies_done", "depends_on": ["COORD-66"],
          "schema": "switchboard.dependency_status_healed.v1"}}
```

The result is a task that reads as two contradictory things at once:

| Surface | Says |
|---|---|
| `get_task.status` | **"Not Started"** |
| `get_task_execution.lifecycle_phase` | `review` |
| `active_claims` | 1 active claim |
| `session_health.active_session_count` | 1 |
| `git_state` | PR #975 open, head `92cf9a61` |
| `review_verdict.current_verdict` | `null` (**"missing"**) |
| classifier | `route=remediation`, `state=blocked` |

**Root cause:** `storage/repositories/tasks.py:255-290`. The healer selects on the status
string alone —

```sql
SELECT * FROM tasks WHERE status='Blocked' ...
UPDATE tasks SET status='Not Started', assignee=NULL, updated_at=? WHERE task_id=? AND status='Blocked'
```

— and only skips a task whose block is "exceptional", where exceptional is a fixed
allowlist of four activity kinds (`tasks.py:196-201`):

```
git.pr_merged_semantic_blocked, git.default_branch_semantic_blocked,
review.remediation_escalated, task.human_blocker
```

**The completion classifier's own `Blocked(route=remediation)` is not in that list.** So
the janitor sees `Blocked` + dependencies satisfied, concludes "stale dependency block",
resets the task to `Not Started`, **and clears the assignee** — destroying the completion
owner's live routing state and the task's ownership record, repeatedly.

**Why it matters:** two different meanings are crammed into one `Blocked` string —
*blocked on dependencies* and *blocked pending remediation* — and a janitor with no
authority over the second is allowed to overwrite it. ADR-0008 W3 gives the roaming daemon
a narrow bookkeeping allowlist (sweep expired wakes and leases, reconcile provenance,
regenerate briefs, publish findings) and states it "may not ... advance completion phases".
Rewriting a task's status and nulling its assignee is squarely outside that.

It also compounds breakdown 57: the healer clears `assignee` while the stale task claim
survives, so the task simultaneously has **no assignee** and **an owner that blocks
dispatch**. And it makes the board actively lie — `Not Started` on a task with a pushed PR,
477 lines of finished work, and a live work session.

**Fix direction:** the healer must key off the *cause* of the block, not the string. Either
the completion owner writes a durable block-cause marker that joins the exceptional
allowlist, or — better, and in the ADR's spirit — dependency-blocked becomes a distinct
state from completion-route-blocked so no janitor can confuse them.

**Not repaired.**

---

### Observation — what the pair actually costs (2026-07-27 ~04:00-04:25 UTC)

COORD-67 and COORD-68 are, at time of writing, in a stable wedge that no component will
escape on its own:

1. runner finishes, opens a PR, `complete_claim` → In Review — **claim not released** (57);
2. PR is red on a **stale base** (56), so the classifier correctly routes to remediation
   and sets `Blocked`;
3. the janitor "heals" that `Blocked` back to `Not Started` and clears the assignee (58);
4. `start_remediation` is refused by the dead runner's surviving claim (57);
5. the effect goes terminal with `next_retry_at: null` (50) — no scheduled escape;
6. goto 3.

Six breakdowns interlocking, of which **five are already in this file**. The originating
defect — the stale base — has a one-command fix. Nothing in the loop can apply it.

**No intervention taken.** Both tasks left in this state deliberately so the evidence
survives.

---

### Observation — BREAKDOWN 52 confirmed live on COORD-79 / COORD-80 (2026-07-27 ~04:35 UTC)

Operator question: *"why didn't autopilot launch runners for 79 and 80 yet?"* It did. The
wakes cannot be claimed by anything in the fleet.

Both tasks' dependencies are satisfied — COORD-77 and COORD-78 are Done (`844e974c`
merged) — and `explain_task_block` reports `blocked: false` for both. Autopilot's scope is
`active` (generation 3, ticking). It issued a wake for each:

```
COORD-79  effect_type=wake  status=issued  updated_at=1785125492
COORD-80  effect_type=wake  status=issued  updated_at=1785125492
```

Both then sat pending. The placement decision on COORD-79's wake:

```
"placement": { "session_policy": "docs_review", ... }
candidates:
  host/steve-mbp-co16   eligible=false  reason_codes=["session_policy_not_supported"]
                        physical_capacity={"active_sessions": 0, "available_sessions": 16,
                                           "max_sessions": 16}
  host/plan-vm-message-wake   eligible=false  [... session_policy_not_supported ...]
  4 × ephemeral               eligible=false  [... session_policy_not_supported ...]
"eligible_host_count": 0
"reason_code": "no_eligible_persistent_capacity"
```

**Sixteen of sixteen slots free on a healthy host, and the fleet reports "no eligible
persistent capacity."** COORD-79 and COORD-80 carry no explicit
`policy_profile:`/`session_profile:` tag, so they resolve to the switchboard board default
`docs_review` (breakdown 52), which no host advertises. COORD-67 and COORD-68 launched
earlier today only because their descriptions do carry `session_profile:code_strict`.

The loop from here is deterministic: the wake expires at its 900s deadline, the scope ticks,
a fresh wake is issued against the same wall, forever — burning a dispatch every 15 minutes
against capacity that is sitting idle. Nothing in the chain reports a policy mismatch as a
refusal; the host tick still reads `pending: N, acted: [], refused: []`.

This is the second live confirmation today that the highest-leverage missing behaviour is
**a recorded skip**. The information needed to diagnose this in one read already exists in
the placement decision — it simply never reaches an operator surface or a finding.

**Not repaired** (observed under standing instruction not to intervene).
**Run date:** 2026-07-27 (22:40 UTC)
**Operator:** claude/UI-69-planner (observer mode — no intervention after arming)
**Host:** `host/steve-mbp-co16`
**Tasks launched:** UI-68 (`runtime=codex`, `role=implementation`); UI-71 gated behind it
**Deliverable under test:** `deliverable-autopilot-dock-honest-runner-liveness`
**Autopilot scope:** `autopilot-ae656a96cff64701` (deliverable scope, `status=active`, generation 1)

> Standing instruction for this run: **do not intervene**. Document only.
> Nothing below has been repaired.

---

## Step 0 — Preconditions (PASS)

| Field | Value |
|---|---|
| `get_project_execution_readiness` | `passed=true`, `status=ready`, `blockers=[]` |
| autopilot state | `ready`, profile `autopilot-default`, `enabled=true` |
| configuration | topology valid, policy revision 6, canonical `6th-Element-Labs/projectplanner` |
| provider | all selected connections active (selector_count 1) |
| scm | `scm-a9db19c5b1d94400`, covers canonical repo lifecycle |
| persistent capacity | `not_required`; eligible `host/steve-mbp-co16` |
| ephemeral capacity | ready, burst enabled, max_concurrent 2 |
| live_host_count | 2 |

## GOOD PATH — arming to live runner worked hands-off

Recorded because it worked and should not be re-litigated.

- Scope created `1785191542`, ticked at `1785191633` (≈91s).
- UI-68 runner live at `1785191598`: `run_ac7409834b93b352`,
  `agent/codex/ui-68`, `host/steve-mbp-co16`, `status=running`, `stale=false`,
  `heartbeat_ttl_s=180`, `fence_epoch=1`, `execlease-3fef8eaa92b64fd489c9`.
- Output age fresh throughout the observed window (`last_output_at` advancing).
- **Dependency gating held.** UI-71 `depends_on=[UI-68]` was correctly *not*
  started; `list_runner_sessions` shows exactly one live runner. No double-drive.

## BREAKDOWN 43 — a runner's `status` is never reconciled when its lease expires, and the watchdog that would notice cannot see it ⚠️

**Severity:** high — three runners on this board currently report `status: "running"`
while having produced nothing for hours. Every read surface believes them.

**Evidence** (`GET /ixp/v1/runner_sessions?project=switchboard&include_stale=true`,
2026-07-27 22:35 UTC, unrelated to this run's task):

```
UI-68        running stale=False last_output_at=1785191744  fault=None      <- this run, healthy
COORD-94     running stale=True  last_output_at=1785170054  fault=raised
BUG-154      running stale=True  last_output_at=None        fault=None
SIMPLIFY-17  running stale=True  last_output_at=None        fault=None
```

COORD-94's fault, verbatim:

```
"message": "live runner lease with no PTY output for 11936s (bound 1800s)",
"kind": "runner_progress_stalled", "output_age_s": 11936.314, "bound_s": 1800.0,
"raised_at": 1785171867.5230937, "output_bytes": 751480
```

**Why it matters.** Two independent gates both fail open here:

1. `runner_progress_monitor._eligible()` requires `_is_live_lease(row)`
   (`runner_progress_monitor.py:68-76`), and `_default_sessions()` sweeps with
   `include_stale=False` (`:153-157`). So once a lease expires the runner leaves
   the watchdog's view **permanently** — it can never be faulted, and an
   existing fault can never be cleared (COORD-94 still carries a fault raised
   at `1785171867`).
2. `evaluate_progress()` returns `None` when `last_output_at` is absent
   (`:92-94`, "Old hosts that never report progress cannot invent a stall
   signal"). That choice is defensible on its own, but it means BUG-154 and
   SIMPLIFY-17 — which never reported output at all — are invisible to the
   watchdog *and* therefore produce no `_runner_progress_item` attention entry.

The compound result: a runner can die, keep `status: "running"` forever, raise
no fault, and generate no attention item. Nothing escalates. `stale=true` is the
only surviving evidence, and it is a field most surfaces do not lead with.

**Note:** this is adjacent to the specced lease-orphan work (ADR-8 two-task cut,
`tests/test_adapter35_runner_lease_retry.py`), but that work is about *recovering
the task*. This entry is narrower and separate: the runner row's own `status`
field is never reconciled, so the board reports a false "running" indefinitely.

**Fix direction (not applied here):** on lease expiry, reconcile the row's
`status` off `running` rather than leaving it host-reported, or have the read
model derive a status from lease liveness instead of trusting the stored column.
Either way `status` must stop being the field surfaces believe. Clearing a stale
fault also needs a path that does not require eligibility.

**Not repaired.**

## BREAKDOWN 44 — `create_deliverable` upsert is a full replace, and it can empty `proof_requirements` while `status=in_progress` ⚠️

**Severity:** high — this is the closure-gate intake requirement silently deleting itself.

**Evidence.** Sequence run against `deliverable-autopilot-dock-honest-runner-liveness`
while scoping this deliverable:

1. Created with `end_state` + `acceptance_criteria` + valid `proof_requirements`
   (2 gates) at `status=in_progress`. Accepted.
2. Re-upserted passing **only** `deliverable_id`, `title`, `status`,
   `acceptance_criteria` to correct a formatting problem.
3. Response came back with `"end_state": ""`, `"why_it_matters": ""`,
   `"owner_person_or_role": ""`, `"policy_constraints": {}`, and
   `"proof_requirements": {}` — while `"status": "in_progress"` was retained.

**Why it matters.** `PM_ENFORCE_DELIVERABLE_INTAKE` exists to guarantee that an
`in_progress` deliverable has `end_state`, `acceptance_criteria`, and a
well-formed `proof_requirements` (docs/DELIVERABLE-CLOSURE-GATE.md). The gate is
enforced on the *transition* into `in_progress`, so an upsert that stays
`in_progress` never re-checks it — and because unspecified fields are cleared
rather than merged, a partial edit strips the gate's own preconditions. The
deliverable then sits `in_progress` with zero required gates, and
`verify_deliverable_closure` has nothing to hold it on.

An operator correcting a typo in one field silently disarms the closure gate.

**Fix direction (not applied here):** either make the upsert a merge for
unspecified fields, or re-run the intake assertion on every write that leaves
status at `in_progress` — not only on the transition into it.

**Not repaired.** (Restored by hand in this session; the defect is unchanged.)

## BREAKDOWN 45 — `acceptance_criteria` is comma-split, so any criterion containing a comma is shredded

**Severity:** medium — corrupts the text the closure gate is checked against.

**Evidence.** Submitted as newline-separated prose. Returned as:

```json
["1. Left nav",
 "page header and dock header read \"Autopilot\"; ... Internal identifiers (fleet-dock element id",
 "toptab-fleet",
 "SwitchboardFleetDock",
 "_renderFleetDock",
 "/ixp/v1 routes) are unchanged.",
 "2. FleetDock.runnerConditions returns conditions worst-first over seven ranks: exited",
 "lost_host", "waiting_on_you", "silent", "idle", ...]
```

**Why it matters.** Acceptance criteria are the human-readable contract Gate 1
(`scope`) and the closure report are judged against. Splitting on commas turns
one criterion into a list of sentence fragments — `"toptab-fleet"` and
`"silent"` became standalone "criteria". A reviewer reading the deliverable sees
noise, and any automated criterion count is inflated and meaningless.

**Fix direction (not applied here):** split on newlines only, or accept a JSON
list. If comma-splitting is load-bearing for some caller, it needs to be opt-in
rather than the default for a free-text field.

**Not repaired.** (Worked around by writing comma-free criteria.)

## BREAKDOWN 46 — `archive_task` leaves deliverable links behind as `state=missing` with `blocks_deliverable=true`

**Severity:** medium — a deliverable can be blocked forever by tasks that no longer exist.

**Evidence.** After archiving 7 tasks that had been linked to the deliverable,
`get_deliverable_dependency_graph` reported:

```
stats: {"node_count": 9, "blocker_count": 9, "todo_count": 2, "edge_count": 1}
nodes: UI-69/70/72/73/74/75/76 -> {"state": "missing", "status": null,
                                   "blocker": true, "provenance": null}
```

and `get_deliverable` still listed each as a `task_links` entry with
`"blocks_deliverable": true` and `"task": {"error": "unknown task"}`.

**Why it matters.** `archive_task` succeeded and returned `archived: true` for
each task, but the deliverable's link rows were not touched. The deliverable
then reports 9 blockers of which 7 cannot ever reach a terminal state — an
unresolvable closure condition produced by a supported operation. `archive_task`
already refuses to run when a task has active claims or leases, so it clearly
inspects related state; deliverable links are simply not in that set.

**Fix direction (not applied here):** archiving a task should either cascade to
its deliverable links or refuse while `blocks_deliverable=true` links exist,
the same way it refuses on active claims. A dangling blocking link is the same
class of broken edge that `add_dependency` already fails closed on.

**Not repaired.** (Unlinked by hand in this session.)

### Observe tick — UI-68 (2026-07-27 22:40 UTC)

- `status=running stale=False output=fresh fault=none`. Background watch armed
  on runner state + PR appearance; ticks appended below as they land.

### Observe tick — UI-68 reached In Review hands-off (2026-07-27 22:53 UTC)

**The full loop worked with no intervention.** Recorded as the good path.

| Event | Time | Evidence |
|---|---|---|
| scope active | 1785191542 | `autopilot-ae656a96cff64701` |
| runner live | 1785191598 | `run_ac7409834b93b352`, `agent/codex/ui-68` |
| task claimed | 1785191709 | `Not Started` → `In Progress` (111s after runner start) |
| draft PR opened | ~1785192651 | #1007, head `da4cd565`, 8 files, +296/−34 |
| CI dispatched | ~1785192700 | `ci_state` `none` → `pending` (event-driven gate fired) |
| claim completed | 1785192781 | `In Progress` → `In Review`, `has_ended_session=true` |
| runner released | 1785192781 | `status=expired` after complete_claim, not before |

Dependency gating held throughout: UI-71 stayed `Not Started` with no wake in
flight. Exactly one runner for the whole run. No double-drive.

Commit series matched the plan one-for-one, in order:
`3e91c00e` rename → `c8471a7b` ladder → `8d543f60` task-first card →
`d069180f` condition-led actions → `da4cd565` pill labels.

## OBSERVATION — the agent completed the loop but silently substituted its own semantics, and wrote tests that ratify them

**Severity:** medium — not an autopilot failure. Autopilot did its job. This is a
fidelity gap between a specced plan and what a self-reviewing agent ships.

**Evidence.** The plan (`docs/superpowers/plans/2026-07-28-autopilot-dock-runner-ladder.md`)
specified seven ranks with `silent` keyed on the presence of WATCH-19's
`progress_fault` and `idle` meaning *no task bound*. Shipped in
`da4cd565:static/js/fleet-dock.js`:

```js
if (age != null && age >= 600) {
    out.push({ key: 'silent', label: `Silent ${this.shortAge(age)}`, ... });
} else if (age != null && age >= 120) {
    out.push({ key: 'idle', label: `Idle ${this.shortAge(age)}`, ... });
} else if (age != null) {
    out.push({ key: 'working', label: 'Working', ... });
```

Three divergences:

1. **`silent` now fires on a hardcoded 600s** instead of on `progress_fault`.
   The server's own watchdog bound is 1800s (`runner_progress_monitor`), so the
   card will call a runner "Silent" for 20 minutes while the control plane
   considers it fine and raises no attention item. The UI and the watchdog now
   disagree about what silence means.
2. **`idle` was redefined** from "running, no `task_id` bound" to "output age
   between 120s and 600s". A runner actively working with two minutes of quiet
   now reports `Idle 3m`, and a runner with no task bound is no longer flagged
   at all. The rank name survived; its meaning did not.
3. **`runnerOutputAge` reads `s.progress_fault`**, but the payload carries it at
   `s.environment.progress_fault` (`runner.py:822`). The fault fast-path is dead
   code. It still computes a correct age by falling through to
   `environment.last_output_at`, so nothing visibly breaks — which is why nothing
   caught it.

**What did survive:** the load-bearing rule. `runnerOutputAge` returns `null`
when output age is unknown, and the ladder's `null` branch falls through to an
uptime-labelled rank. The shipped test asserts it:

```js
if (!unknownConditions[0].label.includes('47m') || unknownConditions[0].label.includes('Silent'))
```

Acceptance criterion 3 holds — no "Silent 0m", no "0s".

**Why it matters.** The plan supplied nine named test cases with full bodies.
The shipped ladder test carries three assertions, covering `running_unknown` and
the dirty secondary chip — precisely the slice its implementation satisfies.
There is no test for the rank ordering, for `waiting_on_you` outranking `silent`,
or for `idle` semantics. So CI goes green, self-review passes, and the divergence
merges. This is the known cost of ADR-0021 self-review: CI is the bar, and CI
cannot catch a semantic substitution that the tests were written around.

**Fix direction (not applied here):** when a plan supplies verbatim test bodies,
the task should require those tests rather than "tests for this behaviour" —
tests-as-spec, not tests-as-afterthought. Cheap partial mitigation: have the
closure gate assert a minimum assertion count per specced criterion.

**Not repaired.** Observer-mode run; the operator is holding all fixes until the
loop is proven end to end.

### CI red on UI-68 — cause identified: the PLAN was wrong, not the agent (2026-07-27 23:10 UTC)

`Switchboard CI / VM gate` failed on `da4cd565`
(`ecir-dbd9f550b3ad41d4`, run 30312156176, `failure_class=workflow_failed`).

Faithful local reproduction (`PYTHON=<project venv 3.13>` — see caveat below):
**exactly 1 failure of 593 files.**

```
== FAIL tests/test_arch_ms14_test_layout.py (exit 1) ==
AssertionError: test_autopilot_dock_autopilot_pill_labels.py imports the shared path shim
```

`tests/test_arch_ms14_test_layout.py` (ARCH-MS-14) is an architectural guard: it
AST-parses every `tests/test_*.py` and requires `from path_setup import ...`.
The five new test files use `pathlib.Path(__file__).resolve().parents[1]` instead.

**This is a planner defect, not an agent defect.** The plan
(`docs/superpowers/plans/2026-07-28-autopilot-dock-runner-ladder.md`) supplied
those test files verbatim, and every one of them opens with:

```python
import pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
```

The convention was visible in the file the planner had already read
(`tests/test_ui_deployment_fleet_tab.py:7`, `from path_setup import ROOT`) and
was not carried into the plan. The agent implemented the specified tests
faithfully; the specification violated a guard the repo already enforces.

**Significance — this is the system working.** ADR-0021 accepts agent
self-review on the bet that CI is the real bar. Here self-review passed (the
agent's own five tests are green, verified at its SHA) and CI caught a genuine
convention violation anyway. The guard did exactly its job, on the planner.

**Caveat on the first reproduction attempt.** An earlier local run reported 8
failures, all `TypeError: dataclass() got an unexpected keyword argument 'slots'`.
That was the observer's environment — the default `python3` on this host is
3.9.6, CI runs 3.12, and `slots=True` needs 3.10+. Those 8 were not real. Noted
because reporting them would have handed the operator a false cause; a repro is
only evidence when the interpreter matches.

**Fix direction (not applied here):** the plan's test scaffolding must use
`from path_setup import ROOT`. More usefully: any plan that ships verbatim test
bodies should be checked against the repo's own guard tests before dispatch —
ARCH-MS-14 is discoverable and would have caught this at authoring time.

**Not repaired.** Left red deliberately: the open question this run exists to
answer is whether Autopilot routes a red gate to remediation and converges, or
repair-loops as it did on COORD-57 (BREAKDOWN 42 territory). Watching.

### CORRECTION to the fidelity observation above (2026-07-27 23:15 UTC)

The earlier observation ("the agent substituted its own semantics") is **wrong
about cause and unfair to the agent.** `get_task(UI-68).git_state.known_signal`
records, verbatim:

```json
{"failure_class": "missing_data", "source": "task_plan_reference",
 "observed": "docs/superpowers/plans/2026-07-28-autopilot-dock-runner-ladder.md absent at canonical base SHA",
 "handling": "implemented against full board acceptance contract; UI-71 attention join excluded"}
```

The plan was committed on the observer's worktree branch and never merged, so it
did not exist at the agent's base SHA `ae95edc1`. The agent never had the nine
verbatim test bodies or the rank table. It reconstructed the ladder from the task
description alone, **declared the missing input as a typed `missing_data` signal
instead of hiding it** (fail-fix-early policy, working exactly as written), and
still landed the load-bearing rule correctly.

The divergences are real and still worth fixing, but they are a *planner*
failure: referencing a plan path the executor cannot read. Second planner defect
this run, same root cause as the `path_setup` one — authoring against a private
branch.

**Fix direction (not applied):** a task that cites a plan path must either have
that path merged to the base branch first, or inline the contract in the task
body. Cheap guard: reject dispatch when a task description references a
`docs/**` path absent at the base SHA.

## BREAKDOWN 47 — remediation advances the PR head but not the board's `head_sha`, and the assigned recovery route cannot ever fix it ⚠️

**Severity:** high — non-convergent by construction. This is the shape of
BREAKDOWN 42 again: a state routed to a recovery that is structurally incapable
of resolving it.

**Evidence.** Remediation pushed the correct minimal fix
(`e0426552 UI-68 use shared test path shim`, 5 files, −10 net) and recorded a
full passing test run **at the new head**:

```json
"executed_test_run": {"head_sha": "e04265521b691ec97e09080b3762e30594df7e70",
  "exit_code": 0, "passed": true, "run_id": "testrun-aa52f344301f44cd",
  "commands": ["for f in tests/test_autopilot_dock_*.py; do python3 \"$f\"; done",
               "python3 tests/test_arch_ms14_test_layout.py",
               "node --check static/app.js", "node --check static/js/fleet-dock.js",
               "git diff --check",
               ".../bin/python scripts/run_ui_playwright.py"]}
"mutation_check": {"test": "tests/test_autopilot_dock_autopilot_pill_labels.py",
                   "observed_exit_code": 1, "restored_exit_code": 0}
```

But the task's own top-level field was never advanced:

```
git_state.head_sha                            = da4cd565…   (stale)
git_state.evidence.executed_test_run.head_sha = e0426552…   (current)
PR #1007 head_sha                             = e0426552…   (current)
```

`state_machine.py:814-816` compares the stale top-level value:

```python
if board_head and board_head != head_sha:
    return _decision("blocked", "coordination_retry", "board_pr_head_mismatch",
                     retry="bounded")
```

Result: `route=coordination_retry`, `retry=bounded`, exhausted at `attempt: 5`,
then `route=human`, `route_owner=operator`, `current_effect=escalate_human`,
`board_status=Blocked`.

**Why it matters.** A bounded coordination retry can never clear a stale board
head — nothing in that route writes `head_sha`. Only a new `complete_claim` (or
an explicit evidence advance) does, and the agent had already completed its claim
before remediation ran. So the loop burned five attempts on a repair that was
mechanically impossible, then escalated. The work itself is *finished and green*:
the recorded run at `e0426552` passed every gate including Playwright and the
mutation check. The task is Blocked purely on bookkeeping.

**Fix direction (not applied here):** `record_executed_test_run` already carries
the authoritative `head_sha` — advance `git_state.head_sha` from it (it is the
same write path that stamps hygiene), or have the state machine compare against
the newest recorded evidence head rather than the last-claimed head. Either
removes an unreachable state. If the mismatch must stay blocking, its route
should be one that can actually write the head, not `coordination_retry`.

**Not repaired.**

## BREAKDOWN 48 — the human escalation was created and then silently not delivered on either channel ⚠️

**Severity:** high — the circuit breaker worked and the human was never told.

**Evidence.** Two `attention.push_missed` activity records on UI-68:

```json
{"attention_id": "provider:attention-3670ce81ca7248dc8ee546872e45bf7d",
 "delivered": false,
 "results": [{"channel": "slack", "dry_run": true, "sent": false},
             {"channel": "email", "sent": false,
              "error": "(550, b'5.4.5 Daily user sending limit exceeded. ... gsmtp')"}]}
```

**Why it matters.** This is the good half of the story failing at the last inch.
Autopilot bounded its retries, declined to loop 50x as it did on COORD-57, and
correctly escalated to the operator — and then both delivery channels failed:
Slack is in `dry_run`, and Gmail refused on a daily sending limit. `delivered:
false` is recorded honestly rather than swallowed, which is the right behaviour,
but nothing reads that record. The operator learned the run was blocked only
because a human observer happened to be polling.

An escalation nobody receives is indistinguishable from a hang. Every
`escalate_human` outcome inherits this.

**Fix direction (not applied here):** treat `delivered: false` on all channels as
its own alertable condition — a `push_missed` with no successful channel should
raise a visible board-level signal (the Autopilot dock's Needs-you strip is the
natural home), not just an activity row. Separately: Slack `dry_run: true` on a
production project is a config state worth surfacing at readiness time, and the
Gmail limit argues for a channel that does not share a consumer quota.

**Not repaired.**

### Scoreboard for this run

| Segment | Result |
|---|---|
| arm → scope → wake → runner | hands-off PASS |
| claim → In Progress | hands-off PASS |
| implement (5 commits, plan order) | PASS, despite the plan being unreadable |
| draft PR → ready | hands-off PASS |
| CI dispatch | hands-off PASS |
| complete_claim → In Review | hands-off PASS |
| dependency gating (UI-71 held) | PASS |
| CI red → diagnose → remediate | hands-off PASS (correct minimal fix) |
| executed-test evidence + Playwright + mutation check | PASS, all recorded |
| repair loop bounded (no 50x) | PASS — stopped at attempt 5 |
| board head advance after remediation | **FAIL — BREAKDOWN 47** |
| human escalation delivery | **FAIL — BREAKDOWN 48** |

Eleven of thirteen segments ran hands-off. Both failures are bookkeeping and
notification, not code or dispatch.

### CORRECTION — the repair loop is NOT bounded. It is the COORD-57 shape. (2026-07-27 23:25 UTC)

The scoreboard above claims "repair loop bounded (no 50x) — PASS, stopped at
attempt 5". **That was premature and is wrong.** Attempt 5 was simply where the
counter happened to be when first sampled. Eight minutes later:

```
ci_state       = success
mergeable      = clean          <- the PR is mergeable
head_sha       = e0426552…      <- matches the recorded green evidence
state          = blocked
route          = human
reason_code    = board_pr_head_mismatch
current_effect = escalate_human
attempt        = 33
state_version  = 33
terminal       = False          <- never settles
```

`route=human` did not stop the loop; it only changed the loop's effect. The
controller keeps re-ticking, re-deciding, and re-escalating. `terminal: False`
means there is no absorbing state.

**Everything else about the PR is now green.** CI passed on `e0426552`,
`mergeable_state=clean`. The single thing holding the task is
`git_state.head_sha` still reading the superseded `da4cd565`. One stale field is
producing an unbounded loop on a finished, mergeable, fully-evidenced change.

Correct the scoreboard row: **repair loop bounded → FAIL.**

## BREAKDOWN 49 — the human escalation is not idempotent: one new attention request per tick, none deliverable, none resolvable ⚠️

**Severity:** critical — this is BREAKDOWN 47 and 48 compounding into a queue flood.

**Evidence.** `GET /api/attention/requests?project=switchboard&limit=200`:

```
total attention requests = 17
UI-68 attention requests = 17
  ('pending', 'board_pr_head_mismatch') -> 17
first created_at = 1785193424.645204   last = 1785193927.9272485
all bound to runner_session_id = run_93048f035ca6ad10
```

17 requests in 503 seconds — **one every ~30 seconds** — every one `pending`,
every one the same `reason_code`, every one for the same task and runner session.
100% of the operator queue for this project is now a single stuck task
restating itself.

**Why it matters.** Three defects multiply:

1. The condition cannot be fixed by its assigned route (BREAKDOWN 47), so it
   never clears.
2. Each escalation's push fails on both channels (BREAKDOWN 48), so nothing
   surfaces.
3. The escalation creates a *new* attention request per tick rather than
   updating or deduping the existing one — so the queue grows without bound.

An `escalate_human` effect that fires every 30s is not an escalation, it is a
retry loop wearing an escalation's clothes. And because `create_attention_request`
is being called per tick with no idempotency key on
`(task_id, reason_code, head_sha)`, the operator queue becomes unusable for every
*other* task on the board. At this rate a single stuck task produces ~2,880
pending requests a day.

**Design feedback for UI-71 (this deliverable).** The dock's planned
Waiting-on-you join is keyed by `runner_session_id`, so all 17 collapse to one
chip on one card — the join shape holds up under this failure. But the Needs-you
inbox itself would show 17 identical rows. Whatever dedupe the dock applies,
the queue needs it at the write path too.

**Fix direction (not applied here):** make `escalate_human` idempotent on
`(task_id, reason_code, head_sha)` — update `updated_at` / bump a repeat counter
on the existing pending request instead of inserting a new one. Independently,
an effect that has already escalated should stop re-deciding: `route=human`
needs to be absorbing until the operator acts or the head changes, which is what
`terminal` should be expressing and currently is not.

**Not repaired.** Left running deliberately; the operator is holding all fixes.
Note for whoever does repair this: there are 17+ pending rows to reconcile, and
the count is still climbing.
