# Mission Trace: persistent task journeys, one read surface, and typed memory

**Status:** Proposed · scoped against master `ef439a19` (fix BUG-239/240), 2026-07-30
**Owner:** operator (Steve) · drafted by Claude
**Schema family:** `switchboard.mission_trace.v1`

**Provenance and confidence.** Built from a 5-agent read of the codebase (evidence
stores, Mission Bot pipeline, the ~50-entry breakdown corpus, API/MCP conventions,
replay/audit infra), then adversarially reviewed by 4 independent agents whose verdicts
were **all "flawed"** — 13 blockers, 30 majors. This is v2: every blocker is corrected
below, and §11 records what the first draft got wrong, because that list is itself
evidence about which assumptions in this codebase are dangerous.

---

## 0. The one-paragraph version

Every recurring Autopilot failure of the last month is a question the system cannot
answer about its own past: *what did this task already try, at which head, and why?*
The raw material mostly exists but is unusable: the current-state authority
(`completion_runs`) overwrites its reason_code on every tick; the livelock counter is
incremented in production and **read by nobody**; the per-episode corpus
(`decision_records`) covers only the In-Review→merged loop and collapses ticks by
design; and the pre-PR half of a task's life (dispatch, claim refusal, launch failure,
host skip) is recorded nowhere durable. This design adds **one** append-only event
ledger with a grain that can actually hold those events, exposes **one** exact-head read
surface (`get_mission_context`) over MCP + REST + the dock, and — as a separate,
flag-gated, highest-risk phase — gives the Mission Bot reducer a typed memory input so
loops become decisions instead of archaeology. It pays the ADR-0006 subtraction rule
explicitly (§2.2), carries zero lifecycle authority, and upgrades the audit export to a
windowed, cursored, scope-separated surface with tamper-evidence.

---

## 1. Problem statement, grounded in incidents

### 1.1 The recorded-then-lost pattern (all claims re-verified for v2)

