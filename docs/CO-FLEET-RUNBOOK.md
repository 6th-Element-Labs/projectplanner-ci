# Switchboard CO Fleet runbook

CO Fleet is a runtime provisioner, not a Terraform apply loop. Terraform (or
CloudFormation) is appropriate for the slow-changing IAM, AMI pipeline, budgets,
and base launch templates established by CO-1/CO-2. `co_fleet.py` owns the
per-wake decision that cannot sensibly wait for infrastructure reconciliation:

1. Read a pending `policy.mode=co_fleet` wake with its durable CO-9 placement decision.
   If a healthy persistent Agent Host was selected, leave EC2 untouched and let that host
   claim the wake. Provision only `action=provision_ephemeral` decisions.
2. Select `co-general` or `co-build` from required capabilities.
3. Fail closed on the SSM guardrails, launch switch, budget readback, and 4+2/6 caps.
4. Derive one launch-template version from the explicitly pinned CO-2 base version.
5. Inject only an SSM/Secrets Manager reference plus task/runtime/lane selector.
6. Request diversified Spot across instance types and AZs; use On-Demand only when
   the wake explicitly permits fallback.
7. Wait up to three minutes for the exact `host/<instance-id>` registration with
   the requested runtime/lane/capabilities and `allow_work=true`.
8. Leave the durable wake pending for that registered Agent Host to claim and launch.
   Ephemeral hosts filter the queue by their injected `PM_WAKE_ID`, so another
   same-lane wake cannot capture capacity carrying the wrong task/account affinity.
9. After 10-15 minutes idle and a final empty-work read, send a fixed-schema drain
   marker through SSM. The worker immediately advertises `allow_work=false` and
   `status=draining`, so it cannot claim another wake.
10. Snapshot and interrupt managed runners, checkpoint and push eligible task branches,
    release provider credential leases, purge isolated provider homes, and publish a
    redacted `switchboard.co_drain.receipt.v1` in host capacity.
11. Terminate only after the matching durable `drained` receipt and another empty-work
    read. If no acknowledgement arrives by `CO_DRAIN_TIMEOUT_SECONDS` (default 120),
    terminate through the explicit `terminate_forced_timeout` audit path.

The Plan VM runs only this coordination daemon. Claude Code/Codex and repository
work execute on the EC2 worker. The immutable image supplies runtime dependencies
and system units; the Agent Host process executes from the exact checked S3-mirror
revision, allowing application roll-forward/back independently of AMI replacement.

## Secret contract

Dispatch accepts `ssm:/path` or `secretsmanager:arn:...`; it never accepts a raw
token or API key. The referenced JSON may contain the allowlisted worker variables
(`PM_MCP_TOKEN`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, and related runtime config).
The worker IAM role resolves it at boot and writes it to a root-owned `0600`
environment file. EC2 user data, instance tags, wake activity, and provisioner logs
contain no secret value. Instance tags store only a SHA-256 prefix of the reference.

BYOA/hybrid dispatches may additionally carry a durable
`switchboard.co_account_binding.v1` object. If any account-affinity field is supplied,
tenant, user, project, provider, provider account, opaque credential reference, task,
active task claim, and Work Session are all required; an optional auth lane is preserved.
A stable hash binds the account tuple so account substitution fails closed. Provider
capacity is read authoritatively before placement. After the selected host registers a
runner, that host acquires an exact host/runner-bound credential lease and presents both
identifiers atomically with `claim_wake`; caller-supplied dispatch lease ids are rejected.
The wake retains the identifiers for the later scheduler/runtime resolver, while provisioner
receipts redact provider-account and credential identifiers and expose only the affinity
hash. These fields are never copied into user data, EC2 tags, host metadata, or logs.
Ephemeral `host_id`, `runner_session_id`, and `credential_lease_id` remain unset until the
registered host completes claim-time admission; the dispatcher is forbidden from guessing them.

During drain, the runner binding retains only what is needed to release or fence the
personal-login lease: Work Session id, lease id, provider, and the non-reversible account
affinity hash. The durable drain receipt omits the lease id, provider-account id, credential
reference, process log tail, and all credential values. Codex, Claude, and Cursor runtime
homes are purged after the managed process is interrupted; CO-7's active-lease fence remains
the writeback authority, so an interrupted or stale Codex process cannot overwrite newer
auth state.

## Control and rollback

The real-time launch switch is SSM `/switchboard/co/launch-enabled`:

```json
{"enabled": true, "reason": "normal operations"}
```

Set `enabled=false` to stop new launches immediately. Existing active work is not
hard-killed; idle scale-in continues. Capacity failures are completed as typed,
escalated wake failures instead of remaining silently queued.

Spot interruption and EC2 rebalance notices use IMDSv2 and enter the same drain path.
Persistent Agent Hosts use the same request schema with
`reason=persistent_host_removal` and `termination_kind=persistent_host`; an operator or
host-removal workflow writes the configured `PM_CO_DRAIN_REQUEST_PATH` marker before
stopping that host.

## Hybrid placement contract

Agent Hosts advertise `switchboard.agent_host_placement.v1` inside their capacity record:
host class, cost class, CPU/memory/disk headroom, installed runtime binaries, project and
tenant allowlists, provider/account-affinity allowlists, supported credential leases,
repositories, session policies, isolation modes, concurrency, wakeability, and drain state.
Missing or mismatched fields fail closed for hybrid work.

Switchboard persists `switchboard.hybrid_placement_decision.v1` on every hybrid wake. The
decision records reason-coded candidates, selected host/class, physical headroom, cost class,
fair-share bucket, and the independent provider-capacity state. It intentionally excludes raw
provider accounts, credential references, and lease identifiers. Persistent `already_paid`
capacity wins when eligible. Saturation or incompatible inventory produces a Spot-first
ephemeral decision; On-Demand remains disabled unless the wake policy explicitly allows it.

Pending persistent reservations count against physical slots, so two concurrent wakes cannot
both be promised the final local slot. Elastic wakes are round-robined by tenant/project
fair-share bucket. If a claimed host's heartbeat expires, the wake returns to `pending` with a
bounded recovery count and explicit checkpoint/workspace-reconstruction requirements. Planned
drain still performs the stronger CO-4 sequence: stop claims, checkpoint, purge credentials,
release account leases, acknowledge, then terminate.

Base versions are explicit environment values:

```bash
CO_GENERAL_LT_VERSION=5
CO_BUILD_LT_VERSION=5
```

Roll forward or back by changing those values to a tested version and restarting the
service. Every worker records both `CO:BaseLTVersion` and `CO:DerivedLTVersion`, so a
bad configuration can be identified and reversed without changing the AMI in place.

## Operations

```bash
python3 co_fleet.py inspect
python3 co_fleet.py run-once
python3 co_fleet.py scale-in-once
```

Production runs `deploy/switchboard-co-fleet.service` with the least-privilege policy
in `deploy/switchboard-co-fleet-iam-policy.json`. Keep `PM_MCP_TOKEN` in the protected
service environment, never in the unit or repository.

Measure cold starts from the emitted `switchboard.co_fleet_receipt.v1`
`wake_to_register_seconds` and the matching `CO:WakeToRegisterSeconds` instance tag.
Acceptance is p50 <= 90 seconds and p95 <= 180 seconds from a zero-capacity start.
