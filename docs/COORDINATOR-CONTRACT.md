# Coordinator Agent — Operating Contract & Escalation Policy

- **Status:** SIMPLIFY-2 lifecycle ownership implemented. COORD modules remain internal policy phases, not independent schedulers.
- **Owner:** Product / Control Plane
- **Relates to:** [PRD §6, §18, FR-23–29](PRD-AGENT-COORDINATION-LAYER.md) · [AGENT-HOST-SPEC](AGENT-HOST-SPEC.md) · [ADR-0003 work provenance](decisions/0003-work-provenance-and-reconciliation.md) · [T0 audit loop](COORDINATOR-AUDIT-LOOP.md) · [T2 review steward](COORDINATOR-REVIEW-STEWARD.md) · `mission_coordinator.py` · `dispatch.py` · `merge_coordinator.py` · `review_steward.py`
- **One line:** One Coordinator leader owns Ready → implementation → review/remediation → merge → reconcile. Every session ensure is `Task Execution.start_task(role=...)`; internal COORD modules preserve their gates but own no queue or timer.

---

## 1. Why a distinct role

Switchboard already coordinates *what work exists and what it depends on* (the board) and *who pulls it* (`claim_next`). What is undefined is the **actor that drives dispatch on the fleet's behalf**: watches the ready frontier, routes work to the right runtimes, shepherds review/merge, and escalates. Today a human does this by hand, or `run_coordinator_tick` (mission_coordinator.py) does a narrow deliverable-scoped slice. This contract promotes that actor to a **named role with a bounded permission envelope**, distinct from coding agents:

| | Coding agent | Coordinator agent |
|---|---|---|
| Job (PRD §18) | **Execute** one task's steps (write code, judgment) | **Plan + dispatch**: route work, steward review/merge, escalate |
| Writes | its own branch/PR; `claim`/`complete_claim` on its task | calls Task Execution, records decisions, and arms safe merges — never task *content* |
| Merge authority | its own PR via safe-merge | arms/executes safe merges only (T3+), canonical-only |
| Sets `Done`? | No — records evidence → In Review | **No — never.** Only the webhook/reconcile path stamps Done |
| Default posture | claims and works | **read-only observer** until explicitly elevated |

The coordinator is *the K8s scheduler, not a pod*: it schedules and supervises; it does not do the work.

---

## 2. The non-bypassable floor (holds in EVERY tier, including autopilot)

These are hard invariants. No coordinator action, policy, or autopilot loop may bypass them. They are enforced by existing control-plane code the coordinator calls *through*, never around:

1. **Done is branch-proven, never coordinator-set** (FR-24). The coordinator may move work to `In Review` via a worker's `complete_claim`, but `Done` is reserved for GitHub/default-branch merge provenance stamped by the webhook/reconcile path, or verifier-stamped offline evidence. The coordinator MUST NOT call `update_task(status="Done")` — it fails closed anyway (`store.pr_backed_by_process` / `PR_BACKED_STATUSES`).
2. **Safe-merge only** (FR-28). Any merge the coordinator arms or executes must pass `store.merge_gate`: canonical repo only, target = default branch, real PR evidence, session hygiene (no conflict markers / clean tree), required status contexts green, tests executed. A blocked finding stops the merge and returns the task to mechanical repair; the coordinator never overrides it.
3. **Project boundaries are absolute.** A coordinator is scoped to exactly one project; every read and write carries that `project`, and its scoped token is minted per project (`create_scoped_token`). It can never read, write, dispatch, or merge across projects. Dispatch targets only that project's lanes and hosts.
4. **Fail-fix-early, never green-wash** (FR-29 / `fail_fix_early_policy`). The coordinator surfaces missing data, red gates, absent hosts, permission denials, and provenance drift at the point of detection. It may use a fallback only when the fallback is *named* and preserves the original failing signal (monitor event / reconcile finding / task comment / blocker). It must never hide a failure behind an optimistic status, a placeholder, or a silent retry.
5. **Mechanical release truth is authoritative.** Dependency readiness, exact-head independent review, required CI, mergeability, credentials, canonical provenance, and reconciliation are enforced uniformly. Legacy approval metadata is retained for audit compatibility and cannot stop dispatch, review, remediation, or merge.
6. **Active scope ownership cannot disappear.** Replacing or archiving a deliverable transfers its live Autopilot scope atomically to the declared replacement, or explicitly stops it with an audited reason and visible operator notification. The same durable `scope_id` and decision history cross a transfer; a live target conflict fails closed rather than creating a second execution stream.

