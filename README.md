# Switchboard

**The neutral control plane for AI work.** Switchboard assigns work to agents, coordinates
them across runtimes and clouds, meters what they cost, enforces human oversight, and proves
what actually got done.

The visible surface looks like a project board. The product is the operating record
underneath it: claims, messages, leases, decisions, runner state, provenance, spend,
outcomes, and human approvals.

Live at `plan.taikunai.com`. Switchboard is built by the agent fleet that Switchboard
coordinates — the board you see is the board that ships it.

> **Naming.** The GitHub repo, live checkout, systemd units, and data directory still use the
> historical `projectplanner` name. Those are compatibility surfaces during the migration, not
> the product name. See [`docs/SWITCHBOARD-RENAME-MIGRATION.md`](docs/SWITCHBOARD-RENAME-MIGRATION.md).

---

## Two users, one board

- **Humans** get a planning *agent* that acts as PM (plan-wide chat, board, signals, weekly
  digest) plus a window to watch a running fleet without interrupting it — and a way to step
  in when it matters.
- **Agents** get coordination primitives — presence, file leases, directed messages, a
  decisions log, per-task working state, delta polling, pre-digested context — so N of them
  can work one plan async without colliding.

The defining bet is **agent-coordination-first, with the human window preserved.** Most of
the market builds from the opposite end.

---

## What's in the system

### Coordination — `IXP` (Instruction Exchange Protocol)

The signaling core every agent speaks, regardless of model or runtime.

- **Presence and handshake** — `prepare_agent_session`, `register_agent`,
  `get_working_agreement`; a project-bound startup contract instead of per-repo folklore.
- **File and resource leases** — `claim_files` / `check_files` / `release_files`,
  `claim_resource`, plus SCM leases so agents serialize on shared files and repos instead of
  racing.
- **Directed messaging and interrupts** — durable agent-to-agent inbox with acks,
  `send_agent_message`, `list_unacked_messages`, tiered interrupts.
- **Delta polling** — `get_lane_delta` returns only what changed (~50 tokens when nothing
  did, vs ~3–5k for a full board read). The token-economics story is a product feature, not
  hygiene.
- **Decisions log** — append-only architectural and coordinator decisions, replayable.

Spec: [`docs/IXP-SPEC.md`](docs/IXP-SPEC.md).

### Work dispatch — `TXP`

- **`claim_next(agent, lane)`** — atomically leases the highest-priority task that is
  unblocked, unclaimed, and in-lane. This is what makes the board an *active dispatcher*
  rather than a passive ledger.
- **Claim lifecycle** — `claim_task` → `complete_claim(evidence=…)` → `abandon_claim` /
  `revoke_claim`, with stale-assignment reporting and orphan recovery.
- **Work sessions** — a session is bound to a task, a claim, and a workspace. `preflight_work_session`,
  `pre_tool_check`, and session health/doctor tools stop unbound writes before they happen.
- **Execution policy and readiness** — per-project policy (`set_project_execution_policy`),
  readiness checks, and a claim gate that refuses agents lacking the required capability.
- **Dependency graph** — `depends_on` with blocked/ready computation and `explain_task_block`.

### Outcomes and cost — `OXP` / Tally

- **Cost per outcome** — tokens and dollars metered per task, per agent, per deliverable, so
  the answer to "what did this feature cost?" is a number, not a vibe. Two honest streams:
  gateway-measured and agent-reported, never silently merged.
- **Spend envelopes and reservations** — `set_spend_envelope`, `reserve_spend`,
  `reconcile_spend`; budgets that warn or halt.
- **KPIs** — `create_kpi`, `link_outcome_to_kpi`, `update_kpi_value`; only *verified*
  outcomes move a KPI.
- **Outcome verification** — `record_outcome` → `verify_outcome` / `reject_outcome`.

Spec: [`docs/TALLY-SPEC.md`](docs/TALLY-SPEC.md), [ADR-0002](docs/decisions/0002-llm-cost-attribution.md).

### Provenance and completion

The part that makes the record trustworthy.

- **Three independent planes** — Capacity (physical execution presence), Communication
  (message delivery truth), Coordination (work through review, merge, and proven Done). No
  state, timeout, or inference in one plane may impersonate authority from another. This is
  the cornerstone architecture: [ADR-0008](docs/decisions/0008-three-plane-separation.md)
  plus the [lifecycle explainer](docs/COMPLETION-LIFECYCLE-PIPELINE.md).
- **Agents do not declare Done.** Done comes from canonical merge provenance (GitHub webhook
  + reconcile) or verifier-stamped offline evidence for non-code work.
- **Reconcile loop** — continuously re-derives board state from git/GitHub truth, sweeps
  orphaned provenance, and self-heals dropped webhooks.
