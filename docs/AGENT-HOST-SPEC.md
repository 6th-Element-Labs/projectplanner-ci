# Agent Host and Wake Supervisor Spec

- **Status:** Draft v0.1
- **Date:** 2026-06-29
- **Product:** Switchboard
- **Layer:** Runner / agent-host control plane
- **Depends on:** `IXP-core`, `TXP claim_next`, runtime adapters, interrupt tiers

## 1. Purpose

Switchboard's bus is durable, but it is pull-based. A message in an inbox does not wake Claude
Code, Codex, Cursor, or a raw API loop by itself. It only becomes action when a runtime is alive,
polls, and acts.

The same is true for runtime memory. A model session may compact its context, hit a platform
limit, restart, or move to a different host. Switchboard cannot make vendor-managed context
windows durable. It can make the work durable outside them: claims, inbox messages, monitors,
wake intents, project contracts, provenance, and outcomes are stored in the substrate so a new
runtime process can rejoin the work without trusting the old chat transcript.

This spec defines the missing layer between the always-on substrate and the runtime adapters:
an **Agent Host** that can keep suitable agent sessions alive, start one when work or a message
needs attention, and truthfully report when no eligible runtime is reachable.

The runtime-specific distinction between a fresh process, exact vendor conversation resume,
checkpoint resume, and reconstructed history is defined in
[`RUNTIME-WAKE-CAPABILITY-MATRIX.md`](RUNTIME-WAKE-CAPABILITY-MATRIX.md). Agent Hosts must report
which continuity mode actually occurred; a successful process spawn is not proof of conversation
resume.

The product guarantee is not "the bus pushes into an absent model." The guarantee is:

> Switchboard can route work or a handoff to a registered agent host; the host either starts or
> resumes a capable runtime session, or records that no eligible host is available.

## 2. Non-Goals

- Not a new model host.
- Not a replacement for Claude Code, Codex, Cursor, LangGraph, or custom API loops.
- Not in-token interruption. Delivery still happens at startup, poll, hook, node, or runner
  boundary.
- Not part of `IXP-core`. The wire bus stays small; wake/launch belongs to runner deployment.
- Not a silent human bypass. Merge/review authority remains explicit in the working agreement.

## 3. Terms

| Term | Meaning |
|---|---|
| Substrate | The always-on planner/Switchboard service: board, MCP, REST, leases, messages, monitors |
| Agent Host | A machine or service with repo checkout, credentials, runtime binaries, and a host daemon |
| Host Daemon | The always-on process on an Agent Host that polls Switchboard and launches runtimes |
| Runtime Session | One Claude/Codex/Cursor/LangGraph/raw-loop process under supervisor control |
| Supervisor | The local runner that owns a runtime process group and can status/kill/snapshot it |
| Wake Intent | Durable request from Switchboard to ensure an eligible runtime session exists |
| Eligibility | Match on project, repo, runtime, lane, capabilities, budget, risk, and policy |

## 4. Deployment Shape

```text
Substrate (Plan VM)
  board + MCP + REST + monitor sweep
       |
       | poll/register/wake/status
       v
Agent Host Daemon(s)
  repo checkout + secrets + runtime launchers
       |
       | create managed Work Session -> start/status/kill
       v
Supervisor
       |
       | spawn
       v
Runtime Session
  adapter handshake -> inbox -> claim_next -> work -> complete_claim -> repeat
```

The Plan VM remains lightweight. It stores durable coordination state and wake intents; it does
not run arbitrary coding agents. Agent Hosts run where repo access, model credentials, build
toolchains, and process control are available.

Open-core boundary: a local Agent Host should be inspectable and open so developers trust how
Switchboard wakes and supervises their own machines. Hosted/managed runner fleets, enterprise
policy, entitlement checks, pooled capacity, and long-term runner audit remain part of the
commercial Switchboard control plane.

## 5. Host Registration

An Agent Host must register separately from individual agent sessions.

