# Interrupt Enforcement Tiers Spec - visible control fidelity

- **Status:** Draft v0.1
- **Date:** 2026-06-28
- **Product:** Switchboard
- **Protocol target:** `IXP-core` signals plus runtime/deployment enforcement
- **Purpose:** give operators truthful, visible guarantees about how each agent session can
  be stopped, redirected, or killed.

> The product must be honest: nothing reaches an LLM mid-token. But "no mid-token
> interrupt" does not mean "no control." Switchboard should advertise the exact enforcement
> tier for every live session: advisory poll, hook-level deny, runner kill, or managed
> control.

---

## 1. Why this spec exists

Cross-runtime control is uneven. Claude Code, Codex, Cursor, LangGraph, and custom loops do
not expose the same hooks. A buyer will forgive that if the product tells the truth and gives
an escalation path. They will not forgive a vague "stop agent" button that sometimes means
"sent a message the agent might read later."

This spec turns interrupt capability into a first-class session property:

- what signal was sent;
- where it can land;
- expected latency;
- whether pending actions can be denied;
- whether the runner can kill the process;
- when escalation should happen.

---

## 2. Non-negotiable boundary

Switchboard must never claim mid-token delivery.

The strongest in-band guarantee is boundary-latency delivery:

- before a tool call;
- after a tool call;
- before a graph node;
- before a model-loop tool dispatch;
- at a turn boundary;
- at an adapter poll interval.

The only guaranteed hard stop is out of band: a runner or supervisor terminates the process.
That is useful, but it is not an `IXP-core` wire guarantee.

---

## 3. Tier model

### Tier 0 - `observe_only`

The session registers presence, but Switchboard has no reliable interrupt path.

| Property | Value |
|---|---|
| Receives messages | Maybe, manually |
| Can deny tool/action | No |
| Can kill runner | No |
| Expected stop latency | Unknown |
| UI wording | "Visible only" |

Use for early/manual sessions. Do not use for budget enforcement or critical work.

### Tier 1 - `advisory_poll`

The adapter polls inbox/delta at startup, on a timer, or at known coarse boundaries. It can
surface `stop`/`redirect`, but cannot guarantee the next action is blocked.

| Property | Value |
|---|---|
| Receives messages | Yes, by polling |
| Can deny tool/action | No |
| Can kill runner | No |
| Expected stop latency | `poll_interval_s` or next manual boundary |
| UI wording | "Advisory stop" |

This is the minimum acceptable cross-runtime control tier.

### Tier 2 - `hook_deny`

The adapter checks pending signals at a pre-action boundary and can deny the pending tool,
command, node, or tool dispatch.

| Property | Value |
|---|---|
| Receives messages | Yes |
| Can deny tool/action | Yes |
| Can kill runner | No |
| Expected stop latency | next hook boundary |
| UI wording | "Boundary stop" |

This is the IRQ tier. It is the primary product guarantee for cooperative agent control.

### Tier 3 - `runner_kill`

Switchboard or a Switchboard-managed runner can terminate the agent process/session out of
band.

| Property | Value |
|---|---|
| Receives messages | Optional |
| Can deny tool/action | Depends on adapter |
| Can kill runner | Yes |
| Expected stop latency | supervisor kill latency |
| UI wording | "Hard kill available" |

This is the NMI tier. It is guaranteed to halt execution, but may lose local context or skip
normal cleanup unless the runner records a crash/kill event.

Runner kill is only available for **managed sessions**: processes, cloud jobs, or SDK sessions
that Switchboard launched or that explicitly registered a `runner_session_id`. A session that
only connects over MCP from an operator's desktop can advertise `hook_deny` or `advisory_poll`,
but not `runner_kill`, because Switchboard has no process handle to terminate.

Safety limits:

- kill is project-scoped; an actor in one project cannot kill a session in another project;
- self-kill is allowed for cleanup, but killing another agent requires `operator`,
  `system/watchdog`, or an authenticated principal with runner-control scope;
- the runner first sends the soft stop path when available (`stop` signal or native cancel),
  then waits `grace_s`, then terminates;
- `SIGKILL`/hard terminate is only used after grace expiry or when policy says immediate hard
  stop (`reason=budget_exhausted`, `reason=secret_exposure`, `reason=operator_emergency`);
- before kill, the runner must snapshot any available state: task id, claim id, leases, cwd,
  branch/head SHA, last stdout/stderr tail, and adapter-reported `agent_state`;
- after kill, task claims are not silently completed; they remain `In Progress` or are moved by
  an explicit abandon/requeue policy recorded in the same audit trail.

### Tier 4 - `managed`

The runtime is launched under Switchboard control and supports both boundary deny and runner
kill, with state save/resume when available.

| Property | Value |
|---|---|
| Receives messages | Yes |
| Can deny tool/action | Yes |
| Can kill runner | Yes |
| Expected stop latency | next hook boundary or kill timeout |
| UI wording | "Managed control" |

This is the target for hosted/cloud workers and high-trust enterprise deployments.

---

## 4. Capability advertisement