- **Branch retirement** — merged branches are archive-tagged and deleted automatically.
- **Coordination receipts and external-effect ledger** — every outward action is claimed,
  issued, and verified, so retries can't double-fire.
- **Audit and replay** — `get_audit_export`, `get_execution_transcript`,
  `replay_decision_corpus`, decision episodes.

### Execution plane

Running agents anywhere, governed in one place.

- **Switchboard Connect** — the boot and lease boundary for agents: capacity advertisement,
  opaque assignment identity, provider process launch, lease heartbeat and expiry. DHCP +
  SIP registrar, deliberately small. [`docs/SWITCHBOARD-CONNECT.md`](docs/SWITCHBOARD-CONNECT.md).
- **Agent Host** — a signed, enrollable local host that owns the managed process and the
  runner-kill path (control fidelity T3). Signed release bundles; live runners survive updates.
- **Wake substrate** — `claim_wake` / `complete_wake` and wake intents start or reuse a
  runtime, or honestly report "no eligible host online" instead of pretending delivery.
- **Runner sessions** — the liveness authority. Registration, heartbeat, control requests,
  browser PTY terminal with Watch/Chat.
- **Cloud execution** — dispatch to vendor-hosted agents (Claude Code, Codex, Cursor) as well
  as local hosts, with a provider auth and credential-vault boundary.
- **Provider credentials** — enrollment, leases, rotation, revocation, and a capability
  matrix that records what each runtime can *actually* do rather than what it claims.

### Autopilot and the coordinator

- **Deliverable Autopilot** — kick a deliverable off once and the fleet drains it: propose a
  breakdown, get approval, dispatch, review, remediate, merge, stamp.
- **Mission Bot** — a scoped LLM pager that maps GitHub/CI events to agent action with a
  no-stuck-merge invariant. The v4 rewrite was quarantined and v1 restored in SIMPLIFY-31
  (2026-07-31); **v5 — one-assignment lifecycle collapse** is the proposed successor.
  Triage procedure: [`docs/SWITCHBOARD-RUNBOOK.md`](docs/SWITCHBOARD-RUNBOOK.md) §1.2.
- **Coordinator** — audit loop, escalation, mediated dispatch, and recorded coordinator
  decisions under an explicit contract ([`docs/COORDINATOR-CONTRACT.md`](docs/COORDINATOR-CONTRACT.md)).
- **Review and remediation** — review verdicts, findings, remediation tracking, and metrics.
  Merge gates *observe*; dispatch gates *enforce* ([ADR-0020](docs/decisions/0020-merge-gates-observe-not-enforce.md)).

### The human layer

- **Board UI** — board, epics, gantt, scope, risks, decisions, mission, fleet dock, exec view,
  plan hub, settings.
- **Needs-you queue** — a universal alert surface for anything requiring a human, including
  provider question round-trips, with push delivery.
- **Ask the plan** — RAG-grounded plan-wide chat over the project doc corpus, with citations
  and propose-to-confirm edits. Nothing changes until a human approves; every applied change
  is audited with actor and timestamp.
- **Plan signals and digests** — overdue / due-soon / blocked / ready / critical-slip
  detection, per-owner next-best-action, and a scheduled chief-of-staff brief.
- **Email intake** — forward mail to the project inbox; each message is deduped, cleaned,
  ingested into the RAG corpus as a citable source, and triaged against the plan with a
  reasoned disposition (auto-apply / propose / needs-human / FYI).
- **Live narration** — event-driven LLM narration of what the fleet is doing, with health
  and SLO monitoring.
- **Third-party bridges** — GitHub, Linear, Jira, and Slack host UIs as channels into the
  same durable record (in progress).

### Access and governance

- **Auth** — global ActionEngine-style auth behind a strangler flag; sessions, password reset,
  signup, and per-project scoping.
- **RBAC and tokens** — org/user/project roles, scoped MCP/API tokens (`create_scoped_token`,
  `revoke_scoped_token`), project creation permissions.
- **Project lifecycle** — create, archive, restore, consolidate, and a verified two-phase
  purge with impact reporting.
- **Repo constitution** — machine-checked repo shape so projects start and stay organized for
  agents.

### CI, merge, and the box

- **External CI mirror** — heavy per-PR test runs are pushed to a CI location declared in the
  project's `repo_topology`, keeping the production VM free. Authority separation means tests
  can run anywhere without forging provenance ([`docs/CI-STRATEGY.md`](docs/CI-STRATEGY.md)).
