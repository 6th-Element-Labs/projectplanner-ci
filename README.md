# Switchboard

Switchboard is the model-agnostic agent coordination layer behind `plan.taikunai.com`:
a live project board, MCP/REST control plane, durable agent inbox, claim scheduler,
runtime adapters, host wake substrate, reconcile loop, and Tally cost-to-outcome ledger.

Compatibility note: the GitHub repo, live checkout, systemd units, and data directory still
use the historical `projectplanner` name. Treat those names as compatibility surfaces during
the migration, not as the product name. See
[`docs/SWITCHBOARD-RENAME-MIGRATION.md`](docs/SWITCHBOARD-RENAME-MIGRATION.md).

The original app began as a tiny, standalone Asana-style project-board web app with a
per-task **Ask Taikun** agent (RAG over plan docs + propose-to-confirm task edits). It was
extracted from the ActionEngine `taikun-pm` satellite (ADR 0007) into its own repo so it is
**not** part of the core platform and never ships to a fresh ActionEngine install.

Runs as **two small processes on one cheap VM**:
- `app` — FastAPI on `127.0.0.1:8110` (board UI + task CRUD + live xlsx/MSPDI export + the agent)
- `gateway` — a bundled **LiteLLM** proxy on `127.0.0.1:8095` exposing `taikun-chat` / `taikun-embed`

The app talks only to the local gateway (so the OpenAI key lives in the gateway, not
the app, and models are swappable in `deploy/gateway/config.yaml`). Storage is a single
**SQLite** file — no database server. Caddy fronts it with auto-HTTPS at
`plan.taikunai.com`.

> Why no workflow engine? The agent is an interactive ReAct loop (a few tool calls) —
> the in-process / non-durable class. The durable workflow engine is core-coupled and
> unnecessary here. The shared *gateway* is the only platform piece worth reusing, and
> it's standalone, so it's bundled.

## Layout
```
app.py store.py export.py rag.py agent.py   # the service
static/                                       # board UI (index.html + app.js)
plan-docs/                                    # docs the agent RAGs over + project-plan.json (source)
seed_plan.json                                # dated slim plan seeded into SQLite on first run
build_plan_artifacts.py                       # regenerate seed_plan.json (rebase kickoff/timeline)
requirements.txt                              # app deps
deploy/
  gateway/config.yaml + requirements.txt      # the bundled LiteLLM gateway
  projectplanner.service / -gateway.service   # compatibility systemd units
  Caddyfile                                    # plan.taikunai.com -> :8110 (auto-HTTPS)
  PROVISION.md                                 # spin up the cheap VM end-to-end
```

## Run locally
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r deploy/gateway/requirements.txt
cp .env.example .env   # set OPENAI_API_KEY + a master key (PM_LLM_KEY == LLM_GATEWAY_MASTER_KEY)
litellm --config deploy/gateway/config.yaml --port 8095 &     # the gateway
uvicorn app:app --port 8110                                   # the app -> http://localhost:8110/
```

## Rebase the timeline
```bash
python3 build_plan_artifacts.py 2026-06-01    # any Monday kickoff -> regenerates seed_plan.json
```
On a live VM, update dates in-place with a dates-only SQLite UPDATE (don't wipe/reseed —
that drops user edits). See PROVISION.md.

## Deploy to a VM at plan.taikunai.com
See [deploy/PROVISION.md](deploy/PROVISION.md). Roughly: a t4g.micro (~$6/mo), one venv,
two systemd units, Caddy, and a Route 53 A record.

## Docs

| Doc | What it covers |
|---|---|
| [`docs/AGENT_ROADMAP.md`](docs/AGENT_ROADMAP.md) | Phased build plan (Phases 0–7, single-agent operator) |
| [`docs/AGENT_OPERATOR_FEATURES.md`](docs/AGENT_OPERATOR_FEATURES.md) | Operator-level agent features (autonomous delivery loop, action queue, outcome tracker, live-meeting agent) |
| [`docs/MULTI_AGENT_COORDINATION.md`](docs/MULTI_AGENT_COORDINATION.md) | Multi-agent coordination layer — file leases, git↔board sync, directed IM, decisions log (derived from first-hand six-agent session data) |
| [`docs/PRODUCT_ROADMAP.md`](docs/PRODUCT_ROADMAP.md) | Switchboard product roadmap, competitive positioning, moat, and commercial wedges |
| [`docs/MARKET-LANDSCAPE.md`](docs/MARKET-LANDSCAPE.md) | Living market tracker for agent collaboration, governance, security, orchestration, and workplace-suite moves |
| [`docs/P0-SPEC.md`](docs/P0-SPEC.md) | Switchboard P0 implementation floor: authenticated writes, agent identity, REST/MCP parity, idempotency, and `IXP-core` conformance |
| [`docs/IXP-PUBLIC-PACKAGE.md`](docs/IXP-PUBLIC-PACKAGE.md) | Public protocol package boundary: open specs/adapters/conformance, license posture, governance, and hosted/commercial line |
| [`docs/IXP-CONFORMANCE.md`](docs/IXP-CONFORMANCE.md) | Conformance badge language, required evidence, non-claims, and current reference fixture status |
| [`docs/RUNTIME-ADAPTERS-SPEC.md`](docs/RUNTIME-ADAPTERS-SPEC.md) | Runtime adapter packs for Claude Code, Codex, Cursor, LangGraph, raw OpenAI loops, and generic REST clients |
| [`docs/INTERRUPT-TIERS-SPEC.md`](docs/INTERRUPT-TIERS-SPEC.md) | Visible stop/redirect guarantees: advisory poll, hook-level deny, runner kill, and managed control |
| [`docs/CLAIM-NEXT-SPEC.md`](docs/CLAIM-NEXT-SPEC.md) | `claim_next` / `+TXP` dispatch profile: atomic task assignment, task claims, budget/model guidance |
| [`docs/BUG-INTAKE-CONTRACT.md`](docs/BUG-INTAKE-CONTRACT.md) | Bug Intake Agent contract: report schema, severity, dedupe, human gate, and conversion policy |
| [`docs/TALLY-SPEC.md`](docs/TALLY-SPEC.md) | Tally / `+OXP` cost-to-outcome and KPI ledger: gateway-measured plus agent-reported spend |
| [`docs/SWITCHBOARD-RENAME-MIGRATION.md`](docs/SWITCHBOARD-RENAME-MIGRATION.md) | Safe migration from `projectplanner` repo/ops identity to Switchboard product identity |
| [`docs/decisions/0001-…`](docs/decisions/0001-multi-agent-coordination-primitives.md) | ADR: build order for the multi-agent coordination primitives |
| [`docs/decisions/0005-store-module-decomposition.md`](docs/decisions/0005-store-module-decomposition.md) | ADR: strangler-split `store.py` — foundation shipped (ARCH-1…5); remainder superseded by ADR-0006 |
| [`docs/decisions/0006-control-plane-done-enough.md`](docs/decisions/0006-control-plane-done-enough.md) | **ADR: the control plane is done enough** — the one-page provenance model, the subtraction rule (no new mechanism without deleting one), the kill list, and the four-horizon master plan (stabilize → prove on Helm → COORD → productize) |
| [`docs/MCP.md`](docs/MCP.md) | MCP server design and tool reference |
| [`docs/UNIVERSAL_WORKFLOW_UI.md`](docs/UNIVERSAL_WORKFLOW_UI.md) | Universal workflow UI spec |