`register_agent` accepts and `list_active_agents` returns:

```json
{
  "control": {
    "mode": "hook_deny",
    "poll": true,
    "poll_interval_s": 10,
    "hook_deny": true,
    "runner_kill": false,
    "state_save": "adapter",
    "resume": "manual",
    "max_signal_latency": "next_tool_call",
    "last_verified_at": "2026-06-28T00:00:00Z",
    "verified_by": "interrupt-smoke:claude-code-pretool"
  }
}
```

Fields:

| Field | Meaning |
|---|---|
| `mode` | Highest truthful tier: `observe_only`, `advisory_poll`, `hook_deny`, `runner_kill`, `managed` |
| `poll` | Adapter polls inbox/delta without model memory |
| `poll_interval_s` | Maximum timer interval when timer polling exists |
| `hook_deny` | Adapter can deny a pending action at a boundary |
| `runner_kill` | Supervisor can terminate the process/session |
| `state_save` | `none`, `prompted`, `adapter`, or `native` |
| `resume` | `none`, `manual`, or `automatic` |
| `max_signal_latency` | Human-readable bound: e.g. `next_tool_call`, `10s_poll`, `unknown` |
| `verified_by` | Smoke test or adapter assertion that produced the claim |

If a field is unknown, it must be omitted or set to `unknown`; never infer higher fidelity.

---

## 5. Signal semantics by tier

`IXP-core` signals stay the same:

| Signal | Intent |
|---|---|
| `heads_up` | Inform the session; no halt required |
| `redirect` | Change the next line of work |
| `stop` | Halt the current line of work at the next boundary |

Tier-specific behavior:

| Tier | `heads_up` | `redirect` | `stop` |
|---|---|---|---|
| `observe_only` | record only | record only | record only |
| `advisory_poll` | surface | surface/instruct | surface/instruct |
| `hook_deny` | surface | deny pending action and inject/surface new instruction | deny pending action |
| `runner_kill` | surface if polling exists | poll path first, kill if deadline missed | kill if deadline missed or immediate hard stop |
| `managed` | surface | save state, deny/redirect, resume | deny; kill on escalation |

`ack` must reflect what actually happened:

```json
{
  "message_id": 4821,
  "handled": true,
  "handling": "denied_pre_tool",
  "detail": "Blocked shell command before execution",
  "lease_ids_released": ["lease_123"]
}
```

Allowed `handling` values:

- `observed`
- `surfaced`
- `denied_pre_tool`
- `redirected`
- `saved_state`
- `killed`
- `missed_deadline`
- `unsupported`

---

## 6. Stop escalation flow

Default operator flow:

1. Operator sends `stop` or budget governor emits `stop`.
2. Message is written with `requires_ack=true`, high priority, and `ack_deadline`.
3. Switchboard watches for ack.
4. If the session is `hook_deny`, wait until deadline or boundary ack.
5. If no ack and `runner_kill=true`, escalate to kill.
6. If no kill path, mark the session "uncontrolled" and alert the operator.

Example policy:

```json
{
  "signal": "stop",
  "priority": 100,
  "ack_deadline_s": 30,
  "escalate": {
    "after_deadline": "runner_kill",
    "notify": ["operator", "task_owner"]
  }
}
```

Hard kill must append an activity event even when the agent cannot ack:

```json
{
  "kind": "runner.killed",
  "agent_id": "codex/CHART-8#b12e",
  "task_id": "CHART-8",
  "reason": "stop_ack_deadline_missed",
  "actor": "operator/steve"
}
```

---

## 7. Budget and policy interrupts

Interrupts are also how Tally and dispatch enforce economic policy.

Recommended budget behavior:

| Budget state | Signal/action |
|---|---|
| 70 percent spent | `heads_up`: summarize progress and remaining risk |
| 90 percent spent | `redirect`: wrap up and produce decision/evidence |
| 100 percent spent | `stop` with required ack |
| 100 percent plus missed ack | `runner_kill` when available |

The budget governor must use the advertised tier. A Tier 1 session can be warned but not
reliably halted. A Tier 3/4 session can be killed when policy requires it.

---

## 8. Runner API

Runner kill is not `IXP-core`; it belongs to a deployment/runner binding.

Minimum managed runner operations:

- `POST /runner/v1/sessions` - start a supervised session.
- `GET /runner/v1/sessions/{runner_session_id}` - inspect process/job state.
- `POST /runner/v1/sessions/{runner_session_id}/kill` - terminate.
- `POST /runner/v1/sessions/{runner_session_id}/signal` - optional native runtime signal.
- `GET /runner/v1/events?since_cursor=` - runner lifecycle feed.

The runner must authenticate writes and record all actions in the Switchboard activity log.

### 8.1 Session object

```json
{
  "project": "switchboard",
  "agent_id": "codex/ADAPTER-2#b12e",
  "runner_session_id": "run_01J...",
  "runtime": "codex",
  "pid": 41231,
  "task_id": "ADAPTER-2",
  "claim_id": "taskclaim-...",
  "cwd": "/work/projectplanner",
  "started_at": "2026-06-28T00:00:00Z",
  "state": "running",
  "control": {
    "mode": "managed",
    "hook_deny": true,
    "runner_kill": true,
    "state_save": "adapter",
    "resume": "manual"
  }
}
```

