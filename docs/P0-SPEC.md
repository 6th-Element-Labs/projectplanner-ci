# P0 Spec — Switchboard `IXP-core` reference hardening

- **Status:** Draft v0.1
- **Date:** 2026-06-28
- **Product:** Switchboard
- **Protocol target:** `IXP-core` reference implementation
- **Purpose:** close the current public write/auth gap and make the ProjectPlanner
  implementation an honest baseline for cross-LLM, cross-runtime agent coordination.

> P0 is not a scale rewrite. It is the floor: authenticated writes, bound agent identity,
> REST/MCP parity for the core protocol, idempotent mutations, presence/handshake support,
> and a conformance smoke test. After P0, Switchboard can safely claim "IXP-core reference
> implementation." Before P0, it is a useful prototype with an open-write security gap.
>
> P0 does not by itself wake an absent runtime. Always-on host registration, wake intents, and
> supervised runtime launch are specified separately in
> [`AGENT-HOST-SPEC.md`](AGENT-HOST-SPEC.md).
>
> P0 also does not finish the commercial identity product. It closes the unsafe write floor.
> The multi-human SaaS shell lives in the ACCESS lane: sessions, org/user/project roles,
> scoped MCP/API tokens, project-creation permissions, invites, subscriptions/agent
> entitlements, feedback-to-plan, and restricted UI controls.

---

## 1. Why P0 exists

Switchboard's product claim is that independent agents, running behind different models and
runtimes, can coordinate through one neutral substrate. The current code already contains much
of the coordination substrate: board state, activity log, MCP tools, file leases, directed
messages, decisions, agent state, GitHub sync, and delta polling.

The missing floor is trust and conformance:

- writes are open by default on the web API;
- MCP writes are protected only if `PM_MCP_TOKEN` is configured;
- agent ids are self-asserted strings, not bound to credentials;
- REST parity for the `IXP-core` operations is not yet a formal surface;
- mutating coordination calls do not yet share a required idempotency contract;
- presence/heartbeat is implied by leases/state, not first-class.

Those gaps are acceptable in a local prototype. They are not acceptable for a network-reachable
coordination layer, and they prevent honest `IXP-core` conformance.

---

## 2. P0 outcome

P0 is done when a fresh deployment can truthfully advertise:

1. Every network-reachable write path requires authentication.
2. Every write is attributed to an authenticated human, agent, or system actor.
3. Agents can perform the `IXP-core` session-start handshake:
   `register_agent -> inbox -> check/claim`.
4. Core coordination operations exist over both MCP and REST with the same semantics.
5. Retryable write operations accept idempotency keys and deduplicate safely.
6. A conformance smoke test proves the checklist against a clean local database.
7. Existing single-user/local usage remains simple with an explicit dev-mode switch.

---

## 3. Scope

### In scope

- Per-agent bearer credentials for MCP and REST/write APIs.
- A shared auth middleware used by web API, MCP write tools, and `/ixp/v1/*`.
- Actor binding: authenticated credential decides `actor`; callers cannot spoof it.
- First-class presence: register, heartbeat, list active agents.
- Generic resource leases: `file`, `port`, `build_dir`, `worktree`, `binary`, `branch`.
- Directed messages/signals with inbox, ack, status.
- Cursor delta feed.
- Idempotency for `claim`, `send`, and task-mutating endpoints.
- REST shim for `IXP-core`.
- Conformance smoke tests and deployment checks.
- Documentation updates showing current conformance.
- Protocol hooks needed by the Agent Host layer: authenticated presence, messages, monitors,
  and runner-compatible audit fields.

### Out of scope

- Multi-tenant SaaS onboarding, billing, or self-serve workspaces.
- Full OAuth/user-login UI and RBAC beyond the minimum write credential model.
- Subscription/agent entitlement management, invite flows, and permissioned project creation.
  These are required before external collaborators use hosted Switchboard, but they are ACCESS
  tasks rather than the `IXP-core` reference floor.
- `TXP` work dispatch (`claim_next`, see [`CLAIM-NEXT-SPEC.md`](CLAIM-NEXT-SPEC.md)) except
  for reserving compatible auth/idempotency shapes.
- Agent Host wake/launch control (see [`AGENT-HOST-SPEC.md`](AGENT-HOST-SPEC.md)) except for
  preserving compatible auth, monitor, and runner shapes.
- `OXP`/Tally cost ledger (see [`TALLY-SPEC.md`](TALLY-SPEC.md)) except for preserving
  actor/timestamp data it will need.
- Go/NATS/Postgres kernel extraction.
- Hard distributed locks. P0 keeps leases advisory.

