# Centralized Autopilot Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing Task Execution / Mission Bot V4 completion owner the single durable Autopilot owner for every explicitly started current or future project, then cut that same owner into a separately promoted service without weakening any security or merge gate.

**Architecture:** Keep ADR-0008's three planes intact. Capacity remains the only execution-liveness owner and is reached only through `start_task`; Communication carries facts only; Coordination owns explicit Start and the fenced scope lease; the existing W4 completion owner becomes one bounded application service. First centralize the owner in-process, then add a transactional effect outbox, prove a read-only candidate in shadow mode, run isolated conformance, and only then replace the current coordinator process. A failed process cut is a No-Go for the release, never a Human stop for a mission.

**Tech Stack:** Python 3.12+, SQLite with the repository's single-writer wrapper, FastAPI/Uvicorn for the candidate process health surface, systemd for deployment, existing Switchboard Task Execution, Mission Bot V4, GitHub integration, and the canonical `scripts/switchboard_ci.sh` gate.

## Global Constraints

- The approved design is [`docs/superpowers/specs/2026-08-10-centralized-autopilot-service-design.md`](../specs/2026-08-10-centralized-autopilot-service-design.md).
- ADR-0008 and ADR-0025 are binding. Do not add a fourth plane, second lifecycle writer, retry daemon, policy engine, scheduler, or database backend.
- Authentication, authorization, tenant/project isolation, Work Session, exact-head, CI, review, security, permission, merge-queue, and canonical-provenance checks remain enabled.
- Machine conditions may produce `continue` or `wait`; only an authenticated, execution-bound `agent_requires_human` receipt may produce `human`.
- Remediation round and delivery-attempt counts are telemetry only. Neither may select a terminal route or Human.
- Every execution start goes through `switchboard.application.commands.task_execution.start_task`.
- Every implementation task below is first materialized as its own `code_strict` Switchboard task. Create its worktree from a newly fetched `origin/master`; its `codex/` branch name must include the server-issued task ID and a short slug. Never stack implementation on this design branch or another task branch.
- Every task runs its focused tests, `git diff --check`, and then `bash scripts/switchboard_ci.sh` before push. The PR head, merge-group head, and canonical `origin/master` ancestry must all be proven independently.
- Do not cut live authority, deploy, or remove the rollback path until the preceding gate explicitly passes.

## Current-to-target responsibility map

| Current file | Current responsibility | Target responsibility |
|---|---|---|
| `src/switchboard/domain/mission_bot_v4/controller.py` | Pure four-state decision from hydrated facts | Canonical `continue` / `wait` / `human` / `done` reducer contract |
| `src/switchboard/storage/repositories/review_remediations.py` | Review finding classification plus remediation persistence | Use the canonical reducer classification; round stays telemetry |
| `src/switchboard/application/mission_bot_v4/worker.py` | Calls `start_task`, then advances the journal cursor | Atomically queues one effect and cursor; performs no effect inline |
| `src/switchboard/application/mission_bot_v4/runtime.py` | Projects facts and runs one scoped tick | Compatibility wrapper over the centralized application service |
| `src/switchboard/application/mission_bot_v4/coordinator.py` | Production scoped W4 caller | Calls only the centralized application service |
| `src/switchboard/storage/repositories/mission_journal.py` | Mission items/events and CAS cursor | Mission items/events plus atomic decision/effect enqueue |
| `src/switchboard/storage/repositories/completion_runs.py` | Completion current-state repository | Sole repository API for all `completion_runs` writes |
| `src/switchboard/storage/repositories/provenance.py` | Contains a direct `completion_runs` upsert | Calls the completion-run repository in the same transaction |
| `src/switchboard/storage/repositories/attention.py` | Contains a direct `completion_runs` update | Calls the completion-run repository in the same transaction |
| `coordinator_daemon.py` and `projectplanner-coordinator-autopilot.service` | Current process loop and emergency dry-run route | Temporary green facade and rollback caller of the same bounded package |
| `src/switchboard/services/autopilot/` | Does not exist | Candidate process composition root, health/readiness, shadow and active modes |

---

## Task 1: Lock the Mission Bot V4 release contract

**Files:**

- Create: `src/switchboard/domain/mission_bot_v4/contracts.py`
- Create: `src/switchboard/domain/mission_bot_v4/review_routing.py`
- Modify: `src/switchboard/domain/mission_bot_v4/controller.py`
- Modify: `src/switchboard/domain/mission_bot_v4/__init__.py`
- Modify: `src/switchboard/storage/repositories/review_remediations.py`
- Create: `tests/test_arch_ms128_autopilot_release_contract.py`
- Test: `tests/test_coord113_scoped_mission_worker.py`
- Test: `tests/test_coord126_human_escalation_stop.py`

**Interfaces:**

```python
from typing import Literal, NotRequired, TypedDict

MissionResult = Literal["continue", "wait", "human", "done"]
EffectKind = Literal["start_task", "merge_queue", "reconcile_task"]


class EffectIntent(TypedDict):
    kind: EffectKind
    key: str
    payload: dict[str, object]


class MissionDecision(TypedDict):
    result: MissionResult
    reason: str
    effect: NotRequired[EffectIntent]
    evidence: NotRequired[dict[str, object]]
```

```python
def route_review_findings(
    findings: list[dict[str, object]], *, round_no: int
) -> dict[str, object]:
    """Classify one exact-head verdict; round_no is returned as telemetry only."""
```

