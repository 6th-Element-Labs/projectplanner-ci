# Deliverable closure gate — verify, grade, and stamp before archive

- **Status:** Shipped (DELIVERABLES-12 … DELIVERABLES-22)
- **Date:** 2026-07-12
- **Product:** Switchboard
- **Deliverable:** `deliverable-closure-gate` on `project=switchboard`

> Closing a deliverable is not the same as closing its tasks. A deliverable is safe to mark
> **done** / **archived** only after an automated verifier proves the **whole** shipped against
> the acceptance criteria and proof requirements set at creation.

Gates 3 (economics / Tally rollup) and 4 (operator archive confirm) are deferred. This spec
covers **Gate 1 (scope)** and **Gate 2 (functional proof)** plus the operator button that
kicks off a verifier agent.

---

## Problem

Today:

- **Record outcome** (`submit_deliverable_outcome`) drafts a **breakdown proposal** — it starts
  work from a plain-English outcome, it does **not** verify closure.
- Deliverables often ship with **empty** `acceptance_criteria` and `proof_requirements`.
- Proof is scattered across linked tasks (merge provenance, offline evidence) with no
  deliverable-level **grade** or **stamp**.
- Operators cannot press one button and get “did we meet our functional goals?” with an auditable
  report.

---

## Product flow (target)

```mermaid
sequenceDiagram
    participant Op as Operator
    participant UI as Mission Page
    participant SB as Switchboard
    participant Agent as Verifier agent
    participant Harness as Gate harness

    Note over Op,Harness: At deliverable creation
    Op->>UI: Create deliverable with end_state,<br/>acceptance_criteria, proof_requirements
    UI->>SB: create_deliverable

    Note over Op,Harness: When work is winding down
    Op->>UI: Verify & stamp closure
    UI->>SB: request_deliverable_closure_verification
    SB->>Agent: dispatch (wake / claim / prompt)
    Agent->>SB: get_deliverable + get_mission_status
    Agent->>Harness: run registered checks
    Harness-->>Agent: per-check pass/fail + artifacts
    Agent->>SB: verify_deliverable_closure(report)
    SB-->>UI: grade + closure_report_id
    UI->>Op: Show grade; enable Done only on pass/waiver
```

### Two buttons on the mission header (do not conflate)

| Button | Today | Target |
|---|---|---|
| **Record outcome** | Drafts milestone/task breakdown from outcome text | Unchanged — *start* work |
| **Verify & stamp closure** | *(missing)* | Runs scope + functional gates; dispatches verifier agent; stamps grade |

---

## Gate 1 — Scope complete

**Question:** Are all linked tasks in a terminal state that does not block closure?

Checks (automated):

| Check | Pass condition |
|---|---|
| `no_blockers` | `get_mission_status.blockers` empty |
| `no_in_review` | `progress.in_review_count == 0` |
| `no_in_progress` | No linked task in `In Progress` / `Ready` / `Blocked` |
| `terminal_or_waived` | Every linked task is `Done` with terminal provenance, `Cancelled` with audited reason, or explicit waiver in closure report |
| `done_with_proof_ratio` | Optional minimum (default 1.0 for non-waived links) |

Output: `scope.pass`, `scope.blockers[]`, `scope.non_terminal_tasks[]`, `scope.waivers[]`.

---

## Gate 2 — Functional goals met

**Question:** Did the deliverable meet the acceptance criteria and proof requirements recorded
at creation?

### Intake at creation (DELIVERABLES-13)

When a deliverable moves to `in_progress`, require:

```json
{
  "end_state": "Plain-English success statement",
  "acceptance_criteria": [
    "8-agent concurrent MCP load gate passes committed SLO ratchet",
    "All linked perf tasks have merge or offline evidence",
    "ARCH-19 decision recorded; ARCH-21 waived or done"
  ],
  "proof_requirements": {
    "schema": "switchboard.deliverable_proof_requirements.v1",
    "gates": [
      {"id": "scope", "required": true},
      {"id": "harness:concurrent_load_gate", "required": true},
      {"id": "harness:test_concurrent_load_ratchet", "required": true},
      {"id": "harness:test_mcp_observability", "required": false}
    ],
    "proof_pointers": [
      {"kind": "task", "project": "switchboard", "task_id": "HARDEN-62"},
      {"kind": "artifact", "path": "perf/concurrent_load_slo.json"},
      {"kind": "adr", "path": "docs/decisions/0006-control-plane-freeze.md"}
    ]
  }
}
```

`proof_requirements.gates[]` references the **gate registry** (DELIVERABLES-14).

