# ARCH-MS execution tracker — Phase 0 platform modernization

**Charter:** [ADR-0009](decisions/0009-microservices-modernization.md)  
**Board:** `project=switchboard` · workstream **ARCH-MS** · deliverable **`arch-ms-phase-0`**  
**Mission end state:** ADR-007 rails complete; `src/switchboard/` scaffold live; one REST+MCP pair
uses `application/` commands; monolith ratchets stop growing.

**Canonical main (tracker baseline):** `5305090` (2026-07-12)  
**View:** [`?project=switchboard&deliverable=arch-ms-phase-0#tab-mission`](https://plan.taikunai.com/?project=switchboard&deliverable=arch-ms-phase-0#tab-mission)

---

## How to use this doc

| Symbol | Meaning |
|---|---|
| ⬜ Not started | No merged repo work for this task |
| 🟡 In progress | Claimed, partial land, or open PR |
| ✅ Done (repo) | Merged on canonical `master` with identifiable evidence |
| 🔗 Shipped elsewhere | Requirement met by a non-ARCH-MS task (evidence linked) |

Update the **Repo evidence** column when a PR merges. Board status (`Not Started` / `In Progress` /
`In Review` / `Done`) follows Switchboard provenance rules — agents use `complete_claim`; Done
requires merge webhook or reconcile.

---

## Milestones

| ID | Title | Intent |
|---|---|---|
| `m0-enforcement` | 0.1 Enforcement (ADR-0007) | Ratchet, CI discovery, dead-surface deletion, Caddy + poll parity, census |
| `m0-scaffold` | 0.2 Scaffold (`src/switchboard/`) | Package skeleton, `application/` commands, REST/MCP adapters, CI proof gate |
| `m0-security` | 0.3 Security P0 | MCP read auth, readiness probe hygiene |

---

## Task table (ARCH-MS-1 … ARCH-MS-24)

| Task | Title | Milestone | Deps | Board | Tracker | Repo evidence |
|---|---|---|---|---|---|---|
| **ARCH-MS-1** | ADR-0009 charter + ARCH-MS-EXECUTION tracker | 0.2 | — | Done | ✅ | PR #314 — `docs/decisions/0009-microservices-modernization.md`, `docs/ARCH-MS-EXECUTION.md` |
| **ARCH-MS-2** | Size ratchet + CI test discovery (CONSOL-6) | 0.1 | — | Not Started | 🟡 | `test_size_ratchet.py`; `scripts/switchboard_ci.sh` pytest discovery + `TEST_DENYLIST` |
| **ARCH-MS-3** | Delete dead MCP/REST surfaces (CONSOL-7, CONSOL-9) | 0.1 | — | In Review | ✅ | **CONSOL-7** PR #276 + `test_consol7_dead_surfaces.py`; **CONSOL-9** PR #297 + `test_consol9_h2_census.py`. `gmail_source.py` deferred → ARCH-MS-11 |
| **ARCH-MS-4** | Caddy security headers + mission poller ETag (CONSOL-8) | 0.1 | — | Not Started | ✅ | `deploy/Caddyfile` security headers + access log; `app.py` mission_status / dependency_graph `max_age=5` + ETag |
| **ARCH-MS-5** | MCP read auth — bearer required on `/mcp` | 0.3 | — | Not Started | ⬜ | Reads still open; writes use `_require_write` only (`mcp_server.py`) |
| **ARCH-MS-6** | `pyproject.toml` package scaffold (lockfile pending) | 0.2 | 1 | In Progress | 🔗 | **HARDEN-54** / PR #303 — `pyproject.toml`, `.python-version`, `uv.lock`; lockfile task split to ARCH-MS-13 |
| **ARCH-MS-7** | `src/switchboard/` package skeleton | 0.2 | 1 | In Review | 🟡 | `src/switchboard/` package tree + `settings.py` + `scripts/switchboard_path.py` |
| **ARCH-MS-8** | `create_task` application command + REST/MCP wire | 0.2 | 7 | Not Started | ⬜ | — |
| **ARCH-MS-9** | `test_arch_ms0_scaffold` CI gate | 0.2 | 7, 8 | Not Started | ⬜ | — |
| **ARCH-MS-10** | `PM_*` env flag census + delete unread flags | 0.1 | 2 | Not Started | ⬜ | 112+ flags per ADR-0007; census not complete |
| **ARCH-MS-11** | Extract inbox routing; retire `gmail_source.py` | 0.1 | 10 | Not Started | ⬜ | `gmail_source.py` still in tree |
| **ARCH-MS-12** | Numbered transactional DB migrations | 0.1 | 2 | Not Started | 🔗 | **BUG-47** / PR #301 — ledgered migrations; `test_schema_migrations.py` |
| **ARCH-MS-13** | Lockfile + Python 3.12 pin (reproducible builds) | 0.1 | 6 | Not Started | 🔗 | **HARDEN-54** / PR #303 — `uv.lock`, generated `requirements*.txt` |
| **ARCH-MS-14** | `tests/` directory + path shim for new tests | 0.1 | 2 | Not Started | ⬜ | Root-level test scripts still; no `tests/` package |
| **ARCH-MS-15** | `get_task` query + `update_task` application command | 0.2 | 8 | Not Started | ⬜ | — |
| **ARCH-MS-16** | `api/routers/tasks.py` — extract task REST routes | 0.2 | 15 | Not Started | ⬜ | — |
| **ARCH-MS-17** | `mcp/tools/tasks.py` — extract task MCP tools | 0.2 | 15 | Not Started | ⬜ | — |
| **ARCH-MS-18** | Migrate `services/auth` → `api/routers/auth` | 0.2 | 7 | In Review | 🟡 | Auth package moved to `src/switchboard/api/routers/auth`; app and tests use the package seam |
| **ARCH-MS-19** | `mcp/tools/board.py` — first MCP tool module pattern | 0.2 | 17 | Not Started | ⬜ | — |
| **ARCH-MS-20** | `runner_*` → `runner_store.py` leaf extraction | 0.2 | 7 | Not Started | ⬜ | ADR-0007: ~445 lines, 2 external callers — headroom for ratchet relief |
| **ARCH-MS-21** | Split `static/app.js` → `static/js/{api,state,board,mission}` | 0.2 | 2 | Not Started | ⬜ | `static/app.js` still monolith (ratchet ceiling 6,566 lines) |
| **ARCH-MS-22** | `/health/deep` — stop leaking project identifiers | 0.3 | 5 | Not Started | 🔗 | **BUG-48** / PR #299 |
| **ARCH-MS-23** | Global auth cutover — remove `PM_GLOBAL_AUTH` gate | 0.3 | 18 | Not Started | 🔗 | **ACCESS-16** / PR #300 — legacy login + flag deleted |
| **ARCH-MS-24** | Phase 0 exit gate — ratchet lowered, application layer proven | 0.2 | 11,12,13,14,16,17,19,20,21,22,23 | Not Started | ⬜ | Blocked on scaffold + enforcement remainder |

---

## Ratchet snapshot (master @ `5305090`)

| File | Ceiling (lines) | Notes |
|---|---|---|
| `store.py` | 15,470 | ARCH-MS-20 extracted runner persistence/control |
| `app.py` | 3,276 | Pre-existing master drift remeasured by ARCH-MS-20 gate |
| `mcp_server.py` | 3,157 | Pre-existing master drift remeasured by ARCH-MS-20 gate |
| `static/app.js` | 6,566 | Held |
| repo root `*.py` | 201 | Master had 200; ARCH-MS-20 adds planned `runner_store.py` leaf |

Source: `test_size_ratchet.py` (CONSOL-6).

---

## Dependency sketch

```mermaid
flowchart TD
  MS1[ARCH-MS-1 Charter]
  MS2[ARCH-MS-2 Ratchet+CI]
  MS3[ARCH-MS-3 Dead surfaces]
  MS4[ARCH-MS-4 Caddy+poll]
  MS5[ARCH-MS-5 MCP read auth]
  MS6[ARCH-MS-6 pyproject]
  MS7[ARCH-MS-7 src/switchboard]
  MS8[ARCH-MS-8 create_task cmd]
  MS9[ARCH-MS-9 scaffold test]
  MS24[ARCH-MS-24 Exit gate]

  MS1 --> MS6
  MS1 --> MS7
  MS7 --> MS8
  MS7 --> MS18
  MS7 --> MS20
  MS8 --> MS9
  MS8 --> MS15
  MS15 --> MS16
  MS15 --> MS17
  MS17 --> MS19
  MS2 --> MS10
  MS2 --> MS12
  MS2 --> MS14
  MS2 --> MS21
  MS10 --> MS11
  MS6 --> MS13
  MS5 --> MS22
  MS18 --> MS23
  MS11 --> MS24
  MS12 --> MS24
  MS13 --> MS24
  MS14 --> MS24
  MS16 --> MS24
  MS17 --> MS24
  MS19 --> MS24
  MS20 --> MS24
  MS21 --> MS24
  MS22 --> MS24
  MS23 --> MS24
```

---

## Suggested claim order (ready tasks)

Tasks with satisfied dependencies and remaining work:

1. **ARCH-MS-1** — this charter (in flight)
2. **ARCH-MS-2** — close CONSOL-6 (verify discovery covers ratchet; document denylist policy)
3. ~~**ARCH-MS-3** — finish CONSOL-7 deletions + CONSOL-9 census execution~~ (done; gmail_source → ARCH-MS-11)
4. **ARCH-MS-5** — MCP read auth (P0; blocks ARCH-MS-22 formal closure if regressed)
5. **ARCH-MS-7** — package skeleton (unblocks scaffold chain)
6. **ARCH-MS-10** — flag census (unblocks inbox extraction)

---

## Changelog

| Date | Actor | Note |
|---|---|---|
| 2026-07-12 | ARCH-MS-1 | Initial tracker + ADR-0009 charter; baseline master `5305090` |
| 2026-07-12 | ARCH-MS-3 | CONSOL-7/9 closed; added `test_consol7_dead_surfaces.py`; gmail_source scoped to ARCH-MS-11 |
