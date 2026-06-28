# Tally Spec - cost-to-outcome and KPI ledger

- **Status:** P0 implemented through TALLY-2
- **Date:** 2026-06-28
- **Product:** Switchboard
- **Protocol profile:** `+OXP` - outcome exchange / cost settlement, projected over the
  activity log
- **Purpose:** implement cost-per-outcome early enough that Switchboard feels economically
  alive, not like a protocol demo with a future dashboard.

> Tally maps cost to outcomes, and outcomes to KPIs. The commercial unit is not tokens. It
> is verified progress per dollar.

---

## 1. Product thesis

The wedge is cost-per-outcome. Buyers do not care that a fleet spent 340k tokens; they care
whether those tokens produced a verified feature, a resolved incident, a completed RFP
section, a reduced backlog, or a measurable KPI movement.

Tally must ship early in a rough but honest form:

- gateway-measured spend for calls that route through Switchboard's gateway;
- agent-reported spend for external runtimes such as Claude Code, Codex, Cursor, and custom
  loops;
- verified outcomes as the denominator;
- KPI links so cost can be discussed in business terms;
- confidence/source labeling so advisory data is not mistaken for finance-grade billing.

The first version can be simple. It cannot be deferred into "later analytics" without losing
the product's commercial center.

---

## 2. Relationship to `IXP-core` and TXP

Tally/OXP is a read-side projection over the activity log plus a small set of ingestion
events. It should not invent a second coordination protocol.

| Layer | Tally dependency |
|---|---|
| `IXP-core` | authenticated actor, timestamp, activity log, messages, signals |
| `+TXP` | task claims, completion/abandon events, dispatch reason |
| adapters | usage reports and runtime metadata |
| gateway | exact usage/cost for routed calls |
| verification | outcome denominator and KPI links |

Tally may expose APIs for recording usage/outcomes, but its main job is projection:

```text
spend events + task claims + verification events + KPI links -> cost per verified outcome
```

---

## 3. Honest two-stream ledger

### Stream A - gateway-measured

Calls through the local LiteLLM gateway can be measured exactly enough for product use:

- provider;
- model;
- prompt/completion/total tokens;
- request id;
- response cost;
- latency;
- metadata: project, task, agent, call site.

This stream is `source=gateway` and has high confidence.

### Stream B - agent-reported

External coding/knowledge-work runtimes often bill directly through their own vendor account.
Switchboard cannot see that spend unless the adapter or agent reports it.

This stream is `source=agent_report` and must be labeled advisory.

Agent reports may include:

- exact token counts exposed by the runtime;
- approximate token counts;
- cost from runtime billing UI;
- time-based estimate;
- zero/unknown cost with evidence that the adapter could not access usage.

The UI must show the distinction. Tally should be useful before it is perfect, but it must
not silently pretend Stream B is gateway-verified.

---

## 4. Data model

Tally can build on ADR-0002's `llm_spend` ledger and add outcome/KPI tables.

### 4.1 Spend events

```sql
CREATE TABLE IF NOT EXISTS llm_spend (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id        TEXT,
  source            TEXT NOT NULL,      -- gateway | agent_report | provider_reconcile
  confidence        TEXT NOT NULL,      -- exact | reported | estimated | unknown
  project           TEXT NOT NULL,
  task_id           TEXT,
  claim_id          TEXT,
  outcome_id        TEXT,
  agent_id          TEXT,
  principal_id      TEXT,
  runtime           TEXT,
  call_site         TEXT,
  provider          TEXT,
  model             TEXT,
  prompt_tokens     INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens      INTEGER NOT NULL DEFAULT 0,
  cost_usd          REAL NOT NULL DEFAULT 0.0,
  latency_ms        REAL,
  status            TEXT NOT NULL DEFAULT 'ok',
  metadata_json     TEXT NOT NULL DEFAULT '{}',
  created_at        REAL NOT NULL
);
```

Required indexes:

```sql
CREATE INDEX IF NOT EXISTS ix_spend_project_task ON llm_spend(project, task_id);
CREATE INDEX IF NOT EXISTS ix_spend_project_outcome ON llm_spend(project, outcome_id);
CREATE INDEX IF NOT EXISTS ix_spend_project_agent ON llm_spend(project, agent_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_spend_request
ON llm_spend(request_id) WHERE request_id IS NOT NULL;
```

### 4.2 Outcomes

```sql
CREATE TABLE IF NOT EXISTS outcomes (
  id              TEXT PRIMARY KEY,
  project         TEXT NOT NULL,
  task_id         TEXT,
  epic_id         TEXT,
  claim_id        TEXT,
  type            TEXT NOT NULL,      -- feature | fix | review | doc | analysis | decision | incident
  title           TEXT NOT NULL,
  status          TEXT NOT NULL,      -- proposed | verified | rejected | superseded
  verifier        TEXT,
  verification    TEXT,               -- ci | human | evaluator | external_metric
  evidence_json   TEXT NOT NULL DEFAULT '{}',
  value_json      TEXT NOT NULL DEFAULT '{}',
  created_at      REAL NOT NULL,
  verified_at     REAL
);
```

