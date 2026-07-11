#!/usr/bin/env python3
"""ACCESS-14: contributors can create projects that are private to them.

Dependency-light — exercises store + the auth repository directly (no fastapi/mcp), so it
runs under the plain gate. Verifies: contributors have write:projects; a private project is
visible to its creator, invitees, and org admins/owners but NOT ordinary org peers; an
'org' project is visible to all org members; visibility persists and validates."""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="access-priv-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "required"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402
import scripts.switchboard_path  # noqa: E402,F401
from switchboard.api.routers.auth import store as astore  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def sees(user_id, project_id, superadmin=False):
    return project_id in astore.accessible_project_ids(user_id, superadmin)


try:
    store.init_project_registry()
    astore.init()
    org = store.ensure_org("org-acme", "Acme", slug="acme")["id"]
    # org roster: an owner, an admin, two ordinary members (creator + peer), plus an outsider
    for uid, role in [("u-owner", "owner"), ("u-admin", "admin"),
                      ("u-contrib", "member"), ("u-peer", "member")]:
        store.ensure_user(uid, display_name=uid)
        store.add_org_member(org, uid, role=role)
    store.ensure_user("u-invitee", display_name="invitee")  # not in the org

    # --- scope wiring: contributors carry write:projects, viewers do not -------------------
    ok("write:projects" in store.role_scopes("contributor"), "contributor role grants write:projects")
    ok("write:projects" not in store.role_scopes("viewer"), "viewer role does not grant write:projects")
    store.grant_project_role("switchboard", "user", "u-contrib", "contributor", created_by="test")
    eff = store.effective_principal_scopes("switchboard", "u-contrib")
    ok("write:projects" in eff, "a contributor grant yields write:projects in effective scopes")

    # --- contributor creates a PRIVATE project --------------------------------------------
    res = store.create_project("Contrib Secret", project_id="contrib-secret", org_id=org,
                               owner_principal_id="u-contrib", visibility="private", actor="u-contrib")
    ok(res.get("created") and res["project"]["id"] == "contrib-secret", "contributor creates a project")
    ok(store.project_access("contrib-secret").get("visibility") == "private",
       "created project is stored private")

    ok(sees("u-contrib", "contrib-secret"), "creator sees their private project")
    ok(not sees("u-peer", "contrib-secret"), "an ordinary org peer does NOT see the private project")
    ok(sees("u-admin", "contrib-secret"), "an org admin sees the private project (oversight)")
    ok(sees("u-owner", "contrib-secret"), "the org owner sees the private project (oversight)")
    ok(not sees("u-invitee", "contrib-secret"), "an outsider does not see the private project")
    ok(sees("u-nobody", "contrib-secret", superadmin=True), "a superadmin sees all projects")

    # --- inviting a user shares just that private project ---------------------------------
    store.grant_project_role("contrib-secret", "user", "u-invitee", "viewer", created_by="u-contrib")
    ok(sees("u-invitee", "contrib-secret"), "an invited (granted) user sees the private project")

    # --- an 'org' project is visible to all org members -----------------------------------
    store.create_project("Team Board", project_id="team-board", org_id=org,
                         owner_principal_id="u-contrib", visibility="org", actor="u-contrib")
    ok(sees("u-peer", "team-board"), "an ordinary org peer sees an 'org'-visibility project")
    ok(not sees("u-invitee", "team-board"), "a non-member still does not see an 'org' project")

    # --- validation ------------------------------------------------------------------------
    bad = store.set_project_access("team-board", org, visibility="secret")
    ok(bad.get("error"), "invalid visibility value is rejected")

    # --- ACCESS-15: web gates align (needs fastapi; skip cleanly if absent) ----------------
    try:
        import app as webapp
    except Exception as exc:  # noqa: BLE001
        print(f"  SKIP  app-level gate checks need optional dependency: {exc}")
    else:
        ok(webapp._write_required_scopes("/api/projects") == ("write:projects",),
           "POST /api/projects requires write:projects (contributors), not write:system")
        ok(webapp._write_required_scopes("/api/access/x") == ("write:system",),
           "admin surfaces (/api/access) still require write:system")
        seer = {"id": "u-admin", "projects": [{"id": "contrib-secret"}]}
        ok("read" in webapp._global_user_scopes(seer, "contrib-secret"),
           "read gate: a user who can SEE a project (accessible set) can READ it")
        blind = {"id": "u-peer", "projects": []}
        ok("read" not in webapp._global_user_scopes(blind, "contrib-secret"),
           "read gate: a user who cannot see a project gets no read on it")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
