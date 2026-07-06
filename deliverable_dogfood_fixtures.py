"""DELIVERABLES-8 dogfood mission fixtures for QA and operator proof.

Reusable seed helpers for cross-board deliverables. Intended for isolated temp DBs
and CI — not for mutating live production boards.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

QA_SCRATCH_HOME = "qa2scratch20260702a"
QA_SCRATCH_TARGET = "qa2target20260702a"
QA_SCRATCH_DELIVERABLE = "qa-scratch-cross-board"

HELM_HOME = "helmrenderer"
HELM_RENDERER_DELIVERABLE = "helm-cpp-webgpu-renderer"

ACCESS_HOME = "switchboard"
ACCESS_DELIVERABLE = "switchboard-access-rollout"

STALE_HOME = "qa2stale20260702a"
STALE_DELIVERABLE = "stale-blocked-signals"


def _ensure_project(store, project_id: str, title: str, actor: str = "test") -> Dict[str, Any]:
    if store.has_project(project_id):
        return {"project_id": project_id, "created": False}
    created = store.create_project(title, project_id=project_id, actor=actor)
    store.init_db(project_id)
    return created


def seed_qa_scratch_fixture(store, actor: str = "test") -> Dict[str, Any]:
    """QA scratch deliverable spanning qa2scratch20260702a and qa2target20260702a."""
    _ensure_project(store, QA_SCRATCH_HOME, "QA Scratch Home", actor=actor)
    _ensure_project(store, QA_SCRATCH_TARGET, "QA Scratch Target", actor=actor)

    board = store.create_project_board(
        {
            "id": QA_SCRATCH_DELIVERABLE,
            "title": "QA scratch cross-board smoke",
            "kind": "mission",
            "status": "active",
            "end_state": "Scratch and target boards roll up into one mission cockpit.",
        },
        actor=actor,
        project=QA_SCRATCH_HOME,
    )
    deliverable = store.create_deliverable(
        {
            "id": QA_SCRATCH_DELIVERABLE,
            "board_id": board["id"],
            "title": "QA scratch cross-board smoke",
            "status": "in_progress",
            "end_state": "Scratch and target boards roll up into one mission cockpit.",
            "why_it_matters": "Proves safe cross-project linking on disposable QA boards.",
            "confidence": 0.7,
        },
        actor=actor,
        project=QA_SCRATCH_HOME,
    )
    home_ms = store.add_deliverable_milestone(
        deliverable["id"],
        {"title": "Home slice", "status": "in_progress"},
        actor=actor,
        project=QA_SCRATCH_HOME,
    )
    target_ms = store.add_deliverable_milestone(
        deliverable["id"],
        {"title": "Target slice", "status": "not_started"},
        actor=actor,
        project=QA_SCRATCH_HOME,
    )
    home_task = store.create_task(
        {"workstream_id": "SCRATCH", "title": "Scratch-side link proof", "status": "In Progress"},
        actor=actor,
        project=QA_SCRATCH_HOME,
    )
    target_task = store.create_task(
        {"workstream_id": "TARGET", "title": "Target-side acceptance", "status": "Not Started"},
        actor=actor,
        project=QA_SCRATCH_TARGET,
    )
    store.link_task_to_deliverable(
        deliverable["id"],
        QA_SCRATCH_HOME,
        home_task["task_id"],
        milestone_id=home_ms["milestones"][0]["id"],
        data={"role": "implementation", "blocks_deliverable": False},
        actor=actor,
        project=QA_SCRATCH_HOME,
    )
    store.link_task_to_deliverable(
        deliverable["id"],
        QA_SCRATCH_TARGET,
        target_task["task_id"],
        milestone_id=target_ms["milestones"][1]["id"],
        data={"role": "acceptance", "blocks_deliverable": True},
        actor=actor,
        project=QA_SCRATCH_HOME,
    )
    store.update_mission_narrative(
        deliverable["id"],
        "QA scratch cross-board smoke is active across scratch and target projects.",
        actor=actor,
        project=QA_SCRATCH_HOME,
    )
    return {
        "fixture": "qa_scratch",
        "home_project": QA_SCRATCH_HOME,
        "target_project": QA_SCRATCH_TARGET,
        "deliverable_id": deliverable["id"],
        "linked_projects": [QA_SCRATCH_HOME, QA_SCRATCH_TARGET],
        "home_task_id": home_task["task_id"],
        "target_task_id": target_task["task_id"],
    }


def seed_helm_renderer_fixture(store, actor: str = "test") -> Dict[str, Any]:
    """Helm C++ + WebGPU Renderer spanning helmrenderer, helm, and vulkan."""
    for project_id, title in (
        (HELM_HOME, "Helm Renderer Mission Home"),
        ("helm", "Helm Runtime"),
        ("vulkan", "Vulkan Proof Slice"),
    ):
        _ensure_project(store, project_id, title, actor=actor)

    board = store.create_project_board(
        {
            "id": HELM_RENDERER_DELIVERABLE,
            "title": "Helm C++ + WebGPU Renderer",
            "kind": "mission",
            "status": "active",
            "end_state": (
                "Helm renders chart layers in the browser from shared C++ nautical semantics, "
                "with WebGPU visible to users and deterministic fixture parity."
            ),
        },
        actor=actor,
        project=HELM_HOME,
    )
    deliverable = store.create_deliverable(
        {
            "id": HELM_RENDERER_DELIVERABLE,
            "board_id": board["id"],
            "title": "Helm C++ + WebGPU Renderer",
            "status": "in_progress",
            "end_state": board["end_state"],
            "why_it_matters": "Humans steer one renderer outcome while agents execute board tasks.",
            "confidence": 0.45,
            "policy_constraints": {"runtime_language": "c++", "renderer": "webgpu"},
            "proof_requirements": {"merge_provenance": True, "fixture_parity": True},
        },
        actor=actor,
        project=HELM_HOME,
    )
    model_ms = store.add_deliverable_milestone(
        deliverable["id"],
        {"title": "Define shared render model", "status": "done"},
        actor=actor,
        project=HELM_HOME,
    )
    ingest_ms = store.add_deliverable_milestone(
        deliverable["id"],
        {"title": "Build WebGPU ingest", "status": "in_progress"},
        actor=actor,
        project=HELM_HOME,
    )
    runtime_ms = store.add_deliverable_milestone(
        deliverable["id"],
        {"title": "Integrate into Helm runtime", "status": "not_started"},
        actor=actor,
        project=HELM_HOME,
    )
    renderer_task = store.create_task(
        {
            "workstream_id": "RENDER",
            "title": "Integrated visible-renderer acceptance",
            "status": "In Progress",
        },
        actor=actor,
        project=HELM_HOME,
    )
    helm_task = store.create_task(
        {
            "workstream_id": "ENGINE",
            "title": "Boat/runtime C++ policy integration",
            "status": "Not Started",
        },
        actor=actor,
        project="helm",
    )
    vulkan_task = store.create_task(
        {
            "workstream_id": "PROOF",
            "title": "Backend-neutral renderer proof slice",
            "status": "Done",
        },
        actor=actor,
        project="vulkan",
    )
    store.mark_task_merged(vulkan_task["task_id"], "cafebabe" * 5, pr_number=7, project="vulkan")
    store.link_task_to_deliverable(
        deliverable["id"],
        HELM_HOME,
        renderer_task["task_id"],
        milestone_id=ingest_ms["milestones"][1]["id"],
        data={"role": "implementation", "blocks_deliverable": False},
        actor=actor,
        project=HELM_HOME,
    )
    store.link_task_to_deliverable(
        deliverable["id"],
        "helm",
        helm_task["task_id"],
        milestone_id=runtime_ms["milestones"][2]["id"],
        data={"role": "integration", "blocks_deliverable": True},
        actor=actor,
        project=HELM_HOME,
    )
    store.link_task_to_deliverable(
        deliverable["id"],
        "vulkan",
        vulkan_task["task_id"],
        milestone_id=model_ms["milestones"][0]["id"],
        data={"role": "proof", "blocks_deliverable": False},
        actor=actor,
        project=HELM_HOME,
    )
    store.update_mission_narrative(
        deliverable["id"],
        "Renderer mission spans helmrenderer, helm, and vulkan with one visible end state.",
        actor=actor,
        project=HELM_HOME,
    )
    return {
        "fixture": "helm_renderer",
        "home_project": HELM_HOME,
        "deliverable_id": deliverable["id"],
        "linked_projects": [HELM_HOME, "helm", "vulkan"],
        "done_task_id": vulkan_task["task_id"],
        "active_task_id": renderer_task["task_id"],
        "blocking_task_id": helm_task["task_id"],
    }


def seed_access_rollout_fixture(store, actor: str = "test") -> Dict[str, Any]:
    """Switchboard Access rollout spanning ACCESS, HARDEN, and QA workstreams."""
    _ensure_project(store, ACCESS_HOME, "Switchboard Dogfood", actor=actor)

    board = store.create_project_board(
        {
            "id": ACCESS_DELIVERABLE,
            "title": "Switchboard Access rollout",
            "kind": "mission",
            "status": "active",
            "end_state": (
                "Multiple humans can safely access Switchboard, invite collaborators, "
                "scope agents to projects, and provide feedback without unauthorized dispatch."
            ),
        },
        actor=actor,
        project=ACCESS_HOME,
    )
    deliverable = store.create_deliverable(
        {
            "id": ACCESS_DELIVERABLE,
            "board_id": board["id"],
            "title": "Switchboard Access rollout",
            "status": "in_progress",
            "end_state": board["end_state"],
            "why_it_matters": "Dogfood becomes a safe multi-human product.",
            "confidence": 0.55,
            "policy_constraints": {"auth_mode": "required"},
            "proof_requirements": {"merge_provenance": True},
        },
        actor=actor,
        project=ACCESS_HOME,
    )
    auth_ms = store.add_deliverable_milestone(
        deliverable["id"],
        {"title": "Auth and session protection", "status": "in_progress"},
        actor=actor,
        project=ACCESS_HOME,
    )
    token_ms = store.add_deliverable_milestone(
        deliverable["id"],
        {"title": "Scoped MCP/API tokens", "status": "not_started"},
        actor=actor,
        project=ACCESS_HOME,
    )
    qa_ms = store.add_deliverable_milestone(
        deliverable["id"],
        {"title": "Login smoke and feedback", "status": "not_started"},
        actor=actor,
        project=ACCESS_HOME,
    )
    access_task = store.create_task(
        {"workstream_id": "ACCESS", "title": "Session auth shell", "status": "In Progress"},
        actor=actor,
        project=ACCESS_HOME,
    )
    harden_task = store.create_task(
        {"workstream_id": "HARDEN", "title": "Scoped token enforcement", "status": "Not Started"},
        actor=actor,
        project=ACCESS_HOME,
    )
    qa_task = store.create_task(
        {"workstream_id": "QA", "title": "Multi-human login smoke", "status": "Not Started"},
        actor=actor,
        project=ACCESS_HOME,
    )
    store.link_task_to_deliverable(
        deliverable["id"],
        ACCESS_HOME,
        access_task["task_id"],
        milestone_id=auth_ms["milestones"][0]["id"],
        data={"role": "implementation", "blocks_deliverable": False},
        actor=actor,
        project=ACCESS_HOME,
    )
    store.link_task_to_deliverable(
        deliverable["id"],
        ACCESS_HOME,
        harden_task["task_id"],
        milestone_id=token_ms["milestones"][1]["id"],
        data={"role": "hardening", "blocks_deliverable": True},
        actor=actor,
        project=ACCESS_HOME,
    )
    store.link_task_to_deliverable(
        deliverable["id"],
        ACCESS_HOME,
        qa_task["task_id"],
        milestone_id=qa_ms["milestones"][2]["id"],
        data={"role": "verification", "blocks_deliverable": False},
        actor=actor,
        project=ACCESS_HOME,
    )
    store.update_mission_narrative(
        deliverable["id"],
        "Access rollout is active across ACCESS, HARDEN, and QA workstreams.",
        actor=actor,
        project=ACCESS_HOME,
    )
    return {
        "fixture": "access_rollout",
        "home_project": ACCESS_HOME,
        "deliverable_id": deliverable["id"],
        "linked_projects": [ACCESS_HOME],
        "workstreams": ["ACCESS", "HARDEN", "QA"],
        "access_task_id": access_task["task_id"],
        "harden_task_id": harden_task["task_id"],
        "qa_task_id": qa_task["task_id"],
    }


def seed_stale_blocked_fixture(store, actor: str = "test") -> Dict[str, Any]:
    """Deliberately stale/blocked mission to prove red/yellow narrative signals."""
    _ensure_project(store, STALE_HOME, "QA Stale/Blocked Mission", actor=actor)

    board = store.create_project_board(
        {
            "id": STALE_DELIVERABLE,
            "title": "Stale/blocked signal probe",
            "kind": "mission",
            "status": "blocked",
            "end_state": "Operators see truthful blockers and stale narrative flags.",
        },
        actor=actor,
        project=STALE_HOME,
    )
    deliverable = store.create_deliverable(
        {
            "id": STALE_DELIVERABLE,
            "board_id": board["id"],
            "title": "Stale/blocked signal probe",
            "status": "blocked",
            "end_state": board["end_state"],
            "why_it_matters": "Mission pages must not hide blocked work behind optimistic text.",
            "confidence": 0.2,
        },
        actor=actor,
        project=STALE_HOME,
    )
    milestone = store.add_deliverable_milestone(
        deliverable["id"],
        {"title": "Unblock dependency slice", "status": "blocked"},
        actor=actor,
        project=STALE_HOME,
    )
    gate_task = store.create_task(
        {
            "workstream_id": "BLOCK",
            "title": "Dependency gate waiting on upstream",
            "status": "Not Started",
        },
        actor=actor,
        project=STALE_HOME,
    )
    store.link_task_to_deliverable(
        deliverable["id"],
        STALE_HOME,
        gate_task["task_id"],
        milestone_id=milestone["milestones"][0]["id"],
        data={"role": "gate", "blocks_deliverable": True},
        actor=actor,
        project=STALE_HOME,
    )
    brief = store.generate_mission_brief(
        project=STALE_HOME,
        deliverable_id=deliverable["id"],
        actor=actor,
    )
    store.create_deliverable(
        {
            "id": STALE_DELIVERABLE,
            "status": "blocked",
        },
        actor=actor,
        project=STALE_HOME,
    )
    store.update_task(
        gate_task["task_id"],
        {"status": "Blocked", "description": "Upstream dependency still open."},
        actor=actor,
        project=STALE_HOME,
    )
    store.update_mission_narrative(
        deliverable["id"],
        "We are on track and shipping soon.",
        actor="human",
        project=STALE_HOME,
    )
    return {
        "fixture": "stale_blocked",
        "home_project": STALE_HOME,
        "deliverable_id": deliverable["id"],
        "linked_projects": [STALE_HOME],
        "blocked_task_id": gate_task["task_id"],
        "generated_brief": (brief.get("mission_brief") or {}),
    }


def seed_all_dogfood_fixtures(store, actor: str = "test") -> Dict[str, Any]:
    """Seed all four DELIVERABLES-8 dogfood fixtures."""
    return {
        "qa_scratch": seed_qa_scratch_fixture(store, actor=actor),
        "helm_renderer": seed_helm_renderer_fixture(store, actor=actor),
        "access_rollout": seed_access_rollout_fixture(store, actor=actor),
        "stale_blocked": seed_stale_blocked_fixture(store, actor=actor),
    }


def linked_projects_for_fixture(meta: Dict[str, Any]) -> List[str]:
    return list(meta.get("linked_projects") or [])


def assert_no_cross_project_deliverable_leak(store, home_project: str,
                                             linked_projects: List[str]) -> List[str]:
    """Return error strings if deliverable records leaked into linked project DBs."""
    errors: List[str] = []
    for project_id in linked_projects:
        if project_id == home_project:
            continue
        listed = store.list_deliverables(project=project_id)
        if listed:
            errors.append(f"{project_id} unexpectedly owns deliverables: "
                            f"{[d.get('id') for d in listed]}")
        boards = store.list_project_boards(project=project_id)
        if boards:
            errors.append(f"{project_id} unexpectedly owns boards: "
                            f"{[b.get('id') for b in boards]}")
    return errors