- [ ] **Step 1: Write the release-contract tests first**

Add tests that use the production `ReviewFinding` contract and the production review-routing function. The central assertions are:

```python
def test_machine_findings_continue_at_any_round() -> None:
    for category in ("ci", "security", "permission", "conflict", "correctness"):
        finding = ReviewFinding.model_validate({
            "schema": "switchboard.review_finding.v1",
            "id": f"ARCH-MS-128-{category}",
            "location": "src/switchboard/example.py:10",
            "category": category,
            "severity": "high",
            "invariant_violated": "The exact-head gate is red.",
            "repair_requirement": "Repair the exact-head finding and rerun the gate.",
            "class": "auto",
            "state": "open",
        }).model_dump(mode="json", by_alias=True)
        for round_no in (4, 10, 100):
            decision = route_review_findings([finding], round_no=round_no)
            assert decision["result"] == "continue"
            assert decision["requested_role"] == "remediation"
            assert decision["round_no"] == round_no
            assert decision["human_required"] is False


def test_only_explicit_escalate_finding_is_human() -> None:
    finding = ReviewFinding.model_validate({
        "schema": "switchboard.review_finding.v1",
        "id": "ARCH-MS-128-human",
        "location": "docs/decision.md:1",
        "category": "human_decision",
        "severity": "high",
        "invariant_violated": "The assignment does not choose the product behavior.",
        "repair_requirement": "Choose one named product behavior.",
        "class": "escalate",
        "state": "open",
    }).model_dump(mode="json", by_alias=True)
    assert route_review_findings([finding], round_no=100)["result"] == "human"
```

Add controller cases proving capacity unavailable is `wait`, a later unhandled event is `continue`, authenticated `agent_requires_human` is `human`, board `Blocked` is ignored, and canonical terminal provenance is `done`.

- [ ] **Step 2: Run the new test and confirm the contract is red**

Run:

```bash
python3 tests/test_arch_ms128_autopilot_release_contract.py
```

Expected: failure because `contracts.py`, `review_routing.py`, and the canonical `result` field do not exist.

- [ ] **Step 3: Add the typed decision vocabulary and pure review router**

Implement `MissionDecision` and `route_review_findings`. The router must use only the finding's authenticated contract class; it must not branch on `round_no`, task status, claim status, environment variables, or project configuration.

Use this routing rule:

```python
open_findings = [row for row in findings if str(row.get("state") or "open") == "open"]
automatic = [row for row in open_findings if row.get("class") == "auto"]
escalations = [row for row in open_findings if row.get("class") == "escalate"]
result: MissionResult = "human" if escalations else "continue" if automatic else "wait"
```

- [ ] **Step 4: Normalize the existing controller without changing ADR-0008 authority**

Return the canonical `result` on every branch. Preserve compatibility fields during this task so current callers remain green:

```python
if context.get("terminal_provenance"):
    return {"result": "done", "state": "DONE", "action": "wait", "reason": "terminal_provenance"}
if _authenticated_human_request(context.get("human_request")):
    return {"result": "human", "state": "HUMAN", "action": "wait", "reason": "authenticated_agent_request"}
if latest_sequence > handled_through:
    return {
        "result": "continue",
        "state": "ACTIVE",
        "action": "start_task",
        "reason": "unhandled_event",
        "requested_role": requested_role,
        "event_pointer": handled_through + 1,
    }
return {"result": "wait", "state": "WAITING", "action": "wait", "reason": "no_unhandled_event"}
```

An active mission with missing input remains `wait` and emits release-health evidence. It must not invent a fifth mission result or a Human route.

- [ ] **Step 5: Make the remediation repository consume the pure router**

Replace its local `auto` / `escalations` / `human_required` decision with `route_review_findings`. Keep active-claim and terminal-task conflicts as typed non-Human coordination blocks. Keep `round_no` in the row and activity event only.

- [ ] **Step 6: Run focused tests**

Run:

```bash
python3 tests/test_arch_ms128_autopilot_release_contract.py
python3 tests/test_coord113_scoped_mission_worker.py
python3 tests/test_coord126_human_escalation_stop.py
```

Expected: all pass; rounds 4, 10, and 100 remain automatic, while the explicit escalation fixture remains Human.

- [ ] **Step 7: Commit the contract**

```bash
git add src/switchboard/domain/mission_bot_v4 src/switchboard/storage/repositories/review_remediations.py tests/test_arch_ms128_autopilot_release_contract.py tests/test_coord113_scoped_mission_worker.py tests/test_coord126_human_escalation_stop.py
git commit -m "test(autopilot): lock Mission Bot V4 release contract"
```

---

## Task 2: Make decision delivery crash-safe with one transactional outbox

**Files:**

- Modify: `src/switchboard/storage/migrations/runner.py`
- Modify: `src/switchboard/storage/repositories/mission_journal.py`
- Create: `src/switchboard/storage/repositories/mission_effects.py`
- Create: `src/switchboard/application/mission_bot_v4/effects.py`
- Modify: `src/switchboard/application/mission_bot_v4/worker.py`
- Modify: `src/switchboard/application/mission_bot_v4/runtime.py`
- Modify: `src/switchboard/application/mission_bot_v4/__init__.py`
- Create: `tests/test_arch_ms128_autopilot_outbox.py`
- Test: `tests/test_coord109_mission_journal.py`
- Test: `tests/test_coord113_scoped_mission_worker.py`

