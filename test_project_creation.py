#!/usr/bin/env python3
"""Self-contained test for dynamic project creation.

Run:
    python3 test_project_creation.py
"""
import json
import os
import shutil
import sys
import tempfile
import types

_TMP = tempfile.mkdtemp(prefix="project-create-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ.pop("PM_MCP_TOKEN", None)
os.environ.pop("PM_PUBLIC_CI_REPO", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _stub_heavy_imports():
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
    _mk("agent", _task_brief=lambda t, full=False: t, run=lambda *a, **k: {},
        _search_tasks=lambda args, project="maxwell": [],
        board_summary_text=lambda project="maxwell": "")
    for n in ("digest", "intake", "notify", "rag", "signals"):
        _mk(n)


_stub_heavy_imports()
import store       # noqa: E402
import mcp_server  # noqa: E402
import jobs        # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    store.init_project_registry()
    store.init_db("switchboard")

    created = store.create_project("Vulkan", actor="test")
    ok(created.get("created") is True, "store.create_project creates a dynamic project")
    ok(created["project"]["id"] == "vulkan", "project id is slugified to vulkan")
    ok("vulkan" in store.project_ids(), "dynamic project appears in project_ids")
    ok(any(p["id"] == "vulkan" and p["label"] == "Vulkan" for p in store.projects()),
       "dynamic project appears in project switcher payload")
    ok(store.get_meta("project", project="vulkan") == "Vulkan",
       "dynamic project metadata is initialized")
    empty_topology = store.get_project_repo_topology("vulkan")
    ok(empty_topology["valid"] is False and
       empty_topology["schema"] == "switchboard.project_repo_topology.v1" and
       empty_topology["scope"] == "project" and
       empty_topology["code_repo_gate"]["status"] == "blocked" and
       "roles.canonical.repo" in empty_topology["missing"],
       "dynamic project without canonical repo fails the code repo gate")
    ok(empty_topology["project_hierarchy"]["compatibility"]["current_switchboard_project_id"] ==
       "vulkan" and
       empty_topology["project_hierarchy"]["compatibility"]["repo_topology_is_board_level_truth"] is False,
       "repo topology declares project scope with workspace compatibility metadata")

    helm_topology = store.get_project_repo_topology("helm")
    ok(helm_topology["roles"]["canonical"]["repo"] == "StevenRidder/Helm" and
       helm_topology["roles"]["public_ci"]["repo"] == "StevenRidder/helm-ci" and
       helm_topology["roles"]["public_ci"]["shared"] is True,
       "Helm topology exposes canonical and shared public-CI roles")
    ok(helm_topology["roles"]["public"]["publish_scripts"] == ["scripts/publish-public-mirror.sh"] and
       helm_topology["authority"]["done"] == "canonical",
       "Helm topology keeps public mirror as publish evidence only")
    switchboard_topology = store.get_project_repo_topology("switchboard")
    ok(switchboard_topology["roles"]["canonical"]["repo"] == "6th-Element-Labs/projectplanner" and
       switchboard_topology["roles"]["public_ci"]["shared"] is True,
       "Switchboard topology has its own canonical repo and shared public-CI slot")

    repo_created = store.create_project(
        "Chart Renderer", actor="test", github_repo="OpenCPN/OpenCPN")
    ok(repo_created.get("created") is True and
       repo_created["project"]["github_repo"] == "OpenCPN/OpenCPN",
       "store.create_project can set github_repo in one step")
    ok(store.get_project_github_repo("chart-renderer") == "OpenCPN/OpenCPN",
       "dynamic project resolves its configured github_repo")
    chart_topology = store.get_project_repo_topology("chart-renderer")
    ok(chart_topology["roles"]["canonical"]["repo"] == "OpenCPN/OpenCPN" and
       chart_topology["code_repo_gate"]["passed"] is True,
       "github_repo backfills the canonical repo topology role")
    bad_repo = store.create_project(
        "Bad Repo", actor="test", github_repo="not-a-valid-repo")
    ok("error" in bad_repo and "owner/name" in bad_repo["error"] and
       "bad-repo" not in store.project_ids(),
       "invalid github_repo fails closed without creating a project")

    duplicate = store.create_project("Vulkan", actor="test")
    ok(duplicate.get("created") is False and duplicate["project"]["id"] == "vulkan",
       "duplicate create is idempotent")
    duplicate_with_repo = store.create_project(
        "Vulkan", actor="test", github_repo="StevenRidder/vulkan-renderer")
    ok(duplicate_with_repo.get("created") is False and
       duplicate_with_repo["project"]["github_repo"] == "StevenRidder/vulkan-renderer",
       "duplicate create can attach github_repo to an existing dynamic project")
    configured_topology = store.set_project_repo_topology(
        project="vulkan",
        canonical_repo="StevenRidder/vulkan-renderer",
        public_ci_repo="StevenRidder/public-CI",
        public_ci_required_status_contexts="public-ci/full-suite",
        public_ci_sync_scripts="scripts/public-ci.sh",
        topology_type="private_canonical_public_mirror_public_ci")
    ok(configured_topology["repo_topology"]["roles"]["public_ci"]["repo"] == "StevenRidder/public-CI" and
       configured_topology["repo_topology"]["roles"]["public_ci"]["required_status_contexts"] ==
       ["public-ci/full-suite"],
       "project repo topology can point at a shared public-CI repo")
    bad_topology = store.set_project_repo_topology(
        project="vulkan", public_ci_repo="not-a-valid-repo")
    ok("error" in bad_topology and bad_topology["role"] == "public_ci",
       "invalid public-CI repo fails closed")

    task = store.create_task({"workstream_id": "VKPLAN", "title": "root seam"}, project="vulkan")
    ok(task["task_id"] == "VKPLAN-1", "normal task creation works on dynamic project")
    ok(not any(t["task_id"] == "VKPLAN-1" for t in store.list_tasks(project="switchboard")),
       "dynamic tasks do not leak into switchboard")
    payload = store.board_payload(project="vulkan")
    ok(payload["rollups"]["total_tasks"] == 1 and payload["rollups"]["total_workstreams"] == 1,
       "dynamic project board rollups are computed from live tasks")
    ok(payload["rollups"]["status_counts"].get("Not Started") == 1 and
       payload["rollups"]["workstream_counts"].get("VKPLAN") == 1,
       "dynamic project rollups expose status and workstream counts")

    listed = json.loads(mcp_server.list_projects())
    ok(any(p["id"] == "vulkan" for p in listed["projects"]),
       "MCP list_projects includes dynamic project")
    mcp_created = json.loads(mcp_server.create_project(
        "Vulkan Renderer", None, project_id="vkrender",
        github_repo="StevenRidder/OpenCPN"))
    ok(mcp_created.get("created") is True and mcp_created["project"]["id"] == "vkrender",
       "MCP create_project creates a second dynamic project")
    ok(store.get_project_github_repo("vkrender") == "StevenRidder/OpenCPN",
       "MCP create_project wires github_repo in one step")
    mcp_topology = json.loads(mcp_server.set_project_repo_topology(
        None, project="vkrender",
        public_ci_repo="StevenRidder/public-CI",
        public_ci_required_status_contexts="public-ci/full-suite"))
    ok(mcp_topology["repo_topology"]["roles"]["public_ci"]["repo"] == "StevenRidder/public-CI",
       "MCP set_project_repo_topology configures the shared public-CI role")
    contract = json.loads(mcp_server.get_project_contract(project="vkrender"))
    ok(contract["source_of_truth"] == "switchboard_project_contract" and
       contract["project_hierarchy"]["scope"] == "project" and
       contract["repo_topology"]["roles"]["canonical"]["repo"] == "StevenRidder/OpenCPN" and
       contract["repo_topology"]["roles"]["public_ci"]["repo"] == "StevenRidder/public-CI",
       "project_contract exposes project-scoped repo topology roles")
    agreement = json.loads(mcp_server.get_working_agreement(project="vkrender"))
    ok(agreement["project_hierarchy"]["scope"] == "project" and
       agreement["repo_topology"]["roles"]["public_ci"]["repo"] == "StevenRidder/public-CI" and
       agreement["code_repo_gate"]["passed"] is True,
       "working agreement exposes project hierarchy, repo topology, and code repo gate")
    mcp_task = json.loads(mcp_server.create_task("VKPLAN", "MCP-root", None, project="vkrender"))
    ok(mcp_task["task_id"] == "VKPLAN-1",
       "MCP create_task can write to a freshly created dynamic project")
    mcp_summary = mcp_server.board_summary(project="vkrender")
    ok('"total_tasks": 1' in mcp_summary and '"VKPLAN": 1' in mcp_summary,
       "MCP board_summary reports live rollups for dynamic projects")

    original_reconcile_alerts = jobs.store.run_reconcile_alerts
    original_recon_projects = os.environ.pop("PM_RECON_ALERT_PROJECTS", None)
    seen_projects = []

    def fake_reconcile_alerts(project="maxwell", alert_to="switchboard/operator",
                              min_severity="medium", dedupe_window_s=3600,
                              incremental=False, **kwargs):
        seen_projects.append(project)
        return {"project": project, "ok": True, "alert_sent": False,
                "deduped": False, "finding_count": 1,
                "findings": [{"large_detail": "x" * 100_000}]}

    jobs.store.run_reconcile_alerts = fake_reconcile_alerts
    try:
        scheduled = jobs.reconcile_alerts()
    finally:
        jobs.store.run_reconcile_alerts = original_reconcile_alerts
        if original_recon_projects is not None:
            os.environ["PM_RECON_ALERT_PROJECTS"] = original_recon_projects
    ok({"vulkan", "vkrender", "helm", "switchboard"}.issubset(set(seen_projects)),
       "scheduled reconcile defaults across dynamic and built-in projects")
    ok(all("findings" not in r and r.get("finding_count") == 1
           for r in scheduled.get("results") or []),
       "scheduled reconcile retains bounded summaries, not full finding payloads")

    # UI-15: webhook-delivery evidence powers the "Verify connection" button.
    deliveries0 = store.github_webhook_deliveries("chart-renderer")
    ok(deliveries0["delivered"] is False and deliveries0["delivery_count"] == 0,
       "github_webhook_deliveries reports no delivery before any webhook lands")
    store.append_activity("pr.merged", "github-webhook", {"pr": 1},
                          task_id=None, project="chart-renderer")
    deliveries1 = store.github_webhook_deliveries("chart-renderer")
    ok(deliveries1["delivered"] is True and deliveries1["delivery_count"] == 1 and
       deliveries1["last_delivery_event"] == "pr.merged" and
       deliveries1["last_delivery_at"] is not None,
       "github_webhook_deliveries flips to delivered on the first github-webhook activity")
    ok(store.github_webhook_deliveries("vulkan")["delivered"] is False,
       "webhook-delivery evidence is board-scoped and never leaks across projects")
    ok(store.github_repo_reachable("") is None and store.github_repo_reachable("noslash") is None,
       "repo reachability probe returns None for malformed input without a network call")
    set_repo = store.set_project_github_repo("StevenRidder/vulkan-renderer", project="vulkan")
    ok(set_repo.get("github_repo") == "StevenRidder/vulkan-renderer" and
       store.get_project_github_repo("vulkan") == "StevenRidder/vulkan-renderer",
       "set_project_github_repo records the repo for an existing project (Settings path)")

    reserved = store.create_project("Helm", project_id="helm", actor="test")
    ok("error" in reserved and "reserved" in reserved["error"],
       "built-in project ids are reserved")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
