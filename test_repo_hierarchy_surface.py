#!/usr/bin/env python3
"""REPO-5 proof: project hierarchy and repo roles are visible to agents and operators."""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="repo-hierarchy-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: E402
    from app import app  # noqa: E402
    import agent  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  repo hierarchy proof requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0)

P = "repo5home"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    store.create_project("Repo5 Home", project_id=P, github_repo="6th-Element-Labs/projectplanner",
                         actor="seed")
    store.init_db(P)
    mission = store.create_project_board({
        "title": "Publication cockpit",
        "kind": "mission",
        "purpose": "Track repo-role surfacing as one visible outcome.",
    }, actor="seed", project=P)
    deliverable = store.create_deliverable({
        "title": "Hierarchy visible everywhere",
        "board_id": mission["id"],
        "proof_requirements": {"publication_evidence": True},
    }, actor="seed", project=P)
    task = store.create_task({
        "workstream_id": "REPO",
        "title": "surface hierarchy fixture",
    }, actor="seed", project=P)
    store.link_task_to_deliverable(
        deliverable["id"], P, task["task_id"],
        data={"board_id": mission["id"]},
        actor="seed",
        project=P,
    )

    ctx = store.get_project_context(P)
    ok(ctx.get("project_hierarchy", {}).get("scope") == "project",
       "project context exposes project_hierarchy")
    ok(len(ctx.get("boards_missions") or []) == 1,
       "project context lists boards/missions")
    ok((ctx.get("repo_role_guide") or {}).get("done_authority", {}).get("repo")
       == "6th-Element-Labs/projectplanner",
       "project context names canonical Done repo")

    helm_guide = store.repo_topology_role_guide("helm")
    ok(helm_guide["done_authority"]["repo"] == "StevenRidder/Helm",
       "helm guide names canonical Done repo")
    ok("helm-ci" in (helm_guide["ci_verification"]["repo"] or ""),
       "helm guide names helm-ci as CI verification repo")
    ok("CI-only" in helm_guide["ci_verification"]["message"],
       "helm guide documents helm-ci is CI-only")

    agreement = store.get_working_agreement(P)
    ok(agreement.get("project_hierarchy") is not None and agreement.get("repo_topology"),
       "working agreement includes hierarchy and repo_topology")
    ok((agreement.get("repo_topology") or {}).get("roles", {}).get("canonical", {}).get("repo")
       == "6th-Element-Labs/projectplanner",
       "working agreement repo_topology names canonical repo")
    ok(agreement.get("repo_role_guide", {}).get("done_authority", {}).get("repo")
       == "6th-Element-Labs/projectplanner",
       "working agreement includes repo_role_guide")

    P2 = "repo5peer"
    store.create_project("Repo5 Peer", project_id=P2,
                         github_repo="6th-Element-Labs/projectplanner", actor="seed")
    store.init_db(P2)
    store.init_db("switchboard")
    sw_mission = store.create_project_board({
        "title": "Switchboard mission cockpit",
        "kind": "mission",
        "purpose": "Built-in project mission rollup fixture.",
    }, actor="seed", project="switchboard")
    sw_deliverable = store.create_deliverable({
        "title": "Built-in mission rollup",
        "board_id": sw_mission["id"],
        "proof_requirements": {"publication_evidence": True},
    }, actor="seed", project="switchboard")
    peer_task = store.create_task({
        "workstream_id": "REPO",
        "title": "cross-project hierarchy fixture",
    }, actor="seed", project=P2)
    store.link_task_to_deliverable(
        sw_deliverable["id"], P2, peer_task["task_id"],
        data={"board_id": sw_mission["id"]},
        actor="seed",
        project="switchboard",
    )
    peer_detail = store.get_task(peer_task["task_id"], project=P2)
    peer_links = (peer_detail.get("project_context") or {}).get("deliverable_links") or []
    ok(len(peer_links) == 1 and peer_links[0].get("deliverable_home_project") == "switchboard",
       "cross-project deliverable link is visible on the linked task")

    store.register_agent("codex/repo5-claim", "codex", task_id=peer_task["task_id"],
                         project=P2, ttl_s=600)
    claim = store.claim_task(peer_task["task_id"], "codex/repo5-claim",
                             idem_key="repo5-mission-claim", project=P2)
    completed = store.complete_claim(
        claim["claim_id"],
        evidence='{"branch":"codex/REPO-5-peer","head_sha":"d" * 40}',
        project=P2,
    )
    ok(completed.get("mission", {}).get("deliverable_id") == sw_deliverable["id"],
       "complete_claim auto-resolves cross-project mission rollup from deliverable home project")
    ok(completed.get("mission", {}).get("mission_project") == "switchboard",
       "complete_claim mission rollup scans built-in deliverable home projects")
    sw_after = store.get_deliverable(sw_deliverable["id"], project="switchboard") or {}
    ok((sw_after.get("proof_requirements") or {}).get("publication_evidence") is True,
       "mission status promotion preserves deliverable proof_requirements on upsert")

    detail = store.get_task(task["task_id"], project=P)
    pc = detail.get("project_context") or {}
    ok(pc.get("repo_role_guide") is not None and pc.get("project_hierarchy"),
       "task detail includes project_context with hierarchy and repo guide")
    ok(len(pc.get("deliverable_links") or []) == 1,
       "task detail includes deliverable links")
    ok(any(c.get("level") == "mission" for c in pc.get("hierarchy_breadcrumb") or []),
       "task hierarchy breadcrumb includes mission layer")

    brief = agent._task_brief(detail, full=True)
    ok(brief.get("project_context", {}).get("repo_role_guide") is not None,
       "MCP task brief carries project_context")

    client = TestClient(app)
    board = client.get(f"/api/board?project={P}").json()
    # HARDEN-35: the board payload no longer bundles the ~9KB project_context
    # blob — the UI fetches it from the dedicated /context endpoint below.
    ok("project_context" not in board,
       "REST board payload no longer bundles project_context (HARDEN-35)")
    rest_task = client.get(f"/api/tasks/{task['task_id']}?project={P}").json()
    ok(rest_task.get("project_context", {}).get("hierarchy_breadcrumb"),
       "REST task detail still includes project_context")
    rest_ctx = client.get(f"/api/projects/{P}/context").json()
    ok(rest_ctx.get("repo_topology", {}).get("valid") is True,
       "REST project context exposes repo_topology")
    ok(rest_ctx.get("repo_role_guide") is not None,
       "REST project context carries the repo_role_guide the board used to bundle")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