**Interfaces:**

```python
def commit_decision(
    self,
    task_id: str,
    *,
    project: str,
    expected_version: int,
    handled_through: int,
    state: str,
    requested_role: str,
    decision: MissionDecision,
) -> dict[str, object]:
    """CAS the mission cursor and insert its optional effect in one transaction."""
```

```python
def deliver_next_effect(
    *, project: str, owner: str, ports: EffectPorts, now: float | None = None
) -> dict[str, object]:
    """Lease and deliver at most one pending effect by its stable key."""
```

- [ ] **Step 1: Add failing crash, duplicate, and unlimited-retry tests**

Cover these exact cases:

```python
def test_decision_and_effect_commit_atomically() -> None:
    receipt = journal.commit_decision(
        TASK,
        project=PROJECT,
        expected_version=1,
        handled_through=1,
        state="ACTIVE",
        requested_role="implementation",
        decision={
            "result": "continue",
            "reason": "unhandled_event",
            "effect": {
                "kind": "start_task",
                "key": "v4:7:ARCH-MS-128:1:implementation",
                "payload": {"task_id": TASK, "role": "implementation"},
            },
        },
    )
    assert receipt["cursor_advanced"] is True
    assert receipt["effect"]["status"] == "pending"


def test_delivery_replay_uses_one_effect_key() -> None:
    ports.crash_after_receiver = True
    with pytest.raises(SimulatedProcessCrash):
        deliver_next_effect(project=PROJECT, owner="worker-a", ports=ports, now=100)
    ports.crash_after_receiver = False
    replay = deliver_next_effect(project=PROJECT, owner="worker-b", ports=ports, now=200)
    assert replay["effect_key"] == "v4:7:ARCH-MS-128:1:implementation"
    assert ports.start_keys == [replay["effect_key"], replay["effect_key"]]
    assert ports.execution_ids == ["execution-1", "execution-1"]


def test_attempt_100_remains_pending() -> None:
    effect = seed_pending_effect(attempts=99, next_attempt_at=0)
    receipt = deliver_next_effect(project=PROJECT, owner="worker", ports=capacity_full, now=1000)
    assert receipt["attempts"] == 100
    assert receipt["status"] == "pending"
    assert receipt["terminal"] is False
```

- [ ] **Step 2: Run the outbox test and confirm it is red**

Run:

```bash
python3 tests/test_arch_ms128_autopilot_outbox.py
```

Expected: failure because the migration, repository, and delivery application service do not exist.

- [ ] **Step 3: Add migration `0137_mission_effects`**

Use this additive SQLite contract:

```sql
CREATE TABLE IF NOT EXISTS mission_effects (
  effect_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  event_sequence INTEGER NOT NULL,
  effect_kind TEXT NOT NULL CHECK(effect_kind IN ('start_task','merge_queue','reconcile_task')),
  effect_key TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','delivering','delivered')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL NOT NULL DEFAULT 0,
  lease_owner TEXT NOT NULL DEFAULT '',
  lease_expires_at REAL,
  receipt_json TEXT NOT NULL DEFAULT '{}',
  last_error TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(project_id, effect_key)
)
```

Add an index on `(project_id, status, next_attempt_at, created_at)`.

- [ ] **Step 4: Commit cursor and effect in one `BEGIN IMMEDIATE` transaction**

`MissionJournalRepository.commit_decision` must:

1. reread and CAS `mission_items.version`;
2. verify `handled_through` names an existing event for the same mission;
3. insert the stable effect with `INSERT OR IGNORE`;
4. compare an ignored duplicate's full kind/payload/sequence and fail on collision; and
5. update the mission cursor and state before commit.

Do not call an external port inside this transaction.

- [ ] **Step 5: Implement one-effect delivery with capped delay and no cap on attempts**

Use a lease to make recovery explicit. On a typed capacity or external refusal, return the row to `pending` with:

```python
delay_seconds = min(300, 2 ** min(attempts, 8))
```

This caps delay only. No attempt number may produce `human`, `done`, `failed`, or deletion. On process death, an expired `delivering` lease becomes eligible again. A successful receiver response is persisted as `delivered` by the same stable effect key.

- [ ] **Step 6: Refactor the worker to enqueue instead of performing inline**

The worker still validates the exact scope fence immediately before the write, but replaces the direct `ports.start_task` call plus later cursor update with one `journal.commit_decision` call. Effect delivery lives in `effects.py`. Keep `task_execution.start_task` as the only start adapter.

- [ ] **Step 7: Run focused tests**

Run:

```bash
python3 tests/test_arch_ms128_autopilot_outbox.py
python3 tests/test_coord109_mission_journal.py
python3 tests/test_coord113_scoped_mission_worker.py
```

Expected: all pass; simulated crashes and attempt 100 preserve one pending effect and never create Human.

- [ ] **Step 8: Commit the outbox**

```bash
git add src/switchboard/storage/migrations/runner.py src/switchboard/storage/repositories/mission_journal.py src/switchboard/storage/repositories/mission_effects.py src/switchboard/application/mission_bot_v4 tests/test_arch_ms128_autopilot_outbox.py tests/test_coord109_mission_journal.py tests/test_coord113_scoped_mission_worker.py
git commit -m "feat(autopilot): add transactional mission effect outbox"
```

