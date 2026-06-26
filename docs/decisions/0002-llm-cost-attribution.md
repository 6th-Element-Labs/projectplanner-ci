# ADR-0002 — LLM cost attribution by project / task / agent

- **Status:** Proposed
- **Date:** 2026-06-27
- **Author:** Helm multi-agent session (Claude Code)
- **Relates to:** [`docs/PRODUCT_ROADMAP.md`](../PRODUCT_ROADMAP.md) Bet 1;
  mirrors ActionEngine's gateway audit callback
  (`actionengine/services/llm_gateway/audit_callback.py`, ADR-0004 there).

---

## Context

Roadmap Bet 1 ("cost-per-outcome accounting") needs per-task / per-project / per-agent token
and dollar spend. ActionEngine already solved the *mechanism* for its DBOS workflow engine: a
**LiteLLM custom callback** (`audit_callback.audit_logger_instance`) fires on every gateway
call, pulls normalized usage + `response_cost` + threaded metadata out of LiteLLM's
`standard_logging_object`, and POSTs one record to a ledger (`platform.llm_calls` via the
llm-audit microservice). The cost is computed by LiteLLM from its model-pricing tables — we
get `cost_usd` per call for free.

ProjectPlanner's gateway is the same shape (`deploy/gateway/config.yaml`) but **stateless,
routing-only** — no callback, no ledger. We want to adopt the proven pattern.

### The honest constraint: the gateway only sees what routes through it

This is the load-bearing nuance for the "cost per outcome" pitch. There are **two distinct
spend streams**, and only one flows through our gateway:

- **Stream A — ProjectPlanner's own AI (gateway-tracked, exact $).** `agent.py` (ask_plan /
  per-task agent), `summarize.py`, `digest.py`, `ocr.py`, `transcribe.py`, `rag.py` all call
  `127.0.0.1:8095`. These we can attribute precisely and automatically.
- **Stream B — the coding fleet (NOT gateway-tracked).** The Claude Code agents doing the
  actual Helm work bill Anthropic directly through their own runtime. They never touch our
  gateway, so a gateway callback can never see their spend. This is the *larger* number and
  the one "cost to accomplish task X" most wants.

Pretending Stream A is the whole story would be the kind of hidden-gap that the
[fail-and-fix-early policy] forbids. So the design captures both, in one ledger, with a
`source` column — and is explicit that Stream B depends on agents self-reporting.

## Decision

### 1. A single `llm_spend` ledger (SQLite, in `store.py`)

```sql
CREATE TABLE IF NOT EXISTS llm_spend (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id        TEXT,                       -- idempotency key (gateway call id); null for agent reports
    source            TEXT NOT NULL,              -- 'gateway' | 'agent_report'
    project           TEXT,                       -- 'helm' | 'maxwell' | ...
    task_id           TEXT,
    agent_id          TEXT,                       -- 'claude/ENGINE-11' | 'pm-agent' | 'summarizer'
    call_site         TEXT,                       -- 'ask_plan' | 'summarize' | 'ocr' | 'digest' | 'coding'
    provider          TEXT,
    model             TEXT,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    cost_usd          REAL NOT NULL DEFAULT 0.0,
    latency_ms        REAL,
    status            TEXT NOT NULL DEFAULT 'ok',
    created_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_spend_project_task ON llm_spend(project, task_id);
CREATE INDEX IF NOT EXISTS ix_spend_created ON llm_spend(created_at);
CREATE UNIQUE INDEX IF NOT EXISTS ux_spend_request ON llm_spend(request_id) WHERE request_id IS NOT NULL;
```

The `UNIQUE … WHERE request_id IS NOT NULL` makes gateway ingestion idempotent (a retried
callback POST never double-counts) while letting agent reports (no request id) insert freely.

### 2. Stream A — gateway callback (mirror ActionEngine)

Add a dependency-light `deploy/gateway/cost_callback.py` next to `config.yaml`, registered as:

```yaml
litellm_settings:
  drop_params: true
  callbacks: cost_callback.cost_logger_instance
```

It subclasses `litellm.integrations.custom_logger.CustomLogger`, implements
`async_log_success_event` / `async_log_failure_event`, extracts
`{response_cost, prompt_tokens, completion_tokens, total_tokens, model, provider, id}` from
`kwargs["standard_logging_object"]` (exactly as ActionEngine's `_build_record` does), pulls our
metadata, and **POSTs to a new local app endpoint** `POST /api/llm/ingest` on `:8110` (rather
than writing SQLite directly — keeps the callback free of app imports, same decoupling
ActionEngine gets from its microservice). **Fail-open:** a logging error never affects the call.

Metadata is threaded by adding a `"metadata"` field to each gateway request body. Our keys:

```python
"metadata": {"project": "helm", "task_id": "ENGINE-11",
             "agent_id": "summarizer", "call_site": "summarize"}
```

Each of the six call sites above passes its own `call_site` / `agent_id`. (LiteLLM surfaces a
body-level `metadata` dict in the logging object; the callback reads it like ActionEngine's
`_pull_metadata`.) For the per-task agent, `task_id`/`project` are already in scope.

### 3. Stream B — agent self-report MCP tool

Add `report_usage(project, task_id, agent_id, model, input_tokens, output_tokens, ...)` to
`mcp_server.py`. Coding agents call it (e.g. at task completion, or per-turn) so their
Anthropic-billed spend lands in the same ledger with `source='agent_report'`,
`call_site='coding'`. Cost is computed from a small per-model price table in `store.py`
(input/output $/Mtok) since these calls never hit LiteLLM's pricing engine. This stream is
**advisory and best-effort** — its accuracy depends on agents reporting. That limitation is
stated, not hidden; future work can reconcile against Anthropic's usage API.

### 4. Read API

- `store.spend_by_task(project, task_id)` / `spend_by_project(project)` /
  `spend_by_agent(...)` → aggregates `{tokens, cost_usd, by_source, by_model}`.
- MCP `get_task_cost(task_id)` and a board column / digest line: "cost so far."
- Budgets (later): a `budget_usd` field on tasks/epics; `claim_next`/`update_task` warn or
  block when exceeded (ties to Bet 1's halt behavior).

## Alternatives rejected

- **LiteLLM's own Postgres spend logs + virtual keys.** Heavier (Prisma/DB), and ActionEngine
  deliberately avoided it ("no LiteLLM DB/Prisma — gateway is stateless"). We keep our own
  ledger for the same reason and to unify both streams in one place.
- **Callback writes SQLite directly.** Couples the gateway process to the app's DB layer and
  schema. POSTing to `/api/llm/ingest` keeps the callback dumb and dependency-light.
- **Gateway-only tracking.** Would silently miss Stream B (the fleet) — the bigger number —
  and quietly misrepresent "cost per outcome." Rejected on fail-and-fix-early grounds.

## Open questions

- **Agent identity** (shared with ADR-0001): `agent_id` is self-asserted. Fine for now;
  a sold product needs real agent auth before spend numbers are billable.
- **Per-turn vs per-task reporting** for Stream B — start per-task (cheap, low-noise), revisit.
- **Reconciliation** with provider billing APIs (Anthropic/OpenAI usage endpoints) to true-up
  Stream B — deferred.

[fail-and-fix-early policy]: ../../CLAUDE.md