```json
{
  "project": "switchboard",
  "host_id": "host/steve-mbp",
  "hostname": "Steves-MacBook-Pro.local",
  "agent_host_version": "0.1.0",
  "repo_root": "/Users/steveridder/Library/CloudStorage/Dropbox/Git/projectplanner",
  "runtimes": [
    {
      "runtime": "claude-code",
      "launcher": "claude",
      "profiles": ["ixp.v1", "txp.dispatch.v0"],
      "control": {"mode": "hook_deny", "runner_kill": true},
      "policy": {
        "mode": "lane_scoped",
        "allow_message_only": true,
        "allow_work": true,
        "allow_global_claim": false,
        "allowed_lanes": ["ADAPTER", "DISPATCH", "RECON"]
      },
      "lanes": ["ADAPTER", "DISPATCH", "RECON"],
      "capabilities": ["docs", "python", "github", "tests"]
    },
    {
      "runtime": "codex",
      "launcher": "codex",
      "profiles": ["ixp.v1", "txp.dispatch.v0"],
      "control": {"mode": "managed", "runner_kill": true},
      "lanes": ["PROTO", "RECON"],
      "capabilities": ["docs", "python", "tests"]
    }
  ],
  "limits": {
    "max_sessions": 2,
    "max_sessions_per_runtime": {"claude-code": 1, "codex": 1},
    "max_cost_usd_per_hour": 5.0
  },
  "heartbeat_ttl_s": 60
}
```

Host policy is part of the contract, not an operator assumption:

- Default host mode is `message_only`. It may register, drain inbox, and satisfy lane-less
  handoff wakes, but it must not call `claim_next`.
- Work-capable hosts must opt in with `allow_work=true` and explicit `allowed_lanes`.
- A lane-scoped wake may call `claim_next(lane=...)` only when the requested lane is advertised
  by the host inventory.
- A lane-less/global `claim_next` wake is refused unless `allow_global_claim=true`. The default
  is false because global dispatch is how one agent accidentally takes unrelated work.
- Runtime command templates live on the host. Wake payloads select a runtime/profile/lane; they
  do not carry arbitrary shell commands.

Required operations:

- `register_host(host)` creates or refreshes host inventory.
- `heartbeat_host(host_id)` renews liveness and current capacity.
- `list_agent_hosts(project, runtime?, lane?, capability?)` returns non-stale hosts.
- `host_status(host_id)` returns capacity, active sessions, recent failures, and last heartbeat.

A stale host must not be selected for a wake intent. Stale host state is evidence, not failure:
it tells the operator no always-on worker is currently listening.

### Effective runtime profile (SIMPLIFY-5)

Every Agent Host registration and heartbeat carries
`capacity.runtime_profile` (`switchboard.agent_host_runtime_profile.v1`). The profile includes
only non-secret finishing facts: the effective work module for each advertised runtime,
`PM_AUTO_WORK_SESSION`, `agent_host_version`, presence of `git`, `gh`, and the runtime CLI, and
the host-proven `runner_watch` relay fact. Its `hash` is SHA-256 over the version and canonical
components. `list_agent_hosts` also exposes the same object at top-level `runtime_profile` so
operators do not have to infer effective configuration from a launch script or SSH autopsy.

Hybrid placement carries a `switchboard.runtime_profile_requirement.v1` subset. A missing,
tampered, or drifted profile excludes the host and records the exact component, for example
`profile drift: PM_AGENT_WORK_MODULE_CODEX=claude_personal_worker:run (expected
adapters.codex_local_worker:run)`. `PM_EXPECTED_AGENT_HOST_VERSION` and
`PM_EXPECTED_AGENT_HOST_PROFILE_HASH` are optional coordinator rollout fences for an exact build
or complete profile. This is admission and observation only; Switchboard does not rewrite host
configuration.

`runner_watch` remains host-proven. A placement requirement may demand it, but cannot add it to
an advertisement. A host whose PTY/relay imports fail advertises `false` and is refused when
Watch is required.

### Personal host enrollment (ADAPTER-18)

