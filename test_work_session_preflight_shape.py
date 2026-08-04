#!/usr/bin/env python3
"""Work Session hygiene.repo_preflight must be a report object, never a scalar.

Prod incident (2026-08-05): an MCP agent created a simplemark Work Session with
``hygiene = {"repo_preflight": true}``. The validator only checks that
``hygiene`` itself is a JSON object, so the boolean was stored verbatim. The
read path (``_work_session_health``) does ``preflight.get("verdict")`` behind an
``or {}`` guard that passes truthy non-dicts straight through, so every
mission-status/attention build for the project crashed with
``AttributeError: 'bool' object has no attribute 'get'`` -- a full 500 outage
for the mission tab.

Two guarantees under test:
1. Write path: create/update reject a non-object ``hygiene.repo_preflight``
   with a contract error (a bare ``true`` is not a preflight attestation).
2. Read path: a legacy row that already carries a scalar renders as health with
   an explicit ``missing_work_session_preflight`` finding instead of raising --
   missing evidence stays visible, never a crash and never a green.
"""
import json
import os
import sqlite3
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="preflight-shape-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

P = "switchboard"
store.init_db(P)
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


task = store.create_task({"workstream_id": "BUG", "title": "preflight shape guard"},
                         project=P)
TASK_ID = task["task_id"]


def _base_payload(session_id, hygiene):
    return {
        "work_session_id": session_id,
        "task_id": TASK_ID,
        "agent_id": f"codex/{TASK_ID}-test",
        "repo_role": "canonical",
        "branch": f"codex/{TASK_ID}-test",
        "worktree_path": os.path.join(_TMP, "worktree", session_id),
        "status": "active",
        "hygiene": hygiene,
    }


# --- 1. create_work_session rejects scalar repo_preflight -------------------
res = store.create_work_session(
    _base_payload("worksession-shape-bool", {"repo_preflight": True}), project=P)
ok(res.get("error") == "invalid_work_session",
   "create rejects hygiene.repo_preflight=true with invalid_work_session, got: "
   + json.dumps({k: res.get(k) for k in ("error", "errors")}))
ok(any("repo_preflight" in e for e in (res.get("errors") or [])),
   "create error names hygiene.repo_preflight")

res = store.create_work_session(
    _base_payload("worksession-shape-str", {"repo_preflight": "clean"}), project=P)
ok(res.get("error") == "invalid_work_session",
   "create rejects hygiene.repo_preflight='clean' string")

# Valid shapes still pass: object report, or key absent entirely.
res = store.create_work_session(
    _base_payload("worksession-shape-ok", {"repo_preflight": {"ok": True, "verdict": "allow"}}),
    project=P)
ok(res.get("created") is True, "create accepts object repo_preflight report")
res = store.create_work_session(_base_payload("worksession-shape-none", {}), project=P)
ok(res.get("created") is True, "create accepts hygiene without repo_preflight")

# --- 2. update_work_session rejects scalar repo_preflight -------------------
res = store.update_work_session(
    "worksession-shape-ok", {"hygiene": {"repo_preflight": True}}, project=P)
ok(res.get("error") == "invalid_work_session",
   "update rejects hygiene.repo_preflight=true")

# --- 3. legacy scalar row reads as missing preflight, not a crash -----------
db = os.environ["PM_SWITCHBOARD_DB_PATH"]
c = sqlite3.connect(db)
c.execute("UPDATE work_sessions SET hygiene_json=? WHERE work_session_id=?",
          (json.dumps({"repo_preflight": True}), "worksession-shape-ok"))
c.commit()
c.close()

try:
    session = store.get_work_session("worksession-shape-ok", project=P)
except AttributeError as exc:
    session = None
    ok(False, f"get_work_session raised AttributeError on legacy scalar row: {exc}")

if session is not None:
    health = session.get("health") or {}
    codes = {f.get("code") for f in (health.get("findings") or [])}
    ok(bool(health), "legacy scalar row still produces a health report")
    ok("missing_work_session_preflight" in codes,
       "legacy scalar preflight surfaces as missing_work_session_preflight, got: "
       + json.dumps(sorted(x for x in codes if x)))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