---

## Task 3: Establish one bounded application owner and reject bypass writers

**Files:**

- Create: `src/switchboard/application/mission_bot_v4/ports.py`
- Create: `src/switchboard/application/mission_bot_v4/service.py`
- Create: `src/switchboard/application/mission_bot_v4/fact_intake.py`
- Modify: `src/switchboard/application/mission_bot_v4/runtime.py`
- Modify: `src/switchboard/application/mission_bot_v4/coordinator.py`
- Modify: `src/switchboard/application/commands/review_verdicts.py`
- Modify: `src/switchboard/application/commands/human_blocker.py`
- Create: `src/switchboard/integrations/github_merge_queue.py`
- Modify: `src/switchboard/storage/repositories/completion_runs.py`
- Modify: `src/switchboard/storage/repositories/provenance.py`
- Modify: `src/switchboard/storage/repositories/attention.py`
- Create: `tests/test_arch_ms128_autopilot_ownership.py`
- Create: `tests/test_arch_ms128_github_merge_queue.py`
- Test: `tests/test_coord115_human_request_parking.py`
- Test: `tests/test_coord116_human_answer_projection.py`

**Interfaces:**

```python
class AutopilotPorts(Protocol):
    def validate_scope(self, authority: Mapping[str, object], *, project: str, task_id: str) -> Mapping[str, object]:
        raise NotImplementedError

    def task(self, task_id: str, *, project: str) -> Mapping[str, object] | None:
        raise NotImplementedError

    def runner_live(self, task_id: str, *, project: str) -> bool:
        raise NotImplementedError

    def capacity_pending(self, task_id: str, *, project: str) -> bool:
        raise NotImplementedError

    def start_task(self, task_id: str, *, project: str, effect_key: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        raise NotImplementedError

    def merge_queue(self, task_id: str, *, project: str, effect_key: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        raise NotImplementedError

    def reconcile_task(self, task_id: str, *, project: str, effect_key: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        raise NotImplementedError


class AutopilotService:
    def tick_scope(self, task_id: str, *, project: str, scope_project: str, scope_authority: Mapping[str, object]) -> dict[str, object]:
        raise NotImplementedError

    def append_fact(self, envelope: Mapping[str, object]) -> dict[str, object]:
        raise NotImplementedError

    def deliver_one(self, *, project: str) -> dict[str, object]:
        raise NotImplementedError

    def mission(self, task_id: str, *, project: str) -> dict[str, object] | None:
        raise NotImplementedError
```

- [ ] **Step 1: Write the ownership guard first**

The AST/SQL test must enumerate production Python files and fail when a write to an owned table occurs outside these repositories and migrations:

```python
OWNED_TABLES = {
    "mission_items", "mission_events", "mission_effects", "completion_runs",
    "review_verdicts", "review_findings", "review_remediations",
}
ALLOWED_WRITERS = {
    "src/switchboard/storage/repositories/mission_journal.py",
    "src/switchboard/storage/repositories/mission_effects.py",
    "src/switchboard/storage/repositories/completion_runs.py",
    "src/switchboard/storage/repositories/review_verdicts.py",
    "src/switchboard/storage/repositories/review_remediations.py",
    "src/switchboard/storage/migrations/runner.py",
}
```

Also assert:

- only `fact_intake.py` calls `record_human_requested_in`;
- only the canonical provenance projector calls the typed Done transition;
- only `effects.py` imports `task_execution.start_task`; and
- `coordinator.py`, REST/MCP adapters, the janitor, and service composition roots contain no owned-table SQL.

- [ ] **Step 2: Run the guard and record the two current production bypasses**

Run:

```bash
python3 tests/test_arch_ms128_autopilot_ownership.py
```

Expected: red on the direct `completion_runs` writes in `provenance.py` and `attention.py`. The test must print exact file and line evidence.

- [ ] **Step 3: Add transaction-aware completion-run repository methods**

Add:

```python
def record_canonical_merge_in(connection: sqlite3.Connection, data: Mapping[str, object], *, actor: str) -> dict[str, object]:
    return transition_completion_run_in(connection, data, actor=actor)


def record_human_resume_in(connection: sqlite3.Connection, *, run_id: str, task_id: str, expected_version: int, evidence: Mapping[str, object], actor: str, now: float) -> dict[str, object]:
    """CAS one Human decision receipt without allowing a caller-selected route."""
```

Replace the direct SQL in `provenance.py` and `attention.py` with those methods while retaining their surrounding transactions.

- [ ] **Step 4: Add `AutopilotService` as the sole application orchestrator**

Move the projection/reduction/delivery sequence from `runtime.run_scoped_mission_tick` behind `AutopilotService.tick_scope`. Leave `run_scoped_mission_tick` as a compatibility wrapper that constructs the service and calls that method. Do not duplicate the reducer or adapters.

- [ ] **Step 5: Bind merge queue and reconciliation to typed authority doors**

Build `GitHubMergeQueueAdapter` beside the existing pull-request readiness adapter. Reuse the canonical repository resolver, project credential resolver, and transport pattern from `github_pull_requests.py`. It must reread the PR, reject a non-canonical repository or stale head, call GitHub's merge-queue mutation by exact PR node ID, and reread queue truth. The stable outbox effect key makes a provider replay return `already_enqueued`, not enqueue a second time.