**Enforcement (shipped, DELIVERABLES-13).** The check lives in `store.create_deliverable`,
the single choke point every write goes through (MCP tool, `POST /api/deliverables`, the
breakdown/outcome flow, and any future Mission Page status control). It fires only on the
**transition into `in_progress`** (a new deliverable created as `in_progress`, or a status
change into it) — re-saving an already-`in_progress` deliverable is not re-validated, so
legacy deliverables with empty criteria stay editable. A rejection returns
`{"error": "deliverable intake incomplete", "details": [...]}` (HTTP 400 with the same body
on the REST route) naming each missing field. It validates: non-empty `end_state`, non-empty
`acceptance_criteria`, and a well-formed `proof_requirements` — a non-empty `gates` list where
each gate is `{id: str, required: bool}` with unique ids and (if present) the correct `schema`.
Each id must resolve to the Gate 1 `scope` built-in, the DELIVERABLES-14 registry, or a valid
inline gate definition; dangling references fail at intake instead of surfacing at closure.

Gated by **`PM_ENFORCE_DELIVERABLE_INTAKE`** (default **off**, mirroring the
`PM_VERIFY_COMPLETION_PUSH` rollout style) so existing deliverables and legacy flows are
unaffected; DELIVERABLES-22 turns it on per-prod after new deliverables are backfilled.

### Gate registry (DELIVERABLES-14)

Manifest: `deliverable_gates/manifest.json` (or per-deliverable YAML).

Each entry:

```json
{
  "id": "harness:concurrent_load_gate",
  "kind": "script",
  "command": ["python3", "scripts/concurrent_load_gate.py"],
  "timeout_s": 600,
  "env_allowlist": ["LOAD_GATE_*"],
  "required": true
}
```

Kinds: `script`, `pytest`, `store_check` (pure store assertions), `offline_evidence` (task must
have terminal offline provenance).

### Verifier agent prompt (DELIVERABLES-17)

On **Verify & stamp closure**, dispatch an agent with:

1. `prepare_agent_session(project, deliverable_id=...)`
2. `get_deliverable` + `get_mission_status`
3. Run each gate in `proof_requirements.gates`
4. Collect proof pointers (task provenance, artifact hashes)
5. Call `verify_deliverable_closure` with structured report
6. Post summary comment on deliverable; do **not** set `status=done` (operator or webhook path)

---

## Closure report schema

`switchboard.deliverable_closure_report.v1`:

```json
{
  "schema": "switchboard.deliverable_closure_report.v1",
  "report_id": "closure-mcp-agent-path-performance-1",
  "deliverable_id": "mcp-agent-path-performance",
  "project_id": "switchboard",
  "generated_at": 1783800000.0,
  "generated_by": "agent:cursor/DELIVERABLES-20-dogfood",
  "grade": "pass",
  "gates": {
    "scope": {"pass": true, "checks": []},
    "functional": {"pass": true, "checks": [
      {"id": "harness:concurrent_load_gate", "pass": true, "duration_s": 42.1, "artifact_hash": "..."},
      {"id": "harness:test_concurrent_load_ratchet", "pass": true}
    ]}
  },
  "acceptance_criteria_results": [
    {"criterion": "8-agent concurrent MCP load gate passes", "pass": true, "evidence": ["HARDEN-62", "perf/concurrent_load_slo.json"]}
  ],
  "waivers": [
    {"task_id": "ARCH-21", "reason": "cancelled_redundant_with_ARCH-19", "approved_by": "operator"}
  ],
  "recommendation": "safe_to_mark_done",
  "evidence_hash": "sha256:..."
}
```

`grade` is one of: `pass` (all required gates green), `hold` (a required check failed), `waive`
(operator-approved exceptions carry it). `report_id` is server-assigned
(`closure-<deliverable_id>-<n>`, monotonic per deliverable) so
`get_deliverable_closure_report(report_id=…)` can address a specific historical report; omitting it
returns the latest.

Persisted as deliverable activity `deliverable.closure_verified` and surfaced on mission header
(`metadata.last_closure_report`).

---

## API / MCP surface (DELIVERABLES-16)

| Tool / route | Purpose |
|---|---|
| `verify_deliverable_closure(project, deliverable_id, report_json?)` | Run gates (if no report) or accept agent-submitted report; stamp grade |
| `get_deliverable_closure_report(project, deliverable_id, report_id?)` | Fetch latest or specific report |
| `request_deliverable_closure_verification(project, deliverable_id, agent_id?)` | Operator button → dispatch verifier |
| `POST /api/deliverables/{id}/closure_verify` | REST parity |
| `POST /api/deliverables/{id}/closure_request` | REST dispatch |

