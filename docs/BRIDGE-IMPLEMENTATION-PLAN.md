# Bridge Implementation Plan — the execution layer under GitHub / Slack / Linear / Jira

**Status:** Draft v1 · **Task:** BRIDGE-1 · **Companion to:** [PRD-INTEGRATION-LAYER.md](PRD-INTEGRATION-LAYER.md) (DOGFOOD-13, PR #203)
**Scope:** the detailed engineering plan the PRD stubs out — shared substrate, per-platform work items with effort and acceptance, prerequisites, tenancy, milestones.
**Effort legend:** S = under a week · M = 1–3 weeks · L = 3–6 weeks (single operator + agent fleet; estimates assume the fleet does the typing and the operator does app registrations/approvals).

---

## 0. Reading order

1. The PRD carries the thesis, use cases, and "2+2=5" per platform — read it first.
2. §1 here is the honest inventory: what exists, what exists-but-isn't-wired, what's missing.
3. §2 is the shared substrate every adapter rides on (build once).
4. §3–§7 are per-platform scopes. §8 prerequisites. §10 milestones.

Nothing in this plan invents a new coordination mechanism; every component generalizes something already in production (per the ADR-0006 subtraction rule, each section names what it reuses).

---

## 1. Inventory — what we already have, with honest state

| Capability | Where it lives | State |
|---|---|---|
| GitHub merge webhook → provenance-Done | `app.py` webhook route + `store.py` (`git.pr_merged`, default-branch backfill) | **Proven in prod** (stamps Done today; `?project=` pinning required — see memory of HARDEN-2/BUG-24) |
| Reconcile sweep as webhook backstop | `store.reconcile`, `reconcile_alerts`, orphan-merge sweep (RECON-11) | **Proven in prod** |
| Field-level authority model | `repo_topology` (`canonical` vs `public_ci` vs `public` vs `release`; `authority: {done: canonical, …}`) | **Proven in prod** — the template for §2.2 |
| Dependency-aware dispatch | `store.claim_next` (lanes, capabilities, budget, identity, work-session gates) | **Proven in prod** |
| Isolation: managed worktrees + leases | SESSION-7 (`create_managed_work_session`), file/resource leases, SESSION-11 auto-session loop | **Proven** (auto path on for host-spawned agents; external runtimes adopt incrementally) |
| Executed-test evidence | SESSION-10 (`scripts/work_session_test_run.py`, `switchboard.executed_test_run.v1`) | **Proven** (PROOF-3 canary + SESSION-10's own completion) |
| code_strict completion/merge gates | `merge_gate`, `complete_claim` work-session gate | **Live on switchboard** (claim + completion enforced; advisory for non-loop runtimes) |
| Cost ledger | `llm_spend` table, `store.report_usage`, `POST /tally/v1/spend/ingest` (idempotent by `request_id`) | **Built, NOT fed** — LiteLLM has no callback; live jobs don't self-report (UI-12 / §8.1) |
| Plain-English narration | `narrate.py` + `pending_narrations` queue + deliverable headers, 45s systemd timer | **Proven in prod** (task + deliverable narration live) |
| Digest | `digest.py` (weekly chief-of-staff brief) | Built; email/Slack delivery via `notify` module |
| Directed messages + acks + monitors | `send_agent_message` (`requires_ack`, deadlines), `ack_message`, monitor escalation | **Proven in prod** (used this cycle) |
| Human gates | `_task_human_gate_state`, bug-intake gate policy | Built |
| Offline (non-code) provenance | RECON-7 `verify_offline_completion`, QA-4 proof | **Proven** |
| Scoped tokens | `create_scoped_token` / revoke / list (ACCESS-3) | Built (no UI — UI-4) |
| Audit export | `get_audit_export` (HARDEN-13) | Built |
| Intake → triage | `ingest_and_triage`, `api/intake` (email/transcript → proposed changes) | Built (Maxwell-proven) |
| Off-box backups | HARDEN-43/44 | **In flight** (branch `claude/HARDEN-43-offbox-sqlite-backups` active) |

The only *new* invention in this whole plan is the small stuff: an id-mapping table, an outbound queue, and one JSON contract. Everything else is plumbing existing systems to new sockets.

---

## 2. Shared substrate (build once, every adapter rides it)

### 2.1 C-1 · `external_refs` — id mapping (S)

One row per (surface, external object) ↔ our task. Registry-level, like `project_access`.

```sql
CREATE TABLE IF NOT EXISTS external_refs (
    surface       TEXT NOT NULL,          -- 'github' | 'slack' | 'linear' | 'jira' | ...
    external_id   TEXT NOT NULL,          -- stable id, e.g. 'gh:owner/repo#123', 'linear:ENG-42'
    external_url  TEXT,
    project_id    TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    content_hash  TEXT,                   -- last-seen hash of authoritative-inbound fields
    last_inbound_at REAL, last_outbound_at REAL,
    sync_state    TEXT NOT NULL DEFAULT 'ok',   -- ok | pending | conflict | orphaned
    created_at    REAL NOT NULL, updated_at REAL NOT NULL,
    PRIMARY KEY (surface, external_id)
);
CREATE INDEX ix_external_refs_task ON external_refs(project_id, task_id, surface);
```

Store helpers: `link_external_ref`, `get_external_ref`, `list_external_refs(task_id)`. Surfaced read-only on `get_task` (so the UI/narrator can say "mirrored from Linear ENG-42").
**Acceptance:** round-trip create/lookup by both keys; duplicate external_id fails closed.

### 2.2 C-2 · `surface_topology` — the authority contract (S doc + S code)

Generalizes `repo_topology` (same shape, same fail-closed philosophy). Stored per project in meta, validated like session-policy profiles.

```json
{
  "schema": "switchboard.surface_topology.v1",
  "surfaces": {
    "github": {
      "role": "tracker",
      "repo": "6th-Element-Labs/projectplanner",
      "authority": {
        "title": "surface", "description": "surface", "priority": "surface",
        "labels": "surface", "assignee": "surface",
        "status_done": "switchboard",
        "cost": "switchboard", "evidence": "switchboard", "narration": "switchboard"
      },
      "state_map": {"open": "In Progress", "closed": "Done"},
      "dispatch_trigger": {"label": "agent:ok"},
      "credentials_ref": "scoped_token:github-bridge"
    }
  },
  "rules": {
    "conflict": "authority_wins",
    "unknown_field": "surface_wins",
    "done_requires": ["merge_provenance", "offline_evidence"]
  }
}
```

Key decisions encoded here, not in adapter code: who owns each field; how host states map to ours (per-surface `state_map` — this is where Linear's per-team workflows and Jira's workflow schemes get absorbed as *config*); that Done can never be asserted by a surface. `set_project_surface_topology` / `get_project_surface_topology` mirror the repo_topology API.
**Acceptance:** invalid authority values fail closed; a surface write to a switchboard-authority field is rejected and logged as a finding.

### 2.3 C-3 · adapter interface (M)

`bridge/base.py` — deliberately tiny; adapters are translators, not brains:

```python
class SurfaceAdapter:
    surface: str                                   # 'github' | 'linear' | ...
    def verify(self, request) -> bool              # HMAC/signature check
    def handle_event(self, event) -> list[Change]  # inbound webhook → normalized changes
    def push(self, item: OutboxItem) -> PushResult # outbound write (comment/status/check/field)
    def snapshot(self, external_id) -> dict        # fetch current host state (for reconcile)
```

`Change` = `{project, task_id|external_id, field, value, actor, evidence}` — applied through the **existing** `create_task`/`update_task`/`add_comment` paths with a `system_actor` (identity rules from HARDEN-27 apply unchanged). Adapters registered per project from surface_topology.
**Acceptance:** a fake adapter driven by fixture events produces board changes byte-identical to native API writes.

### 2.4 C-4 · `bridge_outbox` — durable outbound queue (M)

Mirrors the `pending_narrations` pattern (proven): DB table + drain job on a systemd timer, instead of best-effort inline HTTP calls that lose writes when a host is down or rate-limits.

```sql
CREATE TABLE IF NOT EXISTS bridge_outbox (
    id INTEGER PRIMARY KEY, surface TEXT NOT NULL,
    external_id TEXT, project_id TEXT, task_id TEXT,
    kind TEXT NOT NULL,          -- comment | status | check_run | field | attachment
    payload TEXT NOT NULL,       -- JSON
    idem_key TEXT UNIQUE,        -- prevents double-posting on retry
    attempts INTEGER DEFAULT 0, next_attempt_at REAL, last_error TEXT,
    created_at REAL NOT NULL, delivered_at REAL
);
```

Drain: `jobs.py bridge_flush` (new job, same runner/timer pattern as `narrate_pending`, ~30–60s). Exponential backoff; per-surface rate-limit awareness (respect `Retry-After`/`X-RateLimit-Remaining`); dead-letter after N attempts → a monitor fires.
**Acceptance:** kill the host mid-post → no duplicates after recovery (idem_key); rate-limit responses defer, not drop.

### 2.5 C-5 · inbound webhook routing (S)

Extend the proven GitHub route pattern: `POST /bridge/v1/{surface}/webhook?project=…` — **project pinned in the URL** (the HARDEN-2/BUG-24 lesson: bare webhook URLs fail closed on shared surfaces), per-surface signature verification in the adapter, events appended to an `bridge_events` log (replayable — RECON-8 alignment), then handled.
**Acceptance:** bad signature → 401 and no side effect; replay of a delivered event is a no-op.

### 2.6 C-6 · bridge reconcile sweep (M)

The backstop, because webhooks lie (we learned this the hard way — orphan-merge sweep). Per surface, on the existing reconcile timer: walk `external_refs`, call `adapter.snapshot()`, diff against our task per the authority map, emit findings:

- host changed a host-authority field we missed → apply inbound (late delivery repair)
- host changed a **switchboard-authority** field (someone hand-closed a Jira ticket) → *revert or flag*, per `rules.conflict`, and log a finding
- orphaned refs (issue deleted) → mark `sync_state=orphaned`

**Acceptance:** manually close a mirrored GitHub issue without provenance → sweep reopens it (or flags, per config) within one cycle.

### 2.7 C-7 · narration router (S)

`narrate.py` already writes task + deliverable narrations on a 45s timer. Add one hook: after `set_task_narration`, if the task has external_refs on surfaces whose topology grants us `narration`, enqueue a `bridge_outbox` comment/update. Same text, N destinations. (Digest gets the same treatment for Slack.)
**Acceptance:** a status transition on a mirrored task produces exactly one narration comment per surface (idem-keyed by narration fingerprint).

### 2.8 C-8 · spend attribution (S — this is UI-12, promoted to a hard prerequisite)

Design (LiteLLM side): enable a success callback posting per-call usage to `POST /tally/v1/spend/ingest` with `request_id` (LiteLLM call id — the ledger is already idempotent on it), model, provider, tokens, cost. Attribution: callers thread `task_id`/`claim_id`/`agent_id`/`source` through LiteLLM `metadata` (adapters set it in the loop; `narrate.py`/`summarize.py`/`digest.py` tag `source=narrator|summarizer|digest`). Unattributed calls still land (source=`gateway`, confidence=`provider_actual`) so the ledger is complete even before every caller is tagged.
**Acceptance:** one narrate cycle produces ledger rows whose summed cost matches the gateway's own accounting for the same window; task-tagged calls roll up on the task's Economics panel.

### 2.9 C-9 · bridge health (S)

`GET /bridge/v1/status`: per surface — last inbound event, outbox depth, dead-letters, last sweep findings. Rendered later as a cockpit tile (UI-3/8 family). A monitor fires on outbox stall (reuses the durable-monitor machinery).

**Shared substrate total: ~3–4 weeks of fleet work.** C-1/2/5 first (they define the contract), C-4/3 next, C-6/7/8/9 in parallel lanes.

---

## 3. GitHub — Phase 1 (the proof surface)

### 3.1 Operator setup (human actions, ~an hour)
1. Create a **GitHub App** (org-owned): permissions `issues:rw`, `pull_requests:rw`, `checks:rw`, `contents:ro`, `projects:rw` (Projects v2 is GraphQL-only); webhook events `issues`, `issue_comment`, `pull_request`, `push`, `check_suite`; webhook URL `https://plan.taikunai.com/bridge/v1/github/webhook?project=<pinned>`.
2. Install on the pilot repo(s). Store app id + private key + webhook secret (see §9 secrets).
3. Note: today's CI posting uses an operator-gated `gho_` token (memory: agents can't mint it). The App's **installation tokens** replace that dependency for bridge writes — one fewer human-held secret in the hot path.

### 3.2 Work items

| # | Item | What it does | Reuses | Effort |
|---|---|---|---|---|
| G-1 | App auth service | JWT → installation token, cached with expiry | — | S |
| G-2 | Issues mirror-in | `issues.*` events → `create_task`/`update_task` + `external_refs`; title/body/labels/assignee per authority map | C-3/C-5, existing webhook plumbing | M |
| G-3 | Dispatch trigger | `agent:ok` label (+ optional assignee = our bot) → estimate comment → auto/one-click `claim_task` | `dispatch_to_claude_code`, intake scoring (BUG-3 heuristic first, LLM later) | S |
| G-4 | Provenance close | merge event (already consumed!) → close mirrored issue + receipt comment ("merged `abc123`, tests ✓, $0.61") | existing `git.pr_merged` path | S |
| G-5 | Evidence **Check Run** | post `Switchboard / evidence` check on PR head SHA: executed-test hash, session hygiene, cost | `executed_test_run.v1`, `merge_gate`, G-1 | M |
| G-6 | Issue reconcile sweep | §2.6 applied to issues (hand-closed without provenance → reopen/flag) | C-6 | S |
| G-7 | Projects v2 (optional, later) | mirror project item status via GraphQL | — | M |

### 3.3 Field mapping (authority in parentheses)

`title/body/labels/milestone/assignee` → task fields (**GitHub**) · `state open→In Progress-eligible, closed` (**split**: human-closed non-agent issues = GitHub; agent-worked = Switchboard-only via provenance) · linked PR → `git_state` (**Switchboard**) · check run + receipt comment + narration (**Switchboard-authored**).

### 3.4 Acceptance demo (the wedge, end-to-end, on our own repo)
File an issue on `6th-Element-Labs/projectplanner`, label `agent:ok` → estimate comment appears → dispatched agent works in a managed worktree → PR opens with the `Switchboard / evidence` check → squash-merge → **the issue closes itself with a receipt comment**. That demo is the pitch.

**Phase total: ~2–3 weeks** after substrate. Risks: Check Runs require App auth (G-1 first); 5k req/hr/installation ceiling (outbox rate-limiting handles); Projects v2 GraphQL quirks (deferred to G-7).

---

## 4. Slack — Phase 2 (the human-loop accelerator)

This is ACCESS-11/13 wearing bridge clothes — not a tracker adapter (no task mirroring), an **interaction** adapter.

### 4.1 Operator setup
Slack app: bot token scopes `chat:write`, `commands`, `users:read`, `im:write`; interactivity request URL `https://plan.taikunai.com/bridge/v1/slack/webhook` (we have a public HTTPS endpoint — no Socket Mode needed); signing-secret verification in the adapter.

### 4.2 Work items

| # | Item | What it does | Reuses | Effort |
|---|---|---|---|---|
| S-1 | App + signature verify + event routing | slash command + interactivity endpoint | C-5 | S |
| S-2 | Approval buttons | human-gate / merge-approval → Block Kit Approve/Deny; response writes the gate decision | human gates, `merge_gate` | M |
| S-3 | Ack + escalation | `requires_ack` messages → DM with Ack button; deadline monitor escalates to channel | agent_messages + monitors | S |
| S-4 | Digest post | `digest.py` output + fleet summary → scheduled `chat.postMessage` | digest, narration | S |
| S-5 | Mention intake | `@Switchboard <ask>` → `ingest_and_triage` → proposed task (to our board or, later, via tracker adapters) with thread updates | intake pipeline | M |

**Identity note:** Slack user ↔ our user mapping via email (`users:read.email`) — feeds the same `users` table ACCESS-2 owns.
**Acceptance:** an agent's human-gated merge produces a Slack button; tapping Approve unblocks it and the tap is attributed to the mapped user in the audit trail.
**Phase total: ~1–2 weeks.**

---

## 5. Linear — Phase 3 (the trust upgrade)

### 5.1 Operator setup
Linear OAuth app (workspace admin installs); webhooks for `Issue`, `Comment`, `IssueLabel`; an **agent identity** so our writes render as an assignable teammate (Linear's agent API); API key fallback for single-workspace pilots.

### 5.2 The one real design problem: state mapping
Linear workflow states are **per-team custom**. This is exactly what `surface_topology.state_map` exists for: per-team config `{ "Todo": "Not Started", "In Progress": "In Progress", "In Review": "In Review", "Done": "Done*", … }` — with `Done*` marked switchboard-authority (we move an issue to their Done state only on provenance; a human dragging to Done on an agent-worked issue gets swept per `rules.conflict`). A small onboarding step reads the team's states via GraphQL and proposes the map for confirmation.

### 5.3 Work items

| # | Item | Effort |
|---|---|---|
| L-1 | OAuth + webhook ingest + signature verify | S |
| L-2 | Issue mirror-in + per-team state-map onboarding | M |
| L-3 | Agent-assignee dispatch (assign → claim; "claimed by claude-code/opus" activity) | M |
| L-4 | Provenance-gated Done transition + receipt comment | M (rides G-4 logic) |
| L-5 | Narration → issue comments + **project updates** (NARRATE deliverable header → Linear project update draft) | S |
| L-6 | Cycle economics comment (needs C-8 ledger data) | S |

**Acceptance demo:** assign a Linear issue to the fleet; watch it claim, work, PR, and flip to Done with a receipt — without anyone opening our board.
**Phase total: ~2–3 weeks.** Risks: agent-API specifics may shift (young API); custom fields are limited → cost/evidence live in comments/attachments initially.

---

## 6. Jira — Phase 4 (the compliance jackpot)

### 6.1 Approach decision (open question in the PRD, recommendation here)
**Pilot: plain REST + webhooks with an org-scoped OAuth 2.0 (3LO) app** — fastest to a design partner, no marketplace review. **Marketplace: Forge app later** (Atlassian-hosted, enterprise trust) once the pilot proves the shape. Building Forge first would front-load weeks of platform learning before any customer signal.

### 6.2 The real work
- **Workflow scheme mapping (the L item).** Jira statuses/transitions are per-project schemes with permissions. Same `state_map` answer as Linear, plus: our Done write must be a *legal transition* for our app user — onboarding validates the scheme and fails closed with a "grant transition permission" instruction.
- **Evidence bundle.** Custom fields (`Switchboard Cost`, `Provenance SHA`, `Evidence`) created by the app + attachments: executed-test log (hashed), session-hygiene verdict, `get_audit_export` bundle for the ticket's task. This is the CAB/SOX artifact — HARDEN-13 output finally gets a consumer.
- **ADF.** Comments are Atlassian Document Format, not markdown — one rendering helper (`narration → ADF`), annoying but mechanical.

### 6.3 Work items
J-1 OAuth/3LO + webhooks (M) · J-2 mirror-in + workflow-scheme onboarding (**L** — the hard one) · J-3 custom fields + evidence attachments (M) · J-4 ADF renderer (S) · J-5 provenance-gated transition + sweep (M) · J-6 JSM incident flow (M, later) · J-7 Forge packaging + marketplace (L, later).

**Acceptance demo:** a Jira ticket worked by an agent carries, on transition to Done: merge SHA, hashed test log, hygiene verdict, cost, and an exportable audit bundle — screenshot-ready for a change-advisory board.
**Phase total: ~4–6 weeks pilot; Forge/marketplace separate.**

---

## 7. Ops tier (Asana / Monday / Notion / ClickUp) — Phase 5, thin by design

Same adapter interface; provenance = **offline evidence** (verifier-stamped artifact URLs — RECON-7), since there's no merge event. One pilot integration chosen by design-partner demand, not speculatively. No work items scoped until then — the substrate makes each of these ~S/M once the pattern exists. Honest note from the PRD stands: the proof story is weaker here; it waits until receipts have earned trust in the code world.

---

## 8. Prerequisites — "make it true first" (gate for any external pilot)

| # | What | Why it gates | State | Effort |
|---|---|---|---|---|
| P-1 | **Spend ingestion** (C-8 / UI-12) | An empty cost ledger posted into a customer's tracker is a credibility grenade | ledger built, unfed | S |
| P-2 | **Enforcement adoption** | receipts must reflect enforced reality: code_strict default ON (done), auto-work-session ON for host loop (done), external runtimes adopt via SESSION-11 flag | mostly done; watch list | S (monitoring) |
| P-3 | **Backups** (HARDEN-43/44) | we'd hold *other companies'* mirrored work records | in flight on the board | — |
| P-4 | **Box capacity** | 2-vCPU/911MB already needed cgroup guards (HARDEN-32); bridge adds webhook + outbox load | plan: keep timers cgrouped; design partners = 1–2 tenants max on current box; upgrade decision at M3 | S |

---

## 9. Multi-tenancy, secrets, and security

- **Isolation:** per-project SQLite files are a genuinely good tenancy story for design partners (one file per customer project; no cross-tenant queries possible by construction). Real multi-tenant SaaS is explicitly out of scope for this plan.
- **Secrets:** today `.env` plaintext on one box. Bridge adds per-surface app keys + per-tenant tokens → move bridge credentials to a `surface_credentials` registry table, encrypted at rest (Fernet key in `.env` as interim; SSM later), referenced from surface_topology as `credentials_ref` — never inline. Scoped tokens (ACCESS-3) bind each bridge's board writes to a per-surface principal, so audit shows `github-bridge` not a shared env token (HARDEN-27 rules apply).
- **Egress consent:** narration/cost/evidence leaving our system into a host tracker is an *external effect* — writes go through the outbox with the surface named, and per-surface topology flags (`narration: switchboard` present/absent) are the consent switch. No cross-surface data mixing: an outbox item carries exactly one task's data to exactly one surface.
- **Deletion:** dropping a surface deletes its `external_refs` + outbox rows and revokes its credentials; mirrored task data remains ours (it's our execution record).

---

## 10. Milestones

| M | Deliverable | Exit criterion | Est. |
|---|---|---|---|
| M0 | P-1 spend ingestion + P-3 backups land | Economics panels show real $ for a full week; restore drill passes | 1–2 wk |
| M1 | Substrate: C-1/2/5 + C-4/3 skeleton | fixture adapter round-trips a fake issue end-to-end in CI | 1–2 wk |
| M2 | **GitHub MVP on our own repo** (G-1..G-6) | the §3.4 demo: an issue closes itself with a receipt | 2–3 wk |
| M3 | Slack approvals + digest (S-1..S-4) | a human-gate resolves from a phone; morning digest posts | 1–2 wk |
| M4 | Linear pilot with one design partner | partner ships ≥10 agent-worked issues through their Linear | 2–3 wk |
| M5 | Jira pilot (REST) with one enterprise partner | one CAB review passes on our evidence bundle | 4–6 wk |

Sequencing note: M0 and M1 can run in parallel lanes; M2 is the go/no-go gate for outreach (the demo *is* the pitch).

## 11. Success metrics

- **Time-to-first-receipt** for a new repo/workspace (target: under 1 hour from app install).
- % of agent-worked mirrored issues that close **with provenance** (target: 100% — anything else is a sweep finding).
- Spend-attribution coverage: % of gateway calls landing in the ledger with a task/source tag (target: >95% by M2).
- Human approval latency via Slack vs. board (expect minutes vs. hours).
- Design-partner retention at 30 days (the only metric that validates the market bet).

## 12. Board mapping & open items

- **Feeds this plan:** UI-12 (=P-1), HARDEN-43/44 (=P-3), ACCESS-11/13 (=§4), PROTO-6 (publish IXP so third parties write adapters), TALLY-4/7 (deepen §2.8 into reconciliation + planned-vs-actual), BUG-3 (real intake scorer behind G-3), RECON-8 (event replay under §2.5's event log), UI-3/7/8 (the cockpit that §2.9 feeds).
- **Proposed next BRIDGE tasks** (numbering continues from BRIDGE-1 = this doc): BRIDGE-2 surface-topology contract implementation (C-1/2/5) · BRIDGE-3 outbox + adapter skeleton (C-3/4) · BRIDGE-4 GitHub MVP (G-1..G-6) · BRIDGE-5 Slack (S-1..S-4) · BRIDGE-6 Linear · BRIDGE-7 Jira pilot. (The PRD's §7 table proposed a slightly different split; this supersedes it with the substrate called out explicitly.)
- **Open questions carried from the PRD:** Forge-vs-REST answered (§6.1: REST pilot, Forge later); Projects-v2-vs-Issues answered (§3: Issues first, Projects v2 = G-7); cockpit visualization of the cross-surface graph and pricing shape remain open.