Bind `reconcile_task` to `switchboard.application.commands.reconcile_task_merge.execute` with the production repository, GitHub-read, and `mark_task_merged` ports already used by the MCP adapter. Do not put GitHub or reconciliation rules in the reducer.

- [ ] **Step 6: Route ingress through typed fact intake**

`fact_intake.py` owns versioned projections from durable GitHub observations, Capacity terminal facts, exact authenticated Human receipts, review verdicts, and canonical merge provenance. Existing commands keep their public signatures. They first persist their source fact, then invoke the in-process `AutopilotService` compatibility owner; they no longer run review remediation or write mission state independently. This preserves current behavior before the process cut while giving all lifecycle decisions one code path. Reject a Human envelope unless all of these are present and exact:

```python
schema == "switchboard.work_session_human_blocker.v1"
source_tool == "agent_requires_human"
binding in {"registered_agent", "direct_session"}
bool(agent_id)
provenance_stamp == "switchboard.resolve_write_actor.v1"
```

- [ ] **Step 7: Run ownership, effect-adapter, and Human-path tests**

Run:

```bash
python3 tests/test_arch_ms128_autopilot_ownership.py
python3 tests/test_arch_ms128_github_merge_queue.py
python3 tests/test_coord115_human_request_parking.py
python3 tests/test_coord116_human_answer_projection.py
python3 tests/test_coord126_human_escalation_stop.py
```

Expected: all pass; the only Human producer is the authenticated command path, and the two direct completion-run writes are gone.

- [ ] **Step 8: Commit the bounded owner**

```bash
git add src/switchboard/application/mission_bot_v4 src/switchboard/application/commands/review_verdicts.py src/switchboard/application/commands/human_blocker.py src/switchboard/integrations/github_merge_queue.py src/switchboard/storage/repositories/completion_runs.py src/switchboard/storage/repositories/provenance.py src/switchboard/storage/repositories/attention.py tests/test_arch_ms128_autopilot_ownership.py tests/test_arch_ms128_github_merge_queue.py tests/test_coord115_human_request_parking.py tests/test_coord116_human_answer_projection.py
git commit -m "refactor(autopilot): centralize lifecycle ownership"
```

---

## Task 4: Package the same owner as a read-only shadow candidate

**Files:**

- Create: `src/switchboard/services/autopilot/__init__.py`
- Create: `src/switchboard/services/autopilot/__main__.py`
- Create: `src/switchboard/services/autopilot/app.py`
- Create: `src/switchboard/services/autopilot/health.py`
- Create: `src/switchboard/services/autopilot/ports.py`
- Create: `src/switchboard/services/autopilot/runner.py`
- Create: `src/switchboard/services/autopilot/settings.py`
- Create: `src/switchboard/api/autopilot_port_adapters.py`
- Create: `deploy/autopilot/switchboard-autopilot.service.example`
- Create: `docs/AUTOPILOT-INDEPENDENCE-GATE.md`
- Create: `tests/test_arch_ms128_autopilot_service.py`
- Create: `tests/test_arch_ms128_autopilot_shadow_parity.py`
- Modify: `deploy/service-boundary-contract.json`

**Interfaces:**

```python
@dataclass(frozen=True)
class AutopilotServiceSettings:
    service_name: str = "switchboard-autopilot"
    host: str = "127.0.0.1"
    port: int = 8127
    mode: Literal["shadow", "active"] = "shadow"
    poll_seconds: int = 5
    shadow_db_path: str = "/var/lib/projectplanner/autopilot-shadow.db"
```

```python
class ProjectDiscoveryPort(Protocol):
    def execution_enabled_projects(self) -> Sequence[str]:
        raise NotImplementedError


class ShadowRecorderPort(Protocol):
    def record_comparison(self, receipt: Mapping[str, object]) -> None:
        raise NotImplementedError
```

- [ ] **Step 1: Write service isolation and shadow-safety tests**

Assert the candidate package:

- imports only typed application/service ports, never root `store`, `auth`, `dispatch`, or sibling service internals;
- starts in `shadow` when no mode is specified;
- exposes cheap `/health` and fail-closed `/ready` on port 8127;
- discovers a project added to the registry after startup, proving future projects need no allowlist edit;
- writes comparison receipts only to the separate shadow database; and
- raises `ShadowEffectForbidden` if any effect adapter is called in shadow mode.

Use an exact parity receipt:

```python
{
    "schema": "switchboard.autopilot_shadow_comparison.v1",
    "project": "switchboard",
    "task_id": "ARCH-MS-128",
    "event_sequence": 9,
    "current_digest": "sha256:current",
    "candidate_digest": "sha256:candidate",
    "matched": True,
    "candidate_sha": "0123456789abcdef0123456789abcdef01234567",
}
```

- [ ] **Step 2: Run the candidate tests and confirm they are red**

Run:

```bash
python3 tests/test_arch_ms128_autopilot_service.py
python3 tests/test_arch_ms128_autopilot_shadow_parity.py
```

Expected: failure because the service package and shadow recorder do not exist.

- [ ] **Step 3: Implement the candidate composition root**

Use FastAPI lifespan to start one bounded polling coroutine. `runner.py` discovers execution-enabled projects from the registry on every sweep and processes only scopes with explicit active authority. It delegates decisions to `AutopilotService`; it does not contain routing rules.

