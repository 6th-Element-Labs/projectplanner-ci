#!/usr/bin/env python3
"""QA-9 fail-early negative pass for Switchboard release-candidate hardening."""
import json
import os
import shutil
import sys
import tempfile
import warnings

_TMP = tempfile.mkdtemp(prefix="qa9-negative-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "required"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    warnings.filterwarnings(
        "ignore",
        message=r"Using `httpx` with `starlette\.testclient` is deprecated.*",
        category=Warning,
    )
    from fastapi.testclient import TestClient  # noqa: E402
    import store  # noqa: E402
    from app import app  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  QA-9 negative pass requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0)


P = "qa9negative"
TASK_TOKEN = "qa9-task-token"
BUG_TOKEN = "qa9-bug-token"
passed = failed = 0


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def title_exists(project, title):
    return any(t["title"] == title for t in store.list_tasks(project=project))


def finding(report, code, task_id=None):
    for item in report["findings"]:
        if item.get("code") == code and (task_id is None or item.get("task_id") == task_id):
            return item
    return None


try:
    store.init_project_registry()
    store.create_project(
        "QA-9 Negative",
        project_id=P,
        github_repo="6th-Element-Labs/projectplanner",
        actor="seed",
    )
    store.init_db(P)
    source = store.create_task({"workstream_id": "QA", "title": "QA-9 source"},
                               actor="seed", project=P)
    store.create_principal(
        kind="agent",
        display_name="codex/qa9-task",
        token=TASK_TOKEN,
        scopes=["read", "write:tasks"],
        project=P,
    )
    store.create_principal(
        kind="agent",
        display_name="codex/qa9-bug",
        token=BUG_TOKEN,
        scopes=["read", "write:bug_intake"],
        project=P,
    )

    client = TestClient(app)

    unknown_title = "qa9 unknown project leak"
    unknown = client.post(
        "/api/tasks?project=definitely-not-a-project",
        json={"workstream_id": "QA", "title": unknown_title},
        headers=bearer(TASK_TOKEN),
    )
    ok(unknown.status_code == 400 and "unknown project" in unknown.text,
       "unknown project fails closed with a loud HTTP error")
    ok(not title_exists(P, unknown_title) and
       not title_exists(store.DEFAULT_PROJECT, unknown_title),
       "unknown project request does not write to the selected or default board")

    missing_project_title = "qa9 missing project leak"
    missing_project = client.post(
        "/api/tasks",
        json={"workstream_id": "QA", "title": missing_project_title},
        headers=bearer(TASK_TOKEN),
    )
    ok(missing_project.status_code in {400, 401, 403, 422},
       "missing project on REST write does not silently route to a board")
    ok(not title_exists(P, missing_project_title) and
       not title_exists(store.DEFAULT_PROJECT, missing_project_title),
       "missing-project REST write creates no task on any board")

    bug_payload = {
        "source_task": source["task_id"],
        "source_agent": "codex/qa9-bug",
        "observed_behavior": "negative path should not create hidden success",
        "expected_behavior": "missing project should fail before state changes",
        "repro_steps": "POST /ixp/v1/bugs/submit without project",
        "evidence": {"case": "missing_project"},
        "severity_hint": "medium",
        "affected_surface": "IXP bug intake",
        "failure_class": "invalid_input",
    }
    before_bug_count = len([t for t in store.list_tasks(project=P) if t["_wsId"] == "BUG"])
    missing_protocol_project = client.post(
        "/ixp/v1/bugs/submit",
        json=bug_payload,
        headers=bearer(BUG_TOKEN),
    )
    ok(missing_protocol_project.status_code in {401, 403, 400},
       "missing project on protocol write fails instead of silently filing elsewhere")
    ok(len([t for t in store.list_tasks(project=P) if t["_wsId"] == "BUG"]) == before_bug_count,
       "missing-project protocol write does not create a BUG on the intended project")

    bad_token_title = "qa9 bad token leak"
    bad_token = client.post(
        f"/api/tasks?project={P}",
        json={"workstream_id": "QA", "title": bad_token_title},
        headers=bearer("not-the-token"),
    )
    ok(bad_token.status_code == 401,
       "bad bearer token is rejected before write")
    ok(not title_exists(P, bad_token_title),
       "bad-token request does not write a task")

    missing_task = client.get(f"/api/tasks/NOPE-404?project={P}", headers=bearer(TASK_TOKEN))
    ok(missing_task.status_code == 404,
       "missing task lookup returns a loud not-found response")
    missing_verify = client.post(
        f"/api/tasks/NOPE-404/verify_offline?project={P}",
        json={"evidence": {"operator": "qa9"}},
        headers=bearer(TASK_TOKEN),
    )
    ok(missing_verify.status_code == 404 and
       missing_verify.json()["detail"]["error"] == "task not found",
       "missing offline-verification target preserves structured error detail")

    offline = store.create_task({"workstream_id": "QA", "title": "malformed evidence"},
                                actor="seed", project=P)
    store.update_task(offline["task_id"], {"status": "In Review"}, actor="seed", project=P)
    malformed = client.post(
        f"/api/tasks/{offline['task_id']}/verify_offline?project={P}",
        json={"evidence": {"operator": "qa9"}, "evidence_hash": "not-a-sha"},
        headers=bearer(TASK_TOKEN),
    )
    malformed_detail = malformed.json()["detail"]
    ok(malformed.status_code == 409 and
       malformed_detail["error"] == "invalid_evidence_hash" and
       malformed_detail["task_id"] == offline["task_id"],
       "malformed offline evidence fails with structured invalid_evidence_hash")
    ok(store.get_task(offline["task_id"], project=P)["status"] == "In Review",
       "malformed evidence does not advance task status")

    stale = store.create_task({"workstream_id": "QA", "title": "stale claim and lease"},
                              actor="seed", project=P)
    stale_claim = store.claim_task(
        stale["task_id"],
        "codex/qa9-stale",
        principal_id="qa9-principal",
        actor="codex/qa9",
        ttl_seconds=60,
        project=P,
    )
    file_lease = store.claim_files(
        "codex/qa9-file-stale",
        ["qa9-negative.txt"],
        task_id=stale["task_id"],
        ttl_minutes=0,
        project=P,
    )
    with store._conn(P) as c:
        c.execute("UPDATE task_claims SET expires_at=0 WHERE id=?",
                  (stale_claim["claim_id"],))
        c.execute("UPDATE resource_leases SET claimed_at=0, ttl_seconds=1 WHERE id=?",
                  (stale_claim["lease"]["lease_id"],))
    stale_report = store.reconcile(project=P)
    for code in ("stale_task_claim", "stale_resource_lease", "stale_file_lease"):
        item = finding(stale_report, code, stale["task_id"])
        ok(item and item["failure_class"] == "failed_gate" and item.get("expected_signal"),
           f"reconcile reports {code} as a typed failed gate")
    ok(file_lease["lease_id"] in [f["detail"].split()[2] for f in stale_report["findings"]
                                  if f["code"] == "stale_file_lease"],
       "expired file lease identity is preserved in reconcile detail")

    pr_task = store.create_task({
        "workstream_id": "QA",
        "title": "PR unavailable",
        "description": "policy_profile:no_repo\nSynthetic reconcile fixture; not a code-work claim.",
    }, actor="seed", project=P)
    pr_claim = store.claim_task(
        pr_task["task_id"],
        "codex/qa9-pr",
        principal_id="qa9-principal",
        actor="codex/qa9",
        project=P,
    )
    store.complete_claim(
        pr_claim["claim_id"],
        evidence=json.dumps({
            "branch": f"codex/{pr_task['task_id']}",
            "head_sha": "abc123def456",
            "pr_number": 999999,
            "pr_url": "https://github.com/6th-Element-Labs/projectplanner/pull/999999",
        }),
        actor="codex/qa9",
        project=P,
    )
    original_token = store._github_token
    original_pr = store._github_pr
    seen_pr = []

    def fake_pr(repo, number, token=""):
        seen_pr.append({"repo": repo, "number": number, "token": token})
        return None

    store._github_token = lambda: ""
    store._github_pr = fake_pr
    try:
        pr_report = store.reconcile(project=P)
    finally:
        store._github_token = original_token
        store._github_pr = original_pr
    unavailable = finding(pr_report, "pr_state_unavailable", pr_task["task_id"])
    ok(pr_report["external_checks"]["github_prs"] == "checked_unauthenticated" and
       seen_pr and seen_pr[0]["token"] == "",
       "missing GitHub token is visible as an unauthenticated PR check")
    ok(unavailable and unavailable["failure_class"] == "broken_connection" and
       unavailable.get("expected_signal"),
       "unavailable GitHub PR state is a typed broken connection")

    message_task = store.create_task({"workstream_id": "QA", "title": "unreachable agent"},
                                     actor="seed", project=P)
    unreachable = store.send_agent_message(
        "codex/qa9",
        "claude/missing-agent",
        "please ack",
        task_id=message_task["task_id"],
        requires_ack=True,
        ack_deadline_minutes=-1,
        principal_id="qa9-principal",
        project=P,
    )
    ok(unreachable["delivery_status"] == "unreachable" and
       unreachable["fallback"]["failure_class"] == "unreachable_agent",
       "unreachable directed agent delivery is typed and visible")
    swept = store.sweep_coordination_monitors(project=P)
    timeout = store.get_message_status(unreachable["id"], project=P)
    ok(swept["fired"] >= 1 and
       timeout["monitor"]["result"]["failure_class"] == "unreachable_agent" and
       not timeout["monitor"]["result"].get("wake_id"),
       "expired ack monitor preserves failure class without lifecycle effects")

    unbound_task = store.create_task({"workstream_id": "QA", "title": "unbound identity"},
                                     actor="seed", project=P)
    store.add_comment(unbound_task["task_id"], "env-mcp-token",
                      "shared-token write with no registered runtime", project=P)
    unbound = store.send_agent_message(
        "codex/qa9",
        "claude/unbound-agent",
        "are you live?",
        task_id=unbound_task["task_id"],
        requires_ack=True,
        principal_id="qa9-principal",
        project=P,
    )
    unbound_after = store.get_task(unbound_task["task_id"], project=P)
    ok(any(a["kind"] == "principal.unbound_write" and
           a["payload"].get("failure_class") == "unbound_identity"
           for a in unbound_after["activity"]),
       "shared-principal write is audited as unbound_identity")
    ok(unbound["delivery_status"] == "identity_unbound" and
       unbound["fallback"]["failure_class"] == "unbound_identity",
       "directed message preserves unbound identity risk instead of implying safe takeover")

    dep = store.create_task({"workstream_id": "QA", "title": "dependency done", "status": "Done"},
                            actor="seed", project=P)
    stale_summary = store.create_task(
        {"workstream_id": "QA", "title": "stale summary", "depends_on": [dep["task_id"]]},
        actor="seed",
        project=P,
    )
    store.set_task_summary(
        stale_summary["task_id"],
        "This task is blocked on dependencies and cannot begin.",
        activity_cursor=1,
        project=P,
    )
    summary_detail = store.get_task(stale_summary["task_id"], project=P)
    state = summary_detail["rationale_state"]
    ok(summary_detail["dependency_state"]["ready"] and summary_detail["rationale"] is None and
       state["stale"] and state["failure_class"] == "missing_data" and
       state.get("expected_signal"),
       "stale generated task summary is suppressed and typed as missing data")

    bug = store.submit_bug({
        **bug_payload,
        "project": P,
        "source_task": source["task_id"],
        "failure_class": "broken_connection",
        "observed_behavior": "GitHub PR state was unavailable during QA-9",
        "expected_behavior": "Switchboard should preserve the connection failure signal",
        "evidence": {"finding": "pr_state_unavailable"},
    }, actor="codex/qa9-bug", project=P)
    report = (bug.get("bug") or {}).get("agent_state", {}).get("bug_report", {})
    ok(bug.get("submitted") and
       report.get("fail_fix_signal", {}).get("failure_class") == "broken_connection",
       "BUG-15 taxonomy can turn a QA-9 finding into structured BUG intake")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
