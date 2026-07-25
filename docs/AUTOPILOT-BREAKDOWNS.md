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
