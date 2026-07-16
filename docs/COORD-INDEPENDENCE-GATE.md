# Coordination / board independence gate (ARCH-MS-104)

**Verdict: No-Go.** Keep Coordination/board in the modular monolith. Do not install a
Coord unit, route Caddy to `:8123`, or set `PM_COORD_HTTP_PRIMARY=service`.

The machine-readable authority is
[`coord_independence_verdict.json`](coord/coord_independence_verdict.json), enforced by
`scripts/arch_ms104_coord_independence.py`. A future Go amendment must make every required
gate pass; changing prose alone cannot authorize ARCH-MS-105.

## Why No-Go

The day-one surface is read-only and operationally affordable, but it is not process
independent. The current board, coordination, monitor/delta, and signals modules still import
root `store`, `auth`, `dispatch`, or `signals` facades. Starting another uvicorn now would turn
those in-process dependencies into a networked monolith—the exact failure ADR-0013 forbids.

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
| G1 Ports / import independence | **Fail — blocks cut** |
| G2 Route and writer inventory | Pass |
| G3 Auth and explicit project scope | Pass after BUG-73 fix |
| G4 SQLite contention | Pass |
| G5 VM resource budget | Pass |
| G6 Tasks production acceptance | Pass |

To reopen Path A, introduce dependency-injected Coord query/auth ports with a zero-forbidden-import
ceiling, bind them directly to package repositories, rerun this executable gate, and merge an
amended `verdict=go` artifact. Until then, ARCH-MS-105 is Go-only and must remain blocked.