`runner_session_id` is the stable kill target. `agent_id` is still the human-readable bus
address and may be reused across sessions; the runner must not kill a newer session because an
old `agent_id` matched.

### 8.2 Kill request policy

Kill request:

```json
{
  "project": "helm",
  "agent_id": "claude-code/ENGINE-11#a7c4",
  "runner_session_id": "run_01J...",
  "reason": "operator_stop",
  "grace_s": 5,
  "requeue": false,
  "release_leases": false
}
```

Required fields:

| Field | Rule |
|---|---|
| `project` | Required isolation boundary |
| `runner_session_id` | Preferred kill target; required when more than one session has used the same `agent_id` |
| `agent_id` | Bus identity; used for audit and fallback lookup |
| `reason` | One of `operator_stop`, `stop_ack_deadline_missed`, `budget_exhausted`, `secret_exposure`, `session_stuck`, `self_cleanup` |
| `grace_s` | 0-60 seconds; default 5 |
| `requeue` | If true, abandon the active task claim after kill |
| `release_leases` | If true, release leases held by this agent after kill |

Kill response:

```json
{
  "agent_id": "claude-code/ENGINE-11#a7c4",
  "runner_session_id": "run_01J...",
  "status": "killed",
  "signal": "SIGTERM",
  "escalated_signal": "SIGKILL",
  "killed_at": "2026-06-28T00:00:00Z",
  "snapshot_id": "snapshot_01J...",
  "claim_action": "left_in_progress",
  "leases_action": "left_held_until_ttl"
}
```

### 8.3 Mandatory audit events

Every runner operation writes activity rows, even when the agent process is already wedged:

| Event | When |
|---|---|
| `runner.session_started` | Session launch/register under runner control |
| `runner.stop_requested` | Operator/watchdog requests soft stop or hard kill |
| `runner.state_snapshot` | Runner captures cwd, branch, claim, leases, stdout/stderr tail |
| `runner.killed` | Process/session terminated |
| `runner.kill_failed` | Runner could not terminate or could not identify the target |
| `runner.claim_abandoned` | Optional requeue policy abandoned the active claim |
| `runner.leases_released` | Optional cleanup released leases |

Kill audit payload:

```json
{
  "kind": "runner.killed",
  "project": "switchboard",
  "agent_id": "codex/ADAPTER-2#b12e",
  "runner_session_id": "run_01J...",
  "task_id": "ADAPTER-2",
  "claim_id": "taskclaim-...",
  "reason": "stop_ack_deadline_missed",
  "actor": "switchboard/monitor",
  "grace_s": 5,
  "signal": "SIGTERM",
  "escalated_signal": "SIGKILL",
  "snapshot_id": "snapshot_01J..."
}
```

### 8.4 Requeue and cleanup policy

Default is conservative: killing a process **does not** imply the work is safe to requeue and
does not imply file leases are safe to release. The task remains visible as interrupted until
an operator or verifier chooses one:

| Policy | Effect |
|---|---|
| `leave_in_progress` | Default; preserves forensic state |
| `abandon_claim` | Releases task claim and returns task to scheduler |
| `release_leases` | Releases resource leases after snapshot |
| `archive_session` | Keeps task state but hides dead runner from active sessions |

Automatic `abandon_claim` is allowed only when the runner proves no unpushed/local-only work
exists or when the killed session was launched in a disposable worktree.

---

## 9. UI requirements

Every active session row should show:

- runtime/model;
- task/lane;
- heartbeat age;
- control tier;
- poll interval or boundary type;
- pending signals;
- last ack;
- kill availability;
- leases held.

Button labels should be tier-aware:

| Tier | Primary control |
|---|---|
| `observe_only` | "Send message" |
| `advisory_poll` | "Request stop" |
| `hook_deny` | "Stop at boundary" |
| `runner_kill` | "Kill session" plus advisory stop |
| `managed` | "Stop" with escalation path |

The UI must not display one generic "Stop" affordance without explaining the tier behind it.

---

## 10. Conformance tests

Interrupt tier conformance requires:

1. Session advertises a tier at registration.
2. A `heads_up` signal is visible and ackable.
3. A `stop` signal at Tier 1 is surfaced but not reported as denied.
4. A `stop` signal at Tier 2 denies a fake protected action.
5. A Tier 3 runner kill records `runner.killed`.
6. A missed ack deadline escalates according to policy.
7. UI/API never reports mid-token delivery.
8. Downgrading an adapter capability downgrades the visible tier immediately.

---

## 11. Exit criteria

Interrupt enforcement is product-ready when:

- every registered session has a displayed control tier;
- at least one adapter proves Tier 2 `hook_deny`;
- at least one managed runner proves Tier 3 `runner_kill`;
- stop/redirect status is visible to both sender and operator;
- budget governor can use the same signal path;
- docs and UI never imply mid-token interruption.
