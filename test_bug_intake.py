#!/usr/bin/env python3
"""BUG-2 proof for structured bug submission over REST and MCP."""
import json
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="bug-intake-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "required"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from fastapi.testclient import TestClient  # noqa: E402
    import store  # noqa: E402
    from app import app  # noqa: E402
    import mcp_server  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  bug intake proof requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0)


P = "bug2intake"
BUG_TOKEN = "bug-intake-token"
TASK_TOKEN = "task-write-token"
passed = failed = 0


class FakeCtx:
    def __init__(self, token):
        request = type("Request", (), {})()
        request.headers = {"authorization": f"Bearer {token}"}
        context = type("RequestContext", (), {})()
        context.request = request
        self.request_context = context


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def bug_tasks():
    return [t for t in store.list_tasks(project=P) if t["_wsId"] == "BUG"]


try:
    store.create_project("BUG2 Intake", project_id=P,
                         github_repo="6th-Element-Labs/projectplanner",
                         actor="seed")
    store.init_db(P)
    source = store.create_task({"workstream_id": "QA", "title": "source task"},
                               actor="seed", project=P)
    store.create_principal(
        kind="agent",
        display_name="codex/bug-reporter",
        token=BUG_TOKEN,
        scopes=["read", "write:bug_intake"],
        project=P,
    )
    store.create_principal(
        kind="agent",
        display_name="codex/task-writer",
        token=TASK_TOKEN,
        scopes=["read", "write:tasks"],
        project=P,
    )

    client = TestClient(app)
    payload = {
        "project": P,
        "source_task": source["task_id"],
        "observed_behavior": "claim_next offered human-gated work before approval",
        "expected_behavior": "human-gated work should stay unclaimable until approved",
        "repro_steps": "1. Create a gated task\n2. Call claim_next(lanes='HARDEN')",
        "evidence": {"command": "claim_next", "result": "unexpected claim"},
        "severity_hint": "high",
        "affected_surface": "TXP scheduler",
        "failure_class": "invalid_input",
    }

    missing_auth = client.post("/ixp/v1/bugs/submit", json=payload)
    ok(missing_auth.status_code == 401,
       "REST submit_bug rejects missing bearer token")
    forbidden = client.post(
        "/ixp/v1/bugs/submit",
        json=payload,
        headers={"Authorization": f"Bearer {TASK_TOKEN}"},
    )
    ok(forbidden.status_code == 403,
       "REST submit_bug rejects generic task-write scope")
    before_invalid = len(bug_tasks())
    invalid = client.post(
        "/ixp/v1/bugs/submit",
        json={**payload, "observed_behavior": ""},
        headers={"Authorization": f"Bearer {BUG_TOKEN}"},
    )
    ok(invalid.status_code == 400 and
       invalid.json()["detail"]["error"] == "missing_required_fields",
       "REST submit_bug fails closed on missing required fields")
    ok(len(bug_tasks()) == before_invalid,
       "invalid REST submit_bug does not create a BUG task")
    bad_source = client.post(
        "/ixp/v1/bugs/submit",
        json={**payload, "source_task": "NOPE-404"},
        headers={"Authorization": f"Bearer {BUG_TOKEN}"},
    )
    ok(bad_source.status_code == 400 and
       bad_source.json()["detail"]["error"] == "unknown_source_task",
       "REST submit_bug fails closed on unknown source_task")

    good = client.post(
        "/ixp/v1/bugs/submit",
        json=payload,
        headers={"Authorization": f"Bearer {BUG_TOKEN}"},
    )
    body = good.json()
    bug = body.get("bug") or {}
    report = (bug.get("agent_state") or {}).get("bug_report") or {}
    ok(good.status_code == 200 and body.get("submitted"),
       "REST submit_bug accepts a complete bug report")
    ok(bug.get("_wsId") == "BUG" and bug.get("status") == "Triage",
       "submitted bug becomes a BUG task in Triage")
    ok(report.get("source_task") == source["task_id"] and
       report.get("source_agent") == "codex/bug-reporter",
       "bug_report preserves source task and authenticated source agent")
    ok(report.get("evidence", {}).get("command") == "claim_next" and
       report.get("failure_class") == "invalid_input",
       "bug_report preserves structured evidence and failure class")
    source_after = store.get_task(source["task_id"], project=P)
    ok(any(a["kind"] == "bug.reported_from_task" for a in source_after["activity"]),
       "source task receives an audit link to the submitted bug")
    no_auto_claim = store.claim_next("codex/bug-worker", lanes=["BUG"], project=P)
    ok(not no_auto_claim.get("claimed") and
       no_auto_claim["dispatch_reason"]["skipped"]["status"] >= 1,
       "submitted Triage bugs are not auto-claimable")

    mcp_result = json.loads(mcp_server.submit_bug(
        source_task=source["task_id"],
        source_agent="codex/mcp-reporter",
        observed_behavior="MCP payload shape was confusing",
        expected_behavior="MCP tool should return structured error details",
        repro_steps="Call tool with malformed evidence JSON",
        evidence='{"url":"https://example.test/evidence"}',
        severity_hint="medium",
        affected_surface="MCP",
        failure_class="malformed_payload",
        duplicate_of=bug["task_id"],
        title="MCP structured error regression",
        ctx=FakeCtx(BUG_TOKEN),
        project=P,
    ))
    mcp_bug = mcp_result.get("bug") or {}
    mcp_report = (mcp_bug.get("agent_state") or {}).get("bug_report") or {}
    ok(mcp_result.get("submitted") and mcp_bug.get("_wsId") == "BUG",
       "MCP submit_bug creates a BUG intake task")
    ok(mcp_report.get("duplicate_of") == bug["task_id"] and
       mcp_report.get("evidence", {}).get("url") == "https://example.test/evidence",
       "MCP submit_bug preserves duplicate link and JSON evidence")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
