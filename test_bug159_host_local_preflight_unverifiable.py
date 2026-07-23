#!/usr/bin/env python3
"""BUG-159: the coordinator must not claim a remote worktree is missing.

``preflight_work_session`` used to end in a fallback that ``stat()``s the Work
Session's ``worktree_path`` on the COORDINATOR's filesystem. For a workspace on
an enrolled Mac host that is always False, so it emitted a blocking
``worktree_missing`` deny and flipped a healthy session to ``unsafe`` -- blocking
the code_strict completion and merge gates over a fact the server cannot observe.

BUG-115 already suppressed this, but only while a LIVE runner owned the session
(matched by metadata.work_session_id or a ``direct-session/<runner>`` principal).
Any other host-local session -- an operator/MCP principal, or a direct-CLI session
whose runner has since exited -- still got the false deny.

The fix separates "I cannot see it" from "it is not there". Only workspaces the
coordinator actually owns (managed workspaces under ``workspace_root``) may
produce ``worktree_missing``; everything else reports a non-blocking, explicitly
unverifiable state. Nothing lands unverified: completion/merge still require a
real host attestation (BUG-97) or observed evidence.
"""
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
TMP = tempfile.mkdtemp(prefix="bug159-")
os.environ["PM_DB_PATH"] = os.path.join(TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(TMP, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
# Pin the managed workspace root so "coordinator-owned" is unambiguous here.
MANAGED_ROOT = os.path.join(TMP, "workspaces")
os.environ["PM_WORKSPACE_ROOT"] = MANAGED_ROOT
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import store  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def _codes(result):
    return {str(f.get("code")) for f in (result.get("preflight") or {}).get("findings") or []}


def _verdict(result):
    return (result.get("preflight") or {}).get("verdict")


def _health(result):
    return ((result.get("updated") or {}).get("work_session") or {}).get("health") or {}


P = "switchboard"
HEAD = "d" * 40
# A path on somebody else's machine. The coordinator can never stat this.
HOST_LOCAL = "/Users/steveridder/Dropbox/Git/projectplanner/.worktrees/BUG-159"

store.init_db(P)


def _session(task_id, agent_id, session_id, principal_id, worktree_path):
    task = store.create_task({"workstream_id": "BUG", "title": task_id},
                             actor="seed", project=P)
    return store.create_work_session({
        "work_session_id": session_id,
        "task_id": task["task_id"],
        "agent_id": agent_id,
        "principal_id": principal_id,
        "repo_role": "canonical",
        "storage_mode": "worktree",
        "worktree_path": worktree_path,
        "branch": f"claude/{task_id}-branch",
        "head_sha": HEAD,
        "base_sha": HEAD,
        "dirty_status": "clean",
        "status": "active",
        "policy_profile": "code_strict",
    }, actor=agent_id, project=P)["work_session"]


print("BUG-159 host-local preflight is unverifiable, not missing")

# 1. The exact SIMPLIFY-21 shape: an ordinary MCP principal, no runner at all.
s1 = _session("BUG159A", "claude/BUG-159", "worksession-bug159a",
              "env-mcp-token", HOST_LOCAL)
r1 = store.preflight_work_session(s1["work_session_id"], actor="env-mcp-token",
                                  project=P, expected_branch=s1["branch"])
ok("worktree_missing" not in _codes(r1),
   "an operator/MCP-principal host-local session is not false-denied worktree_missing")
ok(_verdict(r1) != "deny",
   "the recorded preflight verdict is not a deny")
ok("work_session_preflight_unverifiable" in _codes(r1),
   "the report names the honest state: unverifiable from the coordinator")
ok((r1.get("preflight") or {}).get("unverifiable") is True and
   (r1.get("preflight") or {}).get("ok") is False,
   "unverifiable is explicitly not a pass -- nothing was verified")

h1 = _health(r1)
ok(h1.get("status") != "unsafe" and h1.get("safe") is not False,
   "the Work Session stays usable instead of flipping to unsafe")
ok(int(h1.get("blocking_count") or 0) == 0,
   "no blocking finding is raised for a path the coordinator cannot see")
ok(any(f.get("code") == "work_session_preflight_unverifiable"
       for f in h1.get("findings") or []),
   "session health surfaces the unverifiable signal rather than hiding it")

# 2. Runner liveness must not decide whether a worktree exists. Same session,
#    preflighted twice, with no runner either time -> same honest answer.
r1b = store.preflight_work_session(s1["work_session_id"], actor="env-mcp-token",
                                   project=P, expected_branch=s1["branch"])
ok(_verdict(r1b) == _verdict(r1) and "worktree_missing" not in _codes(r1b),
   "the verdict does not depend on a runner happening to be alive")

# 3. Fail-closed is preserved exactly where the coordinator DOES own the path.
missing_managed = os.path.join(MANAGED_ROOT, "switchboard", "BUG159B", "gone")
s2 = _session("BUG159B", "claude/BUG-159b", "worksession-bug159b",
              "env-mcp-token", missing_managed)
r2 = store.preflight_work_session(s2["work_session_id"], actor="env-mcp-token",
                                  project=P, expected_branch=s2["branch"])
ok("worktree_missing" in _codes(r2) and _verdict(r2) == "deny",
   "a missing COORDINATOR-OWNED managed workspace still fails closed")
ok(_health(r2).get("status") == "unsafe",
   "a genuinely missing managed workspace still marks the session unsafe")

# 4. A real, present local worktree still gets a genuine verified preflight --
#    the fix must not short-circuit paths the coordinator CAN actually inspect.
present = os.path.join(MANAGED_ROOT, "switchboard", "BUG159C", "wt")
os.makedirs(present, exist_ok=True)
s3 = _session("BUG159C", "claude/BUG-159c", "worksession-bug159c",
              "env-mcp-token", present)
r3 = store.preflight_work_session(s3["work_session_id"], actor="env-mcp-token",
                                  project=P, expected_branch=s3["branch"])
ok("worktree_missing" not in _codes(r3) and
   (r3.get("preflight") or {}).get("unverifiable") is not True,
   "an existing local path is really inspected, not shortcut to unverifiable")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