User-owned macOS and Linux hosts enroll through the signed, versioned lifecycle in
[`AGENT-HOST-ENROLLMENT.md`](AGENT-HOST-ENROLLMENT.md). An operator issues a short-lived,
single-use bootstrap; completion returns one stable narrow host bearer through durable
finalization and binds the
`host_id` to its exact principal and public-key fingerprint. The host's provider login remains
local. Revoked or uninstalled identities cannot register or heartbeat even if their old host id
is replayed.

The installer durably creates a one-install recovery secret before bootstrap consumption.
Retries with the same bootstrap, recovery secret, platform, and key fingerprint return the
same bearer when the completion response was ambiguous. The server stores only the recovery
hash and retains it until the client acknowledges that local finalization is durable.

The installer provisions a per-user launchd or systemd service, atomically updates signed
releases, preserves visible journaled revoke/uninstall intent while offline or response state
is ambiguous, and purges identity and provider-runtime residue only after durable Switchboard
revocation proof. A revoked bearer is accepted only for exact-host revocation readback, while
every local cleanup boundary can resume without authentication. It never starts a cloud/API
lane as a fallback for a sleeping, offline, or revoked personal host.

Switchboard persists the enrolled host's server-issued execution profile. Registration and
heartbeat must match its single Codex runtime, allowed lanes and capabilities, local-auth
proof, work/global-claim modes, and concurrency cap exactly. The host bearer cannot widen this
profile, and an enrolled personal host cannot claim a generic wake. Personal wake creation
binds the authenticated caller and claim principal to the enrollment owner and derives tenant
identity from durable project access, never from optional request fields.

Credential JSON temporaries are created `0600` before write under a `0700` directory; file and
directory durability are fsynced and stale regular temporaries are removed without following
symlinks. Linux materializes personal Work Sessions beneath the dedicated `0700` Agent Host
workspace root included in systemd `ReadWritePaths`. It treats the coordinator path as
server-local metadata, clones the canonical repository into the host root, checks out the
exact bound SHA, and rejects origin mismatch, symlink escape, dirty reuse, or off-SHA reuse.

### Hybrid placement inventory

Work-capable hosts also publish a `switchboard.agent_host_placement.v1` object in `capacity`.
It truthfully describes whether the host is persistent or ephemeral; its cost class; current
CPU, memory, disk, and session headroom; installed runtime binaries; project/tenant/provider/
account-affinity allowlists; repository and Work Session policies; isolation modes;
credential-lease support; and wake/drain state. A hybrid wake never infers an omitted field:
required project, tenant, provider account, repository, isolation, credential-lease support,
runtime, capability, or resource headroom must be explicitly compatible. The lease itself is
acquired only after host selection and is validated against the exact host and runner when the
wake is claimed.

Enrolled personal hosts additionally publish redacted identity generation, owner
user/tenant/project/provider allowlists, local-auth availability, drain state, and session
headroom. Host bearer, raw account proof, and provider credential material are forbidden from
registration and heartbeat payloads.

Host capacity and provider subscription capacity are different admission gates. A free CPU
slot cannot override a personal-plan cooldown, and a ready provider subscription does not make
a saturated host available. Switchboard persists both signals in the placement receipt without
copying credential or provider-account identifiers into activity records.

## 6. Wake Intent

A wake intent is a durable request to ensure a runtime session exists. It is not a message to
the model. It is a request to an Agent Host.

```json
{
  "project": "switchboard",
  "wake_id": "wake_01J...",
  "reason": "ack_timeout",
  "source": {
    "monitor_id": "mon-...",
    "message_id": 44,
    "task_id": "PROTO-2"
  },
  "selector": {
    "runtime": "claude-code",
    "agent_id": "claude-code",
    "lane": "PROTO",
    "capabilities": ["docs", "git"],
    "min_control_mode": "advisory_poll"
  },
  "policy": {
    "dedupe_key": "ack:44:claude-code",
    "start_if_absent": true,
    "reuse_existing": true,
    "max_attempts": 3,
    "deadline_s": 120
  },
  "status": "pending"
}
```

