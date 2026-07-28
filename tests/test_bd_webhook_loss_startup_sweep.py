#!/usr/bin/env python3
"""Startup sweep recovers merges whose webhooks died during a deploy restart.

Observed live 2026-07-28: PR #1017 merged at 03:57:37Z while autodeploy was
hard-restarting the fleet. Caddy 502'd GitHub's delivery, GitHub never retries,
and the event was lost BEFORE the durable webhook inbox could store it — so the
task sat Blocked/github_pr_open until an operator hand-called
POST /ixp/v1/tasks/UI-72/reconcile-merge. Incremental reconcile cannot recover
this class (orphan_merge_discovery is deferred to full mode) and full mode
exceeds the MCP tool deadline.

The fix: a bounded sweep over tasks that still record an open PR, running the
existing per-task reconcile-merge against fresh GitHub state, invoked at
coordinator-daemon startup — the process that restarts on every deploy, which
is exactly the window that loses webhooks.
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="webhook-loss-sweep-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP

from path_setup import ROOT  # noqa: F401,E402

import store  # noqa: E402
import background_jobs  # noqa: E402
from switchboard.storage.repositories import provenance  # noqa: E402

P = "switchboard"
store.init_db(P)

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def make_task(title, pr_number, merged_sha=None, status="In Review"):
    task = store.create_task(
        {"workstream_id": "COORD", "title": title, "status": status},
        actor="sweep-test", project=P)
    tid = task["task_id"]
    with provenance._conn(P) as c:
        provenance._upsert_git_state(c, tid, {
            "branch": f"agent/{tid.lower()}", "head_sha": "a" * 40,
            "pr_number": pr_number,
            "pr_url": f"https://github.com/org/repo/pull/{pr_number}",
            "merged_sha": merged_sha,
        })
    return tid


MERGED_ON_GITHUB = {}


def fake_fetch(repo, number):
    entry = MERGED_ON_GITHUB.get(number)
    if entry is None:
        return {"number": number, "state": "open", "merged": False}
    return {"number": number, "state": "closed", "merged": True,
            "merge_commit_sha": entry, "merged_at": "2026-07-28T03:57:37Z",
            "base": {"ref": "master", "repo": {"default_branch": "master"}},
            "head": {"sha": "a" * 40, "ref": "agent/lost"}}


# Task whose merge webhook was lost: GitHub says merged, board says open.
lost = make_task("webhook lost during restart", 1017)
MERGED_ON_GITHUB[1017] = "c" * 40
# Task with a genuinely open PR: must NOT be touched.
open_pr = make_task("genuinely open", 1020)
# Task already stamped: must be skipped as a candidate entirely.
done = make_task("already merged", 1015, merged_sha="d" * 40, status="Done")

result = background_jobs.sweep_open_pr_merges(
    P, limit=40,
    canonical_repo_for=lambda _p: "org/repo",
    fetch_pull_request=fake_fetch,
)
ok(result.get("checked") == 2, f"sweep checks only open-PR candidates (got {result.get('checked')})")
ok(result.get("stamped") == 1, f"sweep stamps exactly the lost merge (got {result.get('stamped')})")

after = store.get_task(lost, project=P)
g = after.get("git_state") or {}
ok(after.get("status") == "Done", "lost-webhook task is Done after the sweep")
ok(g.get("merged_sha") == "c" * 40, "merged_sha stamped from fresh GitHub state")

still_open = store.get_task(open_pr, project=P)
ok(still_open.get("status") == "In Review",
   "genuinely open PR task is untouched")

# The cap is honored and a failing fetch cannot kill the sweep.
def exploding_fetch(repo, number):
    raise RuntimeError("github unreachable")

survives = background_jobs.sweep_open_pr_merges(
    P, limit=1,
    canonical_repo_for=lambda _p: "org/repo",
    fetch_pull_request=exploding_fetch,
)
ok(survives.get("errors", 0) >= 1 and "checked" in survives,
   "a failing GitHub fetch is counted, not fatal")

# The coordinator daemon runs the sweep at startup — pin the wiring.
src = (ROOT / "coordinator_daemon.py").read_text(encoding="utf-8")
ok("sweep_open_pr_merges" in src,
   "coordinator daemon startup invokes the sweep")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