- **Event-driven gate dispatch** — CI starts on the event, not on a timer.
- **Merge queue** — native GitHub merge queue with required contexts; commutative-CI
  principles so many agents land PRs without blocking each other ([ADR-0010](docs/decisions/0010-ci-concurrency.md)).
- **Ops** — off-box S3 backups with a tested restore runbook, uptime probing from an external
  sandbox, autodeploy, retention sweeps, load shedding, single-writer SQLite atomicity, and
  concurrent-load SLO ratchets.

### Adapters and conformance

Install a self-contained runtime bundle into any project — no package manager required:

```bash
python3 adapters/marketplace.py list
python3 adapters/marketplace.py install claude-code --target /path/to/project
python3 adapters/marketplace.py smoke claude-code
```

Bundles: `claude-code`, `codex`, `cursor`, `openai-loop`, `langgraph`, `agent-host`.

Each install writes a manifest declaring its **honest control-fidelity tier** — Claude Code is
T2 when hooks are honored; Cursor and unwrapped Codex/raw loops are T1; the local Agent Host
is T3 because it owns the managed process and kill path. Unsupported lifecycle features are
marked `false`, never simulated.

Verify any pack against an isolated throwaway board:

```bash
python3 adapters/conformance.py --json
```

See [`adapters/README.md`](adapters/README.md) and [`docs/IXP-CONFORMANCE.md`](docs/IXP-CONFORMANCE.md).

---

## Architecture

Python 3.12, FastAPI, SQLite, fronted by Caddy on a single small VM. The HTTP surface is being
peeled by bounded context rather than rewritten:

| Surface | Port |
|---|---|
| Web app and compatibility routes | `:8110` |
| MCP (Streamable HTTP at `/mcp`) | `:8111` |
| Auth | `:8121` |
| Tasks and exact claim routes | `:8122` |
| Coordination reads | `:8123` |
| Deliverables reads | `:8124` |
| Ingest / intake | `:8126` |
| LiteLLM gateway | `:8095` |

[`deploy/Caddyfile`](deploy/Caddyfile) is route truth;
[ADR-0025](docs/decisions/0025-bounded-context-service-extraction.md) explains the reusable
independence and process-cut policy. The app talks only to the local
gateway, so provider keys live in the gateway and models are swappable in
[`deploy/gateway/config.yaml`](deploy/gateway/config.yaml). Storage is a single SQLite file —
no database server. Background work runs on systemd timers (reconcile, narrate, digest,
inbox, monitors, backup, retention, autodeploy), not a workflow engine.

**Why no workflow engine?** The agent loop is an interactive ReAct loop — the in-process,
non-durable class. The durable workflow engine is core-coupled and unnecessary here. The
shared *gateway* is the only platform piece worth reusing, and it's standalone, so it's
bundled. ([ADR-0007](docs/decisions/0007-application-shell-cleanup.md))

### Scale of the surface

| | |
|---|---|
| MCP tool functions | 266 across 32 domain modules (`src/switchboard/mcp/tools/`) |
| HTTP route handlers | ~374 |
| Test files | 210 root + 474 under `tests/` |
| Docs | 238 markdown files, 29 ADRs |

---

## Repo layout

```
src/switchboard/          # where new product code belongs
  api/ application/ domain/ storage/ services/ mcp/ contracts/
  connect/ integrations/ security/ live_acceptance/
adapters/                 # runtime install bundles + conformance kit
  claude-code/ codex/ cursor/ openai-loop/ langgraph/ agent-host/
static/                   # board UI (index.html + js/)
docs/                     # specs, ADRs, runbooks, evidence
scripts/                  # CI gates, probes, migrations, ops tooling
deploy/                   # Caddyfile, systemd units, gateway, PROVISION.md
tests/ test_*.py          # the suite
app.py mcp_server.py …    # compatibility entrypoints and grandfathered surfaces
```

Root modules are compatibility shims, not destinations for new code. See
[`AGENTS.md`](AGENTS.md) for the binding placement rules.

---

