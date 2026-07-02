#!/usr/bin/env python3
"""REST write-auth regression for the public web task surface."""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="web-write-auth-")
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
except ModuleNotFoundError as exc:
    print(f"  SKIP  FastAPI web write-auth smoke requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0)


P = "switchboard"
TOKEN = "web-write-token"
TITLE = "no-auth write must not land"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def title_exists(title):
    return any(t["title"] == title for t in store.list_tasks(project=P))


try:
    store.create_principal(
        kind="agent",
        display_name="codex/web-auth",
        token=TOKEN,
        scopes=["read", "write:tasks"],
        project=P,
    )
    client = TestClient(app)
    payload = {"workstream_id": "QA", "title": TITLE}

    missing = client.post(f"/api/tasks?project={P}", json=payload)
    ok(missing.status_code == 401, "task create rejects missing bearer token")
    ok(not title_exists(TITLE), "missing-token task create does not write a row")

    bad = client.post(
        f"/api/tasks?project={P}",
        json=payload,
        headers={"Authorization": "Bearer definitely-bad-token"},
    )
    ok(bad.status_code == 401, "task create rejects bad bearer token")
    ok(not title_exists(TITLE), "bad-token task create does not write a row")

    good = client.post(
        f"/api/tasks?project={P}",
        json=payload,
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    ok(good.status_code == 200 and good.json()["title"] == TITLE,
       "task create accepts valid bearer token")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
