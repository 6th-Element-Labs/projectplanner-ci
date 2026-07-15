# Auth independence gate — ownership, outage policy, secrets (ARCH-MS-83)

**Status:** Accepted decisions for deliverable `arch-ms-phase-2` independence gate.  
**Relates to:** [ADR-0011](decisions/0011-phase2-process-strangler.md) ·
[`AUTH-MICROSERVICE-DESIGN.md`](AUTH-MICROSERVICE-DESIGN.md) · board tasks ARCH-MS-82…84 ·
conditional cut ARCH-MS-75.

This document records **Go/No-Go inputs** for an Auth process cut. It does not authorize the cut.

---

## Decision 1 — Exclusive writers (one owner per table)

Shared SQLite (`project_registry.db`) is allowed **in-process** while each table has a single
writer BC. Dual writers on the same table are a **No-Go** for process cut (ADR-0011 Decision 2 #5).

| Table / concern | Exclusive writer | Readers | Notes |
|---|---|---|---|
| `users` | **Auth** (`create_user`, `ensure_identity`) | Access, projects, MCP | Access `ensure_user` delegates to Auth (ARCH-MS-83). |
| `user_auth` | **Auth** | Auth only | Passwords, superadmin, login stats. |
| `auth_sessions_v2` | **Auth** | Auth | Revocable sessions; verify always hits this table. |
| `password_resets` | **Auth** | Auth | Single-use reset tokens. |
| `auth_login_events` | **Auth** | Auth / ops | Rate-limit + audit. |
| Auth table DDL | **Auth** (`auth.store.init`) | — | Additive Auth tables only. |
| `project_role_grants` | **Access** (`grant_project_role` / revoke) | Auth (access resolution) | Grants are Access BC; Auth reads for deny-by-default project lists. |
| Registry base DDL (`users` schema, orgs, …) | **Registry** (`db/schema.py` / `init_project_registry`) | Auth, Access | Schema owner ≠ row owner; row inserts for `users` still go through Auth. |

**Monolith rule:** do not `INSERT INTO users` outside Auth. Scripts (e.g. historical migrate)
must call Auth store APIs.

**Process-cut implication:** Before Auth becomes a second uvicorn sharing the registry DB, confirm
Access grant writers either (a) stay co-located with exclusive-table discipline, or (b) move behind
an Auth/Access port so the Auth process is never a second writer on Access tables. Two writers on
one SQLite file across processes is a No-Go until measured (ARCH-MS-84 ops harness).

---

## Decision 2 — Auth-down behavior (fail-closed; no offline JWT trust)

`session.verify` always:

1. Verifies JWT signature + `exp` (crypto), then
2. Loads `auth_sessions_v2` + fresh user from the registry (**DB required for revocation**).

There is **no** offline-JWT accept path (JWT claims alone never authorize).

| Surface when Auth / registry is unavailable | Required behavior |
|---|---|
| Browser session verify (`current_user` / middleware) | **Fail closed** → unauthenticated (401 / login redirect). Never trust JWT claims alone. |
| Login / register / password reset | **Fail closed** → 5xx/503 (or AuthError from store); do not invent local sessions. |
| Bearer MCP / API principals | Prefer **fail closed** on store errors; do not silently widen to env-token fallbacks that mask Auth outage (call out any remaining fall-open as a Go risk). |
| Caching | No long-lived “Auth-up” cache that survives registry outage for authorize decisions. Short-lived positive caches (if added later) must re-check revocation. |
| Timeouts | Registry/auth store writes already retry once on transient lock; persistent errors raise. |
| Revocation | Requires live `auth_sessions_v2` row; revoked/expired/missing → unauthenticated. |
| `/health` liveness | May stay green; Auth readiness is covered by uptime login/session probe — do not treat liveness as Auth-up. |

**Rejected:** offline JWT validation as a primary Auth-down mode (cannot revoke; converts
in-process revocation into stale network trust).

---

## Decision 3 — Production secrets fail-fast

| Mode | Signing secret |
|---|---|
| `PM_AUTH_MODE=required` (default / production) | **Require** non-empty `PM_JWT_SECRET` before issuing or verifying a non-empty session. Raise `AuthSecretError` — no silent `"dev-insecure-…"` fallback, no `PM_AUTH_TOKEN` substitute. Empty cookies short-circuit as unauthenticated without reading a secret. |
| `PM_AUTH_MODE=dev-open` | Allow `PM_JWT_SECRET`, else `PM_AUTH_TOKEN`, else explicit local DEV fallback (tests / laptop only). |

Implemented in `src/switchboard/api/routers/auth/session.py` (`_secret`, `require_production_secret`).
Call `require_production_secret()` from ops/boot scripts if you want fail-closed before bind.

---

## Go / No-Go checklist (inputs for ARCH-MS-75)

Fill with measured evidence from ARCH-MS-84 before starting ARCH-MS-75.

| # | Input | Go requires | Status |
|---|---|---|---|
| G1 | Exclusive writers per Decision 1 | No dual `users` / session / reset writers; Access-only grants | Code: Auth `ensure_identity` + fail-fast secrets (ARCH-MS-83) |
| G2 | Auth-down fail-closed | Verify never authorizes without DB; matrix above holds under fault injection | Pending ARCH-MS-84 harness |
| G3 | Secrets fail-fast | Production boot refuses missing `PM_JWT_SECRET` | Code + `tests/test_arch_ms83_auth_ownership.py` |
| G4 | Ports independence | ARCH-MS-82 import lint green | Done |
| G5 | Ops proof | SQLite contention, second uvicorn budget, Caddy rollback, 401/403 parity | Pending ARCH-MS-84 |
| G6 | Operator decision | Explicit Go recorded on board | Pending |

**No-Go (keep Auth in-process)** if any of G1–G5 fail, or if cutting would create two writers /
network-wrap unresolved coupling. No-Go is a valid Phase 2 exit path (ADR-0011 Decision 4).
