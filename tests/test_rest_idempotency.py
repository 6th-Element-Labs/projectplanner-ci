#!/usr/bin/env python3
"""ARCH-MS-44: Idempotency-Key on mutating REST endpoints via db/core primitives."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT


TMP = tempfile.mkdtemp(prefix="arch-ms44-idempotency-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from switchboard.api import idempotency as idem_api  # noqa: E402
from switchboard.contracts.openapi import build_openapi_document  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    helper_src = (ROOT / "src/switchboard/api/idempotency.py").read_text(encoding="utf-8")
    ok("IDEMPOTENCY_HEADER" in helper_src and "run_with_idempotency" in helper_src,
       "api/idempotency.py exposes header resolve + db/core run_with_idempotency")
    ok("store._idem_hit" in helper_src and "store._idem_store" in helper_src,
       "REST helper wires db/core _idem_hit / _idem_store through the store facade")
    ok("IdempotentOutcome" in helper_src or "replayed" in helper_src,
       "run_with_idempotency reports whether the response was a replay")

    for path in (
        "src/switchboard/api/routers/tasks.py",
        "src/switchboard/api/routers/claims.py",
        "src/switchboard/api/routers/messaging.py",
        "src/switchboard/api/routers/wakes.py",
    ):
        src = (ROOT / path).read_text(encoding="utf-8")
        ok("inject_idem_key" in src and "raise_if_idem_conflict" in src,
           f"{path} injects Idempotency-Key and maps conflicts to HTTP 409")

    doc = build_openapi_document()
    create_op = doc["paths"]["/api/tasks"]["post"]
    headers = [
        p for p in (create_op.get("parameters") or [])
        if p.get("in") == "header" and p.get("name") == "Idempotency-Key"
    ]
    ok(len(headers) == 1, "OpenAPI createTask documents Idempotency-Key header")
    ok("409" in create_op.get("responses", {}),
       "OpenAPI createTask documents 409 idem_key_conflict")

    client = TestClient(app)

    # --- create_task: header replay ---
    headers = {"Idempotency-Key": "rest-create-1"}
    body = {"workstream_id": "ARCH", "title": "idem create"}
    first = client.post("/api/tasks", params={"project": "switchboard"},
                        json=body, headers=headers)
    second = client.post("/api/tasks", params={"project": "switchboard"},
                         json=body, headers=headers)
    ok(first.status_code == 200 and second.status_code == 200,
       "create_task Idempotency-Key replay returns 200 twice")
    ok(first.json().get("task_id") and first.json()["task_id"] == second.json().get("task_id"),
       "create_task replay returns the same task_id (no duplicate create)")

    conflict = client.post(
        "/api/tasks", params={"project": "switchboard"},
        json={"workstream_id": "ARCH", "title": "different title"},
        headers=headers)
    ok(conflict.status_code == 409, "create_task key reuse with different body is 409")
    conflict_body = conflict.json().get("detail") or conflict.json()
    ok(conflict_body.get("error_code") == "idem_key_conflict",
       "create_task conflict envelope uses idem_key_conflict")

    # --- update_task: body idem_key alias ---
    task_id = first.json()["task_id"]
    patch_body = {"title": "renamed once", "idem_key": "rest-update-1"}
    u1 = client.patch(f"/api/tasks/{task_id}", params={"project": "switchboard"},
                      json=patch_body)
    u2 = client.patch(f"/api/tasks/{task_id}", params={"project": "switchboard"},
                      json=patch_body)
    ok(u1.status_code == 200 and u2.status_code == 200
       and u1.json().get("title") == "renamed once"
       and u2.json().get("title") == "renamed once",
       "update_task body idem_key replay is stable")

    u_conflict = client.patch(
        f"/api/tasks/{task_id}", params={"project": "switchboard"},
        json={"title": "renamed differently", "idem_key": "rest-update-1"})
    u_conflict_body = u_conflict.json().get("detail") or u_conflict.json()
    ok(u_conflict.status_code == 409
       and u_conflict_body.get("error") == "idem_key_conflict",
       "update_task conflict returns 409 idem_key_conflict")

    # --- add_comment ---
    c_headers = {"Idempotency-Key": "rest-comment-1"}
    c1 = client.post(
        f"/api/tasks/{task_id}/comment", params={"project": "switchboard"},
        json={"text": "hello once"}, headers=c_headers)
    c2 = client.post(
        f"/api/tasks/{task_id}/comment", params={"project": "switchboard"},
        json={"text": "hello once"}, headers=c_headers)
    ok(c1.status_code == 200 and c2.status_code == 200, "add_comment replay returns 200")
    comments = [
        a for a in (c1.json().get("activity") or [])
        if a.get("kind") == "comment"
        and (a.get("payload") or {}).get("text") == "hello once"
    ]
    comments2 = [
        a for a in (c2.json().get("activity") or [])
        if a.get("kind") == "comment"
        and (a.get("payload") or {}).get("text") == "hello once"
    ]
    ok(len(comments) == 1 and len(comments2) == 1,
       "add_comment Idempotency-Key does not duplicate the activity row")

    c_conflict = client.post(
        f"/api/tasks/{task_id}/comment", params={"project": "switchboard"},
        json={"text": "hello twice"}, headers=c_headers)
    ok(c_conflict.status_code == 409, "add_comment conflict is HTTP 409")

    # --- send: store-native idempotency via header injection ---
    send_headers = {"Idempotency-Key": "rest-send-1"}
    send_body = {
        "from_agent": "agent-a",
        "to_agent": "agent-b",
        "message": "ping",
        "project": "switchboard",
    }
    s1 = client.post("/ixp/v1/send", json=send_body, headers=send_headers)
    s2 = client.post("/ixp/v1/send", json=send_body, headers=send_headers)
    ok(s1.status_code == 200 and s2.status_code == 200, "ixp send header replay returns 200")
    ok(s1.json().get("id") and s1.json()["id"] == s2.json().get("id"),
       "ixp send replay returns the same message id")
    s_conflict = client.post(
        "/ixp/v1/send",
        json={**send_body, "message": "different"},
        headers=send_headers)
    s_conflict_body = s_conflict.json().get("detail") or s_conflict.json()
    ok(s_conflict.status_code == 409
       and s_conflict_body.get("error_code") == "idem_key_conflict",
       "ixp send conflict maps store idempotency conflict to 409")

    # --- ack via REST wrapper ---
    msg_id = s1.json()["id"]
    ack_headers = {"Idempotency-Key": "rest-ack-1"}
    ack_body = {"message_id": msg_id, "project": "switchboard", "response": "ok"}
    a1 = client.post("/ixp/v1/ack", json=ack_body, headers=ack_headers)
    a2 = client.post("/ixp/v1/ack", json=ack_body, headers=ack_headers)
    ok(a1.status_code == 200 and a2.status_code == 200, "ixp ack header replay returns 200")
    a_conflict = client.post(
        "/ixp/v1/ack",
        json={**ack_body, "response": "changed"},
        headers=ack_headers)
    ok(a_conflict.status_code == 409, "ixp ack conflict is HTTP 409")

    ok(idem_api.IDEMPOTENCY_HEADER == "Idempotency-Key",
       "canonical header name is Idempotency-Key")

finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