| What happens today | Where | Consequence |
|---|---|---|
| Every completion transition **overwrites** the task's state/route/reason_code | `completion_runs` `UNIQUE(task_id)` + `ON CONFLICT DO UPDATE` (`storage/repositories/completion_runs.py:378-396`) | The reason-code timeline is destroyed as it is produced; `decision_records`' own module docstring names this as its reason for existing |
| The livelock counter is written in production and **read by nobody** | `executor.py:380-399` calls `note_stable_replay` on every verified idempotent replay — comment: *"Count it so the classifier's convergence ladder can escalate deterministically"* — but the ladder was **deleted** with the old classifier (`ada0bac4`, #1016) and grep finds **zero consumers** of `stable_replays`/`churn` in any decision path | The exact loop signal for QA-24/COORD-98 is counted and discarded |
| …and that counter is now effectively frozen anyway | Its writers (`transition_completion_run_in`, `note_stable_replay`) are **not on the Mission Bot tick path**: `execute_mission_command` performs no `completion_runs` write, and `run_mission_tick` only *reads* the run to stamp `decision_attempt`/`state_version` (`driver.py:336-341`). The live caller of `transition_completion_run` in `src/` is `report_stale_assignment` | Memory sourcing cannot rely on these counters (§5) |
| The phase ledger is **not** the dormant flight recorder it appears to be | `task_execution_completion_phases` has live production writers via **raw SQL** in `claims.py:1675-1687` (INSERT `review_handoff`/`pending`) and `claims.py:1746-1758` (UPDATE→`succeeded`), using a **uuid5** `transition_id` while `task_completion.py:82-84` and `completion_runs.py:184-186` use **sha256** for the same identity tuple | Two incompatible identities for one row; the table is mutable (pending→final) and cannot be hash-chained as-is (§3.3) |
| The 32KB mission tape is rendered, then dropped | built in `completion_driver.py:283-310`; `task_execution.start_task` forwards `instruction` only to a launcher test seam (`task_execution.py:996-998`), never to `connect_dispatch.enqueue_task` | The booted runner never sees the dossier it was booted for |
| Per-tick effect receipts / WAIT observations survive only in one mutable JSON column overwritten every tick | `autopilot_scopes.last_result_json` (`scoped_completion_coordinator.py:159-161`) | Anything older than the newest tick per scope is gone |
| journald retains ~2h under load | 2026-07-30 incident: 653MB of tick JSON churned the cap | Logs are a cache, not a record; only tables survive |

### 1.2 The recurring bug classes (17)

From the full breakdown corpus, the BUG-2xx history, and `tests/test_bug2*.py`.
Coverage is claimed honestly in §7 — including the classes this design only *observes*.

1. **Memoryless repair loops** — COORD-57 attempt 50; COORD-98 generation 50; UI-71
   attempt 161; QA-19's 97 blind retries; QA-24's 35 `START_REMEDIATION` ticks in 20min
   against a green run already in the table.
2. **Non-idempotent / undeliverable escalations** — BREAKDOWN 49: 17 pending attention
   rows in 503s, all bound to one runner_session_id; BREAKDOWN 48: `delivered:false` on
   every channel, and `attention.push_missed` has **zero readers**.
3. **Diagnostic-discard** — the corpus's self-declared dominant class (7+ in one day).
4. **Stale/wrong-head evidence** — BUG-214/216/218(×5)/234/235/239/240. BUG-239: 83% of
   prod `external_ci_runs` rows have NULL `task_id`, so a green exact-head run read as
   `missing`.
5. **Dead-generation wedges** — BREAKDOWN 57, BUG-206/208/232/237, COORD-82/83.
6. **Silent skips / fail-quietly** — the #860 family; BREAKDOWN 52's
   `pending: 3, acted: [], refused: []`.
7. **Zombie liveness** — BREAKDOWN 21 (9h dead at a prompt, heartbeats green), BUG-228.
8. **Lost webhooks / provenance drops** — UI-72's 502'd merge webhook; BREAKDOWN 51
   (superseded-close means no merge webhook can *ever* fire).
9. **Silent merge-queue ejections** — COORD-76, BUG-201, PR #963, and #1090 this week.
10. **Status-gate claim admission deadlocks** — BUG-202/174/209, BREAKDOWN 58.
11. **Two-copies vocabulary drift** — BREAKDOWN 11/15/46/52.
12. **Host/server contract + deploy drift** — BREAKDOWN 22/43/44/45.
13. **Evidence-grammar / split-surface evidence** — BREAKDOWN 20, UI-71, BUG-203.
14. **Signals recorded but never consumed** — reason-code dominance uncounted; the 117:1
    tick:episode stall fingerprint computed by nothing; `push_missed` unread; **and the
    `stable_replays` counter above.**
15. **Fix-composition failures / evidence-starved rollbacks** — COORD-49×BUG-184; the
    #1086 rollback misdiagnosis.
16. **Lost forensics / retention** — journald 2h; `get_execution_transcript` permanently
    `complete: false` (SIMPLIFY-9 unlanded).
17. **Hot retry without backoff / budget exhaustion** — BREAKDOWN 13/16/52, BUG-238.

---

## 2. Principles and the subtraction ledger

### 2.1 Inherited rules (none invented)

1. **Storage, not mechanism** (ADR-0006 D1; ADR-0021). Gates nothing, routes nothing.
   A missing/broken trace table degrades to a NAMED no-op
   (`skipped: true, reason: "<table>_absent"`) — the `decision_records` pattern.
   **History must never fail truth.**
2. **Append-only events + one small projected head** — the house split.
   Never rewrite a recorded row; outcomes only move rows out of `open`.
3. **Exact-head identity** (BUG-234 cl.2, BUG-239/240). Evidence selected by
   `project + task + PR + exact head`, valid across Work Sessions and URL spellings.
   Who recorded it is provenance, never validity. Gates that fail on head mismatch
   record **both** heads.
4. **History is audit, never a hidden classifier input.** Anything the reducer reads
   must be **in the snapshot** (the only replay substrate) as typed bounded fields.
5. **Causes are required at failure boundaries** — a failure row without
   `cause` (code, message, expected, observed) is itself a defect, conformance-tested.
6. **Never cache a diagnosis; freely cache a fetch.** GitHub-backed reads obey
   `open_prs.py`: ≤1 sweep/project/60s, typed terminal `rate_limited: …`, never 500.
7. **Fleet contract:** repository modules only; `_write_through` for multi-statement
   writes (PERF-2); `*_in(c, …)` variants for atomicity with the causing transition;
   numbered append-only migrations; versioned `switchboard.<name>.v1` envelopes;
   write-time-materialized export projection; server-stamped actor provenance.

### 2.2 Subtraction ledger (ADR-0006 D2 — paid explicitly)

This design adds one table and one read surface. It **deletes**:

- **`coordination_receipts`** — already verdicted DELETE by ADR-0021 (RECON-12); it is a
  read-only projection over `activity` and its role is subsumed by the trace read.
- **`autopilot_scopes.last_result_json` as the record-of-record** — the column may
  remain as a debug convenience, but no surface may read it as history once TRACE-3
  ships; per-tick truth moves to the ledger.
- **The operator practice of hand-joining 7+ stores over SSH** — replaced by one read.

TRACE-4 (typed memory) adds a **new reducer branch**, which is new *mechanism*, not
storage. It is therefore justified separately in §5 against the same rule, and is the
one phase that may be declined without losing the rest of the design.

---

## 3. The persistent record

### 3.1 Why not the existing phase ledger (corrected in v2)

The first draft proposed wiring `task_execution_completion_phases`. Adversarial review
killed that, and the reasons are worth recording because they constrain any alternative:

- **Identity has no attempt dimension.** `UNIQUE(task_id, pr_number, head_sha,
  runner_generation, phase)` — UI-71's 161 attempts and BREAKDOWN 49's 17 rows all sit
  at *one* generation/head/PR and would collapse into a single row. It cannot record
  the fingerprint of bug class 1.
- **It cannot hold pre-PR events.** Writers require `pr_number > 0` and
  `len(head_sha) >= 7` (`task_completion.py:71-73`, `completion_runs.py:310-311`), so
  dispatch, claim refusal, launch failure and host skip have no representable identity.
- **A second write raises inside the caller's transaction.**
  `"completion transition identity conflict"` (`completion_runs.py:197-198`) — thrown
  from `_append_history_in` at `completion_runs.py:398`, i.e. the trace write aborts the
  transition it was meant to document. That is precisely bug class 3.
- **It is not append-only and has two identities.** Live raw-SQL writers in
  `claims.py:1675-1687/1746-1758` with a **uuid5** `transition_id`; the repository
  writers use **sha256** for the same tuple; promotion mutates the row in place.

The table keeps its existing job. Its dual-identity defect is filed as a **separate
prerequisite** (TRACE-0) because a hash chain or a conformance rule over it is
impossible until one derivation wins.

### 3.2 `mission_trace_events` — one new append-only ledger

New table (one `DDL_MIGRATIONS` entry, next free number ≥ `0123`; highest in use is
`0122`):

```sql
CREATE TABLE IF NOT EXISTS mission_trace_events (
  event_id     TEXT PRIMARY KEY,          -- 'mte-' + sha256(project⇟task⇟seq)[:20]
  project      TEXT NOT NULL,
  task_id      TEXT NOT NULL,
  seq          INTEGER NOT NULL,          -- monotonic per (project, task_id)
  occurred_at  REAL    NOT NULL,
  kind         TEXT    NOT NULL,          -- closed registry, §3.3
  outcome      TEXT    NOT NULL,          -- pending|succeeded|failed|refused|observed
  reason_code  TEXT    NOT NULL DEFAULT '',   -- switchboard.reason_code.v1 registry
  -- correlation (ALL nullable — pre-PR events are first-class)
  pr_number    INTEGER,
  head_sha     TEXT,
  generation   INTEGER,
  execution_id TEXT, wake_id TEXT, claim_id TEXT, effect_key TEXT,
  work_session_id TEXT,
  attempt      INTEGER NOT NULL DEFAULT 0,    -- the class-1 fingerprint
  actor        TEXT NOT NULL DEFAULT '',      -- server-stamped provenance only
  server_sha   TEXT NOT NULL DEFAULT '',      -- deploy marker (class 15)
  evidence_json TEXT NOT NULL DEFAULT '{}',
  cause_json    TEXT NOT NULL DEFAULT '{}',   -- REQUIRED non-empty when outcome='failed'
  row_hash     TEXT NOT NULL DEFAULT '',      -- H(canonical insert-time row)
  prev_hash    TEXT NOT NULL DEFAULT '',      -- chain over INSERTS only (§3.4)
  UNIQUE(project, task_id, seq)
);
```

Design notes, each answering a specific review finding:

- **Grain is `(project, task_id, seq)`** — monotonic per task. Repetition is
  representable; `attempt` carries the loop count. Nullable `pr_number`/`head_sha` admit
  pre-PR life.
- **Rows are immutable.** No promotion, no in-place update. A `pending` event is
  followed by a *second* event; current state is a fold. This is what makes §3.4's chain
  sound (the earlier draft chained over mutable columns — invalid).
- **`kind` is a closed registry** with an AST conformance test asserting emitter and
  validator agree (BUG-184 guard pattern, bug class 11).
- **Writes never raise into the caller.** The repository exposes `append_event_in(c,…)`
  for atomicity with the causing transition, wrapped so any failure is logged as a named
  skip and **cannot** abort that transition — enforced by a test that poisons the write
  and asserts the transition still commits.
- **`seq` allocation** happens on the writer thread inside `_write_through`
  (`SELECT COALESCE(MAX(seq),0)+1 … WHERE project=? AND task_id=?`), which is also what
  makes `prev_hash` race-free (§3.4).

### 3.3 Event kinds and write sites

| kind | Emitted at | Bug class |
|---|---|---|
| `dispatch` (+generation, wake_id, idem key) | `connect_dispatch.enqueue_task` | 1, 5 |
| `claim_granted` / `claim_refused` (+admission facts consulted) | `claims.claim_task` both paths | 10 |
| `runner_boot` / `runner_terminal` (+lease-end reason: surrendered / ttl_expired / fenced) | runner terminalization | 5, 7 |
| `decision` (output, reason_code, attempt) | `run_mission_tick`, after `reduce_mission` | 1, 3, 14 |
| `effect_receipt` (incl. WAIT + idempotent replays — not just mutating) | `execute_mission_command` | 3, 14 |
| `ci_verdict` (exact head, failing contexts) | external CI recording | 4 |
| `review_verdict` (+both heads on mismatch) | `review_verdicts.record` | 4 |
| `queue_enqueued` / `queue_ejected` (**GitHub's reason**, dwell) | effects ledger + `open_prs` sweep | 9 |
| `attention_raised` / `attention_delivery` (per-channel `delivered:false`) | the real producer — `human_blocker.py:349`, **not** the classifier (§5) | 2 |
| `skip_refusal` (host wake skips: who, what, reason) | agent_host tick refusal points | 6 |
| `deploy_observed` (merge SHA became prod ancestor) | autodeploy verify | 15 |
| `merged` / `done` | provenance webhook / reconcile | 8 |

**Expectation records** (class 8): `queue_enqueued` carries `expects: merge_provenance`.
A periodic sweep (generalizing the startup-only `sweep_open_pr_merges`) turns the
*absence* of the terminal event into a queryable, alertable state.

### 3.4 Tamper-evidence (corrected)

Chain over **insertions only**, which the immutability rule in §3.2 now makes sound:
`row_hash = H(canonical row at insert)`, `prev_hash = row_hash of (project, task_id,
seq-1)`. Computed on the writer thread inside the same `_write_through` that allocates
`seq`, so there is no read-modify-write race. Chain heads are anchored in a dedicated
`mission_trace_anchors` row — **not** in `activity`, because `delete_task` hard-deletes
activity rows (`tasks.py:1193-1196`) and would silently break anchoring.
`verify_trace_integrity(project, window)` re-walks and reports the first divergence.

### 3.5 Explicitly not stored

No prose logs, prompts, or transcripts (SIMPLIFY-9 stays its own deliverable; the trace
stores `transcript_ref` pointers). No diagnosis or proposed repair — recomputed live.
No authority: nothing reads this table to decide admission, gating, or routing.

---

## 4. The read surface: `get_mission_context(task_id)`

One exact-head projection, rebuilt live per request, never cached as a document.

```jsonc
{
  "schema": "switchboard.mission_trace.v1",
  "project": "switchboard", "task_id": "QA-24", "generated_at": 1785375000.0,
  "current": {                       // instrument panel — fixed size, live
    "pr": 1097,
    "head_sha": "d76647d3…",
    "head_source": "github_live",    // github_live | projection_stale | unavailable
    "runner": "none",                // Capacity/lease liveness, never host-reported status
    "ci":       {"status": "green",  "found_by": "exact_head", "url": "…"},
    "review":   {"status": "passed", "verdict_id": "…", "head_matches": true},
    "executed_tests": {"status": "passed", "work_session": "older_but_valid"},
    "preflight":{"status": "passed", "adopted_from_work_session_id": "…"},
    "merge":    {"status": "not_armed", "queue": null},
    "attention":{"pending": 0, "undelivered": 0},
    "done": false,                   // ONLY canonical merge provenance (ADR-0006)
    "missing": []                    // typed reason codes — the "why isn't this moving" answer
  },
  "memory": {"attempts_at_head": 2, "same_reason_streak": 0, "last_reason_code": "",
             "routes_tried_at_head": ["implementation", "review_merge"]},
  "attempts": [ {"generation": 1, "role": "implementation", "outcome": "published_pr",
                 "head_sha": "…", "events": [{"kind": "dispatch", "at": …}, …],
                 "reason_codes": ["…"], "cause": null} ],
  "attempts_truncated": false        // recent inline; older by cursor
}
```

Operator rules carried verbatim: selection by `project + task + PR + exact head`; agent
identity and Work Session are provenance, not validity; other-head evidence is excluded
from `current` (it stays in `attempts`); repeated reads are identical and cause no
mutation; this JSON never becomes a lifecycle authority.

**Three doors, one implementation** (`application/queries/mission_trace.py` over
`storage/repositories/mission_trace.py`):

1. **MCP** `get_mission_context(task_id, project)` — added to the tools module pattern
   and the fail-closed `READ_TOOLS` census (startup asserts completeness).
2. **REST** `GET /api/tasks/{task_id}/trace?project=` — `('read',)`, sync `def`,
   `ttl_read_cache` + `etag_json`, degrades to `{"unavailable": reason}`.
3. **Dock** — "Trace" in the PR card overflow (beside Close PR) and a task-modal tab
   beside Activity, rendering `current`, `missing`, and the attempts timeline **with
   queue ejections and GitHub's reason** (ending the silent-eject blindness that cost us
   #1090 twice this week).

**Migration insulation, honestly scoped.** The projection joins many stores, so "one
repository module" was overstated. What the design *does* guarantee: every consumer
(dock, agents, auditors, future services) reads through the REST/MCP surface, never SQL;
all trace SQL lives in one repository module; the joined stores are read through their
existing repositories. A Postgres swap (ADR-0018) therefore changes adapters, not
consumers.

**Runner boot context.** The execution assignment stays content-blind:
`launch_pointer.trace = {tool: "get_mission_context", task_id}` — a pointer the agent
fetches over MCP after boot. **The first draft's "re-enable `prior_attempts` — delete
the `del`" is withdrawn** (§11): it would add a key at dispatch that claim-time
re-derivation cannot produce, failing `require_exact_execution_assignment`'s whole-dict
equality and **refusing every managed claim fleet-wide**.

---

## 5. TRACE-4: typed memory (new mechanism, flag-gated, declinable)

**What this is.** The reducer is memoryless — zero prior-attempt inputs. The convergence
ladder that once consumed loop counters was **deleted** with the old classifier
(`ada0bac4`, #1016); `_fresh_decision_context` is a tombstone, not a severed seam. So
this phase **builds a new absorbing-escalation branch inside
`domain/mission_bot/reducer.py`**. That is new mechanism under ADR-0006 and is justified
here on its own merits — or declined, leaving §§3–4 and §6 intact.

**Why it is worth it.** `note_stable_replay` is incremented at exactly the livelock loop
point and read by nobody: the metric that would have stopped QA-24 at tick 3 was counted
35 times and discarded.

**Full cost of a new reducer output** (understated in v1): the `MissionOutput` enum, the
three exhaustive maps `_compat_route`/`_compat_state`/`_compat_effect`
(`reducer.py:97-145`), `completion_runs.STATES`/`ROUTES` frozensets — *an unlisted state
raises `CompletionRunError`, which is literally BUG-184* — a branch in
`execute_mission_command`, and `tests/test_bug184_completion_state_vocabulary.py`.

**Mechanism:**

- Snapshot carries `mission_memory` — typed, bounded: `attempts_at_head` (bucketed int),
  `same_reason_streak` (bucketed int), `last_reason_code` (registry enum),
  `routes_tried_at_head` (≤4 enums), `prior_generation_terminal` (bool).
- **Sourced from `decision_records.tick_count` and `completion_runs.attempt`** — monotone
  per-row state — **not** from episode counts, and not from the convergence counters,
  which §1.1 shows are effectively frozen off the Mission Bot path.
- **Absent block ⇒ memoryless behaviour** (`attempts_at_head=0`), so archived snapshots
  replay to unchanged verdicts. Pinned by test.
- **Memory fields do NOT join `FEATURE_FIELDS`.** `episode_hash` covers the verdict plus
  the feature projection, so a tick that only increments a counter keeps its hash and
  collapses via `tick_count` exactly as today. Two consequences stated plainly: episodes
  collapse across differing memory, and the retained `snapshot_json` is the **first**
  tick's. If a memory signal must ever be poolable it enters as a derived rarely-changing
  enum (`convergence_pressure: none|rising|at_ladder`) via a reviewed `FEATURE_FIELDS`
  migration with a `FEATURES_VERSION` bump.
- **Effect-key safety.** The mission idempotency key is minted from
  `{output, task_id, pr, head_sha, reason_code, role, evidence_identity}`
  (`reducer.py:58-71`) and gates the effect ledger. A memory-driven `reason_code` change
  therefore mints a **new** key for an unchanged world, which would make an
  already-verified mutating effect claimable again — the opposite of absorption.
  **Requirement:** the absorbing branch may only change the verdict to a *terminal,
  non-mutating* output (no ledger claim), and a test asserts no new mutating effect key
  is minted by a memory-only delta.
- **Attention dedupe belongs at the producer.** The tick cannot emit `escalate_human` at
  all (`execute_mission_command` has no such branch; `executor._escalate_human` is
  reachable only via a function with zero `src/` callers), and
  `test_coord46_human_attention.py` pins that classification never writes attention. The
  real producer is `human_blocker.py:349` — the dedupe (and a new
  `attention_requests.repeat_count` + `last_seen_at`, excluded from `request_hash`, keyed
  `project, task_id, reason_code, head_sha`) goes **there**.

**Gates.** Gold 48 + three new scenarios (QA-24's 35-tick loop, UI-71 attempt-161,
BREAKDOWN-49's flood); the property suite's no-spin invariant across all 1,080 PR cells;
`COMPLETION_CLASSIFIER_VERSION` bump.

**Corpus replay is not a valid gate for this phase, and v1 was wrong to name it.** Two
reasons: archived snapshots lack `mission_memory` and by design replay to unchanged
verdicts (so the check certifies nothing), and the gate is *already* 100% noise — the
production driver records an 8-key decision while replay compares it to
`classify_completion`'s output, which carries extra `schema`/`retry_policy` keys, by full
canonical-JSON equality. **TRACE-4 therefore includes a prerequisite:** fix the recorded
decision shape (or compare a declared subset) and pin it with a test that records through
`run_mission_tick` rather than a hand-built decision. Until then, gold + property are the
sole proof.

---

## 6. Enterprise: audit, retention, migration

### 6.1 Today

`get_audit_export` dumps ~26 tables, redacted, `write:system`-gated — but omits the
entire decision/review/attention/completion layer, has no window, no task filter, no
pagination, no streaming, and no integrity chain. A read-only auditor cannot call it.

### 6.2 The audit surface

- **Windowed + cursored:** `since`/`until` + rowid cursors per section. Trace events
  cursor cleanly on `(task_id, seq)`; **sections whose tables lack a stable monotonic
  key are exported whole with an explicit `cursored: false` marker** rather than
  pretending to page.
- **Streaming:** JSONL via `StreamingResponse` with a generator that yields per batch;
  handlers stay sync `def` on the threadpool. JSONL doubles as the **DB-migration
  vehicle** — schema-versioned rows a Postgres importer can replay.
- **Auditor principal — needs a middleware change, not just a scope.** There is no
  `_read_required_scopes` today: `middleware.py:172` applies bare `("read",)` to every
  protected GET, and `deps.py:217-225` grants bare `read` broadly. TRACE-5 therefore
  adds `_read_required_scopes(path)` returning `("read:audit",)` for `/api/audit/*`
  GETs, mirroring `_write_required_scopes`, **and** the handler calls
  `resolve_principal(..., ("read:audit",))`. Acceptance includes a deny test: a
  bare-`read` principal gets 403 on every `/api/audit/*` GET. This matters because the
  existing bundle already carries registry PII (`orgs`, `users`, `org_memberships`).
- **Coverage adds:** trace events, decision_records (projected half), coordinator
  decisions, review verdicts/findings, attention requests/events **with delivery
  outcomes**, preflight runs, completion_runs current state.
- **Export boundary:** write-time-materialized EXPORT vs PRIVATE split, asserted by test.

### 6.3 Retention

Projected/slim halves: forever. Bulky bodies keep the existing 90-day compaction with its
exceptions (escalated / non-convergent / human-resolved retained indefinitely). Trace
event rows are slim by design and retained indefinitely; deletion of `webhook_inbox` /
`idempotency_keys` is unchanged, but the *trace events derived from them* survive — which
is the part audits need.

---

## 7. Coverage matrix (v2 — overstated rows corrected)

| # | Class | Mechanism | Honest verdict |
|---|---|---|---|
| 1 | Memoryless loops | `attempt` on trace events; TRACE-4 memory + absorbing branch | **Fixed only if TRACE-4 ships.** §§3–4 alone make the loop *visible*, not stopped |
| 2 | Escalation floods / undelivered | dedupe + `repeat_count` at `human_blocker.py:349`; `attention_delivery` events; `undelivered` in `current` | Fixed at the real producer (v1 aimed at the classifier, which cannot emit it) |
| 3 | Diagnostic-discard | `cause_json` required on failure + conformance test; effect receipts persisted incl. WAIT | Fixed for new write sites. Does **not** retro-fix causes lost inside a raising transition (§3.1) |
| 4 | Wrong-head evidence | exact-head projection (`found_by`), both-heads on mismatch | Fixed for visibility; the gates themselves were fixed by BUG-239/240 |
| 5 | Dead-generation wedges | correlation columns join claim→execution→terminal event | Fixed (one query: active claim, terminal owner) |
| 6 | Silent skips | `skip_refusal` events from host + claim paths | Fixed |
| 7 | Zombie liveness | `runner` from lease liveness; `runner_terminal` + lease-end reason | Fixed |
| 8 | Lost webhooks | `expects: merge_provenance` + periodic sweep | Fixed |
| 9 | Silent queue ejections | `queue_ejected` + GitHub reason + dwell, rendered in dock | Fixed |
| 10 | Claim admission deadlocks | `claim_refused` with admission facts | Fixed |
| 11 | Vocabulary drift | closed `kind`/reason registries + AST conformance | Fixed for trace vocab; other vocabularies unchanged |
| 12 | Deploy/contract drift | `server_sha` on every event | **Visibility only** — bundle signing is separate work |
| 13 | Evidence-grammar | `missing` names expected key + near-miss | **Visibility only** — typed write paths per evidence kind remain separate |
| 14 | Unconsumed signals | reason-code dominance + tick:episode ratio over the ledger → attention | Fixed for aggregation; consuming them in decisions is TRACE-4 |
| 15 | Rollback misdiagnosis | `server_sha` join: "did the error class change across the deploy?" | Fixed |
| 16 | Lost forensics | durable rows, not journald; `transcript_ref` pointers | Fixed for events; full transcripts remain SIMPLIFY-9 |
| 17 | Budget exhaustion | spend-per-outcome aggregation per (task, reason_code) | **Visibility only** — backoff policy is separate |

---

## 8. Delivery plan

| Phase | Scope | Risk | Proof |
|---|---|---|---|
| **TRACE-0** | Canonicalise `task_execution_completion_phases.transition_id` (uuid5 vs sha256), dual-read/backfill prod rows | S | test: a `claims.py`-created pending row is promotable by `record_transition` |
| **TRACE-1** | `mission_trace_events` migration + repository + `append_event_in` with non-fatal wrapper + `seq`/hash on writer thread | S | poisoned-write test proves the causing transition still commits; chain-verify test |
| **TRACE-2** | Write sites (§3.3), each with its bug class | M | one test per site; conformance: failure without `cause_json` fails |
| **TRACE-3** | `get_mission_context` + REST + MCP census + dock Trace | M | browser test on real DOM; route test; rate-limit degrade test |
| **TRACE-4** | *(declinable)* recorded-decision-shape prerequisite; then snapshot `mission_memory`, new absorbing branch, vocab maps, attention dedupe at producer | **High** | gold 48+3; property no-spin; effect-key-stability test; flag `PM_MISSION_MEMORY=1` |
| **TRACE-5** | `_read_required_scopes` + `read:audit`, windowed/cursored/streamed export, `verify_trace_integrity` | M | bare-`read` denial test; export-boundary test; chain divergence test |

Rollout: merge queue only (no direct pushes — that cost us a red master this week),
ancestry-verified on prod, and the trace's own first job is to watch the QA canaries
converge.

## 9. Risks

- **Trace writes wedging the loop** — the exact failure mode of the existing
  `_append_history_in`. Mitigated by immutable appends, no raise into the caller, and a
  poisoned-write conformance test.
- **TRACE-4 changes decisions** — by design; flag-gated, gold/property-gated,
  effect-key stability asserted, declinable.
- **Write amplification** — events are per-transition, not per-tick; per-tick data stays
  episode-collapsed in `decision_records`.
- **GitHub budget** — projection reuses `open_prs` cache buckets; `head_source` says
  honestly when it served a projection.

## 10. Open questions

1. Hash chain per-project (proposed) or global? Per-project matches SEG isolation.
2. Is TRACE-4 in scope at all, or do we ship §§3–4/§6 and keep loop-stopping manual?
3. Initial pressure threshold for the new absorbing branch (there is no existing
   constant to inherit).
4. Dock Trace on mobile as well as desktop?

## 11. What v1 got wrong (kept deliberately)

Four independent reviewers returned **flawed**; 13 blockers. The corrections:

1. **"`task_execution_completion_phases` has zero production writes"** — false. Live
   raw-SQL writers in `claims.py`; only the *repository* entry points are unwired.
2. **"The convergence ladder is already implemented and merely severed"** — false. It was
   deleted with the old classifier (#1016). TRACE-4 is new mechanism.
3. **"Re-enable `prior_attempts` — delete the `del`"** — actively dangerous. Would refuse
   every managed claim fleet-wide via `execution_assignment_contract_mismatch`.
4. **"The phase ledger already has the right grain"** — false: no attempt dimension,
   PR+head required, raises on repeat, mutable, two identities.
5. **"Absorbing escalation via `escalate_human`"** — the tick cannot emit it; the real
   producer is `human_blocker.py`.
6. **"`repeat_count` bump"** — no such column exists; needs its own migration and a
   carve-out from `request_hash` immutability.
7. **"Corpus replay diff as the TRACE-4 gate"** — the gate is already 100% noise from a
   recorded-shape mismatch, and is blind to snapshot-embedded memory anyway.
8. **"New `read:audit` scope"** — insufficient alone; the middleware has no read-scope
   table, so bare `read` would still pass.
9. **"Anchor the chain in `activity`"** — `delete_task` hard-deletes activity rows.
10. **"One repository module insulates the migration"** — overstated; the projection
    joins many stores.

The pattern in that list is this codebase's own dominant failure mode: *a mechanism that
looks present because a symbol exists.* Every one of these was found by reading the code
rather than trusting the name.

---
*Recon: 5 agents over master `ef439a19`. Adversarial verification: 4 agents, all
verdicts "flawed", 13 blockers / 30 majors — folded into this v2.*
