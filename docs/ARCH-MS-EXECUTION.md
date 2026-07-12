# ARCH-MS execution tracker — Phase 0 platform modernization

**Charter:** [ADR-0009](decisions/0009-microservices-modernization.md)  
**Board:** `project=switchboard` · workstream **ARCH-MS** · deliverable **`arch-ms-phase-0`**  
**Mission end state:** ADR-007 rails complete; `src/switchboard/` scaffold live; one REST+MCP pair
uses `application/` commands; new feature code lands in `src/switchboard/`, not the monoliths.

> **Ratchet retired 2026-07-12.** `test_size_ratchet.py` (the exact-match size gate) was deleted —
> it forced every concurrent PR to compare-and-swap one shared integer against a moving `master`,
> which produced the merge wars that stalled the fleet. Growth is now redirected by ADR-0007
> Decision 3 + review; the Phase 0 progress metric is **lines extracted**, not a ceiling. Net
> monolith growth is enforced commutatively by the per-PR `test_monolith_diff_guard.py`
> (HARDEN-69 / ADR-0010 Lever 1) — no shared counter. See ADR-0007 Decision 2 (retired) and
> ADR-0009 Decision 5 #4.

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
| **ARCH-MS-2** | CI test discovery (CONSOL-6); size ratchet **retired** | 0.1 | — | Done | ✅ | PR #345 — `scripts/switchboard_ci.sh` runs every `test_*.py` via discovery + `TEST_DENYLIST`; the shared-counter ratchet was retired. |
| **ARCH-MS-3** | Delete dead MCP/REST surfaces (CONSOL-7, CONSOL-9) | 0.1 | — | Done | ✅ | **CONSOL-7** PR #276 + `test_consol7_dead_surfaces.py`; **CONSOL-9** PR #297 + `test_consol9_h2_census.py`. `gmail_source.py` deferred → ARCH-MS-11 |
| **ARCH-MS-4** | Caddy security headers + mission poller ETag (CONSOL-8) | 0.1 | — | Done | ✅ | **CONSOL-8** PR #286 + `test_consol8_edge_mission_poll.py`. `deploy/Caddyfile` security headers + access log; `app.py` mission_status / dependency_graph `max_age=5` + ETag; ack poll visibility guard |
| **ARCH-MS-5** | MCP read auth — bearer required on `/mcp` | 0.3 | — | Done | ✅ | **BUG-46** / PR #273 — `mcp_auth.py` + `MCPAuthMiddleware`; `test_mcp_read_auth.py`; prod `PM_AUTH_MODE=required` |
| **ARCH-MS-6** | `pyproject.toml` package scaffold (lockfile pending) | 0.2 | 1 | Done | ✅ | **HARDEN-54** PR #303 + `tests/test_arch_ms6_pyproject_scaffold.py`. `pyproject.toml`, `.python-version`, generated `requirements*.txt`; lockfile → ARCH-MS-13 |
| **ARCH-MS-7** | `src/switchboard/` package skeleton | 0.2 | 1 | Done | ✅ | PR #319 — `src/switchboard/` package tree + `settings.py` + `scripts/switchboard_path.py` |
| **ARCH-MS-8** | `create_task` application command + REST/MCP wire | 0.2 | 7 | Done | ✅ | PR #324 — REST and MCP share the typed `create_task` application command |
| **ARCH-MS-9** | `test_arch_ms0_scaffold` CI gate | 0.2 | 7, 8 | Done | ✅ | PR #331 — `tests/test_arch_ms0_scaffold.py`; auto-discovered by `scripts/switchboard_ci.sh` |
| **ARCH-MS-10** | `PM_*` env flag census + delete unread flags | 0.1 | 2 | Done | ✅ | PR #329 — `scripts/pm_env_flag_census.py`; `tests/test_pm_env_flag_census.py`; tracked declarations fail closed when unread; CONSOL-9 deletion tombstones retained |
| **ARCH-MS-11** | Extract inbox routing; retire `gmail_source.py` | 0.1 | 10 | Done | ✅ | PR #338 — `src/switchboard/integrations/inbox_routing.py`; `inbox_source.py`; `tests/test_arch_ms11_inbox_routing.py` |
| **ARCH-MS-12** | Numbered transactional DB migrations | 0.1 | 2 | Done | 🔗 | **BUG-47** / PR #301 — ledgered migrations; `test_schema_migrations.py` |
| **ARCH-MS-13** | Lockfile + Python 3.12 pin (reproducible builds) | 0.1 | 6 | Done | ✅ | **HARDEN-54** PR #303 + PR #342 / `tests/test_arch_ms13_reproducible_builds.py` — lock metadata, artifact hashes, Python floor, and generated exports fail closed in CI |
| **ARCH-MS-14** | `tests/` directory + path shim for new tests | 0.1 | 2 | Done | ✅ | PR #340 — `tests/path_setup.py`; `tests/test_arch_ms14_test_layout.py`; new tests share the root + `src/` bootstrap |
| **ARCH-MS-15** | `get_task` query + `update_task` application command | 0.2 | 8 | Done | ✅ | PR #335 — shared get-task query and update-task application command |
| **ARCH-MS-16** | `api/routers/tasks.py` — extract task REST routes | 0.2 | 15 | Done | ✅ | PR #347 — `src/switchboard/api/routers/tasks.py`; complete `/api/tasks...` surface; `tests/test_arch_ms16_task_router.py` |
| **ARCH-MS-17** | `mcp/tools/tasks.py` — extract task MCP tools | 0.2 | 15 | Done | ✅ | PR #344 — task tools register from the package adapter; direct Python callers retain compatibility aliases |
| **ARCH-MS-18** | Migrate `services/auth` → `api/routers/auth` | 0.2 | 7 | Done | ✅ | PR #326 — auth package moved to `src/switchboard/api/routers/auth`; app and tests use the package seam |
| **ARCH-MS-19** | `mcp/tools/board.py` — first MCP tool module pattern | 0.2 | 17 | Done | ✅ | PR #348 — board summary, delta, project discovery, and plan signals register from the package adapter |
| **ARCH-MS-20** | `runner_*` → `runner_store.py` leaf extraction | 0.2 | 7 | Done | ✅ | PR #323 — 441 monolith lines moved into the 480-line `runner_store.py` leaf |
| **ARCH-MS-21** | Split `static/app.js` → `static/js/{api,state,board,mission}` | 0.2 | 2 | Done | ✅ | PR #334 — `static/app.js` composition root + `static/js/{api,state,board,mission}.js` |
| **ARCH-MS-22** | `/health/deep` — stop leaking project identifiers | 0.3 | 5 | Done | 🔗 | **BUG-48** / PR #299 |
| **ARCH-MS-23** | Global auth cutover — remove `PM_GLOBAL_AUTH` gate | 0.3 | 18 | Done | 🔗 | **ACCESS-16** / PR #300 deleted the legacy login + flag; PR #327 guards against regression |
| **ARCH-MS-24** | Phase 0 exit gate — extraction proof + application layer proven | 0.2 | 11,12,13,14,16,17,19,20,21,22,23 | In Review | 🟡 | `scripts/arch_ms_phase0_exit_gate.py`; `tests/test_arch_ms24_phase0_exit_gate.py`; project/access repository move closes the measured extraction threshold |