---

## 4. Security model

### 4.1 Credential types

P0 introduces one credential table, sufficient for agents and humans:

```sql
CREATE TABLE IF NOT EXISTS principals (
  id            TEXT PRIMARY KEY,
  kind          TEXT NOT NULL,        -- agent | human | system
  display_name  TEXT NOT NULL,
  project       TEXT NOT NULL,        -- maxwell | helm | ...
  scopes        TEXT NOT NULL,        -- JSON array
  token_hash    TEXT NOT NULL,
  created_at    REAL NOT NULL,
  revoked_at    REAL
);
```

`token_hash` stores a salted hash only. Raw tokens are shown once at creation.

Minimum scopes:

| Scope | Allows |
|---|---|
| `read` | read board, docs, activity, deltas |
| `write:tasks` | create/update/delete tasks, comments, dependencies |
| `write:bug_intake` | submit structured BUG reports without generic task-write authority |
| `write:ixp` | register presence, claim/release resources, send/ack messages, set state |
| `write:system` | GitHub webhook/system maintenance actors only |
| `admin` | create/revoke principals and rotate credentials |

### 4.2 Actor binding

For every write:

- the server derives `actor` from the authenticated principal;
- caller-provided `actor`, `agent_id`, or `author` may be accepted only as payload data when
  it matches the authenticated principal or is explicitly delegated by scope;
- audit entries record both `actor` and, when relevant, the requested `agent_id`.

Reserved system actors:

- `github-webhook`
- `inbox`
- `scheduler`
- `summarizer`

System actors require a configured shared secret or local-only execution context. They are not
open unauthenticated writes.

### 4.3 Dev mode

P0 may keep local development easy, but the unsafe path must be explicit:

- `PM_AUTH_MODE=required` is the production default.
- `PM_AUTH_MODE=dev-open` may allow open writes only when bound to localhost or when an
  operator deliberately opts in.
- first-party web access uses password-backed principals plus an expiring
  `switchboard_session` cookie; adapter/MCP access continues to use bearer principals.
- first admin bootstrap is explicit: set `PM_BOOTSTRAP_ADMIN_LOGIN` and
  `PM_BOOTSTRAP_ADMIN_PASSWORD`, or call `/api/auth/bootstrap` from localhost / with
  `PM_BOOTSTRAP_TOKEN`, then remove the bootstrap secret.
- ACCESS role state is central to the project registry: orgs, users, org memberships,
  project ownership metadata, and project role grants. Project roles expand effective
  scopes during authentication, so a principal can be read-only in one project and
  contributor/admin in another.
- ACCESS-3 exposes project-scoped bearer-token lifecycle APIs. A `write:system` principal can
  create tokens for `human`/`user`, `agent`, `host`, and `system` principals; list token
  principals only in redacted form; and revoke a principal plus its live sessions. Unknown
  principal kinds, unknown scopes, and unknown projects fail closed. Raw tokens are returned
  only once at creation and are never written to activity logs.
- ACCESS-4 gates project creation and cross-project cleanup behind `write:system`, initializes
  project purpose/boundary/owner metadata, grants the creator explicit admin on the new project,
  and surfaces the boundary in project discovery, board payloads, working agreements, and
  agent startup contracts. `claim_next` remains project-scoped and unknown project IDs fail closed.
- startup logs must print a loud warning when `dev-open` is active.
- Caddy/production provision docs must set `PM_AUTH_MODE=required`.

---

## 5. REST surface

REST mirrors MCP semantics under `/ixp/v1`. All mutating calls require `Authorization:
Bearer <token>`.

### 5.0 Access Administration

- `GET /api/access/model?project=...`
  - requires `read`; returns org/project ownership, built-in roles, grants, and the current
    principal's project roles.
- `POST /api/access/project_role?project=...`
  - requires `write:system`; grants a built-in or explicit project role to a principal/user.
- `GET /api/access/tokens?project=...&include_revoked=false&kind=...`
  - requires `write:system`; lists redacted token principals, valid kinds, and role-to-scope
    definitions.
- `POST /api/access/tokens?project=...`
  - requires `write:system`; body accepts `kind`, `display_name`, optional `principal_id`,
    and either `role` or `scopes`; returns `{principal, token, token_returned_once}`.
- `POST /api/access/tokens/{principal_id}/revoke?project=...`
  - requires `write:system`; revokes the principal and any live sessions for that principal.
- `GET /api/projects`
  - requires `read`; returns project labels plus purpose/boundary/owner metadata.
- `POST /api/projects`
  - requires `write:system` on `project=switchboard`; creates an isolated project DB,
    records purpose/boundary metadata, and grants the creator admin on the new project.