**Policy:** `status=done` upsert rejected unless latest closure grade is `pass` or `waive`
(DELIVERABLES-19). `archived` allowed only from `done` (existing UI-11 rule).

The grade must come from the latest report persisted by `verify_deliverable_closure`; callers
cannot supply or overwrite the verifier-owned `last_closure_*` / `closure_reports` metadata in a
general deliverable upsert. A missing grade or `hold` fails closed and leaves the current status
unchanged.

For a cancelled or deliberately removed linked task, the operator records an explicit waiver
while requesting/running closure verification, for example
`waivers_json=[{"task_id":"ARCH-21","reason":"cancelled as redundant","approved_by":"operator"}]`.
The verifier audits that exception in the closure report and emits grade `waive` only when every
remaining required gate passes. The operator may then perform a separate `status=done` upsert;
neither cancellation nor the waiver request marks the deliverable Done by itself.

---

## Dogfood target (DELIVERABLES-20)

Retroactively close `mcp-agent-path-performance`:

**Acceptance criteria (retroactive):**

1. All linked perf/harden tasks Done with merge or offline evidence (ARCH-21 waived).
2. Production-shaped 8-agent concurrent load gate passes (`HARDEN-62` evidence).
3. CI SLO ratchet green (`HARDEN-64`, `perf/concurrent_load_slo.json`).
4. MCP observability exposes lock-wait + write latency (`HARDEN-63`).
5. SQLite stay decision recorded (`ARCH-19`); no Postgres scope unless SLO breach.

**Harness:**

```bash
python3 scripts/concurrent_load_gate.py
python3 test_concurrent_load_ratchet.py
python3 test_mcp_observability.py
```

First successful **Verify & stamp** produces the closure report that makes archive auditable.

---

## Task map (board)

| Task | Title | Milestone |
|---|---|---|
| DELIVERABLES-12 | Author closure gate spec + closure_report schema | 1 — Spec & intake |
| DELIVERABLES-13 | Require acceptance_criteria + proof_requirements at in_progress | 1 |
| DELIVERABLES-14 | Gate registry manifest for harness checks | 2 — Engine |
| DELIVERABLES-15 | verify_deliverable_closure store (scope + functional) | 2 |
| DELIVERABLES-16 | MCP/REST + deliverable.closure_verified audit | 2 |
| DELIVERABLES-17 | request_deliverable_closure_verification agent dispatch | 3 — Operator |
| DELIVERABLES-18 | Mission Page: Verify & stamp closure button + grade UI | 3 |
| DELIVERABLES-19 | Block status=done without pass/waiver closure grade | 3 |
| DELIVERABLES-20 | Dogfood closure on mcp-agent-path-performance | 4 — Dogfood |
| DELIVERABLES-21 | test_deliverable_closure_gate.py + CI registration | 4 |
| DELIVERABLES-22 | Exit gate review + runbook for new deliverables | 4 |
| DELIVERABLES-23 | Agent Host: bounded automated closure-verifier dispatch | 5 — Automation |

Seed: `scripts/seed_deliverable_closure_gate.py` (idempotent).

View: `?project=switchboard&deliverable=deliverable-closure-gate#tab-mission`

---

## Operator rollout and closeout runbook (DELIVERABLES-22)

### 1. Audit before changing lifecycle state

Run the intake inventory against each project that owns deliverables:

```bash
.venv/bin/python scripts/audit_deliverable_intake.py --project switchboard
```

The report separates:

- `compliant`: a deliverable already has `end_state`, non-empty `acceptance_criteria`, and
  structurally valid `proof_requirements.gates`;
- `pending_contract`: a proposed/approved deliverable that must receive that contract before it
  can enter `in_progress`;
- `grandfathered`: an already-active legacy deliverable missing part of the contract.

The rollout is forward-only: an existing `in_progress`/`in_review` row remains editable, but it
must be backfilled before closure verification can make a meaningful product claim. Use
`--require-clean` when the project owner wants legacy debt to fail the audit. Do not manufacture a
generic passing gate to clear the report. The owner must choose proof that actually establishes the
deliverable's acceptance criteria; use an explicit operator waiver when proof is intentionally out
of scope.

### 2. Enable intake enforcement

Production web and MCP systemd units pin `PM_ENFORCE_DELIVERABLE_INTAKE=1`; `.env.example` carries
the same default for non-systemd installations. After deploying:

```bash
sudo systemctl daemon-reload
sudo systemctl restart projectplanner projectplanner-mcp
sudo systemctl show projectplanner projectplanner-mcp -p Environment \
  | grep PM_ENFORCE_DELIVERABLE_INTAKE=1
```