Shadow mode builds observation ports plus `ShadowEffectForbidden` adapters. Active mode is constructible for conformance but must not become the deployment default in this task.

- [ ] **Step 4: Implement decision-digest parity**

Canonicalize only the lifecycle decision fields:

```python
canonical = {
    "result": decision["result"],
    "reason": decision["reason"],
    "effect": decision.get("effect"),
}
digest = "sha256:" + hashlib.sha256(
    json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
```

Store candidate comparison evidence outside production project databases. Any mismatch, missing source decision, malformed fact, or candidate exception is red and blocks promotion.

- [ ] **Step 5: Document and automate the ADR-0025 independence gate**

The gate must fail closed on undeclared imports, owned-table bypasses, cross-project reads, shadow effect capability, readiness failure, SQLite contention regression, resource-budget regression, or inability to restart and replay the outbox. Record an explicit Go or No-Go; No-Go keeps the bounded package in-process.

- [ ] **Step 6: Run the candidate and existing service-boundary tests**

Run:

```bash
python3 tests/test_arch_ms128_autopilot_service.py
python3 tests/test_arch_ms128_autopilot_shadow_parity.py
python3 tests/test_arch_ms126_validation_policy.py
python3 scripts/service_cut_inventory.py validate --inventory deploy/service-cut-inventory.json
```

Expected: all pass; the example unit is shadow-only and no live inventory entry or Caddy route exists yet.

- [ ] **Step 7: Commit the shadow candidate**

```bash
git add src/switchboard/services/autopilot src/switchboard/api/autopilot_port_adapters.py deploy/autopilot docs/AUTOPILOT-INDEPENDENCE-GATE.md deploy/service-boundary-contract.json tests/test_arch_ms128_autopilot_service.py tests/test_arch_ms128_autopilot_shadow_parity.py
git commit -m "feat(autopilot): add shadow service candidate"
```

---

## Task 5: Build the isolated conformance VM gate

**Files:**

- Create: `scripts/autopilot_conformance.py`
- Create: `tests/test_arch_ms128_autopilot_conformance.py`
- Create: `deploy/autopilot/conformance.env.example`
- Create: `docs/AUTOPILOT-CONFORMANCE-OPERATOR.md`
- Modify: `docs/COMPLETION-CONFORMANCE-OPERATOR.md`
- Modify: `docs/superpowers/specs/2026-07-26-completion-conformance-harness-design.md`

**Interfaces:**

```python
SCENARIOS = (
    "clean_to_done",
    "security_remediation_to_done",
    "service_restart_recovery",
    "host_restart_recovery",
    "capacity_wait_then_launch",
    "stale_head_replacement",
    "merge_reconcile_idempotency",
)


def run_scenario(name: str, *, client: ConformanceClient, candidate_sha: str) -> dict[str, object]:
    """Run one bounded sandbox scenario and return exact provenance evidence."""
```

- [ ] **Step 1: Write the harness safety and scenario tests**

The CLI must refuse to run unless all of these are exact:

```python
project == "conformance"
repository == "6th-Element-Labs/switchboard-autopilot-conformance"
host_identity.startswith("host/conformance/")
candidate_sha == service_health["git_sha"]
```

Tests must prove a `switchboard`, `atlas`, arbitrary customer project, or different repository input exits nonzero before any write. Fake clients then run all seven scenarios and assert one receipt per scenario.

- [ ] **Step 2: Run the harness test and confirm it is red**

Run:

```bash
python3 tests/test_arch_ms128_autopilot_conformance.py
```

Expected: failure because the harness does not exist.

- [ ] **Step 3: Implement the scenario runner using public typed boundaries**

The harness may call explicit Start, Task Execution queries, sandbox GitHub operations, host controls, and canonical reconciliation. It may not update project databases directly. Each receipt includes the candidate SHA, service identity, project, repository, task, PR/head/merge SHA, execution generations, decision digests, timestamps, and final canonical Done proof.

- [ ] **Step 4: Provision the separate-VM contract**

The environment example and operator guide require a separate database directory, sandbox GitHub App installation, conformance-only token, conformance host identity, and no credentials for `switchboard`, `atlas`, or customer repositories. Run the candidate in `active` mode only on this VM.

- [ ] **Step 5: Run local hermetic tests and one actual conformance VM pass**

Local:

```bash
python3 tests/test_arch_ms128_autopilot_conformance.py
```

VM:

```bash
python3 scripts/autopilot_conformance.py run-all \
  --project conformance \
  --repository 6th-Element-Labs/switchboard-autopilot-conformance \
  --candidate-sha "$(git rev-parse HEAD)" \
  --evidence-out .artifacts/autopilot-conformance.json
```

Expected: seven passing receipts and a top-level `passed: true`. Do not continue to production cutover when the VM evidence is missing or red.

- [ ] **Step 6: Commit the conformance gate**

```bash
git add scripts/autopilot_conformance.py tests/test_arch_ms128_autopilot_conformance.py deploy/autopilot/conformance.env.example docs/AUTOPILOT-CONFORMANCE-OPERATOR.md docs/COMPLETION-CONFORMANCE-OPERATOR.md docs/superpowers/specs/2026-07-26-completion-conformance-harness-design.md
git commit -m "test(autopilot): add isolated service conformance gate"
```

---

## Task 6: Cut over one writer and require a production promotion canary