- `GET /api/projects/{project}/repo_topology`
  - requires `read`; returns Project-scoped repository roles. `canonical` is the only
    code-truth/Done authority; `public_ci` is shared verification evidence only. Current
    `project` path ids are workspace compatibility aliases, not board-level repo authority.
- `POST /api/projects/{project}/repo_topology`
  - requires `write:system` on `project=switchboard`; configures Project-level canonical,
    shared public-CI, public mirror, and release evidence repository roles.
- `POST /api/tasks/{task_id}/move?project=...`
  - requires `write:system`; moves a task across isolated project DBs only with explicit
    source/destination projects and audited dependency handling.
- `POST /api/tasks/{task_id}/archive?project=...`
  - requires `write:system`; archives active task state with provenance and refuses active leases.

### 5.1 Presence

- `POST /ixp/v1/register_agent`
  - accepts optional `protocol` envelope; returns `protocol_compatibility`.
- `POST /ixp/v1/heartbeat`
- `GET /ixp/v1/agents?project=&lane=`

Example:

```json
{
  "project": "helm",
  "agent_id": "codex/CHART-8#a1b2",
  "runtime": "codex",
  "model": "gpt-5",
  "lane": "CHART",
  "task": "CHART-8",
  "ttl_s": 120
}
```

### 5.2 Resource leases

- `POST /ixp/v1/claim`
- `POST /ixp/v1/check`
- `POST /ixp/v1/release`
- `GET /ixp/v1/leases?project=`

`claim` is all-or-nothing. Conflicts return `retry_after_seconds`. Expired leases do not
conflict. `release` is idempotent.

### 5.3 Messages and signals

- `POST /ixp/v1/send`
  - accepts `ack_deadline_minutes` plus seconds aliases `ack_timeout_seconds` / `ack_timeout_s`.
- `GET /ixp/v1/inbox?project=&to_agent=&unacked=true&signal=`
- `POST /ixp/v1/ack`
- `GET /ixp/v1/message_status?project=&message_id=`

Signals: `heads_up`, `redirect`, `stop`.

P0 does not claim mid-token interruption. Signals are visible at the next tool-call/turn
boundary or via an adapter that polls before tool execution.

### 5.4 Delta feed

- `GET /ixp/v1/delta?project=&lane=&since_cursor=`

Returns `{cursor, updates}`. Empty updates must be cheap and deterministic.

---

## 6. MCP surface

Existing MCP tools stay, but P0 changes their guarantees:

- all write tools require a valid bearer token when `PM_AUTH_MODE=required`;
- write tools use the authenticated principal as actor;
- new MCP tools mirror the REST additions:
  - `register_agent`
  - `heartbeat`
  - `list_active_agents`
  - generic `claim`, `check`, `release`, `list_active_leases`
  - `send`, `inbox`, `ack`, `message_status`
  - `list_scoped_tokens`, `create_scoped_token`, `revoke_scoped_token`
  - `get_delta`
  - `submit_bug` for BUG intake without generic task-write authority
- legacy names (`claim_files`, `send_agent_message`, `get_lane_delta`) may remain as
  compatibility wrappers over the generic implementations.

---

## 7. Idempotency

Retryable mutating operations accept `idem_key`:

- `claim`
- `send`
- `update_task`
- `create_task`
- `add_comment`
- `ack`

Schema:

```sql
CREATE TABLE IF NOT EXISTS idempotency_keys (
  project       TEXT NOT NULL,
  idem_key      TEXT NOT NULL,
  operation     TEXT NOT NULL,
  actor         TEXT NOT NULL,
  request_hash  TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at    REAL NOT NULL,
  PRIMARY KEY (project, idem_key)
);
```

Rules:

- same `(project, idem_key)` + same request body returns the original response;
- same `(project, idem_key)` + different request body returns `409 idem_key_conflict`;
- idempotency rows may expire after a configurable retention window, default 24 hours;
- system actors also use idempotency for webhook-derived writes when possible.

---

## 8. Conformance target

P0 targets `IXP-core` only. No `+TXP` or `+OXP` claims.

| Requirement | P0 target |
|---|---|
| Workspace scoping fail-closed | Required |
| Deterministic serialization | Required; already partially present |
| Authenticated writes | Required |
| Actor recorded from authenticated identity | Required |
| Idempotent retryable writes | Required |
| Presence register/heartbeat/list | Required |
| Advisory resource leases | Required |
| Directed messages/signals | Required |
| Session-start handshake | Required |
| Cursor delta feed | Required |
| Append-only activity log | Required |

