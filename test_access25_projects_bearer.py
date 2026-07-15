#!/usr/bin/env python3
"""ACCESS-25: GET /api/projects accepts Bearer the same way /api/board does."""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="access25-projects-bearer-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "required"
os.environ["PM_JWT_SECRET"] = "test-secret-do-not-use-in-prod"
os.environ["PM_MCP_TOKEN"] = "env-mcp-access25-token"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from fastapi.testclient import TestClient  # noqa: E402
    import store  # noqa: E402
    from app import app  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  ACCESS-25 requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0)

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    client = TestClient(app)

    missing = client.get("/api/projects")
    ok(missing.status_code == 401, "picker rejects anonymous callers")

    bad = client.get("/api/projects", headers={"Authorization": "Bearer not-real"})
    ok(bad.status_code == 401, "picker rejects unknown bearer")

    env_headers = {"Authorization": "Bearer env-mcp-access25-token"}
    env_list = client.get("/api/projects", headers=env_headers)
    env_ids = sorted(p["id"] for p in (env_list.json().get("projects") or []))
    ok(env_list.status_code == 200 and "switchboard" in env_ids and "maxwell" in env_ids,
       "env MCP bearer returns active project picker rows")
    ok(client.get("/api/board", params={"project": "switchboard"},
                  headers=env_headers).status_code == 200,
       "same env bearer still reads /api/board")

    scoped = "scoped-agent-access25-token"
    store.create_principal(kind="agent", display_name="access25-bot", token=scoped,
                           scopes=["read"], project="switchboard")
    scoped_headers = {"Authorization": f"Bearer {scoped}"}
    scoped_list = client.get("/api/projects", headers=scoped_headers)
    scoped_ids = [p["id"] for p in (scoped_list.json().get("projects") or [])]
    ok(scoped_list.status_code == 200 and scoped_ids == ["switchboard"],
       "project-scoped bearer only lists its binding")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