## Run locally

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r deploy/gateway/requirements.txt
cp .env.example .env   # set OPENAI_API_KEY + a master key (PM_LLM_KEY == LLM_GATEWAY_MASTER_KEY)
litellm --config deploy/gateway/config.yaml --port 8095 &     # the gateway
uvicorn app:app --port 8110                                   # the app -> http://localhost:8110/
```

## Test

```bash
scripts/switchboard_ci.sh
```

The canonical gate discovers executable `test_*.py` / `*_test.py` files and runs each in its
own Python process. Tests must pass when executed directly, not only under a pytest fixture
environment.

## Deploy

See [`deploy/PROVISION.md`](deploy/PROVISION.md) — one Python environment, Caddy with
auto-HTTPS, and systemd units for the web/MCP surfaces, bounded-context services, and
background jobs. Recovery procedures live in
[`docs/BACKUP-RESTORE-RUNBOOK.md`](docs/BACKUP-RESTORE-RUNBOOK.md) and
[`docs/SWITCHBOARD-RUNBOOK.md`](docs/SWITCHBOARD-RUNBOOK.md).

---

## Roadmap

### The headline bets

1. **Cost-per-outcome accounting** — the strongest commercial wedge. Everyone shows raw token
   graphs; nobody shows cost per outcome *accomplished*.
2. **Dependency-aware dispatch** — the board as an active dispatcher, not a ledger.
3. **Human approval gates + immutable audit trail** — the safety-critical wedge. "Peek in
   *and* step in." This is what sells into regulated orgs that will not let agents run
   unsupervised.
4. **Human/agent collaboration layer** — turn discussion into governed work state: SME review
   before coding, feedback inbox → plan proposal, decision threads on tasks and PRs, and
   Slack/Teams/GitHub bridges that route humans in without making chat the source of truth.

Bets 1–3 have shipped in first form and are being deepened; bet 4 is the current build front.

### In flight

| Deliverable | State |
|---|---|
| SaaS GitHub App SCM onboarding — platform App → customer install → green readiness | In progress |
| Third-party tracker bridges — GitHub, Linear, Jira, Slack host UIs | In progress |
| Operator UI surface (MCP→UI gap closure) | In review |
| Public adapter marketplace — install bundles for Claude Code, Codex, Cursor | In review |
| Project execution setup — complete readiness from the UI | In review |
| Repo Constitution — projects start and stay organized for agents | In review |
| Mission Bot v5 — one-assignment lifecycle collapse | Proposed |
| Provider execution expansion — BYOK APIs + Claude/Cursor qualification | Proposed |
| Architecture-governed project scoping | Proposed |

### Ranked backlog

Context-pack tool · merge-queue from leases · task model policy and model-aware dispatch ·
agent reliability scoring · replay and simulation harness · dispatch policy simulator ·
fail-early taxonomy · enterprise trust graph (audit exports, provider cost reconciliation,
immutable evidence retention).

Full detail, effort sizing, and rationale: [`docs/PRODUCT_ROADMAP.md`](docs/PRODUCT_ROADMAP.md).
Positioning and the competitive read: same document, §2–3. North star:
[`docs/SWITCHBOARD-MANIFESTO.md`](docs/SWITCHBOARD-MANIFESTO.md).

### Open-core plan

The intended boundary is: **open the coordination contract and runtime on-ramp; sell the
governed workplace.**

- **Open (planned, Apache-2.0):** IXP/TXP/OXP specs and schemas, conformance kit, adapter
  packs, a small typed SDK, and a local reference server.
- **Commercial:** identity/RBAC/SSO, managed runners and wake routing, durable evidence
  graphs and retention, dispatch policy and coordinator recommendations, Tally reconciliation
  and economics, integrations and operations.

External launch is currently **No-Go** pending counsel review, allow-list extraction, secret
scanning, and the rest of the gate in
[`docs/OPEN-CORE-RELEASE-PLAN.md`](docs/OPEN-CORE-RELEASE-PLAN.md). This repository is not the
public package and must not be published by deletion from a full copy.

---

## Docs

Start at [`docs/INDEX.md`](docs/INDEX.md). Contributors and coding agents must also read
[`AGENTS.md`](AGENTS.md).

The cornerstone architecture packet is the
[three-plane ADR](docs/decisions/0008-three-plane-separation.md) paired with the
[Mermaid lifecycle explainer](docs/COMPLETION-LIFECYCLE-PIPELINE.md). The
[decision register](docs/decisions/INDEX.md) separates current authority, program history,
proposals, and explicitly superseded material.

MCP surface: [`docs/MCP.md`](docs/MCP.md). Protocol packaging:
[`docs/IXP-PUBLIC-PACKAGE.md`](docs/IXP-PUBLIC-PACKAGE.md) and
[`docs/IXP-CONFORMANCE.md`](docs/IXP-CONFORMANCE.md). Operations:
[`docs/SWITCHBOARD-RUNBOOK.md`](docs/SWITCHBOARD-RUNBOOK.md).

---

## Origins

Switchboard began as a small Asana-style project board with a per-task **Ask Taikun** agent
(RAG over plan docs plus propose-to-confirm task edits). It was extracted from the
ActionEngine `taikun-pm` satellite under [ADR-0007](docs/decisions/0007-application-shell-cleanup.md)
into its own repo so it is **not** part of the core platform and never ships to a fresh
ActionEngine install. That planner is still in here — it's the human window. Everything above
it is the coordination layer that grew around it.