Required operations:

- `request_wake(selector, reason, source, policy)` creates the intent.
- `claim_wake(host_id, wake_id)` atomically assigns it to one eligible host.
- `complete_wake(wake_id, runner_session_id?, agent_id?, result)` records success/failure.
- `list_wake_intents(status?, host_id?, runtime?)` exposes pending and failed wakes.
- `cancel_wake(wake_id, reason)` stops a stale or superseded wake.

Wake intents must be idempotent by `dedupe_key`. Re-sending the same missed ack or ready-lane
trigger must not launch duplicate sessions.

For a personal wake, every terminal route first compare-and-sets the exact execution connection
tuple to `completed`, `failed`, or `cancelled`, requiring exactly one matching row, and only then
terminalizes the wake in the same transaction. This applies to completion, cancellation,
claim-time and sweeper deadlines, and immediate no-eligible-host failure. A lost successful
completion response can be retried with the same principal, runner, agent, status, and result
to read back the existing receipt; any conflicting retry is denied.

## 7. Host Daemon Loop

An Agent Host daemon runs continuously:

```text
register_host
loop every N seconds:
  heartbeat_host(capacity, active_sessions)
  pull wake intents matching host inventory
  claim one wake if capacity allows
  start/reuse supervisor session
  wait for adapter registration or startup failure
  complete_wake(result)
  reap exited sessions and record runner events
```

Before launching a coding runtime for a task, the host should prefer the managed Work Session
path:

```text
create_managed_work_session(
  project,
  task_id,
  agent_id,
  source_path=host.repo_root,
  workspace_root=host.workspace_root,
  storage_mode=project_policy.default_workspace_mode || worktree,
  policy_profile=code_strict
)
```

The substrate allocates the task-scoped branch, workspace path, base ref, env/port namespace,
one-time session token, and worktree lease; the host performs the returned launch inside that
workspace. If the managed creation returns an error, the host must not fall back silently to the
shared checkout. It should surface the failure as `failed_gate`, `wrong_repo`, `stale_branch`, or
the supplied failure class.

A personal-host wake that opts into exact binding is refused before claim unless task, claim,
Work Session, runner, host, wake, source SHA, and execution-connection fields are present. The
account binding, execution binding, wake, selector, and local inventory must agree on every
repeated identifier, and the source SHA must be a 40-character lowercase Git SHA. Fresh
host-local enrollment selects the native Codex local worker; the credential-vault personal
worker is a separate centrally bound lane. Switchboard derives the stored execution binding
from the active claim and Work Session, then validates the same live rows during wake claim;
the host-local worker adopts the pre-bound session instead of creating a second one.

The host daemon may also proactively keep warm sessions:

- `min_warm_sessions` per runtime/lane;
- schedule windows, e.g. office hours;
- budget/risk caps from Tally;
- project-specific policy, e.g. "Claude Code required for adapter-hook work."

Warm sessions still use normal adapter handshake and `claim_next`. They do not get special
permission to skip leases, provenance, cost reporting, or Done rules.

## 8. Trigger Sources

Switchboard may create a wake intent from:

| Trigger | Example | Wake selector |
|---|---|---|
| Ack timeout | Claude did not ack a direct handoff | target runtime/agent if known |
| Ready work | `claim_next` has unblocked P0 work and no live capable agent | lane + capabilities |
| Stale claim | `In Progress` task has no heartbeat and no pushed evidence | original runtime if useful, otherwise lane |
| Operator request | "start one Claude Code agent for RECON" | explicit runtime/lane |
| Budget incident | Tally says stop or downshift | runtime/session with runner kill |
| Scheduled coverage | keep one host warm for business hours | lane/capability |

P0 should implement ack-timeout and operator-request wakes first. Ready-work wakes can follow
once dispatch policy is stable enough not to surprise users.

## 9. Session Start Contract

When a host launches a runtime, the child process must receive:

