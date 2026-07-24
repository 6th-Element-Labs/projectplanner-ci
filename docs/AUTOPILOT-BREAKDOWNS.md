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