This covers both public write surfaces at their shared store boundary. A proposed deliverable may
still be drafted incrementally, but its transition to `in_progress` fails closed until the complete
contract is supplied.

### 3. Smoke both sides of the gate

In a disposable project, confirm that an incomplete `status=in_progress` upsert returns
`deliverable intake incomplete` and writes no row. Then repeat with an explicit success statement,
acceptance criteria, and at least the scope gate:

```json
{
  "schema": "switchboard.deliverable_proof_requirements.v1",
  "gates": [{"id": "scope", "required": true}]
}
```

Run `python3 test_deliverable_intake_gate.py` for the hermetic negative/positive proof.

### 4. Close a shipped deliverable

1. Confirm every linked implementation task is terminal with merge/offline provenance; do not
   manually rewrite task status. For this rollout, DELIVERABLES-12 through DELIVERABLES-21 must be
   Done before DELIVERABLES-22 is merged.
2. Run **Verify & stamp closure** (or `verify_deliverable_closure`) and inspect every required gate.
3. Stop on `hold`. On `pass` or audited `waive`, perform a separate `status=done` upsert while
   preserving the full current deliverable fields.
4. Read the deliverable back and verify `status=done`, `metadata.last_closure_grade`, the exact
   closure report id, and the `deliverable.closure_verified` activity stamp.
5. Archive only through the later typed-confirm archive control; Done is the shipped state and is
   not itself an instruction to delete history.

For `deliverable-closure-gate`, the final verifier run happens after DELIVERABLES-22 reaches Done,
because the exit-gate task is itself linked to the deliverable. That ordering avoids waiving the
very runbook and production rollout being certified.

---

## Automated verifier dispatch (DELIVERABLES-23)

The dispatch described above (`request_deliverable_closure_verification`) drops a
lane-less, `mode: message_only` wake in the target agent's inbox. Before DELIVERABLES-23,
every host that could see that wake ran it through the generic inbox-only adapter
(`adapters/run_agent.py --inbox-only`), which only proves connectivity — it acks the
message with *"received by inbox-only adapter; no model/action completion performed"*
and does nothing else. Closure requests queued forever unless a human manually ran
`deliverable_closure.verify_and_record_closure` server-side (the DELIVERABLES-20
dogfood pattern).

DELIVERABLES-23 gives `closure_verification`-kind wakes a real, but still bounded,
execution path: `adapters/agent_host.py` recognizes `policy.kind == "closure_verification"`
(`wake_mode()` returns `"closure_verify"`) and launches `adapters/closure_verifier.py`
instead of the ack-only stub. That script calls the same engine used for manual/dogfood
runs — `deliverable_closure.verify_and_record_closure(..., run_scripts=True)` — so scope
and cheap functional gates (`store_check`, `offline_evidence`, short `script`/`pytest`
commands) resolve and persist a real graded report automatically.

**This is not an autonomous LLM agent** — deliberately. It is a deterministic check
runner: store queries plus subprocess execution bounded by each gate's own
`timeout_s`/`env_allowlist` from the registry. No model runs; nothing is invoked outside
the wake's own resolved gate list. `PM_AGENT_HOST_ALLOW_WORK` stays `0` on the safe-default
host — this grants no `claim_next`/global work, only this one bounded action for wakes
that were already exclusively targeting deliverable closure.

**Shared-box safety ceiling.** A gate whose declared `timeout_s` exceeds
`PM_CLOSURE_VERIFIER_AUTO_TIMEOUT_CEILING_S` (default 120s) is left `not_run` rather than
auto-executed — mirroring the dogfood's own choice to run the 8-agent concurrent-load
harness off-box rather than inline on the 2GB VM. A required-but-heavy gate holds the
grade closed (never fabricated as a pass) with a note naming the gate; submit its result
manually (`submitted_functional`) or run verification from a bigger host instead.

---

## Deferred (gates 3 & 4)

- **Gate 3 — Economics:** embed `get_deliverable_tally` in closure report when UI-12 ingest is live.
- **Gate 4 — Archive:** typed confirm + `status=archived` after `done` + closure stamp (UI-11).

---

## References

- [`DELIVERABLES-MISSION-MODEL.md`](DELIVERABLES-MISSION-MODEL.md)
- [`OPERATOR-UI-DESIGN.md`](OPERATOR-UI-DESIGN.md) UI-11
- [`TALLY-SPEC.md`](TALLY-SPEC.md)
- `perf/concurrent_load_slo.json`, `scripts/concurrent_load_gate.py`
- Dogfood deliverable: `mcp-agent-path-performance`