| Env / arg | Meaning |
|---|---|
| `PM_BASE` | Switchboard base URL |
| `PM_PROJECT` | project id |
| `PM_MCP_TOKEN` | scoped credential |
| `PM_AGENT_ID` | stable address, often runtime/lane/session |
| `PM_RUNNER_SESSION_ID` | stable kill/status target |
| `PM_LANE` | preferred lane filter |
| `PM_CAPABILITIES` | advertised capabilities |

The runtime adapter then performs the normal sequence:

```text
get_working_agreement
register_agent(protocol, control, runner_session_id)
inbox(unacked)
claim_next(lane/capabilities) or handle wake-specific message
heartbeat + poll at supported boundaries
```

The host daemon must not mark a wake successful merely because a process spawned. Success means
the runtime registered presence or the supervised entrypoint reached a known ready state.

## 10. Delivery Guarantees

| State | Product wording |
|---|---|
| Active registered session | "Mailbox stored for an active session; delivery occurs at its next inbox boundary. Await ack for handling proof." |
| Supervised wakeable runtime | "Mailbox stored; a live eligible Agent Host can start or reuse this runtime if a wake is requested." |
| Dormant registered host | "Mailbox stored; the matching Agent Host registration is stale, so a wake can queue but cannot start now." |
| No host registered | "Message stored; no eligible agent host is online." |
| Host registered, wake pending | "Wake requested; waiting for host claim." |
| Host claimed wake | "Host is starting or reusing a runtime." |
| Runtime registered | "Agent online; delivery will occur at startup/poll/hook boundary." |
| Ack received | "Recipient acknowledged." |
| Wake failed | "No runtime could be started; operator action required." |

Switchboard must never claim a message was delivered because it was inserted into the inbox.
Inbox insertion is durable storage. Delivery requires runtime registration plus ack/handling
evidence.

Every send/status response exposes `switchboard.message_delivery_receipt.v1` with four separate
facts: `mailbox.stored`, `session_status`, `wakeability`, and
`runtime_delivery_proven`. `delivery_mode` distinguishes `active_session`,
`supervised_wake_available`, `wake_queue_available`, `dormant_registered_host`,
`wake_queued`, `wake_claimed`, and `mailbox_only`. A task comment is a visible fallback only; it
does not change `runtime_delivery_proven`.

For Claude Code, wake/restart requires an Agent Host registration whose runtime inventory contains
`{"runtime":"claude-code"}`. The host heartbeat must be current, the host must have available
session capacity, and its daemon must claim the wake and launch/reuse Claude Code through the local
supervisor. A Claude Code process that is visible in a vendor UI but has not registered its agent
session is still unreachable to Switchboard. A stale host registration is dormant inventory, not
wake capacity; a pending wake remains queued until an eligible host heartbeats again.

## 11. Escalation Policy

Ack-timeout escalation:

```text
send(requires_ack, deadline)
  -> monitor fires
  -> create wake intent for target runtime/agent
  -> host starts/reuses runtime
  -> runtime drains inbox
  -> ack resolves operator uncertainty
  -> if wake fails, notify sender/operator with reason
```

Escalation should be policy-controlled:

- `on_ack_timeout=notify_sender` remains the default minimal behavior.
- `on_ack_timeout=wake_target` creates a wake intent when an eligible host exists.
- `on_ack_timeout=wake_or_operator_alert` wakes first, then notifies an operator if no host
  claims within the wake deadline.

No policy should auto-kill or abandon another agent's claim without runner evidence and the
interrupt-tier rules.

## 12. Security and Safety

- Enrolled personal-host credentials use `read` + `write:agent_host`, not broad IXP or admin.
  Runner mutation is identity-fenced, and post-execution Work Session/claim writes require the
  complete durable personal-execution tuple and the action-specific terminal state.
- A host may only launch runtimes declared in its registration.
- A host must not expose arbitrary shell launch over the network.
- Runtime command templates live on the host, not in untrusted wake payloads.
- Wake payloads select a named runtime/profile; they do not provide raw commands.
- Every launch, claim, failure, kill, and session exit writes activity/audit.
- Host-side secrets must never be copied into Switchboard activity payloads.

