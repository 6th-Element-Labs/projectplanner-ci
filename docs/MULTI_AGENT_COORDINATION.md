# Multi-agent coordination — feature spec

**Derived from first-hand session data (Helm build, 2026-06-26/27):** six parallel Claude Code
agents sharing one repo, one planning board, and one `main` branch for a full work day.
The coordination failures below are observed, not hypothetical.

> This doc is the *agent-coordination* layer on top of the operator roadmap in
> [`AGENT_OPERATOR_FEATURES.md`](AGENT_OPERATOR_FEATURES.md). That doc covers how a single
> agent drives a plan; this one covers what happens when many agents drive it at once.
> Architectural decisions live in [`decisions/0001-multi-agent-coordination-primitives.md`](decisions/0001-multi-agent-coordination-primitives.md).

---

## The coordination failures (raw session data)

Every feature below traces to a specific pain hit during the six-agent Helm session:

| Pain | Root cause | Lost time |
|---|---|---|
| Two agents edite the same file simultaneously | No file-lock signal anywhere on the board | ~30 min hand-reconciliation |
| Board said task X "Not Started"; it had already merged | No git↔board sync | ~20 min per stale-status discovery |
| Agent repeatedly re-fetched `main` to discover it had advanced | No "main moved" push | constant background overhead |
| Agent needed a dep from another epic that wasn't on the plan; buried as a prose comment | No structured cross-agent request primitive | multi-turn board spelunking |
| Port `:10110` held by one agent's build; another's harness ran and emitted 3 false FAILs | No shared-resource broker | false positives, root-cause detective work |
| "Why is the alarm schema frozen?" required reading 5 different doc/ADR/comment sources | No queryable decisions log | re-derivation from scratch each session |

**Design principle:** any coordination pain that required manual cross-referencing (git log,
worktree list, lsof, board comments) should be replaced by a single structured board signal.

---

## Claude Code desktop app compatibility

All features in this spec are request-response MCP tools and server-side background processes.
Claude Code's MCP client (desktop and CLI — same binary) is request-response only: it makes
point-in-time tool calls; it does not open an SSE subscription between calls to receive
server-push events. The practical consequence per feature:

| Feature | Works with Claude Code? | Notes |
|---|---|---|
| Deterministic serialization (§T0) | ✅ fully | Server-side only; agent never sees the change |
| `get_lane_delta` (§1.4) | ✅ fully | Standard request-response tool |
| `retry_after_seconds` on lease conflict (§1.1) | ✅ advisory | LLM reads the hint, uses `Bash(sleep N)`; no protocol enforcement |
| Haiku epistemic summarizer (§3.4) | ✅ fully | Server-side background job; agent sees result via `get_task` |
| MCP SSE push notifications (§3.2) | ⚠️ no | Claude Code doesn't subscribe to SSE stream; use scheduler+notify instead |

---

## Tier 0 — Deterministic serialization (token economics, cross-cutting)

This is the cheapest change with the widest impact. Applies to every MCP tool response.

**The problem:** Anthropic's prompt cache is keyed on exact bytes. If `board_summary` returns
`{"updated_at": "2 minutes ago", ...}` on one call and `{"updated_at": "3 minutes ago", ...}`
on the next, the cache misses on every call. On a six-agent session where each agent polls
`board_summary` at startup, that's six separate full-payload API charges instead of five
cache hits.

**The fix — three rules enforced in `mcp_server.py` response serialization:**

1. **`sort_keys=True` on all JSON responses.** `json.dumps(obj, sort_keys=True)` everywhere.
   One-line fix; makes key order deterministic across calls and Python dict hashing.

2. **Strip all volatile fields before serialization.** Fields that change between calls without
   carrying semantic value must be removed or normalized:
   - ❌ `updated_at_human: "2 minutes ago"` → omit entirely (raw ISO timestamp is fine)
   - ❌ `generated_at: datetime.utcnow()` → omit from tool responses
   - ❌ `server_version: "1.0.3-dev+abc1234"` → normalize to stable release tag only
   - ✅ `updated_at: "2026-06-27T07:30:00Z"` — stable ISO timestamp, keep it

