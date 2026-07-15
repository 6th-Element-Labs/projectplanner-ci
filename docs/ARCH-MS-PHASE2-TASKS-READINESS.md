# Phase 2 — Tasks service readiness (ARCH-MS-78)

**Status:** Accepted readiness package — **readiness-only** (no live Tasks process cut in Phase 2).  
**Board:** `project=switchboard` · task **ARCH-MS-78** · milestone `2c-tasks-cut-or-readiness` · mission `arch-ms-phase-2`  
**Exit-gate artifact:** [`docs/phase2/tasks_readiness.md`](phase2/tasks_readiness.md) (pointer; this file is canonical)  
**Charter:** [ADR-0011](decisions/0011-phase2-process-strangler.md) Decision 1 track **2C**  
**Auth playbook (service #1):** [AUTH-INDEPENDENCE-GATE.md](AUTH-INDEPENDENCE-GATE.md) · [auth-caddy-cutover-rollback.md](runbooks/auth-caddy-cutover-rollback.md)

---

## Decision

| Choice | Verdict |
|---|---|
| **Ship-now** (ARCH-MS-79 live cut) | **No** |
| **Readiness-only** (Phase 2 exit via this doc) | **Yes** |

**Rationale**

1. ADR-0011 2C: Phase 2 leaves Tasks **ready** for a later conditional cut — “not a second simultaneous process split.”
2. Auth Go path just finished (ARCH-MS-75…77). Charter rule is **one BC per cut**.
3. Tasks/claims are already strangulated at contracts, `application/`, REST, MCP, and repositories, but remain tightly coupled through root `store`, shared project SQLite, and Auth/write-binding. A process cut today would convert that coupling into network coupling (forbidden until ports + exclusive writers + ops proof exist — Auth’s ARCH-MS-82…84 lesson).
4. Phase 2 exit gate already accepts `tasks_service_present` **or** this readiness file (`scripts/arch_ms_phase2_exit_gate.py`).

A live Tasks cut (future ARCH-MS-79+) requires a **Tasks independence gate** analogous to Auth’s before any Caddy cutover.

---

## Boundary map — what is already extracted

| Layer | Status | Primary paths |
|---|---|---|
| Versioned contracts + JSON schemas | Done | `src/switchboard/contracts/tasks/v1.py`, `…/claims/v1.py`; `schemas/switchboard.task.*.v1.json`, `schemas/switchboard.claim.*_command.v1.json` |
| Application commands / queries | Core Done | `application/commands/{create,update,move}_task.py`, `queries/get_task.py`; `commands/{claim_task,claim_next,complete_claim}.py` |
| REST routers | Done (fat) | `api/routers/tasks.py`, `api/routers/claims.py` — still import root `store` / `auth` / (tasks) `agent` / `dispatch` |
| MCP tools | Done (partial) | `mcp/tools/tasks.py`, `mcp/tools/claims.py` — CRUD/claim trio via commands; search/comment/archive/deps still store-direct |
| Storage repositories | Done (façade-coupled) | `storage/repositories/tasks.py`, `…/claims.py` + protocols; root `store.py` re-exports |
| Service process | **Missing** | No `src/switchboard/services/tasks/` |
| Caddy / systemd Tasks unit | **Missing** | — |
| Independence / ops gate | **Missing** | Auth-only today |

### Commands already strangulated

| Concern | Command / query |
|---|---|
| Task create / update / move / get | `create_task`, `update_task`, `move_task`, `get_task` |
| Claim exact / next / complete | `claim_task`, `claim_next`, `complete_claim` |

### Still store-direct (not cut-ready without more adapters)

`list_tasks`, `add_comment`, `delete_task`, `archive_task`, `abandon_claim`, `revoke_claim`, `mark_task_offline_done`, task `dispatch` / `chat`, review findings attached under `/api/tasks/...`.

---

## HTTP contract sketch

Stable edge seams callers already use. A future Tasks uvicorn should preserve these shapes; MCP may stay on `:8111` until a later adapter proxies into Tasks HTTP.

### Task CRUD (+ siblings) — `/api/tasks*`

| Method | Path | Notes |
|---|---|---|
| GET | `/api/tasks` | Query: `project`, `workstream`, `status`, `assignee` → `store.list_tasks` |
| GET | `/api/tasks/{task_id}` | `get_task` query |
| POST | `/api/tasks` | Create body (`workstream_id`, `title`, …) + write-binding (`agent_id` / `system_*`) + `idem_key` |
| PATCH | `/api/tasks/{task_id}` | Sparse update; `depends_on` replace (`none` clears) |
| DELETE | `/api/tasks/{task_id}` | Soft-delete path via store |
| POST | `/api/tasks/{task_id}/archive` | Needs `write:system` |
| POST | `/api/tasks/{task_id}/move` | Cross-project move |
| POST | `/api/tasks/{task_id}/comment` | Store-direct today |
| POST | `/api/tasks/{task_id}/claims/{claim_id}/revoke` | Operator revoke |
| … | `…/review_*`, `…/dispatch`, `…/chat` | **Stay on monolith** in a first Tasks cut |

**Create sketch:** `workstream_id`, `title`, optional description / owner / dates / `depends_on` / `risk_level` / `is_blocking`, scopes typically `write:tasks`.

### Claims — `/txp/v1/*`

| Method | Path | Key body |
|---|---|---|
| POST | `/txp/v1/claim_next` | `agent_id`, lanes/capabilities/risk/budget, TTL, `idem_key`, work_session fields, mission filters, `project` |
| POST | `/txp/v1/claim_task` | `task_id`, `agent_id`, TTL ≥ 60, session binding, `project` |
| POST | `/txp/v1/complete_claim` | `claim_id`, `evidence` (branch/`head_sha`/PR/`executed_test_run`/…), optional `final_status`, `mission_project` |
| POST | `/txp/v1/abandon_claim` | `claim_id`, `reason` |
| POST | `/txp/v1/revoke_claim` | Operator fields |

Scopes: `write:ixp`. Completion evidence rules follow session policy (`code_strict` / `docs_review` / `offline_evidence`). Agents never mark **Done**; Done is merge/default-branch or verifier provenance.

### MCP parity (same semantic ops)

`search_tasks`, `get_task`, `create_task`, `update_task`, `add_comment`, `archive_task`, `move_task`, dependency helpers; `claim_next`, `claim_task`, `complete_claim`, `abandon_claim`, `revoke_claim`, `verify_offline_completion`. Prefer command paths where they exist.

---

## Coupling ledger — why not ship-now

| Risk | Observation |
|---|---|
| Shared project SQLite | Tasks/claims write the per-project board DB (tasks, `task_claims`, activity, leases, git state, deliverable links). Not Auth’s exclusive `project_registry` writer story. |
| `_store_facade()` density | Claims repository reaches work sessions, idempotency, provenance, CI/publication gates, messaging through the façade. |
| Auth / write-binding | Routers use `auth` + `resolve_principal` / `store.resolve_write_actor`. A Tasks process must not invent network Auth calls without ports. |
| Fat router attachments | `tasks.py` mounts review, dispatch, and chat — those BCs do not belong in service #2 day one. |
| Cross-repo imports | Tasks ↔ claims ↔ access ↔ coordination ↔ narrations enrichment. |

**Independence prerequisite (Phase 3 3B0, before ARCH-MS-90 Go):** ports for store façade consumers
(ARCH-MS-87); exclusive-writer matrix + Auth/write-binding via ports
([`TASKS-INDEPENDENCE-GATE.md`](TASKS-INDEPENDENCE-GATE.md), ARCH-MS-88); strip or seam
dispatch/chat/review; SQLite contention / rollback / API parity proof (ARCH-MS-89; Auth G5 analogue).

---

## Extract plan (dormant until Go)

Mirror Auth; keep dormant. Port hint: Auth `:8121`, skeleton `:8120` → Tasks **`:8122`**.

1. **Pre-cut (Tasks 2B0 analogue)**  
   Shrink façade usage; inject ports (work session, idempotency, provenance, messaging); decide monolith-owned siblings (review/dispatch/chat); document exclusive writers.

2. **Package**  
   Clone `src/switchboard/services/_skeleton/` → `services/tasks/`. Mount thin `/api/tasks*` CRUD (+ move/archive as decided) and `/txp/v1/claim_*` only. Reuse Auth’s factory + health pattern (`deploy/skeleton/`, `deploy/auth/`).

3. **Side-by-side**  
   Systemd example on `:8122`; hermetic parity (in-process baseline vs Tasks app) before any Caddy traffic.

4. **Caddy**  
   Commented fragment first; live cut = `handle /api/tasks*` → `:8122` **above**
   catch-all, plus **claim-only** TXP paths (`/txp/v1/claim_next`,
   `/txp/v1/claim_task`, `/txp/v1/complete_claim`, `/txp/v1/abandon_claim`,
   `/txp/v1/revoke_claim`) — **not** blanket `/txp/v1/*` (wakes and other TXP
   siblings stay on the monolith). Keep MCP on `:8111` unless redesigned.
   Carve monolith-only task subpaths (`…/dispatch`, `…/chat`, `…/review_*`)
   similar to Auth’s `/api/auth/me*`.

5. **DB sharing**  
   Shared project SQLite until contention measured. Fail closed to in-process if two-process writers are unsafe.

6. **Dual-strip**  
   Env gate patterned on `PM_AUTH_HTTP_PRIMARY=service` (e.g. `PM_TASKS_HTTP_PRIMARY=service`) after parity; hermetic TestClient may leave unset.

7. **Rollback / recovery**  
   Prefer restart Tasks unit / fix Caddy. Emergency remount on monolith. Structure after [`auth-caddy-cutover-rollback.md`](runbooks/auth-caddy-cutover-rollback.md).

---

## Acceptance (this task)

| Criterion | Met by |
|---|---|
| Boundary map | § Boundary map |
| HTTP contract sketch (CRUD + claim/complete) | § HTTP contract sketch |
| Extract plan (process, Caddy, DB, rollback) | § Extract plan |
| Ship-now vs readiness-only | § Decision → **readiness-only** |
| Phase 2 exit gate path | `docs/phase2/tasks_readiness.md` exists |

**Out of scope here:** implementing `services/tasks`, Caddy cut, or ARCH-MS-79.

---

## Related work

| Item | Role |
|---|---|
| ADR-0011 | Phase 2 charter; 2C readiness |
| ARCH-MS-73 | Service skeleton template |
| ARCH-MS-74 | Phase 2 exit gate (Tasks cut **or** readiness) |
| ARCH-MS-75…77 | Auth process cut playbook (service #1) |
| ARCH-MS-79 (board) | **Waived** for Phase 2 — see [`phase2/tasks_cut_waived.md`](phase2/tasks_cut_waived.md); future cut needs independence gate |
