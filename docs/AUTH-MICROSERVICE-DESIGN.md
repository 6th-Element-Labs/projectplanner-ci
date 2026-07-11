# Auth / User-Management Service — Design (Service #1 of the microservices migration)

Status: **proposed** · Owner: Steve · Pattern source: ActionEngine `engine/services/user_management` + `engine/api/users_api.py`

## Why
Today login is **project-scoped**: you sign in *to a project*, sessions live in that project's SQLite DB, and cross-project access is gated on per-project roles. That's why logging in as `steve` on Switchboard leaves Maxwell locked. We want ActionEngine's model:

- **Log in once, globally** — email + password, no project picker.
- **Backend decides what you can see** — from your identity + grants, not a choice at login.
- **Self-service signup** — new account → **empty** workspace (sees no existing projects until granted).
- **Owner sees everything** — a superadmin flag (you), not a pile of per-project grants.

## Strangler approach (how we go microservices without a big-bang)
The monolith keeps running. We extract **one bounded context at a time** into a service with a clean seam (route → service → contracts → store), route the live app through it, delete the old path, repeat. **Auth is service #1.** Later candidates (each its own PR): Tasks CRUD, Deliverables, Access/Tokens, Ingest/Inbox, Tally/Economics.

## Service #1 structure (mirrors ActionEngine)
```
services/auth/
  contracts.py   # Pydantic request/response models (RegisterBody, LoginBody, UserOut, SessionOut)
  service.py     # AuthService: business logic (hashing, user CRUD, access resolution) — no HTTP
  session.py     # SessionManager: issue/verify the session cookie
auth_api.py      # FastAPI routes: /api/auth/* — thin, calls AuthService
```
The routes are thin; all logic lives in `AuthService`; storage stays in `store.py` behind service methods (so we can later split the DB without touching callers).

## Data model — **global** users, sessions, grants (in `project_registry.db`, the shared DB)
```
users            (id, email UNIQUE, display_name, password_hash bcrypt,
                  is_superadmin BOOL, status, created_at, last_login, login_count)
auth_sessions    (token_hash, user_id, expires_at, created_at, ip, user_agent)   # global, not per-project
project_grants   (user_id, project_id, role, scopes, granted_by, granted_at)     # deny-by-default
```
Reuses the existing `grant_project_role` semantics but keyed by global `user_id`. Migration seeds `users` from existing per-project password principals and marks **you** `is_superadmin=true`.

## Endpoints (ActionEngine-parity)
| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/api/auth/register` | `{email, display_name, password≥8}` | Creates user, **no grants**, auto-login. Deny-by-default. |
| POST | `/api/auth/login` | `{email, password, remember_me?}` | **No project.** Sets session cookie. |
| GET | `/api/auth/session` | — | Returns current user + accessible projects, or 401. |
| POST | `/api/auth/logout` | — | Revokes session, clears cookie. |
| GET | `/api/projects` | — | **Filtered**: superadmin → all; else only `project_grants` for the user. |

Session: httpOnly cookie. **Match ActionEngine → HS256 JWT `taikun_session`** (`{sub, email, is_superadmin, iat, exp}`, 7d / 30d remember-me), so the two products share the pattern. Server also keeps `auth_sessions` for revocation.

## Access rules
- New user: `project_grants` empty → `/api/projects` returns `[]` → sees nothing. Can't see anyone else's projects.
- Superadmin (you): bypasses grants → sees all projects, can grant others.
- Granting: `POST /api/access/project_role` (exists) now writes global `project_grants`.

## Frontend (Tabler, same as ActionEngine's `login.html`)
- `login.html`: email + password + "remember me" + **"Create account"** link. **Remove the Project field.**
- `signup.html`: email + display name + password → `/api/auth/register` → land in an empty app that says "No projects yet — ask an owner for access."
- App boot calls `/api/auth/session`; the project switcher is populated from the filtered `/api/projects`.

## Cutover (safe, reversible) — ✅ COMPLETED (ACCESS-16 / PR #300)
> **Historical:** this cutover is finished. The `PM_GLOBAL_AUTH` feature flag and the legacy
> per-project login path have since been removed; global auth is now the single live system.
> The steps below are retained as the original design record.

1. Ship the service + tables **alongside** the current auth (feature flag `PM_GLOBAL_AUTH`).
2. Migrate principals → `users`; mark you superadmin.
3. Flip the login page to global; keep the old per-project path working for one release.
4. Verify (headless): signup→blank, login→your projects, superadmin→all, new user→denied.
5. Remove the old per-project login path.

## Out of scope for #1 (later services / follow-ups)
Magic-link + password-reset (ActionEngine has them; add after core), full user-admin CRUD UI, splitting Tasks/Deliverables into their own services.
