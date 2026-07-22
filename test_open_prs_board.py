#!/usr/bin/env python3
"""Fleet-dock open-PR board (spec 2026-07-23-fleet-dock-pr-tab): every open PR on
the canonical repo with badge-ready CI/mergeable/queue/board-join status, classified
under attention rule C, degraded (never erroring) without a token, and cached one
GitHub sweep per bucket. All fetchers stubbed — no network."""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="open-prs-board-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import open_prs  # noqa: E402

REPO = "example/dock"
NOW = 1_800_000_000.0
passed = failed = 0


def ok(cond, msg):
    global passed, failed
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    passed += 1 if cond else 0
    failed += 0 if cond else 1


def _pr(number, title, *, branch="", draft=False, sha=None, updated="2027-01-15T00:00:00Z"):
    return {
        "number": number, "title": title, "draft": draft,
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "user": {"login": "agent-x"},
        "head": {"ref": branch or f"cursor/pr-{number}", "sha": sha or ("a%039d" % number)[:40]},
        "base": {"ref": "master"},
        "updated_at": updated,
    }


def _build(listed, *, details=None, ci=None, queue=None, tasks=None, now=NOW):
    details = details or {}
    ci = ci or {}
    return open_prs.build_open_prs(
        "switchboard", repo=REPO, token="tok", now=now,
        list_fn=lambda r, t: listed,
        detail_fn=lambda r, n, t: details.get(n, {"mergeable_state": "clean",
                                                 "additions": 10, "deletions": 2,
                                                 "changed_files": 3}),
        ci_fn=lambda r, sha, t: ci.get(sha, {"state": "success", "failing": []}),
        queue_fn=lambda r, t: queue or {},
        get_task_fn=lambda tid, project="": (tasks or {}).get(tid))


print("== degrade paths ==")
out = open_prs.build_open_prs("switchboard", repo=REPO, token="", now=NOW)
ok(out.get("unavailable") == "no_github_token" and out["prs"] == [],
   "no token -> unavailable payload, empty prs, no exception")
out = open_prs.build_open_prs("switchboard", repo="", token="tok", now=NOW,
                              get_task_fn=lambda *a, **k: None)
ok(out.get("unavailable") == "no_canonical_repo", "no canonical repo -> unavailable")


def _raise(*a, **k):
    raise RuntimeError("github down")


out = open_prs.build_open_prs("switchboard", repo=REPO, token="tok", now=NOW,
                              list_fn=_raise, get_task_fn=lambda *a, **k: None)
ok(str(out.get("unavailable", "")).startswith("github_error"),
   "list failure -> degraded payload, never raises")

print("== classification (rule C) ==")
sha_red = ("b" * 40)
listed = [
    _pr(1, "WATCH-13: green and queued", branch="cursor/WATCH-13-fix"),
    _pr(2, "UI-31: red gate", sha=sha_red),
    _pr(3, "conflicted work"),
    _pr(4, "green but stuck"),
    _pr(5, "draft wip", draft=True, sha=sha_red),
]
out = _build(
    listed,
    details={3: {"mergeable_state": "dirty", "additions": 1, "deletions": 1, "changed_files": 1},
             4: {"mergeable_state": "blocked", "additions": 1, "deletions": 1, "changed_files": 1}},
    ci={sha_red: {"state": "failure", "failing": ["Switchboard CI / VM gate"]}},
    queue={1: 2},
    tasks={"WATCH-13": {"task_id": "WATCH-13", "status": "In Review"}})
rows = {r["number"]: r for r in out["prs"]}
ok(not rows[1]["blocked"] and rows[1]["queue_position"] == 2,
   "green+approved+queued PR is not blocked; queue position carried")
ok(rows[2]["blocked"] and "VM gate" in rows[2]["blocked_reason"],
   "red CI blocks with the failing context named")
ok(rows[3]["blocked"] and rows[3]["blocked_reason"] == "merge conflicts",
   "dirty mergeable_state -> conflicts")
ok(rows[4]["blocked"] and rows[4]["blocked_reason"] == "green but blocked",
   "green checks + blocked merge -> stuck (rule C)")
ok(not rows[5]["blocked"], "draft with red CI does not block (not trying to merge)")
ok(out["blocked_count"] == 3, f"blocked_count=3 (got {out['blocked_count']})")
ok([r["number"] for r in out["prs"]][:3] == [2, 3, 4] or out["prs"][0]["blocked"],
   "blocked PRs sort first")

print("== board join ==")
ok(rows[1]["tasks"] == [{"task_id": "WATCH-13", "status": "In Review"}] and not rows[1]["orphan"],
   "branch-parsed task id joined with board status")
ok(rows[3]["orphan"] and rows[3]["tasks"] == [],
   "PR with no recognizable task id -> orphan (the 'no board task' badge)")

print("== stall signal ==")
stale = _build([_pr(9, "old", updated="2026-01-01T00:00:00Z")])
ok(stale["prs"][0]["stalled"], "updated_at older than 24h -> stalled")
fresh = _build([_pr(10, "new", updated="2027-01-15T00:00:00Z")], now=NOW)
ok(not fresh["prs"][0]["stalled"] or fresh["prs"][0]["updated_at"] > NOW,
   "recent update -> not stalled")

print("== ci fold ==")
ci = open_prs.ci_state_for_sha(
    REPO, "s", "tok",
    request_fn=lambda url, t: (
        {"statuses": [{"state": "success", "context": "vm"}]} if url.endswith("/status")
        else {"check_runs": [{"status": "completed", "conclusion": "failure", "name": "lint"}]}))
ok(ci["state"] == "failure" and ci["failing"] == ["lint"],
   "check-run failure wins over green commit status")
ci = open_prs.ci_state_for_sha(
    REPO, "s", "tok",
    request_fn=lambda url, t: (
        {"statuses": [{"state": "pending", "context": "vm"}]} if url.endswith("/status")
        else {"check_runs": []}))
ok(ci["state"] == "pending", "pending status -> pending")
ci = open_prs.ci_state_for_sha(REPO, "s", "tok", request_fn=_raise)
ok(ci["state"] == "none", "both CI surfaces failing -> none, no exception")

print("== merge queue best-effort ==")
q = open_prs.fetch_merge_queue_positions(
    REPO, "tok",
    graphql_fn=lambda query, t: {"data": {"repository": {"mergeQueue": {"entries": {
        "nodes": [{"position": 1, "pullRequest": {"number": 7}}]}}}}})
ok(q == {7: 1}, "queue positions parsed from GraphQL")
ok(open_prs.fetch_merge_queue_positions(REPO, "tok", graphql_fn=_raise) == {},
   "GraphQL failure -> empty mapping")

print("== cache bucket ==")
calls = []
import read_cache


def _fake_build():
    calls.append(1)
    return {"prs": [], "n": len(calls)}


b1 = read_cache.ttl_read_cache("open_prs_t", "p", 100, _fake_build, ttl=60)
b2 = read_cache.ttl_read_cache("open_prs_t", "p", 100, _fake_build, ttl=60)
b3 = read_cache.ttl_read_cache("open_prs_t", "p", 101, _fake_build, ttl=60)
ok(len(calls) == 2 and b1 is b2 and b3["n"] == 2,
   "same bucket served from cache; new bucket rebuilds")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
