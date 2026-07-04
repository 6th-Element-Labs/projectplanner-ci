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
os.environ["PM_AUTH_TOKEN"] = "web-env-token"
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
ENV_TOKEN = os.environ["PM_AUTH_TOKEN"]
TITLE = "no-auth write must not land"
ENV_TITLE = "env-token write must bind identity"
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

    unbound_env = client.post(
        f"/api/tasks?project={P}",
        json={"workstream_id": "QA", "title": ENV_TITLE},
        headers={"Authorization": f"Bearer {ENV_TOKEN}"},
    )
    ok(unbound_env.status_code == 409 and
       unbound_env.json()["detail"]["error"] == "shared_token_requires_bound_actor",
       "task create rejects unbound shared env token")
    ok(not title_exists(ENV_TITLE), "unbound shared-token task create does not write a row")

    bound_env = client.post(
        f"/api/tasks?project={P}",
        json={
            "workstream_id": "QA",
            "title": ENV_TITLE,
            "system_actor": "switchboard/web-fixture",
            "system_reason": "exercise HARDEN-27 REST binding",
        },
        headers={"Authorization": f"Bearer {ENV_TOKEN}"},
    )
    ok(bound_env.status_code == 200 and bound_env.json()["title"] == ENV_TITLE,
       "task create accepts shared env token with explicit system actor and reason")
    created = store.get_task(bound_env.json()["task_id"], project=P)
    ok(created["activity"][0]["actor"] == "switchboard/web-fixture",
       "bound shared-token task create is authored as the explicit system actor")
    ok(any(a["kind"] == "principal.write_bound" and
           a["payload"].get("binding") == "explicit_system_actor"
           for a in created["activity"]),
       "bound shared-token task create records binding evidence")

    unbound_comment = client.post(
        f"/api/tasks/{created['task_id']}/comment?project={P}",
        json={"text": "this must not land"},
        headers={"Authorization": f"Bearer {ENV_TOKEN}"},
    )
    ok(unbound_comment.status_code == 409,
       "task comment rejects unbound shared env token")
    unchanged = store.get_task(created["task_id"], project=P)
    ok(not any(a["kind"] == "comment" and
               a["payload"].get("text") == "this must not land"
               for a in unchanged["activity"]),
       "unbound shared-token comment does not write a row")

    bound_comment = client.post(
        f"/api/tasks/{created['task_id']}/comment?project={P}",
        json={
            "text": "bound comment",
            "system_actor": "switchboard/web-fixture",
            "system_reason": "exercise HARDEN-27 comment binding",
        },
        headers={"Authorization": f"Bearer {ENV_TOKEN}"},
    )
    ok(bound_comment.status_code == 200,
       "task comment accepts shared env token with explicit system actor and reason")
    commented = store.get_task(created["task_id"], project=P)
    ok(any(a["kind"] == "comment" and a["actor"] == "switchboard/web-fixture" and
           a["payload"].get("text") == "bound comment"
           for a in commented["activity"]),
       "bound shared-token comment is authored as the explicit system actor")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
