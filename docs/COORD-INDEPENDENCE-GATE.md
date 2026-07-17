# Coordination / board independence gate (ARCH-MS-104)

**Verdict: Go for ARCH-MS-105 side-by-side build.** The read-only Coord package now has
an independent query/Auth boundary. This authorizes building and parity-testing the service on
`:8123`; it does not authorize production cutover or Caddy ownership yet.

The machine-readable authority is
[`coord_independence_verdict.json`](coord/coord_independence_verdict.json), enforced by
`scripts/arch_ms104_coord_independence.py`. A future Go amendment must make every required
gate pass; changing prose alone cannot authorize ARCH-MS-105.

## Why Go

The day-one surface is read-only and operationally affordable. It now lives behind
`CoordQueryPort` and `CoordReadAuthPort` in `src/switchboard/services/coord`, with production
adapters bound to package repositories outside the service package. The executable gate parses
every Coord package module and requires zero direct imports of root `store`, `auth`, `dispatch`,
or `signals` facades.

## Exact day-one route and transaction inventory

| Route | Repository calls | Transaction / writer boundary |
|---|---|---|
| `GET /api/board` | Tasks board projection + activity/meta | Several bounded read connections and process-local TTL cache; no writes |
| `GET /api/signals` | Full Tasks read + activity/meta reads | Several bounded read connections and process-local TTL cache; no writes |
| `GET /ixp/v1/delta` | Activity delta + task/git-state projection | One project read transaction; no writes |
| `GET /api/coordination` | Presence, messages, decisions, coordinator decisions | Four sequential bounded read connections; no cross-call transaction; no writes |
| `GET /api/coordinator_decisions` | Decisions projection | One project read transaction; no writes |

Writer inventory for the chartered surface is therefore empty. Messaging, wakes, agents,
monitors, coordinator dispatch, and every other write remain explicitly outside day one.

## Auth and project scope

All five routes require an explicit project. `/api/*` reads use the global project-scoped read
gate. The audit discovered that `/ixp/v1/delta` bypassed that middleware as a protocol route but
did not authenticate inside its handler: production returned `200` without a bearer while the
control `/api/board` returned `401`. The branch fixes the handler to require a project-scoped
`read` principal and records the production defect as **BUG-73**.

## SQLite and capacity

Coord day one adds readers, not writers. The executable WAL probe runs one writer beside three
reader connections (80 transactions, 240 reads) and requires zero lock errors plus
`quick_check=ok`. ARCH-MS-103's production soak also recorded zero SQLite lock signals.

Live VM measurement at `2026-07-16T00:29:48Z`:

- 1,931,718,656 bytes RAM total; 692,711,424 bytes available.
- Auth unit memory 50,577,408 bytes; Tasks 70,086,656 bytes; monolith 205,279,232 bytes.
- Conservative Coord ceiling: 96 MiB (100,663,296 bytes).
- Projected available after Coord: 592,048,128 bytes, leaving 55,177,216 bytes above the
  mandatory 512 MiB reserve.

Capacity and reader contention pass. They do not override the failed ports-independence gate.

## Gate result

| Gate | Result |
|---|---|
| G1 Ports / import independence | Pass |
| G2 Route and writer inventory | Pass |
| G3 Auth and explicit project scope | Pass after BUG-73 fix |
| G4 SQLite contention | Pass |
| G5 VM resource budget | Pass |
| G6 Tasks production acceptance | Pass |

All six gates pass. ARCH-MS-105 may build the standalone process and run side-by-side parity.
Production deployment and edge ownership remain gated by the later deployment/acceptance milestones.