**Prerequisites:** Tasks 1-5 are merged to canonical `master`; ADR-0025 gate is Go; production shadow parity is 100% for the agreed observation window; the conformance VM receipt is green for the exact candidate SHA.

**Files:**

- Create: `deploy/switchboard-autopilot.service`
- Create: `scripts/autopilot_promotion_canary.py`
- Create: `tests/test_arch_ms128_autopilot_cutover.py`
- Create: `tests/test_arch_ms128_autopilot_promotion_canary.py`
- Modify: `deploy/service-cut-inventory.json`
- Modify: `deploy/redeploy.sh`
- Modify: `deploy/projectplanner-coordinator-autopilot.service`
- Modify: `src/switchboard/application/commands/review_verdicts.py`
- Modify: `src/switchboard/application/commands/human_blocker.py`
- Modify: `src/switchboard/application/mission_bot_v4/fact_intake.py`
- Modify: `docs/AUTOPILOT-INDEPENDENCE-GATE.md`
- Create: `docs/AUTOPILOT-CUTOVER-RUNBOOK.md`

**Interfaces:**

```python
def verify_promotion_canary(
    *, project: str, task_id: str, candidate_sha: str, client: CanaryClient
) -> dict[str, object]:
    """Accept only exact service identity plus canonical task Done provenance."""
```

- [ ] **Step 1: Write cutover mutual-exclusion and canary tests**

Assert that the old and new processes use the same coordinator profile and scope-fence lease, so simultaneous startup yields one active writer and one authority-denied waiter. Assert the new active service discovers all registry projects dynamically and still ignores projects without an explicit active scope.

The canary test must reject green CI, an open PR, an `In Review` task, or a Done row without canonical merge provenance. It accepts only:

```python
task["status"] == "Done"
task["git_state"]["merged_sha"]
task["git_state"]["in_main_content"] is True
service_health["git_sha"] == candidate_sha
```

- [ ] **Step 2: Run the cutover tests and confirm they are red**

Run:

```bash
python3 tests/test_arch_ms128_autopilot_cutover.py
python3 tests/test_arch_ms128_autopilot_promotion_canary.py
```

Expected: failure because the active unit, inventory entry, and canary do not exist.

- [ ] **Step 3: Add the active unit and declarative inventory**

The live unit runs the same `switchboard.services.autopilot` package on localhost port 8127 with `PM_AUTOPILOT_MODE=active` and the global topology route `PM_AUTOPILOT_PRIMARY=service`. This is a deployment route, not a project policy or retry flag. It uses the existing project registry and active scopes; it has no comma-separated project allowlist. Add health/readiness, restart order, snapshot, and rollback metadata to `service-cut-inventory.json`.

When `PM_AUTOPILOT_PRIMARY=service`, review, Human, GitHub, and Capacity producers persist only their existing durable source records. They do not invoke the in-process compatibility tick or mutate mission lifecycle. The service continuously projects those records, so an unavailable service leaves work queued in source truth and resumes it later. The ownership test must prove that only the active service process calls reducer/effect delivery in this mode.

Change the old coordinator unit to an explicit disabled rollback facade for the same bounded package; it must not be enabled concurrently by deployment automation. Scope leases remain the final mechanical dual-writer fence.

- [ ] **Step 4: Add ordered cutover and rollback commands**

The runbook sequence is:

1. snapshot project databases and shadow evidence;
2. verify exact candidate SHA, readiness, parity, conformance, and zero claimed effects;
3. stop and disable `projectplanner-coordinator-autopilot.service`;
4. start and enable `switchboard-autopilot.service`;
5. verify `/health`, `/ready`, one active scope holder, and outbox drain;
6. run the production canary; and
7. accept the release only after canonical Done.

Rollback stops the candidate first, starts the rollback caller of the same bounded package, validates the new scope fence, and lets the durable outbox replay. It never restores a removed policy, resets mission state, or enables dual writers.

- [ ] **Step 5: Run tests and deployment inventory validation**

Run:

```bash
python3 tests/test_arch_ms128_autopilot_cutover.py
python3 tests/test_arch_ms128_autopilot_promotion_canary.py
python3 scripts/service_cut_inventory.py validate --inventory deploy/service-cut-inventory.json
git diff --check
```

Expected: all pass.

- [ ] **Step 6: Deploy, run the harmless production canary, and retain evidence**

Run the canary only against the dedicated production canary project/repository named in the runbook. It must have no customer-repository authority.

```bash
python3 scripts/autopilot_promotion_canary.py \
  --project autopilot-canary \
  --repository 6th-Element-Labs/switchboard-autopilot-canary \
  --candidate-sha "$(git rev-parse HEAD)" \
  --evidence-out .artifacts/autopilot-promotion-canary.json
```

Expected: exact candidate service identity and one harmless task reconciled to canonical Done. A failure rolls back the platform release and leaves customer missions queued; it does not create Human requests.

- [ ] **Step 7: Commit the cutover**

```bash
git add deploy/switchboard-autopilot.service deploy/projectplanner-coordinator-autopilot.service deploy/service-cut-inventory.json deploy/redeploy.sh src/switchboard/application/commands/review_verdicts.py src/switchboard/application/commands/human_blocker.py src/switchboard/application/mission_bot_v4/fact_intake.py scripts/autopilot_promotion_canary.py tests/test_arch_ms128_autopilot_cutover.py tests/test_arch_ms128_autopilot_promotion_canary.py docs/AUTOPILOT-INDEPENDENCE-GATE.md docs/AUTOPILOT-CUTOVER-RUNBOOK.md
git commit -m "deploy(autopilot): cut over the single lifecycle writer"
```