3. **Static data before dynamic data in every response.** Tool definitions and schema
   descriptions (the stable portion) must appear before per-task status fields (the dynamic
   portion). This maximizes the cacheable prefix length.

**Where the cache hit actually lands:** Claude Code caches the conversation prefix — system
prompt + tool definitions + early turns. If two agent sessions start with the same system
prompt, the same tool definition schemas, and the same early `board_summary` response, they
share a cache hit on every subsequent API call that includes those early turns. Deterministic
serialization is what makes "same response" true across sessions and agents.

**Implementation:** wrap the FastMCP tool response serializer; don't modify individual tools.
One place, covers all tools.

---

## Tier 1 — Collision prevention (highest ROI)

### 1.1 · Live presence + soft file locks

**What:** when an agent begins editing a file (or claims a task), it broadcasts a lease:
`{ agent_id, epic, task_id, files: [...], claimed_at, ttl_minutes }`.
Other agents query "who holds `web/collision.js`?" before they edit. On TTL expiry or
explicit release, the lease evicts.

**Why it's different from comments:** comments are fire-and-forget. A lease is a typed
object with a state machine — it can be queried, renewed, broken, and expired. Agents can't
skim a comment in real time; they can call `check_lease('web/collision.js')` before a write.

**Data model (add to `store.py`):**
```sql
CREATE TABLE file_leases (
  id          TEXT PRIMARY KEY,
  agent_id    TEXT NOT NULL,
  task_id     TEXT REFERENCES tasks(id),
  files       TEXT NOT NULL,     -- JSON array of paths
  claimed_at  TEXT NOT NULL,
  ttl_minutes INTEGER DEFAULT 30,
  released_at TEXT
);
```

**MCP tools to add:**
- `claim_files(task_id, files, ttl_minutes)` → lease id or `{conflict: agent_id, task_id, retry_after_seconds: 60}`
- `release_files(lease_id)`
- `check_files(files)` → list of `{file, held_by, task_id, expires_at}` for any held files
- `list_active_leases()` → board-wide presence view

**`retry_after_seconds` on conflict responses:** when `claim_files` returns a conflict, the
response explicitly includes `retry_after_seconds` (default 60). Claude Code's LLM reads this
field and uses `Bash(sleep 60)` before retrying — eliminating the spin-retry reasoning loop
where the agent has to invent its own backoff. This is advisory (no protocol enforcement), but
it removes the "what should I do?" token burn. The board computes `retry_after_seconds` from
the conflicting lease's remaining TTL: `max(30, remaining_ttl_seconds / 2)`.

**Enforcement model:** advisory, not hard. An agent that calls `claim_files` and gets a
conflict decides what to do (wait, post a comment, pick a different scope). The board does
not block writes — it surfaces information so agents can make better decisions.

**UI surface:** a "Who's working" sidebar chip per task card (like a GitHub avatar trail),
and a board-wide "Active agents" strip at the top. Stale leases (TTL elapsed without
`release_files`) shown in a dimmed state.

---

### 1.2 · Board ↔ git/PR auto-sync

**What:** when a PR merges (GitHub webhook or poll), the board automatically:
1. Links the PR to any task whose id appears in the branch name or commit message
   (`claude/ENGINE-11`, `[NATIVE-12]`, `ENGINE-8: fix harness`).
2. Advances the task status: `In Progress` → `In Review` on PR open; `In Review` → `Done` on merge.
3. Logs the PR URL + merge SHA in the task's activity log.

**Why the board-versus-code ambiguity is expensive:** in the session, `ENGINE-11`/`CHART-8`
were "Not Started" on the board while already merged. Every agent that read those statuses
had to separately verify with `git log`. With auto-sync, the board is always authoritative.

**Implementation:**
- GitHub webhook → `/api/github/webhook` → parse `pull_request.merged` event → extract task
  ids from branch name + commit messages via regex `[A-Z]+-\d+` → `update_task` + `add_comment`.
