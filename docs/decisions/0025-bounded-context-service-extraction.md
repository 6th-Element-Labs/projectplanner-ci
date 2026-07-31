# ADR-0025 — Bounded-context service extraction

- **Status:** Accepted (operator decision, 2026-07-31)
- **Date:** 2026-07-31
- **Canonical reference:** `ADR-0025/service-extraction`
- **Supersedes as current program authority:** [ADR-0009](0009-microservices-modernization.md),
  [ADR-0011](0011-phase2-process-strangler.md),
  [ADR-0012](0012-phase3-tasks-process-strangler.md),
  [ADR-0013](0013-coord-board-process-strangler.md),
  [ADR-0014](0014-deliverables-mission-process-strangler.md),
  [ADR-0015](0015-tally-economics-process-strangler.md), and
  [ADR-0016](0016-ingest-inbox-process-strangler.md)
- **Current route truth:** [`deploy/Caddyfile`](../../deploy/Caddyfile)

## Context

The Phase 0 modernization and Auth, Tasks, Coordination, Deliverables, Tally, and
Ingest programs repeatedly reached the same decision:

- establish a bounded in-process owner first;
- prove independence before creating a process boundary;
- cut one bounded context at a time;
- retain a green facade and reversible route;
- accept a measured No-Go as a valid result; and
- do not turn unresolved in-process coupling into network coupling.

Those rules were copied into seven program charters with different ports, routes,
task numbers, and exit gates. Most associated deliverables are now archived.
Keeping every completed charter in the active architecture path makes temporary
execution detail look like seven independent architectural decisions.

## Decision

Switchboard uses one reusable service-extraction policy.

### 1. Bounded ownership precedes deployment topology

Every capability first has one bounded owner, a typed application interface, and
declared data ownership. REST and MCP remain thin adapters over shared application
commands and queries. New SQL and backend-specific behavior remain under
`src/switchboard/storage/`.

A package boundary is a valid terminal architecture. A separate service process is
not automatically better.

### 2. A process cut requires an operational reason

A separate deployable process is justified only by at least one material need:

- independent scaling;
- security or trust isolation;
- failure isolation;
- a distinct release cadence; or
- an external-runtime requirement.

An extraction proposal must identify the bounded context, accountable owner,
interface and compatibility contract, data ownership, rollback path, responsibility
removed from the old boundary, and the operational reason an in-process boundary is
insufficient.

### 3. Independence is a fail-closed gate

Before live traffic moves, the candidate must prove:

- no undeclared imports into root monolith, shell, or another bounded context;
- explicit ports for required cross-boundary operations;
- exclusive or mechanically safe writers for every owned table;
- project and principal isolation;
- defined outage and degraded behavior;
- secret and credential ownership;
- side-by-side behavior parity;
- measured contention and resource behavior; and
- an executable cutover and rollback drill.

Failure to meet the gate means **keep in process**. No-Go is evidence, not mission
failure.

### 4. One bounded context per cut

Each cut moves one declared surface. It does not quietly absorb adjacent contexts,
all MCP traffic, all TXP/IXP routes, or a second service. Surface expansion requires
an explicit contract update and a fresh independence assessment.

### 5. Green facade and reversible routing

The existing path stays available until the candidate passes side-by-side parity.
The live sequence is:

```text
bounded package
-> independence proof
-> side-by-side process
-> exact Caddy route cut
-> soak and dual-path removal
-> retained rollback procedure
```

Caddy remains the single edge. Approved processes use the repository's established
FastAPI and Uvicorn pattern. No service adds a second reverse proxy.

### 6. Storage topology is a separate decision

SQLite remains the default unless measured workload, isolation, or availability
evidence authorizes another backend. A process cut does not itself authorize
Postgres, shared raw connections, or cross-service table ownership.

### 7. Service-specific facts live outside ADRs

Ports, route lists, environment flags, Caddy matchers, cutover commands, parity
fixtures, and rollback procedures belong in service contracts, configuration,
tests, and runbooks. They are as-built facts and may change without creating
another architecture charter when this policy remains intact.

## Current disposition of the completed charters

The superseded ADRs remain available as program history and cut rationale:

- ADR-0009 records the modular-monolith foundation.
- ADR-0011 and ADR-0012 record the Auth and Tasks cuts.
- ADR-0013, ADR-0014, and ADR-0016 record the Coordination, Deliverables, and
  Ingest surfaces.
- ADR-0015 records the Tally evaluation. Repository source currently contains no
  standalone Tally service or `:8125` Caddy route, so it must not be read as a
  claim that Tally was cut.

Repository source, service units, Caddy, tests, and deployment evidence—not these
historical charters—determine current physical topology.

## Consequences

- New service work uses one policy instead of creating a phase or service ADR.
- A service-specific ADR is warranted only when it changes this extraction policy
  or introduces a genuinely new authority, trust, or data-ownership decision.
- Completed charter details remain reviewable without crowding the active decision
  set.
- Physical deployment must not be inferred from package source inspection alone.

## Alternatives rejected

### Keep one ADR per service cut

Rejected because the architectural reasoning was copied while only routes, ports,
and execution state changed.

### Mandate that every bounded context become a service

Rejected because it rewards network boundaries without an operational need and
turns modular coupling into distributed-system coupling.

### Collapse back into one root monolith

Rejected because typed bounded ownership, thin adapters, and storage placement are
current repository invariants regardless of physical process count.
