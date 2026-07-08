#!/usr/bin/env python3
"""Open-PR backstop (BUG-28, ADR-0006): a pre-review task whose pr_opened webhook
was dropped self-heals to In Review on the next reconcile tick. Module-level logic
plus a real store.reconcile integration."""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="open-pr-backstop-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import orphan_merge_discovery as omd  # noqa: E402
import store  # noqa: E402

REPO = "example/qa-openpr"
passed = failed = 0


def ok(cond, msg):
    global passed, failed
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    passed += 1 if cond else 0
    failed += 0 if cond else 1


def _open_pr(number, task_id, *, draft=False, branch=None, sha=None):
    return {
        "number": number, "draft": draft,
        "title": f"{task_id}: work in progress",
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "head": {"ref": branch or f"cursor/{task_id}-slug", "sha": sha or ("h%040d" % number)[:40]},
        "updated_at": "2026-07-08T00:00:00Z",
    }


CANON = lambda _r: {"canonical": True, "role": "canonical"}  # noqa: E731

# ---- module-level logic (injected fakes) ------------------------------------
calls = []


def _fake_mark(task_id, pr_number, *, pr_url="", branch="", head_sha="",
               actor="", project=""):
    calls.append((task_id, pr_number, branch))
    return {"task_id": task_id, "status": "In Review",
            "git_state": {"pr_number": pr_number, "branch": branch}}


def _run(tasks, git_states, prs, **kw):
    kw.setdefault("project", "p")
    kw.setdefault("repo", REPO)
    kw.setdefault("token", "tok")
    kw.setdefault("role_checker", CANON)
    kw.setdefault("mark_pr_opened_fn", _fake_mark)
    kw.setdefault("fetch_open_prs_fn", lambda repo, **_k: (list(prs), {"open_pr_count": len(prs)}))
    return omd.discover_open_prs(tasks, git_states, **kw)


# 1. Not Started + matching open PR -> advanced
calls.clear()
tasks = [{"task_id": "SAT-1", "status": "Not Started"}]
gs = {"SAT-1": {}}
findings, advanced, checks = _run(tasks, gs, [_open_pr(10, "SAT-1")])
ok(checks["open_pr_backstop"] == "checked", "backstop runs with repo+token")
ok([a["task_id"] for a in advanced] == ["SAT-1"] and calls and calls[0][0] == "SAT-1",
   "Not Started task with an open canonical PR is advanced to In Review")
ok(tasks[0]["status"] == "In Review", "task dict is updated in place")

# 2. non-empty git_state (already has a pr) -> skipped
calls.clear()
_, adv, _ = _run([{"task_id": "SAT-1", "status": "Not Started"}],
                 {"SAT-1": {"pr_number": 9}}, [_open_pr(10, "SAT-1")])
ok(not adv and not calls, "task that already has git_state is not re-advanced")

# 3. In Review / Done are not eligible (pre-review only)
calls.clear()
_, adv, _ = _run([{"task_id": "SAT-1", "status": "In Review"}], {"SAT-1": {}},
                 [_open_pr(10, "SAT-1")])
ok(not adv, "In Review task is not touched by the open-PR backstop")

# 4. ambiguous: two open PRs naming the task -> finding, no advance
calls.clear()
f4, adv4, _ = _run([{"task_id": "SAT-1", "status": "Not Started"}], {"SAT-1": {}},
                   [_open_pr(10, "SAT-1"), _open_pr(11, "SAT-1")])
ok(not adv4 and any(x["code"] == "open_pr_backstop_ambiguous" for x in f4),
   "two open PRs for one task -> ambiguous finding, not auto-advanced")

# 5. guards
_, _, c_norepo = _run([], {}, [], repo="")
ok(c_norepo["open_pr_backstop"] == "skipped_no_repo", "no repo -> skipped_no_repo")
_, _, c_notok = _run([], {}, [], token="")
ok(c_notok["open_pr_backstop"] == "skipped_no_token", "no token -> skipped_no_token")
_, _, c_role = _run([], {}, [], role_checker=lambda _r: {"canonical": False, "role": "public"})
ok(c_role["open_pr_backstop"] == "skipped_non_canonical_repo", "non-canonical -> skipped")

# 6. fetch excludes drafts + stale
def _req(url, token=""):
    return [_open_pr(1, "AAA-1"), _open_pr(2, "BBB-1", draft=True)]


open_prs, meta = omd.fetch_recent_open_prs(REPO, token="t", now=1783500000.0, request_fn=_req)
ok(len(open_prs) == 1 and open_prs[0]["number"] == 1, "fetch_recent_open_prs excludes drafts")

# ---- integration through store.reconcile ------------------------------------
try:
    HOME = "qa-openpr-home"
    store.init_project_registry()
    store.create_project("Open PR Home", project_id=HOME, actor="test")
    store.init_db(HOME)
    store.set_project_github_repo(REPO, project=HOME)
    store.set_project_repo_topology(project=HOME, canonical_repo=REPO)
    t = store.create_task({"workstream_id": "SAT", "title": "dropped-webhook task"},
                          actor="test", project=HOME)
    tid = t["task_id"]
    fake = [_open_pr(77, tid)]
    omd.fetch_recent_open_prs = lambda repo, **_k: (list(fake), {"open_pr_count": len(fake)})
    omd.fetch_recent_merged_prs = lambda repo, **_k: ([], {"merged_pr_count": 0})
    os.environ["PM_GITHUB_TOKEN"] = "test-token"

    res = store.reconcile(project=HOME)
    checks = res.get("external_checks") or {}
    ok(checks.get("open_pr_backstop") == "checked", "reconcile runs the open-PR backstop")
    task = store.get_task(tid, project=HOME) or {}
    ok(task.get("status") == "In Review",
       "dropped-open-webhook task self-heals to In Review via reconcile")
    gsx = task.get("git_state") or {}
    ok(gsx.get("pr_number") == 77, "advanced task carries the open PR's pr_number")
    adv = [b for b in (res.get("backfilled") or []) if b.get("source") == "open_pr_backstop"]
    ok([b["task_id"] for b in adv] == [tid], "reconcile reports the open-PR advance")
    with store._conn(HOME) as c:
        n = c.execute("SELECT COUNT(*) FROM activity WHERE kind='git.open_pr_backstop_advanced'").fetchone()[0]
    ok(n == 1, "advance writes a git.open_pr_backstop_advanced activity")

    res2 = store.reconcile(project=HOME)
    adv2 = [b for b in (res2.get("backfilled") or []) if b.get("source") == "open_pr_backstop"]
    ok(not adv2, "second reconcile is idempotent (git_state no longer empty)")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nopen pr backstop: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