An outcome counts in the denominator only when `status='verified'`.

### 4.3 KPIs

```sql
CREATE TABLE IF NOT EXISTS kpis (
  id             TEXT PRIMARY KEY,
  project        TEXT NOT NULL,
  name           TEXT NOT NULL,
  unit           TEXT NOT NULL,       -- tasks | dollars | hours | percent | incidents | custom
  direction      TEXT NOT NULL,       -- increase | decrease | maintain
  owner          TEXT,
  baseline_value REAL,
  current_value  REAL,
  target_value   REAL,
  period         TEXT,                -- weekly | monthly | release | custom
  created_at     REAL NOT NULL,
  updated_at     REAL NOT NULL
);
```

### 4.4 Outcome to KPI links

```sql
CREATE TABLE IF NOT EXISTS outcome_kpi_links (
  id                TEXT PRIMARY KEY,
  project           TEXT NOT NULL,
  outcome_id        TEXT NOT NULL,
  kpi_id            TEXT NOT NULL,
  contribution      REAL,
  contribution_unit TEXT,
  confidence        TEXT NOT NULL,    -- measured | estimated | directional
  rationale         TEXT,
  created_at        REAL NOT NULL
);
```

This is how Switchboard answers: "What did this agent spend move?"

---

## 5. Core operations

### 5.1 Spend ingestion

Gateway callback:

```http
POST /tally/v1/spend/ingest
```

Agent report:

```text
report_usage(project, task_id, agent_id, model, tokens?, cost_usd?, confidence?, metadata?)
```

Request:

```json
{
  "source": "agent_report",
  "confidence": "reported",
  "project": "helm",
  "task_id": "CHART-8",
  "claim_id": "taskclaim_01J...",
  "agent_id": "codex/CHART#b12e",
  "runtime": "codex",
  "provider": "openai",
  "model": "gpt-5",
  "prompt_tokens": 82000,
  "completion_tokens": 11000,
  "cost_usd": 3.42,
  "metadata": {
    "reporting_method": "runtime_usage"
  }
}
```

### 5.2 Outcome recording

```text
record_outcome(project, task_id?, claim_id?, type, title, evidence, value?) -> outcome
verify_outcome(project, outcome_id, verifier, verification, evidence?) -> outcome
reject_outcome(project, outcome_id, verifier, reason) -> outcome
```

### 5.3 KPI mapping

```text
create_kpi(project, name, unit, direction, baseline?, target?) -> kpi
link_outcome_to_kpi(project, outcome_id, kpi_id, contribution?, confidence?, rationale?) -> link
update_kpi_value(project, kpi_id, current_value, evidence?) -> kpi
```

### 5.4 Queries

```text
get_task_tally(project, task_id) -> tally
get_epic_tally(project, epic_id) -> tally
get_agent_tally(project, agent_id, since?) -> tally
get_kpi_tally(project, kpi_id, since?) -> tally
get_budget_status(project, task_id? epic_id?) -> budget
```

Task tally response:

```json
{
  "project": "helm",
  "task_id": "CHART-8",
  "spend": {
    "cost_usd": 4.91,
    "total_tokens": 117000,
    "by_source": {
      "gateway": {"cost_usd": 1.49, "confidence": "exact"},
      "agent_report": {"cost_usd": 3.42, "confidence": "reported"}
    }
  },
  "outcomes": {
    "verified": 1,
    "proposed": 0,
    "rejected": 0
  },
  "unit_cost": {
    "cost_per_verified_outcome": 4.91
  },
  "kpis": [
    {
      "name": "client-visible chart capability",
      "contribution": 1,
      "confidence": "measured"
    }
  ]
}
```

---

## 6. Cost attribution rules

Default attribution:

1. Spend with `outcome_id` attaches directly to that outcome.
2. Spend with `claim_id` attaches to the claim and any outcome produced by that claim.
3. Spend with `task_id` attaches to the task and verified outcomes for that task.
4. Spend with only `project` remains project overhead.
5. Shared overhead can be allocated by policy but must remain labeled as allocated.

Do not count unverified outcomes in cost-per-outcome. Show them separately as pending value:

```text
verified cost per outcome = spend / verified_outcome_count
pending spend = spend attached to proposed outcomes
overhead spend = spend not tied to an outcome
```

---

## 7. Budget policy

Budgets are where Tally connects to interrupts and dispatch.

Budget scopes:

- task;
- epic;
- project;
- agent/session;
- KPI.

Budget event thresholds:

| Threshold | Event | Default action |
|---:|---|---|
| 70 percent | `budget.warning` | `heads_up` to agent/operator |
| 90 percent | `budget.wrap_up` | `redirect` to summarize/finish |
| 100 percent | `budget.exhausted` | `stop` with required ack |
| overrun plus missed ack | `budget.kill_requested` | runner kill if available |

