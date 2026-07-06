#!/usr/bin/env python3
"""One-shot plan.taikunai.com provisioning: global admin + HELM umbrella deliverables."""
from __future__ import annotations

import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth
import store

TOP_LEVEL = ("switchboard", "maxwell", "helm")
HELM_CHILDREN = (
    "helmrenderer",
    "helmrender",
    "vulkan",
    "install",
    "s101-rendering",
)
ADMIN_SCOPES = ["admin", "read", "write:bug_intake", "write:ixp", "write:system", "write:tasks"]
ROOT_LOGIN = (os.environ.get("PM_ROOT_LOGIN") or "root").strip().lower()
ROOT_PASSWORD = os.environ.get("PM_ROOT_PASSWORD") or ""
ROOT_PRINCIPAL_ID = "user-" + hashlib.sha256(b"global:root").hexdigest()[:16]


def _ensure_root_principal(home_project: str = "switchboard") -> str:
    if not ROOT_PASSWORD or len(ROOT_PASSWORD) < 10:
        raise SystemExit("Set PM_ROOT_PASSWORD (min 10 chars) before running.")
    ph = auth.password_hash(ROOT_PASSWORD)
    if store.get_password_login(ROOT_LOGIN, project=home_project):
        store.set_principal_password(ROOT_PRINCIPAL_ID, ROOT_LOGIN, ph, project=home_project)
        print(f"  updated password for {ROOT_LOGIN} on {home_project}")
        return ROOT_PRINCIPAL_ID
    if store.get_principal_by_id(ROOT_PRINCIPAL_ID, project=home_project):
        store.set_principal_password(ROOT_PRINCIPAL_ID, ROOT_LOGIN, ph, project=home_project)
        print(f"  attached password for existing {ROOT_PRINCIPAL_ID}")
        return ROOT_PRINCIPAL_ID
    store.create_password_principal(
        login=ROOT_LOGIN,
        display_name="Switchboard Root",
        password_hash=ph,
        scopes=ADMIN_SCOPES,
        principal_id=ROOT_PRINCIPAL_ID,
        project=home_project,
    )
    print(f"  created {ROOT_LOGIN} on {home_project}")
    return ROOT_PRINCIPAL_ID


def _grant_all_projects(principal_id: str, login: str, display_name: str) -> None:
    for project_id in store.project_ids():
        store.grant_project_role(
            project_id, "principal", principal_id, "admin",
            created_by="provision/plan-access",
            scopes=ADMIN_SCOPES,
        )
        store.ensure_bootstrap_project_owner(
            project_id, principal_id, login, display_name, actor="provision/plan-access")
    print(f"  granted admin on {len(store.project_ids())} projects to {principal_id}")


def _seed_helm_deliverable(child_project: str) -> None:
    home = "helm"
    deliverable_id = f"helm-{child_project}"
    access = store.project_access(child_project)
    purpose = (access.get("purpose") or f"{child_project} work").strip()
    boundary = (access.get("boundary") or "").strip()
    label = store._project_map().get(child_project, {}).get("label") or child_project

    existing = store.get_deliverable(deliverable_id, project=home)
    if existing and not existing.get("error"):
        print(f"  deliverable {deliverable_id} already exists — linking new tasks only")
        deliverable = existing
    else:
        board = store.create_project_board(
            {
                "id": deliverable_id,
                "title": label,
                "kind": "mission",
                "status": "active",
                "end_state": purpose,
                "description": boundary or purpose,
            },
            actor="provision/plan-access",
            project=home,
        )
        deliverable = store.create_deliverable(
            {
                "id": deliverable_id,
                "board_id": board["id"],
                "title": label,
                "status": "in_progress",
                "end_state": purpose,
                "why_it_matters": f"Umbrella deliverable for project={child_project} under HELM.",
            },
            actor="provision/plan-access",
            project=home,
        )
        print(f"  created deliverable {deliverable_id}")

    ms = store.add_deliverable_milestone(
        deliverable_id,
        {"title": f"{label} execution", "status": "in_progress"},
        actor="provision/plan-access",
        project=home,
    )
    milestone_id = ms["milestones"][-1]["id"]
    linked = {link.get("task_id") for link in (deliverable.get("task_links") or [])}
    count = 0
    for task in store.list_tasks(project=child_project):
        tid = task.get("task_id")
        if not tid or tid in linked:
            continue
        store.link_task_to_deliverable(
            deliverable_id,
            child_project,
            tid,
            milestone_id=milestone_id,
            actor="provision/plan-access",
            project=home,
        )
        count += 1
    print(f"  linked {count} tasks from {child_project} -> {deliverable_id}")


def main() -> None:
    store.init_project_registry()
    print("== Global root admin ==")
    root_id = _ensure_root_principal("switchboard")
    _grant_all_projects(root_id, ROOT_LOGIN, "Switchboard Root")

    steve = store.get_password_login("steve", project="switchboard")
    if steve:
        print("== Expanding steve to all projects ==")
        _grant_all_projects(steve["principal_id"], "steve", steve.get("display_name") or "Steve")

    print("== HELM umbrella deliverables ==")
    with store._registry_conn() as c:
        c.execute(
            "UPDATE projects SET label=? WHERE id=?",
            ("HELM — Marine Nav Companion", "helm"),
        )
    for child in HELM_CHILDREN:
        if not store.has_project(child):
            print(f"  skip missing project {child}")
            continue
        _seed_helm_deliverable(child)

    print("== Done ==")
    print(f"Root login: {ROOT_LOGIN} (home project switchboard; session works on all granted projects)")
    print(f"Top-level switcher (set PM_TOP_LEVEL_PROJECTS): {','.join(TOP_LEVEL)}")
    print("Mission cockpit: ?project=helm&deliverable=helm-vulkan#tab-mission")


if __name__ == "__main__":
    main()