P0 should add a generated or hand-maintained `docs/IXP-CONFORMANCE.md` with:

- implementation status per checklist item;
- known deviations;
- last verification command and date.

---

## 9. Implementation plan

### P0-1 — Principal/auth substrate

Add principal storage, token creation/revocation helpers, token hashing, and scope checks.

Acceptance:

- can create an agent token for `project=helm`;
- raw token is not stored;
- revoked token fails;
- unknown project fails closed.

### P0-2 — Shared auth middleware

Centralize request auth so web API, MCP, and REST use the same principal resolver.

Acceptance:

- unauthenticated write returns `401` in `required` mode;
- insufficient scope returns `403`;
- reads can remain open if configured;
- dev-open mode is explicit and loudly logged.

### P0-3 — Actor binding and audit

Ensure all write paths record authenticated actor.

Acceptance:

- caller cannot spoof `actor`;
- activity rows include actor + timestamp;
- MCP writes no longer all collapse to generic `MCP` when a principal exists;
- GitHub webhook remains a reserved system actor with signature verification.

### P0-4 — REST `/ixp/v1` parity

Implement REST operations for presence, leases, messages/signals, and deltas.

Acceptance:

- REST and MCP operations return semantically equivalent results;
- error shapes are machine-readable;
- all write operations enforce auth.

### P0-5 — Presence and handshake

Add `agent_presence` table and handshake helpers.

Acceptance:

- registered agent appears in `list_active_agents`;
- heartbeat extends expiry;
- stale agents disappear after TTL;
- session-start smoke can run `register -> inbox -> check -> claim`.

### P0-6 — Generic resource leases

Generalize file leases to resource leases while preserving `claim_files` wrappers.

Acceptance:

- resources support `file`, `port`, `build_dir`, `worktree`, `binary`, `branch`;
- conflicts include holder, task, expiry, `retry_after_seconds`;
- expired leases do not conflict;
- release is idempotent.

### P0-7 — Idempotency

Add the idempotency table and helpers.

Acceptance:

- duplicate `claim`/`send` with same key returns original response;
- same key with a changed body returns conflict;
- tests cover retry after simulated network failure.

### P0-8 — Conformance smoke

Add a single command that validates the P0 floor against a clean temp database.

Suggested command:

```bash
python3 test_ixp_core_conformance.py
```

Acceptance:

- passes locally without external network;
- covers auth required, workspace fail-closed, presence, claim conflict, message ack,
  delta cursor, idempotency, and audit actor;
- prints a compact checklist suitable for CI.

### P0-9 — Docs and deployment

Update docs and provision steps.

Acceptance:

- `docs/IXP-CONFORMANCE.md` exists;
- `docs/MCP.md` describes required auth;
- `deploy/PROVISION.md` sets `PM_AUTH_MODE=required`;
- `README.md` no longer implies open writes are safe on a public host.

---

## 10. Verification matrix

| Check | Command or method |
|---|---|
| Local conformance | `python3 test_ixp_core_conformance.py` |
| MCP write auth | call `update_task` without token -> unauthorized |
| REST write auth | `curl -X POST /ixp/v1/send` without token -> `401` |
| Actor binding | attempt spoofed actor -> activity shows authenticated actor |
| Workspace isolation | token for `helm` cannot write `maxwell` |
| Idempotency | repeat same `idem_key`; verify one activity side effect |
| Presence TTL | register, heartbeat, wait expiry, list active |
| Lease conflict | two agents claim same file; second gets conflict/backoff |
| Signal ack | send `stop`, inbox, ack, sender status sees ack |
| Delta feed | first call cursor 0 returns updates; second call same cursor returns empty |

---

## 11. P0 exit criteria

P0 is complete only when all are true:

- production configuration fails closed for writes without credentials;
- a Claude Code agent and a Codex/generic REST agent can both authenticate, register, claim,
  send/ack, and poll deltas in the same workspace;
- the conformance smoke passes in CI or the deploy checklist;
- docs clearly mark the reference implementation as `IXP-core` conformant, or list the exact
  remaining deviations;
- no new product surface claims `+TXP`, `+OXP`, Tally, hard-stop, or mid-token interrupt
  guarantees.

---

## 12. Dependencies

- `PRD-AGENT-COORDINATION-LAYER.md` for product scope and P0 positioning.
- `IXP-SPEC.md` for the normative wire contract.
- `SWITCHBOARD-DESIGN-LOG.md` for the decision trail and risk rationale.
- Existing implementation files: `app.py`, `mcp_server.py`, `store.py`, `deploy/PROVISION.md`.
