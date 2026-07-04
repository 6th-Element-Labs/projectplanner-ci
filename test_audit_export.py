#!/usr/bin/env python3
"""HARDEN-13 enterprise audit export regression."""
import json
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="audit-export-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "required"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import auth  # noqa: E402
import store  # noqa: E402

P = "switchboard"
ADMIN_TOKEN = "audit-admin-token"
VIEWER_TOKEN = "audit-viewer-token"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def authz(token):
    return {"Authorization": f"Bearer {token}"}


def bundle_text(bundle):
    return json.dumps(bundle, sort_keys=True)


try:
    store.init_db(P)
    admin = store.create_principal(
        kind="system",
        display_name="audit admin",
        token=ADMIN_TOKEN,
        scopes=["read", "write:system", "write:tasks", "write:ixp", "admin"],
        project=P,
    )
    viewer = store.create_principal(
        kind="agent",
        display_name="audit viewer",
        token=VIEWER_TOKEN,
        scopes=["read"],
        project=P,
    )
    store.grant_project_role(P, "principal", admin["id"], "admin", created_by="test")
    store.append_activity(
        "access.token_created",
        "test",
        {"principal": store.public_principal_record(admin, project=P), "token_returned_once": True},
        project=P,
    )
    store.revoke_principal_token(viewer["id"], actor="test", project=P)
    store.create_auth_session(admin["id"], "audit-session-token", 3600,
                              user_agent="test", ip="127.0.0.1", project=P)

    task = store.create_task({"workstream_id": "AUDIT", "title": "export evidence"},
                             actor="test", project=P)
    store.register_agent("codex/AUDIT-1", "codex", lane="AUDIT", task_id=task["task_id"],
                         principal_id=admin["id"], actor="test", project=P)
    claim = store.claim_task(task["task_id"], "codex/AUDIT-1", principal_id=admin["id"],
                             actor="test", project=P)
    store.send_agent_message("operator", "codex/AUDIT-1", "please export",
                             task_id=task["task_id"], requires_ack=True,
                             principal_id=admin["id"], project=P)
    store.upsert_runner_session({
        "runner_session_id": "runner-audit-1",
        "host_id": "host/audit",
        "agent_id": "codex/AUDIT-1",
        "runtime": "codex",
        "task_id": task["task_id"],
        "claim_id": claim["claim_id"],
        "status": "running",
        "control": {"mode": "repo_edit", "managed_process": True, "runner_kill": True},
    }, principal_id=admin["id"], actor="test", project=P)
    store.mark_task_pr_opened(task["task_id"], 101, "https://example.test/pr/101",
                              branch="codex/AUDIT-1", head_sha="abc123",
                              actor="github-webhook", project=P)
    store.mark_task_merged(task["task_id"], "def456", pr_number=101,
                           pr_url="https://example.test/pr/101",
                           branch="codex/AUDIT-1", head_sha="abc123",
                           actor="github-webhook", project=P)
    store.report_usage(source="agent_report", confidence="reported", task_id=task["task_id"],
                       claim_id=claim["claim_id"], agent_id="codex/AUDIT-1",
                       principal_id=admin["id"], prompt_tokens=100, completion_tokens=25,
                       cost_usd=1.5, project=P)
    outcome = store.record_outcome("audit", "export proves evidence graph",
                                   task_id=task["task_id"], claim_id=claim["claim_id"],
                                   evidence={"file": "audit.json"}, project=P)
    verified = store.verify_outcome(outcome["id"], verifier="test",
                                    verification="reviewed", project=P)
    kpi = store.create_kpi("verified audit exports", "exports", "increase", project=P)
    store.link_outcome_to_kpi(verified["id"], kpi["id"], contribution=1,
                              confidence="measured", project=P)

    bundle = store.audit_export(project=P)
    text = bundle_text(bundle)
    ok(bundle["schema"] == "switchboard.audit_export.v1",
       "audit export is versioned")
    ok(bundle["summary"]["task_count"] >= 1 and bundle["summary"]["activity_count"] >= 1,
       "audit export summarizes task and activity evidence")
    ok(any(t["task_id"] == task["task_id"] and
           t["provenance"]["type"] == "github_pr_merged"
           for t in bundle["tasks"]),
       "audit export includes task git merge provenance")
    ok(any(c["id"] == claim["claim_id"] for c in bundle["claims"]),
       "audit export includes task claims")
    ok(bundle["messages"] and bundle["runner_sessions"],
       "audit export includes messages and runner sessions")
    ok(bundle["economics"]["project_tally"]["totals"]["spend"]["cost_usd"] >= 1.5,
       "audit export includes Tally spend and outcomes")
    ok(any(p["id"] == admin["id"] for p in bundle["access"]["principals"]),
       "audit export includes scoped principals")
    ok(bundle["access"]["project_role_grants"],
       "audit export includes project role grants")
    ok("token_hash" not in text and "password_hash" not in text and "session_hash" not in text,
       "audit export does not expose stored secret hashes")
    ok(ADMIN_TOKEN not in text and VIEWER_TOKEN not in text and "audit-session-token" not in text,
       "audit export does not expose raw bearer or session tokens")

    try:
        from fastapi.testclient import TestClient  # noqa: E402
        from app import app  # noqa: E402
    except ModuleNotFoundError as exc:
        print(f"  SKIP  audit export REST smoke requires optional dependency: {exc.name}")
    else:
        client = TestClient(app)
        viewer_res = client.get(f"/api/audit/export?project={P}", headers=authz(VIEWER_TOKEN))
        ok(viewer_res.status_code == 401,
           "revoked/read-only principal cannot download audit export")
        admin_res = client.get(f"/api/audit/export?project={P}", headers=authz(ADMIN_TOKEN))
        ok(admin_res.status_code == 200 and admin_res.json()["schema"] == bundle["schema"],
           "write:system principal can download audit export over REST")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