---

## Task 7: Soak, remove the legacy route, and prove rollback

**Prerequisites:** The exact cutover SHA has completed the agreed soak with no parity drift, no duplicate effects, no machine-generated Human events, and successful canaries.

**Files:**

- Move: `deploy/projectplanner-coordinator-autopilot.service` to `deploy/retired/projectplanner-coordinator-autopilot.service`
- Modify: `coordinator_daemon.py`
- Modify: `scoped_completion_coordinator.py`
- Modify: `src/switchboard/application/commands/review_verdicts.py`
- Modify: `src/switchboard/application/commands/human_blocker.py`
- Modify: `src/switchboard/application/mission_bot_v4/fact_intake.py`
- Modify: `src/switchboard/application/mission_bot_v4/runtime.py`
- Modify: `deploy/redeploy.sh`
- Modify: `deploy/service-cut-inventory.json`
- Modify: `docs/AUTOPILOT-CUTOVER-RUNBOOK.md`
- Create: `tests/test_arch_ms128_autopilot_legacy_removal.py`

- [ ] **Step 1: Write the legacy-removal ratchet**

Assert:

- no active deployment unit starts the old coordinator Autopilot loop;
- `coordinator_daemon.py` retains observation/cleanup only and cannot call `start_task`, merge, reconcile, or Human APIs;
- production callers reach lifecycle work only through `AutopilotService`;
- the retired unit cannot be installed by `deploy/redeploy.sh`; and
- rollback commands invoke the same bounded package and require the candidate process to be stopped first.

- [ ] **Step 2: Run the ratchet and confirm it is red**

Run:

```bash
python3 tests/test_arch_ms128_autopilot_legacy_removal.py
```

Expected: failure while the old active unit and compatibility call path remain.

- [ ] **Step 3: Remove only the old process route and compatibility writers**

Move the unit to `deploy/retired`, remove it from restart/install automation, and delete the temporary `PM_AUTOPILOT_PRIMARY=inprocess` compatibility calls. Review and Human commands retain only durable source-fact persistence; the service owns projection and lifecycle processing. Keep the pure reducer, repositories, Task Execution authority doors, and an explicit rollback CLI entry into the same bounded package.

- [ ] **Step 4: Drill rollback without creating a second implementation**

In the conformance environment:

1. stop the candidate between effect claim and receipt persistence;
2. start the documented rollback caller;
3. wait for the delivery lease to expire;
4. prove the same effect key returns the existing execution/merge result;
5. finish canonical Done; and
6. restart the candidate and prove it reads the same terminal cursor.

Save the exact candidate SHA, effect key, execution generation, merge SHA, and terminal cursor in the evidence receipt.

- [ ] **Step 5: Run the complete release gate**

Run:

```bash
python3 tests/test_arch_ms128_autopilot_legacy_removal.py
python3 tests/test_arch_ms128_autopilot_release_contract.py
python3 tests/test_arch_ms128_autopilot_outbox.py
python3 tests/test_arch_ms128_autopilot_ownership.py
python3 tests/test_arch_ms128_autopilot_service.py
python3 tests/test_arch_ms128_autopilot_shadow_parity.py
python3 tests/test_arch_ms128_autopilot_conformance.py
python3 tests/test_arch_ms128_autopilot_cutover.py
python3 tests/test_arch_ms128_autopilot_promotion_canary.py
git diff --check
bash scripts/switchboard_ci.sh
```

Expected: all pass on the exact PR head. Then require the same canonical gate on the merge-group head.

- [ ] **Step 6: Commit legacy removal**

```bash
git add deploy/retired/projectplanner-coordinator-autopilot.service coordinator_daemon.py scoped_completion_coordinator.py src/switchboard/application/commands/review_verdicts.py src/switchboard/application/commands/human_blocker.py src/switchboard/application/mission_bot_v4/fact_intake.py src/switchboard/application/mission_bot_v4/runtime.py deploy/redeploy.sh deploy/service-cut-inventory.json docs/AUTOPILOT-CUTOVER-RUNBOOK.md tests/test_arch_ms128_autopilot_legacy_removal.py
git commit -m "refactor(autopilot): remove the legacy process route"
```

## Final verification and promotion record

- [ ] Every task PR was based on freshly fetched canonical `origin/master`, not this design branch.
- [ ] Every focused test and the full canonical gate passed on the exact PR head.
- [ ] GitHub's merge-group gate passed and every PR is an ancestor of canonical `origin/master`.
- [ ] The ownership ratchet reports one application owner and only bounded repository writers.
- [ ] Production shadow parity is exact for the recorded candidate SHA.
- [ ] Separate-VM conformance passed all seven scenarios for that SHA.
- [ ] The production canary reached canonical Done for that SHA.
- [ ] No machine-generated Human event or attempt-exhaustion result exists in the evidence interval.
- [ ] The old active process route is retired only after soak and the rollback drill uses the same bounded package.
- [ ] Switchboard board tasks are reconciled to Done from canonical merge provenance, not merely marked complete at PR creation.