## 13. Observability

Minimum activity kinds:

| Kind | Meaning |
|---|---|
| `agent_host.registered` | host inventory refreshed |
| `agent_host.heartbeat` | host liveness/capacity renewed |
| `wake.requested` | durable wake created |
| `wake.claimed` | host accepted responsibility |
| `wake.completed` | runtime registered or wake otherwise succeeded |
| `wake.failed` | host could not launch/reuse runtime |
| `runner.session_started` | supervisor started runtime process |
| `runner.session_exited` | process exited normally or failed |

Dashboard must show:

- live hosts and stale hosts;
- active runtime sessions;
- pending/fired wake intents;
- last failure by runtime;
- capacity and policy caps;
- messages that are stored but not deliverable because no host is online.

## 14. P0 Implementation Slice

P0 for this layer is intentionally small:

1. Add `agent_hosts` and `wake_intents` tables.
2. Add REST/MCP tools for `register_host`, `heartbeat_host`, `request_wake`, `claim_wake`,
   `complete_wake`, and `list_wake_intents`.
3. Extend monitor sweep with optional `on_ack_timeout=wake_target`.
4. Add a host daemon CLI that polls wake intents and starts `adapters/run_agent.py` through
   `adapters/codex/supervisor.py`.
5. Prove one live path: send a message to absent Claude, monitor fires, wake intent appears,
   host daemon starts Claude-compatible runtime, runtime registers and drains inbox or records a
   clear launch failure.

Implementation note: the substrate portion is implemented when those tables/tools exist, monitor
sweep can create wake intents through `on_ack_timeout=wake_target`, and the runtime smoke test
proves wake request/claim/complete plus monitor-created wakes. The live product proof still
requires deploying an Agent Host daemon and observing a real runtime start or explicit no-host
result.

Daemon safety note: a wake intent with no `selector.lane` is message-only. The host daemon may
register the runtime adapter and read its inbox, but it must not call `claim_next` without an
explicit lane. Scheduler wakes that should claim work must include a lane/capability selector.

P0 service profile: `deploy/projectplanner-agent-host.service` is the safe default always-on
host for Switchboard dogfood. It runs on the Plan VM as `host/plan-vm-message-wake` with
`PM_HOST_LANES=__MESSAGE_ONLY__`, `PM_HOST_MAX_SESSIONS=1`, and starts child sessions with
`run_agent.py --inbox-only`. This profile is deliberately not a production work executor: it
may satisfy ack-timeout handoff wakes, but it must not accept lane-scoped scheduler wakes or
claim tasks. A future work-capable host must advertise real lanes and a real work module.

Exit criteria:

- no human has to notice an ack timeout in order for a wake attempt to be made;
- when no host is online, the system says that explicitly;
- when a host is online, duplicate wake intents do not spawn duplicate agents;
- the board distinguishes `stored`, `wake_requested`, `runtime_online`, `acked`, and
  `wake_failed`.

## 15. Open Questions

- Should ready-work wake be automatic in P0, or only operator-triggered until budget controls
  harden?
- Which runtimes can be safely launched unattended on Steve's local machine versus a dedicated
  cloud agent host?
- Should Claude Code be launched through a native routine/API, a CLI session, or a wrapper that
  opens a user-owned desktop app?
- What is the right default warm-pool size for knowledge-work dogfood: zero, one per lane, or one
  per runtime?
- How should Tally cap wake storms when many monitors fire at once?

## 16. Browser PTY relay (ADAPTER-22)

Browser Watch/Chat for bound runners uses the Switchboard PTY relay
(`transport=switchboard_pty_relay`), not a direct Agent Host port. When
`PM_SWITCHBOARD_PUBLIC_BASE` / `PM_RUNNER_PTY_RELAY_PUBLIC_BASE` is set, `runner_open`
returns a public relay URL and keeps the loopback stream under
`metadata.local_stream_url` only. Without a public base, host-local `http_chunked` remains
for CO-12 compatibility but is marked `browser_safe=false` / `relay_required=true`.
