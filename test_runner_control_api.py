#!/usr/bin/env python3
"""REST smoke for HARDEN-5 runner control auth and capability sanitization."""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="runner-control-api-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_AUTH_MODE"] = "required"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from fastapi.testclient import TestClient  # noqa: E402
    import store  # noqa: E402
    from app import app  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  FastAPI runner-control smoke requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0)


P = "switchboard"
TOKEN = "runner-api-token"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    store.create_principal(
        kind="agent",
        display_name="codex/runner-api",
        token=TOKEN,
        scopes=["read", "write:ixp"],
        project=P,
    )
    client = TestClient(app)
    unmanaged = {
        "project": P,
        "runner_session_id": "run_unmanaged_api",
        "agent_id": "codex/unmanaged",
        "runtime": "codex",
        "status": "running",
        "control": {"runner_kill": True},
    }
    unauth = client.post("/ixp/v1/register_runner_session", json=unmanaged)
    ok(unauth.status_code == 401, "runner session registration rejects missing token")

    headers = {"Authorization": f"Bearer {TOKEN}"}
    reg = client.post("/ixp/v1/register_runner_session", json=unmanaged, headers=headers)
    ok(reg.status_code == 200, "authorized runner session registration succeeds")
    body = reg.json()
    ok(body["control"]["runner_kill"] is False and "kill" not in body["available_actions"],
       "unmanaged REST registration cannot advertise runner_kill")

    managed = dict(unmanaged)
    managed.update({
        "runner_session_id": "run_managed_api",
        "host_id": "host/api",
        "control": {"tier": "T3", "managed_process": True, "runner_kill": True},
    })
    reg_managed = client.post("/ixp/v1/register_runner_session", json=managed, headers=headers)
    ok(reg_managed.status_code == 200 and "kill" in reg_managed.json()["available_actions"],
       "managed REST registration advertises runner kill")
    kill = client.post("/ixp/v1/request_runner_kill",
                       json={"project": P, "runner_session_id": "run_managed_api",
                             "reason": "test"},
                       headers=headers)
    ok(kill.status_code == 200 and kill.json()["requested"] is True,
       "authorized operator can request managed runner kill")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