---

## Phase 0 exit measurement (immutable baseline `5305090`)

The retired ratchet's moving ceilings remain gone. ARCH-MS-24 instead compares the working tree
to one immutable git baseline, so parallel PRs never edit a shared counter.

| File | Baseline lines | ARCH-MS-24 candidate | Delta | Result |
|---|---:|---:|---:|---|
| `store.py` | 15,789 | 15,245 | −544 | Pass: ≥500-line reduction |
| `app.py` | 3,273 | 3,137 | −136 | Pass: no net growth |
| `mcp_server.py` | 3,154 | 3,015 | −139 | Pass: no net growth |

The added access repository holds 23 AST-identical functions formerly in `store.py`, and
`store.py` re-exports them as a compatibility facade. The versioned JSON audit is generated by
`scripts/arch_ms_phase0_exit_gate.py`; there is no exact-current-size ceiling to update. After
the initial verbatim extraction, intentional repository evolution is declared explicitly in the
audit while undeclared function drift and any move back into `store.py` continue to fail closed.

---

## Dependency sketch

```mermaid
flowchart TD
  MS1[ARCH-MS-1 Charter]
  MS2[ARCH-MS-2 CI discovery]
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
4. ~~**ARCH-MS-5** — MCP read auth (P0; blocks ARCH-MS-22 formal closure if regressed)~~ (done — BUG-46 / PR #273)
5. **ARCH-MS-7** — package skeleton (unblocks scaffold chain)
6. **ARCH-MS-10** — flag census (unblocks inbox extraction)

---

## Changelog

| Date | Actor | Note |
|---|---|---|
| 2026-07-12 | ARCH-MS-1 | Initial tracker + ADR-0009 charter; baseline master `5305090` |
| 2026-07-12 | ARCH-MS-3 | CONSOL-7/9 closed; added `test_consol7_dead_surfaces.py`; gmail_source scoped to ARCH-MS-11 |
| 2026-07-12 | ARCH-MS-11 | Extracted source-independent inbox routing; renamed the IMAP adapter; retired `gmail_source.py` and rewired app/job/tests |
| 2026-07-12 | ARCH-MS-14 | Made `tests/` a package; added the direct-execution root + `src/` path shim; migrated all current nested tests and added a no-drift guard |
| 2026-07-12 | ARCH-MS-10 | Added executable `PM_*` census and CI gate; verified all tracked declarations have runtime defenders; documented CONSOL-9 deleted-name tombstones |
| 2026-07-12 | ARCH-MS-9 | Added `tests/test_arch_ms0_scaffold.py` Phase-0 proof gate (package imports + `create_task` callable + REST/MCP shared handler); deps ARCH-MS-7/8 merged |
| 2026-07-12 | ARCH-MS-24 | Added the fixed-baseline Phase 0 exit audit; moved project/access persistence into `src/switchboard/storage/repositories/access.py`; measured all exit criteria green |
