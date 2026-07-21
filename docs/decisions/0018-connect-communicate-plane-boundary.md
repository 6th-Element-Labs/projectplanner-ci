# ADR-0018 — Separate Connect call control from Communicate agent work

- **Status:** Accepted
- **Date:** 2026-07-21
- **Author:** Codex with operator direction
- **Relates to:** [`SWITCHBOARD-CONNECT-COMMUNICATE-PRD.md`](../SWITCHBOARD-CONNECT-COMMUNICATE-PRD.md) ·
  [`AGENT-HOST-SPEC.md`](../AGENT-HOST-SPEC.md) ·
  [ADR-0006](0006-control-plane-done-enough.md) ·
  [ADR-0017](0017-boundary-delivery-of-ordinary-messages.md) ·
  DISPATCH-10…13

## Context

Switchboard grew two legitimate capabilities but did not preserve their boundary:

1. booting and supervising provider runtimes; and
2. giving running agents a shared MCP/message-board plane.

The legacy launch path consequently accumulated post-boot workflow knowledge. Today:

- `dispatch.py` constructs lifecycle roles, source-SHA rules, deliverable-aware prompts,
  continuation policy, and MCP access details;
- `adapters/agent_host.py` contains claim/Work Session binding and workflow finalization around
  process launch;
- provider workers contain proof, completion, and credential-admission behavior that differs
  by runtime and launch surface;
- a launched CLI can therefore receive different authority and behavior from a desktop agent
  even though both are the same LLM participant on Switchboard.

This made the boot layer an orchestration mainframe. SIMPLIFY reduced some paths but preserved
the wrong responsibility: it automated the post-boot workflow instead of removing it.

The correct model is old and well understood. DHCP assigns a lease. A SIP/PBX control plane
registers endpoints, establishes a call, keeps it alive, and tears it down. It does not decide
what participants say. Switchboard differs from a classic phone call only after setup: agents
do not move to peer-to-peer media; they join Switchboard's shared Communicate plane.

## Decision

### 1. Switchboard has two logical planes

**Switchboard Connect** owns:

```text
discover -> offer -> request -> ack -> launch -> heartbeat -> release/expire/kill
```

**Switchboard Communicate** owns the shared MCP/message-board experience used by running
agents.

They are one product and may share a deployment, edge, database technology, and neutral
identity primitives. They are separate bounded contexts in source, schemas, and runtime
responsibility.

### 2. The agent is the handoff

Connect returns an opaque assignment lease containing connection-plane identity and limits.
The host launches the provider CLI using host-local configuration. The launched agent then
authenticates directly to Communicate.

Connect does not call Communicate to bootstrap, restrict, grade, complete, or supervise work.
Communicate does not call Connect to place, launch, heartbeat, expire, or kill a process.

An edge transport may expose commands from both contexts. For example, a Start command can be
available over MCP while still belonging to Connect. Sharing a transport is not permission to
share domain logic.

### 3. Share identity format, not policy coupling

The planes may share a neutral signed principal/capability envelope and tenant identity. This
is platform infrastructure, not a Connect-to-Communicate dependency.

Connect assigns `principal_ref`, `assignment_id`, and `work_ref`. Communicate independently
authenticates the principal using its configured mechanism. Connect never mints a smaller MCP
tool list or surface-specific role. The same principal has the same Communicate authority
whether launched from desktop, CLI, web, Autopilot, or another caller.

### 4. Connect is content-blind

Connect may persist call-control metadata and aggregate resource counters. It must not receive
or store prompts, messages, transcripts, tool calls, work results, source-control state,
reviews, evidence, or completion decisions.

The opaque `work_ref` is routing data. Connect must not dereference or interpret it.

### 5. Communicate is execution-host-blind

Communicate may understand agents, projects, tasks, tools, messages, and work facts. It must
not select hosts, construct provider commands, manage process groups, account for live runner
capacity, maintain runner heartbeats, or terminate runtimes.

### 6. Connect uses one provider-neutral protocol

Codex, Claude, Cursor, and future runtimes use the same assignment lease. Provider adapters
only translate an Ack into local process syntax. Runtime configuration—including how the
agent reaches Communicate—is installed on the host and is not synthesized by Connect.
Hosts advertise exact runtime/provider capability pairs and current free headroom. Active
processes are already excluded from that headroom; outstanding Offers reserve it.

### 7. Enforce the boundary mechanically

CI must fail when:

- Connect imports MCP, task workflow, claims, Work Sessions, review, evidence, git/PR, or
  completion modules;
- Connect wire fields include content/workflow vocabulary;
- Communicate imports host launch, placement, heartbeat, expiry, or process-kill code;
- provider launchers create different Communicate permissions by runtime or surface.

### 8. Replace the legacy path; do not wrap it forever

Migration may temporarily adapt existing wake/runner storage to the Connect contract. The end
state deletes legacy policy construction and post-boot finalizers. A compatibility wrapper is
not an acceptable terminal architecture.

## Consequences

- Starting an agent becomes a small, horizontally scalable lease operation.
- Switchboard Communicate retains its rich feature set; this ADR does not shrink MCP.
- Agent autonomy begins immediately after launch. Communicate—not Connect—is the shared place
  where agents coordinate and act.
- Connect liveness proves only that an assigned runtime is alive, not that work is correct or
  complete.
- Workflow gates may exist in Communicate or repository CI, but they cannot block or alter
  Connect's process-control contract after Ack.
- Existing claims, Work Sessions, review receipts, and provenance can continue as Communicate
  features if product requirements defend them. They are removed only from Connect.
- Physical co-deployment is allowed. Logical dependency leakage is not.

## Alternatives rejected

### Keep one orchestration control plane

Rejected. It caused provider/surface discrimination, self-certification dead ends, and a boot
path that could not finish work without operator repair.

### Make agents peer-to-peer after launch

Rejected. Switchboard Communicate is the product's shared, durable message board. Agents join
it fully after Connect establishes them.

### Have Connect proxy every Communicate call

Rejected. That makes Connect a surveillance/policy chokepoint, couples availability, and puts
it back in the work path.

### Put MCP credentials and tool lists in every Offer

Rejected. It couples the planes and recreates launch-source permission drift. Communicate
configuration belongs on the host; authority belongs to the principal.

### Split Connect and Communicate into separate services immediately

Rejected as a requirement. The decision is about bounded contexts, not microservices.
Co-deployment is simpler today. They may split physically later without changing the contract.

## Verification

The decision is satisfied only when one end-to-end trace for each supported provider shows:

1. an opaque Connect assignment is acknowledged;
2. the provider process launches from host-local configuration;
3. the agent independently authenticates to Communicate;
4. the agent performs real Communicate operations with the same authority as other surfaces;
5. Connect sees only lease/capacity metadata;
6. exit or kill ends the lease and releases capacity; and
7. no Connect-to-Communicate internal call occurs.