If a tier's automation would require breaking any of the above, that action is
**not** in that tier. It remains a mechanical hold unless it reaches one of the
four exception-only human decision classes in the escalation contract.

---

## 3. Risk tiers — T0 → T4

Tiers are cumulative: each includes the ones below it. **Default for a newly-registered coordinator is T0.** Elevation is per-project, explicit, audited, and revocable (§7). Each tier below lists its **mandate**, **may**, **may NOT**, **scopes** (§6), and **must escalate when** (§5).

### T0 — Observer (read-only) · default
- **Mandate:** See the whole fleet and tell the truth about it.
- **May:** read board/health/economics/dispatch state (`board_summary`, `get_plan_signals`, `get_lane_delta`, `get_mission_status`, `list_active_agents`, `host_status`, `get_mcp_observability`, Tally rollups); produce digests, dependency analyses, and *recommended* plans as output only. The shipped [COORD-2 audit loop](COORDINATOR-AUDIT-LOOP.md) uses a query-only local projection and may append only its own bounded plan artifact.
- **May NOT:** perform any write that changes work state — no wake, no claim, no comment-as-instruction, no merge, no task edits.
- **Scopes:** `read` only.
- **Escalate when:** it observes anything in §5 (surfacing is its whole job at T0).

### T1 — Dispatcher
- **Mandate:** Route ready, unblocked, in-budget, in-policy work to the right runtime — **to each environment's own cloud agents** (§4).
- **May:** create project-scoped wake intents (`dispatch_to_claude_code` / `dispatch.dispatch` → `request_wake(policy={mode:"vendor_cloud"})`); post advisory coordination comments/heads-ups; write its own `set_agent_state`; select model tier / lane hints per PRD §20 (model right-sizing) as *dispatch metadata*.
- **May NOT:** claim or complete a task itself; change a task's status, priority, `sort_order`, `is_blocking`, or dependencies; merge; create implementation tasks; dispatch work whose deps are unsatisfied or whose budget/policy forbids it; start work outside the project boundary.
- **Scopes:** `read`, `write:ixp` (wake/coordination), `write:comments`.
- **Escalate when:** no eligible host is online (surface `requested:false`, do not silently drop); budget policy would be exceeded by dispatching; identity/takeover risk on the target.

