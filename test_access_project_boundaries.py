#!/usr/bin/env python3
"""ACCESS-4 project creation permission and boundary regression."""
import json
import os
import shutil
import sys
import tempfile
import types

_TMP = tempfile.mkdtemp(prefix="access-project-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "required"
os.environ["PM_JWT_SECRET"] = "test-secret-do-not-use-in-prod"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from fastapi.testclient import TestClient  # noqa: E402
    import auth  # noqa: E402
    import store  # noqa: E402
    from app import app  # noqa: E402
    from services.auth import store as auth_store  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  ACCESS project-boundary smoke requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0)


def _stub_mcp_imports():
    class _FastMCP:
        def __init__(self, *a, **k): pass
        def tool(self, *a, **k):
            return lambda f: f
        def __getattr__(self, n): return lambda *a, **k: None

    def _mk(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m

    _mk("mcp"); _mk("mcp.server")
    _mk("mcp.server.fastmcp", Context=object, FastMCP=_FastMCP)
    _mk("mcp.server.transport_security",
        TransportSecuritySettings=type("TSS", (), {"__init__": lambda self, *a, **k: None}))


_stub_mcp_imports()
import mcp_server  # noqa: E402


P = "switchboard"
NEW = "boundarylab"
PASSWORD = "boundary-admin-2026"
ADMIN_EMAIL = "admin@test.com"
PURPOSE = "Boundary lab validates customer project isolation."
BOUNDARY = "Only ACCESS-4 boundary-lab work belongs here; no Helm/Vulkan task leakage."
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def authz(token):
    return {"Authorization": f"Bearer {token}"}


try:
    client = TestClient(app)
    auth_store.init()
    admin = auth_store.create_user(
        ADMIN_EMAIL, "Admin", auth.password_hash(PASSWORD), is_superadmin=True)
    store.ensure_bootstrap_project_owner(P, admin["id"], "admin", "Admin", actor="test")
    ok(client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": PASSWORD}).status_code == 200,
       "global admin login succeeds")
    admin_id = admin["id"]

    contributor = client.post(
        "/api/access/tokens",
        params={"project": P},
        json={"kind": "agent", "display_name": "limited creator", "role": "contributor"},
    )
    contributor_token = contributor.json().get("token")
    viewer = client.post(
        "/api/access/tokens",
        params={"project": P},
        json={"kind": "agent", "display_name": "read-only viewer", "role": "viewer"},
    )
    viewer_token = viewer.json().get("token")
    denied = client.post(
        "/api/projects",
        json={"project_id": "deniedproj", "name": "Denied Project"},
        headers=authz(viewer_token),
    )
    ok(denied.status_code == 403, "viewer principal cannot create projects")

    created = client.post(
        "/api/projects",
        json={
            "project_id": NEW,
            "name": "Boundary Lab",
            "label": "Boundary Lab",
            "pretitle": "ACCESS-4 isolation proof",
            "purpose": PURPOSE,
            "boundary": BOUNDARY,
            "github_repo": "6th-Element-Labs/projectplanner",
        },
    )
    body = created.json()
    project = (body.get("project") or {})
    ok(created.status_code == 200 and body.get("created") is True,
       "admin can create a physically isolated project")
    ok(project.get("access", {}).get("purpose") == PURPOSE and
       project.get("access", {}).get("boundary") == BOUNDARY,
       "project creation records purpose and boundary metadata")
    ok(project.get("owner_grant", {}).get("subject_id") == admin_id and
       project.get("owner_grant", {}).get("role") == "admin",
       "project creator receives explicit admin grant on new project")
    initial_topology = client.get(f"/api/projects/{NEW}/repo_topology")
    ok(initial_topology.status_code == 200 and
       initial_topology.json()["scope"] == "project" and
       initial_topology.json()["project_hierarchy"]["compatibility"]
       ["repo_topology_is_board_level_truth"] is False and
       initial_topology.json()["roles"]["canonical"]["repo"] == "6th-Element-Labs/projectplanner",
       "REST repo_topology exposes project-scoped canonical repo")
    topology_update = client.post(
        f"/api/projects/{NEW}/repo_topology",
        json={
            "public_ci_repo": "6th-Element-Labs/public-CI",
            "public_ci_required_status_contexts": "public-ci/full-suite",
        },
    )
    ok(topology_update.status_code == 200 and
       topology_update.json()["repo_topology"]["roles"]["public_ci"]["repo"] ==
       "6th-Element-Labs/public-CI",
       "REST repo_topology configures shared public-CI")
    bad_topology = client.post(
        f"/api/projects/{NEW}/repo_topology",
        json={"public_ci_repo": "bad repo name"},
    )
    ok(bad_topology.status_code == 400,
       "REST repo_topology rejects invalid repo names")

    duplicate = client.post(
        "/api/projects",
        json={"project_id": NEW, "name": "Boundary Lab"},
    )
    ok(duplicate.status_code == 200 and duplicate.json()["project"]["access"]["boundary"] == BOUNDARY,
       "idempotent project create preserves existing boundary metadata")

    board = client.get("/api/board", params={"project": NEW})
    ok(board.status_code == 200 and board.json()["project"]["id"] == NEW,
       "creator session can read the new project through explicit role grant")
    task = client.post(
        f"/api/tasks?project={NEW}",
        json={"workstream_id": "BOUND", "title": "boundary-local task"},
    )
    ok(task.status_code == 200 and task.json()["task_id"] == "BOUND-1",
       "creator session can write only because of the new-project grant")

    model = client.get("/api/access/model", params={"project": NEW})
    model_body = model.json()
    ok(model.status_code == 200 and model_body["access"]["boundary"] == BOUNDARY,
       "access model exposes project boundary")
    ok(any(g["subject_id"] == admin_id and g["role"] == "admin"
           for g in model_body["grants"]),
       "access model exposes creator admin grant")

    projects = client.get("/api/projects").json()["projects"]
    ok(any(p["id"] == NEW and p["boundary"] == BOUNDARY for p in projects),
       "project discovery includes boundary metadata")

    contract = json.loads(mcp_server.get_project_contract(project=NEW))
    ok(contract["project_access"]["boundary"] == BOUNDARY,
       "MCP project contract exposes boundary metadata")
    ok(any(BOUNDARY in rule for rule in contract["operating_rules"]),
       "MCP project contract includes boundary as an operating rule")

    boot = json.loads(mcp_server.prepare_agent_session(
        project=NEW, lane="BOUND", task_id="BOUND-1",
        agent_id="codex/BOUND-1-test", runtime="codex"))
    ok(BOUNDARY in boot["startup_prompt"],
       "agent startup prompt includes project boundary")

    switch_task = store.create_task(
        {"workstream_id": "BOUND", "title": "switchboard task"},
        actor="test",
        project=P,
    )
    claimed = store.claim_next(
        "codex/boundary-agent", lanes="BOUND", principal_id=admin_id,
        actor="test", project=NEW)
    ok(claimed.get("claimed") is True and claimed["task"]["task_id"] == "BOUND-1",
       "claim_next remains scoped to the selected project")
    ok(store.get_task(switch_task["task_id"], project=P)["status"] == "Not Started",
       "claim_next did not claim the same-lane task from another project")

    unknown = client.get("/api/board", params={"project": "missingproject"})
    ok(unknown.status_code == 400, "unknown project IDs fail closed")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
