# IXP Conformance And Badge Guide

- **Status:** Reference conformance guide v0.1
- **Board anchor:** PROTO-6
- **Date:** 2026-07-06
- **Reference fixture:** [`adapters/conformance.py`](../adapters/conformance.py)
- **Public package:** [`IXP-PUBLIC-PACKAGE.md`](IXP-PUBLIC-PACKAGE.md)

This document defines what Switchboard conformance claims mean. It is intentionally narrower than
the product roadmap: a badge proves protocol behavior, not hosted-product equivalence.

## 1. Current reference command

Run the local reference fixture against an isolated throwaway board:

```bash
python3 adapters/conformance.py
```

Machine-readable capability statement:

```bash
python3 adapters/conformance.py --json
```

The command is network-free. It creates temporary SQLite databases, forces `PM_AUTH_MODE=required`,
registers an agent, exercises messages, leases, delta, task dispatch, Tally, completion evidence,
and reconcile, then removes the temporary data unless `--keep-tmp` is passed.

## 2. Badge levels

### `IXP-core conformant`

An implementation may claim **IXP-core conformant** only when it passes all core checks:

- auth rejects missing/invalid write credentials;
- working agreement advertises `ixp.v1`;
- agent registers stable presence with a compatible protocol envelope;
- inbox drains directed messages and acknowledges them;
- `stop`/redirect-style signals are surfaced at the instruction boundary;
- delta cursor advances and does not replay duplicate updates;
- resource lease claim/release works and releases task leases on completion;
- activity/reconcile can run without false drift for the completed conformance task.

### `IXP-core + TXP/OXP tested`

An implementation may claim **IXP-core + TXP/OXP tested** when it also passes the optional fixture
checks for:

- `claim_next` task dispatch;
- dependency guard behavior;
- `complete_claim` with branch/head evidence;
- Tally usage reporting;
- verified outcome and KPI contribution links.

This is not a promise that every hosted scheduler, budget, policy, or Tally analytics feature is
implemented. It means the adapter can speak the first public slices of `+TXP` and `+OXP`.

### `Switchboard-compatible adapter`

Use this wording for runtime packs that:

- run the shared conformance fixture or an equivalent REST/MCP client fixture;
- advertise a protocol envelope in `register_agent`;
- fail closed on known-incompatible protocol versions;
- document their control fidelity (`observe_only`, `advisory_poll`, `hook_deny`, `runner_kill`,
  or `managed`).

## 3. Required badge evidence

Every public badge claim should include:

| Field | Required |
|---|---|
| Adapter/runtime name | Yes |
| Protocol version | Yes, e.g. `ixp.v1` |
| Profile | Yes, e.g. `p0-dogfood` |
| Fixture command | Yes |
| Fixture source version or commit | Yes |
| Verification date | Yes |
| Capability statement JSON | Recommended |
| Known deviations | Required if any |

Example:

```text
IXP-core conformant
Adapter: local-store
Protocol: ixp.v1 / p0-dogfood
Verified: 2026-07-06
Command: python3 adapters/conformance.py --adapter local-store --runtime reference --json
Fixture: adapters/conformance.py @ <commit>
Deviations: none for IXP-core; TXP/OXP are profile slices, not hosted-product equivalence.
```

## 4. Non-claims

The badge does not claim:

- mid-token interrupt delivery;
- hard process kill through the IXP wire;
- hosted policy, SSO, entitlement, replay, or long-term evidence retention;
- full scheduler parity with the hosted control plane;
- cost-provider reconciliation parity with hosted Tally;
- safety of running unauthenticated writes on a public network.

## 5. Reference status

The current in-repo reference fixture exercises:

| Area | Fixture status |
|---|---|
| Authenticated writes | Covered |
| Working agreement/protocol envelope | Covered |
| Presence registration | Covered |
| Directed inbox, ack, and stop signal surfacing | Covered |
| Delta cursor behavior | Covered |
| Resource lease claim/release | Covered |
| `claim_next` dispatch | Covered as `+TXP` slice |
| Completion evidence | Covered |
| Tally usage/outcome/KPI link | Covered as `+OXP` slice |
| Reconcile no-drift check | Covered |
| Hosted policy/SSO/replay/managed runners | Out of scope |

## 6. Governance for conformance changes

Conformance changes must be reviewable as protocol changes:

- Any new required badge condition must update this document and the fixture.
- Any wire-breaking change must name the protocol version it belongs to.
- Adapter-specific smokes may add checks, but they must not redefine badge semantics.
- A failing conformance check should fail loud; do not replace it with a placeholder pass.