### T2 — Review steward
- **Mandate:** Keep In-Review work moving toward a *trustworthy* green, without deciding merge.
- **May:** trigger/rerun CI gates (`request_external_ci_mirror_run` / scratchpad dispatch), request the right human/SME reviewers, surface per-PR failing-test attribution (`ci_attribution`), nudge stale PRs, request auto-rebase; monitor In-Review via `monitor_in_review`; dispatch a `review_merge` agent for green In-Review work.
- **May NOT:** merge, mark Done, or waive a red/required check.
- **Scopes:** T1 + (no new write scope — CI/mirror runs and review requests are `write:ixp` coordination actions).
- **Escalate when:** a required gate is red after bounded retries; a PR is conflicted/stale; review is required but unavailable; a flake pattern is detected (surface it, don't retry-until-green).
- **Internal phase:** [`COORDINATOR-REVIEW-STEWARD.md`](COORDINATOR-REVIEW-STEWARD.md) / `review_steward.py`, invoked only by the lifecycle leader in production.

### T3 — Merge steward
- **Mandate:** Land PRs that already satisfy *every* safe-merge condition, in dependency order with backpressure.
- **May:** arm GitHub auto-merge or execute a merge **only** when `merge_gate` passes AND the PR is provenance-backed AND review is satisfied AND it is conflict-free AND it respects dependency order and the in-flight backpressure cap (this is exactly `merge_coordinator.plan_merges` → `coordinate(arm_fn=…)`).
- **May NOT:** merge anything with a blocked `merge_gate` finding; override `enforce_admins`; set `Done` (the webhook does); merge outside dependency/backpressure order; touch a non-canonical repo.
- **Scopes:** T2 + `write:tasks` (to record merge intent/attribution on the task). Still **no** `write:system`, **no** `admin`.
- **Escalate when:** `merge_gate` returns any blocked finding; a merge would violate backpressure/saturation; a post-merge reconcile shows drift; the queue/merge hangs.

### T4 — Autopilot
- **Mandate:** Run the full loop (dispatch → steward → merge) unattended **within an explicit envelope**.
- **May:** chain T1–T3 autonomously for tasks that stay inside a declared **budget** (token/$), **project**, **lane set**, **risk ceiling**, and **time box**, under the budget governor (PRD §20): near-cap fires the IRQ ("wrap up / hand back"), at-cap fires the NMI (halt + escalate).
- **May NOT:** exceed the envelope; bypass mechanical release gates; disable its own kill switch; run without an active operator-approved autopilot grant.
- **Scopes:** same as T3 — autopilot adds *autonomy*, never *authority*.
- **Escalate when:** budget IRQ/NMI fires; any §5 class trips; the envelope is exhausted; an operator issues stop.

---

## 4. Routing model — dispatch to each environment's cloud agents (decided)

The coordinator does **not** run work itself and does **not** funnel all execution onto one host. It **routes each ready work item to the cloud agents that belong to that item's environment/project.** Decided design:

- **Mechanism (existing rails):** `dispatch_to_claude_code(task_id, project)` → `dispatch.dispatch(task_id, actor, project)` → `store.request_wake(selector={runtime, lane, agent_id, capabilities:["vendor_cloud"]}, policy={mode:"vendor_cloud"}, task_id, idem_key)`. The wake is a **durable, idempotent intent**, not a push. The trigger-only Claude cloud host claims it, launches the exact pushed task branch through `claude --cloud`, and binds the provider session URL; the hosted agent then pulls/claims the task through MCP. See [CLAUDE-CLOUD-EXECUTION](CLAUDE-CLOUD-EXECUTION.md).
- **Per-environment isolation:** each environment's cloud agents authenticate with *that environment's* Switchboard token and register hosts against *that project*. The coordinator addresses them only by `{project, lane}` selector; it never reaches into another environment's fleet, and dispatch/`_work_hosts` are filtered by project + lane.
- **Pull, not push (no forced execution):** the coordinator expresses *intent to route*; the environment's host decides eligibility (`_host_can_handle`: project, lane, runtime, capability, risk, budget, policy `allow_work`/`allowed_lanes`). This preserves the PRD's "cooperative control" model and keeps execution as the agent's judgment (§18).
- **No eligible host = a visible signal, not a drop** (fail-fix-early). When `request_wake` returns `eligible_host_count == 0` / `requested:false`, the coordinator records the wake as queued and surfaces "no eligible host in <env>" as an escalation — it does not silently discard the work or fabricate progress.
- **Idempotent + attributable:** every dispatch carries a deterministic `idem_key` (e.g. `coord-wake-<deliverable>-<task>`), so retries never double-dispatch, and every routing decision is an audited `wake`/`external_effect` with the coordinator as `actor`.

---

## 5. Escalation classes (what must go to a human)

The coordinator maps every abnormal condition to a class that names *who is pinged*, *what it blocks*, and *the audit event*. Classes align with `fail_fix_signal.v1` failure classes plus coordinator-specific triggers:

| Class | Trigger | Blocks | Notify |
|---|---|---|---|
| **budget_breach** | task/loop near cap (IRQ) or at cap (NMI) | further spend on that item | operator (Slack/Gmail) |
| **failed_gate** | required CI/review gate red after bounded retries | merge | operator + task owner |
| **stale_branch / conflict** | PR conflicted, non-fast-forward, or `merge_gate` `branch_stale`/`conflict_markers` | merge | task owner |
| **missing_provenance** | `Done` without `merged_sha`, or `reconcile` drift | release | operator |
| **absent_permission** | coordinator lacks the scope/tier for an intended action | that action | operator |
| **unreachable_agent / no_host** | `request_wake` finds no eligible host; directed agent won't ack | dispatch of that item | operator |
| **unbound_identity** | target task shows takeover/identity risk | claim/dispatch | operator |

Escalation is **loud and structured** (a monitor event, blocker, or directed message with `requires_ack`) — never a silent skip. A coordinator that cannot resolve a class within its tier hands it up; it never green-washes to keep the loop moving.

---

## 6. Permission model (MCP / API)

- **Principal:** the coordinator is a first-class principal of kind `agent` (or a dedicated `coordinator` display role) bound to **one project** via `create_scoped_token(project, kind, scopes, role)`. It is registered and heartbeated like any agent (`register_agent`) so it is visible to operators and killable.
- **Scope taxonomy (existing):** `read`, `write:comments`, `write:ixp`, `write:tasks`, `write:projects`, `write:bug_intake`, `write:system`, `admin`. Enforcement is per-tool via `_require_write(ctx, project, ("write:X",))` behind the `MCPAuthMiddleware` bearer gate.
- **Tier → scope mapping (least privilege):**

  | Tier | Scopes granted | Never granted |
  |---|---|---|
  | T0 Observer | `read` | everything else |
  | T1 Dispatcher | `read`, `write:ixp`, `write:comments` | `write:tasks`, `write:projects`, `write:system`, `admin` |
  | T2 Review steward | = T1 | same |
  | T3 Merge steward | + `write:tasks` | `write:projects`, `write:system`, `admin` |
  | T4 Autopilot | = T3 (adds autonomy, not scope) | `write:projects`, `write:system`, `admin` |

- **Hard ceiling:** **no coordinator tier is ever granted `write:system` or `admin`.** Governance, token minting, policy/meta writes, and access management are operator-only. The coordinator is never a governance principal — it operates strictly within `read` + `write:ixp` + `write:comments` + (T3+) `write:tasks`, scoped to one project.
- **One token per (coordinator, project, tier):** raising a tier means issuing a new higher-scope token and revoking the old — there is no ambient authority and no cross-project token.

---

## 7. Default-safe policy & lifecycle

- **Born safe:** a new coordinator is **T0 read-only, no project elevation.** It can observe and recommend from day one; it can change nothing.
- **Elevation is deliberate:** raising to T1–T4 is per-project, per-tier, operator-approved, **budget/time-boxed**, audited, and revocable. There is no global "coordinator can do anything" grant.
- **Ship dry-run first:** every acting tier (T1+) ships in **observe/dry-run** (log the plan it *would* execute) before it is allowed to act — mirroring `merge_coordinator.coordinate(dry_run=True)` (default) and the SESSION-12 claim-gate `warn` mode. Turn on acting only after the logged plans look right.
- **Kill switch (NMI):** an operator can demote or stop the coordinator at any tier at any time (`request_runner_kill` for a managed runner; token revoke otherwise). Autopilot (T4) must honor stop at the next tool-call boundary.
- **Bounded loop:** the coordinator tick is rate-limited and returns `retry_after_seconds`; it never busy-loops, and its dispatch decisions are deterministic and idempotent so a restart re-derives, never double-acts.

---

## 8. Audit requirements

- **Every** coordinator action appends to the immutable activity log with `actor=<coordinator principal>`, the **tier** in effect, the **reason**, and the **decision inputs** (what was ready, why it was routed there, the budget/policy state). This is `run_coordinator_tick`'s existing `{plan, executed, escalations, monitors, dispatch}` receipt, extended with tier + inputs.
- **Explainable planner (COORD-3):** every coordinator recommendation/action also writes a structured decision record (`switchboard.coordinator_decision.v1`) with a stable `decision_id`, `inputs_snapshot`, `policy_rule`, `chosen_action`, `skipped_alternatives`, and `result`. Operators read the trail via `list_coordinator_decisions` / `GET /api/coordinator_decisions` (and the `/coordination` page) without chat transcripts. Records are append-only and idempotent under `stable_key` or an unchanged input/rule/action snapshot.
- **Material decisions** (tier elevation, an autopilot merge, a suppressed dispatch) are also `record_decision` entries (append-only, supersede-only), indexed into RAG so `ask_plan` can cite "why did the coordinator do X."
- **Economics:** dispatch/model-tier choices feed Tally (planned-vs-actual, FR-21a) and reliability scoring (FR-22), so the coordinator's routing is itself measured for cost-per-verified-outcome.
- **Provenance-safe:** the coordinator never writes Done/merge provenance directly; it records *intent + evidence pointers*, and the webhook/reconcile path remains the sole authority (§2.1).

---

## 9. Acceptance criteria mapping

| COORD-1 acceptance | Where |
|---|---|
| Coordinator role described | §1 |
| Risk tiers T0 read-only / T1 dispatcher / T2 review steward / T3 merge steward / T4 autopilot | §3 |
| MCP/API permission model | §6 |
| Escalation classes | §5 |
| Audit requirements | §8 |
| Default safe policy | §7 |
| No action bypasses Done provenance | §2.1, §8 |
| No action bypasses safe-merge protocol | §2.2, §3 (T3) |
| No action bypasses project boundaries | §2.3, §4, §6 |
| No action bypasses fail-fix-early policy | §2.4, §5 |
| Routing decision (each env's cloud agents) | §4 |

---

## 10. Non-goals (scope guard)

- Not an execution engine — the coordinator never dictates a task's steps (PRD §18); that stays the coding agent's judgment.
- Not a governance principal — never `write:system`/`admin`, never mints tokens or edits access/policy.
- Not a replacement for `run_mission_coordinator` — this contract is the **policy envelope** that loop (and any future coordinator) runs inside; the primitives (`claim_next`, `request_wake`, `merge_gate`, `reconcile`) are unchanged.
- Not a release-truth bypass — exact-head review, CI, mergeability, credentials, and provenance remain mandatory.
