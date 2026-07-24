#!/usr/bin/env python3
"""COORD-18: durable, independent, SHA-fenced review verdict proof."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shutil
import tempfile
import threading


TMP = tempfile.mkdtemp(prefix="coord18-review-verdict-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_SQLITE_SINGLE_WRITER"] = "1"

import store  # noqa: E402
import auth  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from switchboard.application.commands import review_verdicts as commands  # noqa: E402
from switchboard.application.queries import review_verdicts as queries  # noqa: E402
from switchboard.api.routers.tasks import create_router as create_task_router  # noqa: E402
from switchboard.contracts.reviews import (  # noqa: E402
    REVIEW_FINDING_SCHEMA,
    REVIEW_VERDICT_SCHEMA,
    ReviewFinding,
)
from switchboard.storage.repositories.review_verdicts import (  # noqa: E402
    HISTORICAL_CO8_VERDICT_ID,
)
from switchboard.storage.repositories import review_verdicts as review_repository  # noqa: E402


PROJECT = "switchboard"
WORKER = "codex/COORD-18-worker"
REVIEWER = "codex/COORD-18-independent-review"
WORKER_PRINCIPAL_ID = "principal-worker"
REVIEWER_PRINCIPAL_ID = "principal-reviewer"
HEAD_1 = "a" * 40
HEAD_2 = "b" * 40
HEAD_RACE = "c" * 40
PR_URL = "https://github.com/6th-Element-Labs/projectplanner/pull/518"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def finding(fid="RV-1", *, state="open"):
    payload = {
        "id": fid,
        "location": "src/switchboard/domain/example.py:42",
        "category": "authorization",
        "severity": "high",
        "invariant_violated": "Default-deny authorization cannot be bypassed.",
        "repair_requirement": "Evaluate denial before success signals.",
        "class": "auto",
        "state": state,
    }
    if state != "open":
        payload.update({
            "resolved_by": WORKER,
            "resolved_reason": "Implemented the required repair.",
            "resolved_sha": HEAD_2,
        })
    return payload


def verdict(task_id, *, head=HEAD_1, status="changes_requested", findings=None,
            reviewer=REVIEWER, pr_url=PR_URL):
    return {
        "task_id": task_id,
        "pr_url": pr_url,
        "head_sha": head,
        "reviewer_principal": reviewer,
        "status": status,
        "findings": [finding()] if findings is None else findings,
    }


try:
    store.init_project_registry()
    store.init_db(PROJECT)
    task = store.create_task(
        {"workstream_id": "COORD", "title": "review verdict fixture"},
        actor="coord18-test", project=PROJECT)
    task_id = task["task_id"]
    store.register_agent(WORKER, "codex", lane="COORD", task_id=task_id,
                         project=PROJECT)
    store.register_agent(REVIEWER, "codex", lane="COORD", task_id=task_id,
                         project=PROJECT)
    worker_claim = store.claim_task(
        task_id, WORKER, principal_id=WORKER_PRINCIPAL_ID,
        actor="coord18-test", project=PROJECT)
    ok(worker_claim.get("claimed") is True, "worker claim establishes the implementation principal")
    store.mark_task_pr_opened(
        task_id, 518, PR_URL, branch=f"codex/{task_id}-fixture", head_sha=HEAD_1,
        actor="coord18-test", project=PROJECT)

    # NOTE: COORD-18 originally asserted here that a worker could not review its own
    # implementation. That fence was removed — every fleet agent authenticates through the
    # same shared `env-mcp-token` principal, so "reviewer principal must differ from worker
    # principal" was unsatisfiable: it rejected EVERY review by EVERY agent (not just
    # self-review) and deadlocked the board. Authentication is still fenced, below.

    unbound_reviewer = commands.execute_mapping(
        verdict(task_id), actor=REVIEWER, project=PROJECT)
    ok(unbound_reviewer.get("error_code") == "reviewer_principal_unbound",
       "review verdict fails closed without an authenticated principal ID")

    spoofed = commands.execute_mapping(
        verdict(task_id), actor="another-principal",
        principal_id=REVIEWER_PRINCIPAL_ID, project=PROJECT)
    ok(spoofed.get("error_code") == "reviewer_principal_mismatch",
       "reviewer_principal cannot be spoofed independently of the authenticated actor")

    two_findings = [finding("RV-1"), {
        **finding("RV-2"),
        "location": "src/switchboard/storage/example.py:88",
        "category": "lease_concurrency",
    }]
    recorded = commands.execute_mapping(
        verdict(task_id, findings=two_findings), actor=REVIEWER,
        principal_id="principal-reviewer", project=PROJECT)
    stored = recorded.get("verdict") or {}
    ok(recorded.get("created") is True
       and stored.get("schema") == REVIEW_VERDICT_SCHEMA
       and stored.get("finding_count") == 2
       and stored.get("reviewer_principal_id") == REVIEWER_PRINCIPAL_ID,
       "independent reviewer persists one typed changes_requested verdict")
    ok(all(item.get("schema") == REVIEW_FINDING_SCHEMA for item in stored.get("findings") or []),
       "each finding is a first-class typed record")
    ok(stored.get("valid_for_current_head") is True
       and stored.get("head_sha") == HEAD_1,
       "verdict is valid only for the exact current PR head")

    replay = commands.execute_mapping(
        verdict(task_id, findings=two_findings), actor=REVIEWER,
        principal_id=REVIEWER_PRINCIPAL_ID, project=PROJECT)
    ok(replay.get("idempotent_replay") is True
       and replay.get("verdict", {}).get("verdict_id") == stored.get("verdict_id"),
       "identical write for the same head is idempotent")
    conflict = commands.execute_mapping(
        verdict(task_id, findings=[finding("RV-DIFFERENT")]),
        actor=REVIEWER, principal_id=REVIEWER_PRINCIPAL_ID, project=PROJECT)
    ok(conflict.get("error_code") == "review_verdict_conflict",
       "a different verdict cannot overwrite the same task/head record")

    current = queries.get_for(task_id, project=PROJECT)
    listed = queries.list_findings_for(
        task_id, project=PROJECT, state="open", finding_class="auto",
        severity="high", current_head_only=True)
    ok(current and current["verdict_id"] == stored["verdict_id"],
       "current-head verdict is queryable without transcript context")
    ok(len(listed) == 2 and all(item["valid_for_current_head"] for item in listed),
       "review findings are queryable by state, class, severity, and current head")

    detail = store.get_task(task_id, project=PROJECT)
    ok(detail.get("finding_count") == 2
       and detail.get("review_verdict", {}).get("current_head_finding_count") == 2,
       "task finding_count reflects real code-review findings, not session hygiene")

    store.mark_task_pr_opened(
        task_id, 518, PR_URL, branch=f"codex/{task_id}-fixture", head_sha=HEAD_2,
        actor="coord18-test", project=PROJECT)
    stale = queries.get_for(task_id, project=PROJECT, head_sha=HEAD_1)
    no_current = queries.get_for(task_id, project=PROJECT)
    detail_after_push = store.get_task(task_id, project=PROJECT)
    ok(stale and stale["valid_for_current_head"] is False
       and stale["invalidated_by_head_sha"] == HEAD_2,
       "a new head SHA invalidates the prior verdict without deleting history")
    ok(no_current is None
       and detail_after_push["review_verdict"]["current_verdict_status"] == "missing"
       and detail_after_push["review_verdict"]["historical_finding_count"] == 2,
       "new code requires a fresh verdict while preserving historical findings")
    ok(queries.list_findings_for(
        task_id, project=PROJECT, current_head_only=True) == [],
       "current-head finding query never leaks findings from stale code")

    self_review = commands.execute_mapping(
        {
            **verdict(
                task_id, head=HEAD_2, status="pass", findings=[],
                reviewer=WORKER,
            ),
            "review_mode": "adversarial",
        },
        actor=WORKER,
        principal_id=WORKER_PRINCIPAL_ID,
        project=PROJECT,
    )
    ok(
        self_review.get("error_code") == "adversarial_self_review_forbidden",
        "an implementation actor cannot authorize its own adversarial review",
    )

    pass_result = commands.execute_mapping(
        {**verdict(task_id, head=HEAD_2, status="pass", findings=[]),
         "review_mode": "adversarial"},
        actor=REVIEWER, principal_id=REVIEWER_PRINCIPAL_ID, project=PROJECT)
    ok(pass_result.get("created") is True
       and pass_result.get("verdict", {}).get("status") == "pass",
       "fresh current head accepts an independent passing verdict")
    invalid_pass = commands.execute_mapping(
        verdict(task_id, head=HEAD_2, status="pass", findings=[finding("RV-OPEN")]),
        actor=REVIEWER, principal_id=REVIEWER_PRINCIPAL_ID, project=PROJECT)
    ok(invalid_pass.get("error_code") == "invalid_review_verdict",
       "pass verdict fails closed when it carries an open finding")
    invalid_changes = commands.execute_mapping(
        verdict(task_id, head=HEAD_2, findings=[finding("RV-FIXED", state="fixed")]),
        actor=REVIEWER, principal_id=REVIEWER_PRINCIPAL_ID, project=PROJECT)
    ok(invalid_changes.get("error_code") == "invalid_review_verdict",
       "changes_requested requires an open actionable finding")
    malformed_location = finding("RV-BAD-LOCATION")
    malformed_location["location"] = "no-line-number"
    malformed = commands.execute_mapping(
        verdict(task_id, head=HEAD_2, findings=[malformed_location]),
        actor=REVIEWER, principal_id=REVIEWER_PRINCIPAL_ID, project=PROJECT)
    ok(malformed.get("error_code") == "invalid_review_verdict",
       "malformed file locations fail before persistence")

    # The complete verdict command is one writer transaction. Simultaneous identical
    # calls must converge on one creation and deterministic replays, never leak raw
    # uniqueness errors or duplicate the audit event.
    race_task = store.create_task(
        {"workstream_id": "COORD", "title": "review verdict race fixture"},
        actor="coord18-test", project=PROJECT)
    race_task_id = race_task["task_id"]
    race_worker = f"{WORKER}-race"
    race_reviewer = f"{REVIEWER}-race"
    store.register_agent(race_worker, "codex", lane="COORD", task_id=race_task_id,
                         project=PROJECT)
    store.register_agent(race_reviewer, "codex", lane="COORD", task_id=race_task_id,
                         project=PROJECT)
    race_claim = store.claim_task(
        race_task_id, race_worker, principal_id="principal-worker-race",
        actor="coord18-test", project=PROJECT)
    ok(race_claim.get("claimed") is True,
       "concurrency fixture records an authenticated worker principal")
    race_pr_url = "https://github.com/6th-Element-Labs/projectplanner/pull/519"
    store.mark_task_pr_opened(
        race_task_id, 519, race_pr_url, branch=f"codex/{race_task_id}-fixture",
        head_sha=HEAD_RACE, actor="coord18-test", project=PROJECT)
    race_payload = verdict(
        race_task_id, head=HEAD_RACE, status="pass", findings=[],
        reviewer=race_reviewer, pr_url=race_pr_url)
    barrier = threading.Barrier(12)

    def record_race_verdict(_index):
        barrier.wait()
        return commands.execute_mapping(
            race_payload, actor=race_reviewer,
            principal_id="principal-reviewer-race", project=PROJECT)

    race_results = []
    race_exceptions = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(record_race_verdict, index) for index in range(12)]
        for future in futures:
            try:
                race_results.append(future.result())
            except Exception as exc:  # surfaced explicitly by the regression assertion
                race_exceptions.append(exc)
    ok(not race_exceptions
       and sum(result.get("created") is True for result in race_results) == 1
       and sum(result.get("idempotent_replay") is True for result in race_results) == 11,
       "12 concurrent identical verdicts yield one creation and 11 idempotent replays")
    with store._conn(PROJECT) as c:
        race_verdict_rows = c.execute(
            "SELECT COUNT(*) FROM review_verdicts WHERE task_id=? AND head_sha=?",
            (race_task_id, HEAD_RACE),
        ).fetchone()[0]
        race_event_rows = c.execute(
            "SELECT COUNT(*) FROM activity WHERE task_id=? "
            "AND kind='review.verdict_recorded'",
            (race_task_id,),
        ).fetchone()[0]
    ok(race_verdict_rows == 1 and race_event_rows == 1,
       "concurrent replay persists exactly one verdict and one audit event atomically")

    replacement_pr_url = (
        "https://github.com/6th-Element-Labs/projectplanner/pull/522"
    )
    store.mark_task_pr_opened(
        race_task_id,
        522,
        replacement_pr_url,
        branch=f"codex/{race_task_id}-replacement",
        head_sha=HEAD_RACE,
        actor="coord18-test",
        project=PROJECT,
    )
    replacement_payload = verdict(
        race_task_id,
        head=HEAD_RACE,
        status="pass",
        findings=[],
        reviewer=race_reviewer,
        pr_url=replacement_pr_url,
    )
    replacement_barrier = threading.Barrier(12)

    def record_replacement_verdict(_index):
        replacement_barrier.wait()
        return commands.execute_mapping(
            replacement_payload,
            actor=race_reviewer,
            principal_id="principal-reviewer-race",
            project=PROJECT,
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        replacement_results = list(
            pool.map(record_replacement_verdict, range(12)))
    with store._conn(PROJECT) as c:
        same_sha_rows = c.execute(
            "SELECT pr_url FROM review_verdicts "
            "WHERE task_id=? AND head_sha=? ORDER BY pr_url",
            (race_task_id, HEAD_RACE),
        ).fetchall()
    ok(
        sum(row.get("created") is True for row in replacement_results) == 1
        and sum(
            row.get("idempotent_replay") is True
            for row in replacement_results
        ) == 11
        and [row["pr_url"] for row in same_sha_rows]
        == sorted([race_pr_url, replacement_pr_url]),
        "12 concurrent replacement-PR writes converge without reusing the "
        "same-SHA historical verdict",
    )

    # A failure after the verdict and finding rows are written must roll back the whole
    # command, including its audit event. This catches regressions back to per-statement
    # proxy commits even when the happy-path concurrency test still happens to serialize.
    rollback_task = store.create_task(
        {"workstream_id": "COORD", "title": "review verdict rollback fixture"},
        actor="coord18-test", project=PROJECT)
    rollback_task_id = rollback_task["task_id"]
    rollback_worker = f"{WORKER}-rollback"
    rollback_reviewer = f"{REVIEWER}-rollback"
    store.claim_task(
        rollback_task_id, rollback_worker, principal_id="principal-worker-rollback",
        actor="coord18-test", project=PROJECT)
    rollback_pr_url = "https://github.com/6th-Element-Labs/projectplanner/pull/520"
    store.mark_task_pr_opened(
        rollback_task_id, 520, rollback_pr_url,
        branch=f"codex/{rollback_task_id}-fixture", head_sha="d" * 40,
        actor="coord18-test", project=PROJECT)
    original_insert_findings = review_repository._insert_findings_in

    def fail_after_finding_insert(c, verdict_id, data, *, created_at, recorded_at):
        original_insert_findings(
            c, verdict_id, data, created_at=created_at, recorded_at=recorded_at)
        raise RuntimeError("injected post-finding failure")

    review_repository._insert_findings_in = fail_after_finding_insert
    rollback_failed = False
    try:
        commands.execute_mapping(
            verdict(
                rollback_task_id, head="d" * 40, findings=[finding("RV-ROLLBACK")],
                reviewer=rollback_reviewer, pr_url=rollback_pr_url),
            actor=rollback_reviewer, principal_id="principal-reviewer-rollback",
            project=PROJECT)
    except RuntimeError as exc:
        rollback_failed = str(exc) == "injected post-finding failure"
    finally:
        review_repository._insert_findings_in = original_insert_findings
    with store._conn(PROJECT) as c:
        rollback_verdict_rows = c.execute(
            "SELECT COUNT(*) FROM review_verdicts WHERE task_id=?",
            (rollback_task_id,),
        ).fetchone()[0]
        rollback_finding_rows = c.execute(
            "SELECT COUNT(*) FROM review_findings WHERE task_id=?",
            (rollback_task_id,),
        ).fetchone()[0]
        rollback_event_rows = c.execute(
            "SELECT COUNT(*) FROM activity WHERE task_id=? "
            "AND kind='review.verdict_recorded'",
            (rollback_task_id,),
        ).fetchone()[0]
    ok(rollback_failed and rollback_verdict_rows == 0
       and rollback_finding_rows == 0 and rollback_event_rows == 0,
       "post-insert failure rolls back verdict, findings, and audit event together")

    # REST authorization can authenticate in the route resolver without middleware
    # state (notably dev-open mode).  The durable reviewer identity must come from the
    # resolved write binding, not from optional request.state middleware bookkeeping.
    rest_task = store.create_task(
        {"workstream_id": "COORD", "title": "review verdict REST auth fixture"},
        actor="coord18-test", project=PROJECT)
    rest_task_id = rest_task["task_id"]
    rest_worker = f"{WORKER}-rest"
    rest_reviewer = f"{REVIEWER}-rest"
    rest_resolver = "operator/COORD-19-rest-resolution"
    rest_head = "e" * 40
    rest_pr_url = "https://github.com/6th-Element-Labs/projectplanner/pull/521"
    store.register_agent(rest_worker, "codex", lane="COORD", task_id=rest_task_id,
                         project=PROJECT)
    store.register_agent(rest_reviewer, "codex", lane="COORD", task_id=rest_task_id,
                         project=PROJECT)
    store.register_agent(rest_resolver, "web", lane="COORD", task_id=rest_task_id,
                         project=PROJECT)
    store.claim_task(
        rest_task_id, rest_worker, principal_id="principal-worker-rest",
        actor="coord18-test", project=PROJECT)
    store.mark_task_pr_opened(
        rest_task_id, 521, rest_pr_url, branch=f"codex/{rest_task_id}-fixture",
        head_sha=rest_head, actor="coord18-test", project=PROJECT)
    rest_app = FastAPI()
    rest_app.include_router(create_task_router(
        resolve_project=lambda value: value,
        resolve_principal=lambda request, selected_project, scopes, dev_actor="web":
            auth.authenticate_request(
                request, selected_project, scopes, dev_actor=dev_actor),
    ))
    with TestClient(rest_app) as client:
        rest_response = client.post(
            f"/api/tasks/{rest_task_id}/review_verdict?project={PROJECT}",
            json=verdict(
                rest_task_id, head=rest_head, status="changes_requested",
                findings=[finding("RV-REST-OVERRIDE")],
                reviewer=rest_reviewer, pr_url=rest_pr_url),
        )
        resolution_response = client.post(
            f"/api/tasks/{rest_task_id}/review_findings/RV-REST-OVERRIDE/resolution"
            f"?project={PROJECT}",
            json={
                "head_sha": rest_head,
                "state": "overridden",
                "resolved_reason": "Admin accepts the explicitly documented exception.",
                "resolved_sha": rest_head,
                "resolver_principal": rest_resolver,
            },
        )
    rest_body = rest_response.json()
    ok(rest_response.status_code == 200
       and rest_body.get("created") is True
       and rest_body.get("verdict", {}).get("reviewer_principal_id") == "dev-open",
       "REST review binds the resolver-authenticated principal without middleware state")
    resolution_body = resolution_response.json()
    ok(resolution_response.status_code == 200
       and resolution_body.get("finding", {}).get("state") == "overridden"
       and resolution_body.get("finding", {}).get("resolved_principal_id") == "dev-open"
       and resolution_body.get("verdict", {}).get("status") == "pass",
       "REST admin authority durably overrides an open finding and unblocks the verdict")

    # Historical repair: recreate the live CO-8 identity in the hermetic board,
    # then prove the startup backfill restores all four review findings once.
    with store._conn(PROJECT) as c:
        now = 1784008391.0
        c.execute(
            "INSERT INTO tasks(task_id, workstream_id, workstream_name, title, status, "
            "depends_on, is_blocking, sort_order, created_at, updated_at) "
            "VALUES ('CO-8','CO','CO','CO-8 fixture','Done','[]',0,8,?,?)",
            (now, now),
        )
    store.mark_task_pr_opened(
        "CO-8", 444, "https://github.com/6th-Element-Labs/projectplanner/pull/444",
        branch="codex/CO-8-subscription-capacity",
        head_sha="0b960517fdc9f1a9b269fc77e796e776edf4ed8c",
        actor="coord18-test", project=PROJECT)
    store.seed_if_empty(PROJECT)
    co8_findings = store.list_review_findings(task_id="CO-8", project=PROJECT)
    co8_detail = store.get_task("CO-8", project=PROJECT)
    ok(len(co8_findings) == 4
       and {item["id"] for item in co8_findings} == {
           "CO8-REVIEW-1", "CO8-REVIEW-2", "CO8-REVIEW-3", "CO8-REVIEW-4"},
       "CO-8's four lost review findings are durably backfilled")
    ok(all(item["state"] == "fixed" and not item["valid_for_current_head"]
           for item in co8_findings),
       "historical findings preserve remediation state and remain fenced from the new head")
    historical_verdict = store.get_review_verdict(
        "CO-8", head_sha="94f03c6fb485bd0959eff9070a50c9356218f3ee",
        project=PROJECT)
    historical_contracts = [
        ReviewFinding.model_validate(item)
        for item in historical_verdict["findings"]
    ]
    ok(len(historical_contracts) == 4
       and all(item.state == "fixed" for item in historical_contracts),
       "historical fixed findings remain valid contracts without invented authority metadata")
    authority_metadata_required = False
    try:
        ReviewFinding.model_validate({
            **historical_verdict["findings"][0],
            "state": "waived",
        })
    except ValueError:
        authority_metadata_required = True
    ok(authority_metadata_required,
       "waived findings still require authenticated authority identity and timestamp")
    ok(co8_detail["finding_count"] == 4
       and co8_detail["review_verdict"]["current_head_finding_count"] == 0,
       "CO-8 task detail reports four real findings without treating them as current")
    with store._conn(PROJECT) as c:
        verdict_rows = c.execute(
            "SELECT COUNT(*) FROM review_verdicts WHERE verdict_id=?",
            (HISTORICAL_CO8_VERDICT_ID,),
        ).fetchone()[0]
        event_rows_before = c.execute(
            "SELECT COUNT(*) FROM activity WHERE task_id='CO-8' "
            "AND kind='review.verdict_backfilled'",
        ).fetchone()[0]
    store.seed_if_empty(PROJECT)
    with store._conn(PROJECT) as c:
        event_rows_after = c.execute(
            "SELECT COUNT(*) FROM activity WHERE task_id='CO-8' "
            "AND kind='review.verdict_backfilled'",
        ).fetchone()[0]
    ok(verdict_rows == 1 and event_rows_before == event_rows_after == 1,
       "historical backfill and its audit event are restart-idempotent")

finally:
    shutil.rmtree(TMP, ignore_errors=True)


print(f"\nCOORD-18 review verdicts: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