- Fallback: poll `git log --oneline origin/main..HEAD` in the scheduler if webhook is not
  configured (self-hosted setups).

**MCP addition:** `get_task` response gains `{ prs: [{number, url, merged_at, sha}] }` field.

---

### 1.3 · "Main moved / you're behind" event push

**What:** when `main` advances, emit a push event to any agent that has an open lease on a
file changed by the new commits. The event payload: `{ files_changed, new_sha, prs }`.

**Why polling is expensive:** the six-agent session re-fetched `main` constantly. Each fetch
is a round-trip; the actual signal ("these specific files changed") is almost always "not
yours." The push inverts it: silence means nothing relevant changed; a notification means
"rebase before you merge."

**Implementation:** the scheduler (already in Phase 4 as `systemd timer`) runs `git fetch` +
`git diff --name-only` every N minutes and compares against active leases. Hits get a
`notify` to the claiming agent_id's channel.

---

### 1.4 · `get_lane_delta(lane, since_cursor)` — efficient poll replacement

**What:** instead of calling `board_summary` (full board) or `get_task` per task to detect
changes, agents call `get_lane_delta` with their workstream and a sequence cursor:

```json
// Request: get_lane_delta(lane="ENGINE", since_cursor=4821)
// Response (when nothing changed): {"cursor": 4821, "updates": []}
// Response (when something changed):
{
  "cursor": 4847,
  "updates": [
    {"task_id": "ENGINE-11", "status": "Done", "pr": 43, "merged_sha": "4fc3dc3"},
    {"task_id": "ENGINE-8",  "status": "In Review", "pr": 35}
  ]
}
```