`claim_next` must include budget status and should avoid dispatching work to agents whose
expected cost exceeds the remaining budget unless an operator overrides it.

---

## 8. KPI semantics

KPI links should support three confidence levels:

| Confidence | Meaning |
|---|---|
| `measured` | external metric or verification directly measured contribution |
| `estimated` | human/agent estimate with rationale |
| `directional` | outcome is believed to move KPI, contribution not quantified |

Examples:

| Outcome | KPI link |
|---|---|
| feature shipped | `roadmap_verified_items +1` |
| bug fixed | `known_regressions -1` |
| RFP section accepted | `proposal_completion_percent +8` |
| incident root cause found | `mttr_hours decrease`, directional |
| reusable decision recorded | `future_rework_avoided`, estimated |

The product should make it normal to say "we spent $18.40 to move this KPI by one verified
unit" and equally normal to say "this KPI link is directional only."

---

## 9. UI requirements

Tally UI should appear early in the places operators already look:

- task row: cost chip, budget chip, source confidence;
- task detail: spend by agent/model/source, verified outcomes, KPI links;
- epic view: total spend, verified outcomes, cost per verified outcome;
- agent profile: cost per verified outcome, abandon/rework rate;
- dispatch view: model recommendation and budget remaining;
- project dashboard: KPI movement and spend mapped to outcomes.

Avoid a token dashboard as the main view. Tokens are raw material. Outcomes and KPIs are the
unit of account.

---

## 10. Adapter requirements

Adapters should report usage at one of three levels:

| Level | Behavior |
|---|---|
| `none` | adapter cannot see usage; report capability only |
| `task_summary` | report usage/cost at task completion |
| `turn_level` | report usage per model call/turn |

Every report must include:

- project;
- agent_id;
- runtime;
- task_id or claim_id when known;
- model/provider when known;
- source/confidence;
- raw evidence or reporting method.

Adapters must not fabricate precision. If only dollars are known, token fields can be zero.
If only tokens are known, cost can be estimated and marked `estimated`.

---

## 11. Conformance tests

`+OXP`/Tally conformance requires:

1. Gateway spend ingestion is idempotent by request id.
2. Agent-reported spend lands with `source=agent_report`.
3. Spend records are scoped to project and cannot cross workspaces.
4. A proposed outcome does not count in the denominator.
5. A verified outcome does count in the denominator.
6. Cost per verified outcome separates gateway and agent-reported streams.
7. KPI links preserve confidence and rationale.
8. Budget thresholds emit activity events and signals.
9. `claim_next` returns budget status when Tally is enabled.
10. UI/API labels advisory spend as advisory.

---

## 12. Implementation order

1. Implement or finish ADR-0002 `llm_spend` ledger with `confidence`, `claim_id`, and
   `outcome_id`.
2. Add gateway callback ingestion for Stream A.
3. Add `report_usage` MCP/REST operation for Stream B.
4. Add `outcomes`, `kpis`, and `outcome_kpi_links`.
5. Add task and epic tally read APIs.
6. Add task-detail cost chip and confidence labels.
7. Wire budget warnings into the interrupt path.
8. Feed budget/model guidance into `claim_next`.
9. Add KPI dashboard once enough verified outcomes exist.

---

## 13. Exit criteria

Tally is product-ready for the first commercial wedge when:

- a task can show gateway-measured and agent-reported spend in one ledger;
- spend can be tied to a verified outcome;
- an outcome can be linked to at least one KPI;
- the UI can show cost per verified outcome;
- budget thresholds can send `heads_up`, `redirect`, or `stop`;
- Stream B is clearly labeled by confidence/source;
- the product can answer: "What did this outcome cost, and which KPI did it move?"

## 14. Implementation Notes

TALLY-1 shipped the two-stream `llm_spend` ledger and task spend rollup. TALLY-2 adds the
outcome/KPI denominator:

- `outcomes`, `kpis`, and `outcome_kpi_links` tables;
- REST endpoints under `/tally/v1/outcomes`, `/tally/v1/kpis`, `/tally/v1/outcome_kpi_links`,
  `/tally/v1/task/{task_id}`, and `/tally/v1/kpi/{kpi_id}`;
- MCP tools `record_outcome`, `verify_outcome`, `reject_outcome`, `create_kpi`,
  `update_kpi_value`, `link_outcome_to_kpi`, `get_task_tally`, and `get_kpi_tally`;
- proposed outcomes remain pending value and do not count in cost-per-outcome;
- verified outcomes count in the denominator;
- KPI links preserve measured/estimated/directional confidence and expose cost per contribution
  unit when contribution is numeric;
- spend reported with only `outcome_id` resolves back to the owning task.

The remaining Tally work is TALLY-3: board/UI surfaces, budget chips, and dashboard placement.
