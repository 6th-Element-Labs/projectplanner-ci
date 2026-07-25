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

**Severity: HIGH (process). STATUS: LIVE.**

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