**Why a cursor, not a timestamp:** timestamps have clock-skew across agent sessions and the
board backend. A monotonic sequence cursor (the activity log's `rowid`) is unambiguous: "give
me everything after event 4821" is a single indexed `WHERE rowid > 4821` query. The agent
stores its cursor between polls; the response includes the new cursor to advance to.

**Token savings:** a `board_summary` response is ~2,000–5,000 tokens for a 200-task board.
A `get_lane_delta` response is 0 tokens when nothing changed (empty `updates` array) and
~50–200 tokens when something did. Agents polling every 5 minutes over a 2-hour session:
`board_summary` = 24 × 3,500 = 84,000 tokens; `get_lane_delta` = ~22 zero-updates + 2
small payloads ≈ 2,000 tokens.

**Tool description (what agents see):**
> Use `get_lane_delta` for routine "has anything changed?" checks. Use `board_summary` only
> once at session start for full orientation. Never use `board_summary` in a polling loop.

**Implementation:** `SELECT rowid, task_id, field, old_val, new_val FROM activity_log
WHERE rowid > ? AND task_id LIKE 'ENGINE-%' ORDER BY rowid` — the activity log already
exists (Phase 0); this is a filtered query over it.

---

## Tier 2 — Structured async signals

### 2.1 · Directed IM with ack/read-receipts

**What:** an agent sends a directed message to another agent (or agent_id on a task) and
gets back a structured ack:
`{ to, re_task, message, requires_ack, ack_deadline }` → recipient gets a queued
notification; sender can query `{acked_at, response}` before proceeding.

**Why this is different from task comments:** task comments are a bulletin board — fire and
forget. The ack is the point: "I need to know this landed before I edit `collision.js`."
`add_comment` has no read-receipt; a message primitive does.

**MCP tools:**
- `send_agent_message(to, re_task, message, requires_ack, ack_deadline_minutes)` → message_id
- `ack_message(message_id, response)` — called by the receiving agent
- `list_unacked_messages(agent_id)` — receiving agent's inbox
- `get_message_status(message_id)` — sender polls for ack

**Data model:**
```sql
CREATE TABLE agent_messages (
  id            TEXT PRIMARY KEY,
  from_agent    TEXT NOT NULL,
  to_agent      TEXT NOT NULL,
  task_id       TEXT REFERENCES tasks(id),
  message       TEXT NOT NULL,
  requires_ack  INTEGER DEFAULT 0,
  ack_deadline  TEXT,
  sent_at       TEXT NOT NULL,
  acked_at      TEXT,
  ack_response  TEXT
);
```

---

### 2.2 · First-class cross-task blocking requests

**What:** when agent A's work is blocked on agent B's task completing, A creates a typed
blocking request rather than burying it in a comment:
`{ from_task, to_task, reason, requested_by_agent, status: pending|ack'd|resolved }`.

The blocking request appears as a badge on the blocking task ("1 pending dep request") and
on the blocked task ("waiting on X"). When the blocking task closes, all pending requests
get a push event.

This is different from `depends_on` (which is planned in the seed data): `depends_on` is
set at plan-creation time by the planner. A blocking request is raised at runtime by the
agent actually doing the work when it discovers an unplanned dep.

**MCP tools:**
- `request_dep(from_task, to_task, reason)` → request_id
- `ack_dep_request(request_id)` — blocking agent acknowledges
- `list_dep_requests(task_id)` — both directions (requests I made, requests made of me)

---

### 2.3 · Decisions log (queryable, append-only)

**What:** a structured log of "why" decisions made during agent work — lighter than a full
ADR, more durable than a board comment. Each entry:
`{ task_id, agent_id, title, rejected_alternatives, rationale, timestamp }`.

**Why this matters:** in the session, the `isStyleLoaded()` vs `getStyle()` correctness
analysis lived in a PR comment that nobody reads. Next agent touching `collision.js` will
re-derive the same choice. A decisions log is queryable: `search_decisions('collision.js')`
returns the rationale before any agent edits that file.

**This is not a wiki.** A wiki is free-form and requires maintenance. The decisions log is
append-only (no editing, only supersede), structured, and attached to tasks — it stays
coherent without curation.

**MCP tools:**
- `log_decision(task_id, title, rationale, rejected_alternatives)` → decision_id
- `search_decisions(query)` — semantic search (same RAG as `doc_search`)
- `list_decisions(task_id)` — all decisions on a task

**`ask_plan` integration:** the decisions log is indexed into the RAG corpus; `ask_plan`
answers can cite decisions the same way they cite `plan-docs/*.md`.

---

## Tier 3 — Infra primitives

### 3.1 · Shared-resource broker (ports, build dirs, worktrees)

**What:** a leasing registry for non-file shared resources — TCP ports, build directories
(`/tmp/helm-opencpn`), worktrees (`/tmp/helm-*/`). Same TTL/release model as file leases
(§1.1) but keyed by resource type + name rather than file path.

**Why this is a real problem:** the session had three distinct infra-contention failures —
port `:10110` held by one agent's binary caused another's test harness to emit false FAILs;
the shared `/tmp/helm-opencpn` build clone got clobbered by parallel cmake rebuilds; the
primary checkout had surprise worktree state from prior agents. All three required
detective work to root-cause.

**MCP tools:**
- `claim_resource(type, name, task_id, ttl_minutes)` → `{ok}` or `{conflict: ...}`
- `release_resource(type, name)`
- `list_resources()` → active resource leases board-wide

**Resource types:** `port`, `build_dir`, `worktree`, `binary` (for "I'm currently running
this binary — don't rebuild it").

---

### 3.2 · Event subscriptions

**What:** instead of polling `board_summary` / `get_plan_signals` repeatedly, agents register
interest in event types and receive push notifications:

- `task.status_changed` (for their deps)
- `task.dep_request.resolved`
- `file.lease.released` (for a file they're waiting on)
- `main.advanced` (filtered to their claimed files)
- `pr.merged` (for their task)
- `message.received` (directed IM)

**Why polling is wasteful:** in the session, agents polled the board 20-30 times to discover
events that could have been a single push. On a large multi-agent session this is real token
waste and latency.

**Implementation:** a lightweight `subscriptions` table (agent_id, event_type, filter, channel).
The scheduler (already running) emits events; `notify.send` delivers them to the channel
(Slack webhook / board notification).

**Claude Code compatibility note:** MCP SSE push (server-to-client notifications over the
`GET /mcp` SSE stream) does NOT work with Claude Code — its MCP client is request-response
only and does not subscribe to the SSE stream between tool calls. Use the scheduler+notify
path (Slack/Gmail delivery) as the push channel, not MCP notifications. The `subscriptions`
table registers which channel to deliver to per agent; MCP notifications are not the delivery
mechanism.

---

### 3.3 · Per-agent "current state" structured field

**What:** a machine-readable snapshot per task, distinct from free-text comments:
```json
{
  "agent_id": "claude/native-1",
  "built": ["docs/CLIENT-LICENSE-REGISTER.md"],
  "verified": ["test-engine.sh 17/17 pass"],
  "blocked_on": "CONTRACT-12 frozen",
  "next": "NATIVE-2 WKWebView shell"
}
```

**Why `status` is too coarse:** the board has `Not Started / In Progress / Done` which is
fine for humans. Agents need `I've done X, verified Y, blocked on Z, next is W` — otherwise
every new agent session starts from zero context.

**MCP tools:**
- `set_agent_state(task_id, built, verified, blocked_on, next_action)` → replaces prior state
- `get_agent_state(task_id)` → returns the latest structured state

**Not a replacement for comments.** Comments are the narrative audit trail. Agent state is a
snapshot of the agent's working memory — queryable by other agents planning their next move.

---

### 3.4 · Haiku epistemic summarizer (context squashing)

**What:** a background job (systemd timer, already running from Phase 4) that runs after
every batch of task activity and compresses the activity trail into a dense, pre-chewed
truth matrix per task. Stored in a `task_summaries` table and returned by `get_task` as a
`rationale` field.

**The problem it solves:** when agents try to understand *why* a task is in its current state
— why the alarm schema is frozen, why CONTRACT-10 is a blocker, why a particular guard was
chosen — they resort to reading comment threads, git logs, and ADR files. In the six-agent
session, "why is the alarm schema frozen?" required cross-referencing five separate sources.
The Haiku summarizer pre-digests that trail so `get_task` returns a 50-word answer.

**Model choice:** Claude Haiku 4.5 — cheap enough to run on every task update without
budget concern; fast enough to run in the background without delaying tool responses.

**What it summarizes** (inputs per task):
- Last N comments (capped at 20)
- PR titles/descriptions linked to the task
- Dep-change events from the activity log
- Any `decisions` log entries (§2.3) attached to the task

**Output format** (stored in `task_summaries.rationale`):
```
Current state: In Review. Waiting on ENGINE-11 containment-check.sh extension.
Key decision: guard is map.getStyle() not map.isStyleLoaded() — latter also waits on
all raster sources and can stay false indefinitely, suppressing the overlay permanently.
Blocker: CHART must merge first (owns helm_server.cpp). No scope changes since 2026-06-26.
```

**NOT a replacement for the decisions log.** The decisions log (§2.3) is agent-written,
structured, and authoritative — it captures "why this choice was made" with explicit rejected
alternatives. The Haiku summary captures "what has happened" from the activity trail. They
are complementary. The summarizer indexes the decisions log as an input; the decisions log
is not replaced by the summary.

**`get_task` response addition:**
```json
{
  "id": "ENGINE-11",
  "status": "In Review",
  "rationale": "..50-word Haiku summary..",
  "rationale_generated_at": "2026-06-27T08:15:00Z"
}
```

**Trigger:** run after any batch of activity on a task (scheduler polls activity log for
tasks with unprocessed events since last summary run). Cap at one summarizer run per task
per 15 minutes to avoid Haiku cost on rapid-fire updates.

---

## Design principles (agents ≠ humans)

These constraints are why a standard PM tool's coordination layer doesn't work for agents:

1. **Structured over prose.** Agents parse; they don't skim. Every coordination primitive
   must be a typed object with a schema. Free-text comments are audit trail, not coordination
   signals.

2. **Push over poll.** Agents can't receive async events without explicit subscription.
   But polling burns tokens and context. The right model: silence means nothing relevant changed;
   a notification means something specific happened. Every repeated poll is a design gap.

3. **Idempotent + deduplicated writes.** Agents retry on network error. The board must
   deduplicate (message-id, lease claim) so a double-retry doesn't corrupt state.

4. **Append-only provenance.** Agents can't be trusted to have consistent memory across
   sessions. Every board mutation must carry agent_id + timestamp; the activity log is the
   single source of truth for "what actually happened." The decisions log extends this to "why."

5. **Prevent over resolve.** Agents are bad at interactive conflict resolution. The system
   should prevent collisions (leases, resource broker, "you're behind" push) rather than
   provide tooling to clean them up after. Cleanup costs 10× what prevention costs.

6. **Advisory enforcement only.** Hard locks in a distributed system add deadlock risk and
   require a lock server. Advisory locks (like GitHub's "someone else is editing this file")
   give 90% of the benefit with none of the complexity. An agent that breaks a lease gets
   flagged; it doesn't get blocked at the write layer.

---

## Priority order (what to build first)

| P | Feature | Effort | Addresses |
|---|---|---|---|
| P0 | **Deterministic serialization** (§T0) | XS — `sort_keys=True` + strip volatile in serializer | Cross-cutting cache-hit improvement; 30 min, immediate payoff |
| P0 | **Board↔git auto-sync** (§1.2) | S — webhook + regex + status advance | Single biggest board-quality failure; affects every agent every session |
| P0 | **File leases + `retry_after_seconds`** (§1.1) | M — new table + 4 MCP tools | Collision prevention + backoff hint eliminates spin-retry |
| P0 | **`get_lane_delta`** (§1.4) | S — filtered activity log query + 1 tool | Replaces polling `board_summary`; 40× token reduction on unchanged state |
| P1 | **Directed IM + ack** (§2.1) | M — new table + 4 MCP tools | Handshake before cross-lane edits |
| P1 | **"Main moved" push** (§1.3) | S — scheduler delta + notify | Eliminate constant re-fetch |
| P2 | **Decisions log** (§2.3) | S — append-only table + RAG index | Prevent re-derivation of same choices |
| P2 | **Blocking dep requests** (§2.2) | S — extends deps model | Surface unplanned cross-epic deps |
| P2 | **Haiku epistemic summarizer** (§3.4) | M — background job + `rationale` field | Pre-digests activity trail; eliminates 5-source cross-referencing |
| P3 | **Agent state field** (§3.3) | S — one JSON column + 2 tools | Session continuity across context resets |
| P3 | **Resource broker** (§3.1) | M — new table, same lease model | Port / build-dir contention |
| P4 | **Event subscriptions** (§3.2) | L — scheduler+notify fan-out (not MCP SSE) | Structural poll elimination; use notify channel not SSE |

---

## What NOT to build

- **Full IM / chat channel:** too noisy. Agents don't benefit from a stream they can't
  subscribe to selectively. Structured directed messages (§2.1) cover the real need.
- **Wiki:** `AGENT_ROADMAP.md` + ADRs already function as a wiki. Adding a third surface
  creates sync debt. Enrich the existing RAG corpus instead; `ask_plan` covers discovery.
- **Voting / consensus:** the human is the merge authority. Don't route around that.
- **Hard locks / lock server:** advisory leases give 90% of the benefit without deadlock risk.

---

## See also

- [`AGENT_OPERATOR_FEATURES.md`](AGENT_OPERATOR_FEATURES.md) — how a single agent drives the plan
- [`AGENT_ROADMAP.md`](AGENT_ROADMAP.md) — the phased build (Phases 0–7)
- [`decisions/0001-multi-agent-coordination-primitives.md`](decisions/0001-multi-agent-coordination-primitives.md) — architectural decision on which primitives to build first
