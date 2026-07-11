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
ADMIN_TOKEN = "web-admin-token"
SW_ADMIN = "sw-admin-token"
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

    store.create_project_board(
        {
            "id": "switchboard-live-mission",
            "title": "Switchboard Live Mission",
            "kind": "mission",
        },
        actor="test",
        project=P,
    )
    board_read = client.get(
        f"/api/projects/{P}/boards",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    ok(board_read.status_code == 200 and
       board_read.json()["boards"][0]["id"] == "switchboard-live-mission",
       "project path read resolves bearer auth against the path project")

    board_write = client.post(
        f"/api/projects/{P}/boards",
        json={"id": "switchboard-access-rollout", "title": "Switchboard Access Rollout"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    ok(board_write.status_code == 200 and
       board_write.json()["id"] == "switchboard-access-rollout",
       "project path write resolves bearer auth against the path project")

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

    # UI-15 — GitHub association panel data + Settings-path repo write gating.
    store.set_project_github_repo("6th-Element-Labs/projectplanner", project=P)
    assoc = client.get(
        f"/api/projects/{P}/github_association",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    ok(assoc.status_code == 200, "github_association read resolves bearer auth")
    aj = assoc.json()
    ok(aj["repo_configured"] is True and aj["repo"] == "6th-Element-Labs/projectplanner",
       "github_association returns the configured canonical repo")
    ok(aj["webhook"]["payload_url"].endswith(f"/api/github/webhook?project={P}"),
       "webhook payload URL pins ?project= (HARDEN-2: bare URLs fail closed)")
    ok(aj["webhook"]["secret_env"] == "PM_GITHUB_WEBHOOK_SECRET" and
       f"?project={P}" in aj["webhook"]["gh_command"] and
       "6th-Element-Labs/projectplanner" in aj["webhook"]["gh_command"],
       "gh one-liner carries the repo, secret name, and pinned project")
    ok(aj["verification"]["status"] == "configured" and
       aj["verification"]["delivered"] is False and
       aj["verification"]["repo_reachable"] is None,
       "verification is 'configured' (amber) with no network probe until Verify is pressed")

    repo_forbidden = client.post(
        f"/api/projects/{P}/github_repo",
        json={"github_repo": "6th-Element-Labs/evil"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    ok(repo_forbidden.status_code == 403,
       "github_repo write is refused without write:system")
    ok(store.get_project_github_repo(P) == "6th-Element-Labs/projectplanner",
       "refused github_repo write does not change the recorded repo")

    # The real UI-15 Settings path is a dynamic project (no built-in canonical topology),
    # where recording github_repo actually reroutes Done provenance.
    store.create_project("Webhook Wiring", project_id="webhookwire", actor="test")
    store.create_principal(
        kind="agent", display_name="codex/web-admin", token=ADMIN_TOKEN,
        scopes=["read", "write:tasks", "write:projects", "write:system"], project="webhookwire")
    repo_ok = client.post(
        "/api/projects/webhookwire/github_repo",
        json={"github_repo": "6th-Element-Labs/projectplanner-fork"},
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    ok(repo_ok.status_code == 200 and
       store.get_project_github_repo("webhookwire") == "6th-Element-Labs/projectplanner-fork",
       "github_repo write with write:system records the canonical repo on a dynamic project")
    assoc2 = client.get(
        "/api/projects/webhookwire/github_association",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    ok(assoc2.status_code == 200 and
       assoc2.json()["repo"] == "6th-Element-Labs/projectplanner-fork" and
       assoc2.json()["webhook"]["payload_url"].endswith("/api/github/webhook?project=webhookwire"),
       "the just-wired repo surfaces in the association panel with its pinned payload URL")
    bad_repo = client.post(
        "/api/projects/webhookwire/github_repo",
        json={"github_repo": "not-a-valid-repo"},
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    ok(bad_repo.status_code == 400,
       "github_repo write rejects a malformed owner/name")

    # UI-7 — operator directed messaging + ack inbox (browser-gated /api twins of /ixp).
    no_tok = client.post(
        f"/api/agent_messages/send?project={P}",
        json={"project": P, "to_agent": "codex/mini", "message": "rebase", "requires_ack": True})
    ok(no_tok.status_code == 401, "agent message send rejects missing bearer token")

    sent = client.post(
        f"/api/agent_messages/send?project={P}",
        json={"project": P, "to_agent": "codex/mini", "message": "rebase onto master",
              "requires_ack": True, "ack_deadline_minutes": 15},
        headers={"Authorization": f"Bearer {TOKEN}"})
    ok(sent.status_code == 200 and sent.json().get("id"),
       "operator can send a directed message to a live agent")
    smsg = sent.json()
    mid = smsg["id"]
    ok(smsg["from_agent"] == "codex/web-auth" and smsg["requires_ack"] is True and
       "delivery_status" in smsg,
       "sent message is authored as the operator, flagged requires_ack, with a delivery state")

    empty = client.post(
        f"/api/agent_messages/send?project={P}",
        json={"project": P, "to_agent": "codex/mini", "message": "  "},
        headers={"Authorization": f"Bearer {TOKEN}"})
    ok(empty.status_code == 400, "send rejects an empty message")

    pend = client.get(f"/api/agent_messages/pending?project={P}",
                      headers={"Authorization": f"Bearer {TOKEN}"})
    ok(pend.status_code == 200 and any(m["id"] == mid for m in pend.json()["pending_acks"]),
       "the operator's ack inbox lists the unacked message they sent")

    status = client.get(f"/api/agent_messages/{mid}/status?project={P}",
                        headers={"Authorization": f"Bearer {TOKEN}"})
    ok(status.status_code == 200 and status.json().get("acked_at") is None and
       "delivery_status" in status.json(),
       "message status shows unacked with a delivery state before ack")

    acked = client.post(
        f"/api/agent_messages/ack?project={P}",
        json={"project": P, "message_id": mid, "response": "done"},
        headers={"Authorization": f"Bearer {TOKEN}"})
    ok(acked.status_code == 200, "operator can ack a message on the recipient's behalf")
    pend2 = client.get(f"/api/agent_messages/pending?project={P}",
                       headers={"Authorization": f"Bearer {TOKEN}"}).json()
    ok(not any(m["id"] == mid for m in pend2["pending_acks"]),
       "acked message drops out of the ack inbox")
    status2 = client.get(f"/api/agent_messages/{mid}/status?project={P}",
                         headers={"Authorization": f"Bearer {TOKEN}"}).json()
    ok(status2.get("acked_at") is not None and status2.get("ack_response") == "done",
       "message status reflects the ack and its response")

    # UI-5 — members & access management.
    m_forbidden = client.get(f"/api/access/members?project={P}",
                             headers={"Authorization": f"Bearer {TOKEN}"})
    ok(m_forbidden.status_code == 403, "members list refused without write:system")

    store.create_principal(kind="agent", display_name="switchboard/owner", token=SW_ADMIN,
                           scopes=["read", "write:tasks", "write:projects", "write:system", "admin"],
                           project=P)
    granted = client.post(f"/api/access/project_role?project={P}",
                          json={"subject_kind": "principal", "subject_id": "codex/teammate",
                                "role": "contributor"},
                          headers={"Authorization": f"Bearer {SW_ADMIN}"})
    ok(granted.status_code == 200 and granted.json().get("role") == "contributor",
       "admin grants a project role")

    members = client.get(f"/api/access/members?project={P}",
                         headers={"Authorization": f"Bearer {SW_ADMIN}"})
    mj = members.json()
    ok(members.status_code == 200 and
       any(m["subject_id"] == "codex/teammate" and m["role"] == "contributor"
           for m in mj["members"]),
       "members list shows the new grant with its role")
    ok("role_definitions" in mj and "visibility" in mj and "global_auth" in mj,
       "members payload carries role definitions, visibility, and auth mode for the UI")

    revoked = client.post(f"/api/access/project_role/revoke?project={P}",
                          json={"subject_kind": "principal", "subject_id": "codex/teammate",
                                "role": "contributor"},
                          headers={"Authorization": f"Bearer {SW_ADMIN}"})
    ok(revoked.status_code == 200 and revoked.json().get("revoked") is True,
       "admin revokes a project role")
    members2 = client.get(f"/api/access/members?project={P}",
                          headers={"Authorization": f"Bearer {SW_ADMIN}"}).json()
    ok(not any(m["subject_id"] == "codex/teammate" for m in members2["members"]),
       "revoked grant drops out of the members list")

    revoke_forbidden = client.post(f"/api/access/project_role/revoke?project={P}",
                                   json={"subject_kind": "principal", "subject_id": "x", "role": "viewer"},
                                   headers={"Authorization": f"Bearer {TOKEN}"})
    ok(revoke_forbidden.status_code == 403, "role revoke refused without write:system")

    invite_noglobal = client.post(f"/api/access/invite?project={P}",
                                  json={"email": "teammate@company.com", "role": "contributor"},
                                  headers={"Authorization": f"Bearer {SW_ADMIN}"})
    ok(invite_noglobal.status_code == 400 and
       "global auth" in (invite_noglobal.json().get("detail") or ""),
       "email invite returns a clear message when global auth is off")
    invite_bad = client.post(f"/api/access/invite?project={P}",
                             json={"email": "not-an-email", "role": "contributor"},
                             headers={"Authorization": f"Bearer {SW_ADMIN}"})
    ok(invite_bad.status_code == 400, "invite rejects a malformed email")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
