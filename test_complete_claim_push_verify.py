#!/usr/bin/env python3
"""complete_claim server-side push-verification gate (silent-failed-push leak).

Fail-closed on a provably-absent branch/head_sha; warn+allow on unreachable.
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="complete-claim-push-verify-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_VERIFY_COMPLETION_PUSH"] = "1"  # feature under test
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import push_verification as pv  # noqa: E402
import store  # noqa: E402

P = "switchboard"
AGENT = "codex/ENFORCE-push-verify-test"
store.init_db(P)
passed = failed = 0


# Never hit the network in tests: the verifier is monkeypatched per-case below,
# but guarantee a hard failure if any case forgets to.
def _no_network(evidence, repo, token, **kw):
    raise AssertionError("verify_push_evidence not stubbed for this case")


store.push_verification.verify_push_evidence = _no_network


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    if condition:
        passed += 1
    else:
        failed += 1


def stub_verify(status, **extra):
    def _fn(evidence, repo, token, **kw):
        return {"status": status, "schema": pv.SCHEMA, **extra}
    return _fn


def new_claim(title, order):
    created = store.create_task(
        {"workstream_id": "ENFORCE", "title": title, "sort_order": order},
        actor="test", project=P)
    claimed = store.claim_task(created["task_id"], AGENT, actor="test", project=P)
    ok(claimed.get("claimed") is True, f"{title}: claim starts")
    return created["task_id"], claimed["claim_id"]


EVIDENCE = '{"branch": "codex/ENFORCE-1-x", "head_sha": "abc123"}'

# 1. ABSENT -> fail closed, task NOT In Review, claim stays active, activity logged
tid, cid = new_claim("absent fails closed", 1)
store.push_verification.verify_push_evidence = stub_verify(
    pv.ABSENT, reason="commit_not_on_remote", repo="o/r", ref_kind="commit", ref="abc123")
res = store.complete_claim(cid, EVIDENCE, project=P)
ok(res.get("completed") is False, "absent: completion rejected")
ok(res.get("reason") == "push_not_on_remote", "absent: reason=push_not_on_remote")
ok(res.get("failure_class") == "stale_branch", "absent: failure_class=stale_branch")
task = store.get_task(tid, project=P)
ok(task["status"] != "In Review", f"absent: task not advanced (is {task['status']})")
ok(any(a.get("kind") == "task.complete_blocked_push" for a in task.get("activity", [])),
   "absent: task.complete_blocked_push activity recorded")
# claim still active -> a real (pushed) retry can complete
store.push_verification.verify_push_evidence = stub_verify(pv.PRESENT, ref="abc123")
res2 = store.complete_claim(cid, EVIDENCE, project=P)
ok(res2.get("completed") is True, "absent: same claim completes after push proven")
ok(store.get_task(tid, project=P)["status"] == "In Review", "absent->present: now In Review")

# 2. PRESENT -> completes to In Review, git_state stamped, verification recorded
tid, cid = new_claim("present completes", 2)
store.push_verification.verify_push_evidence = stub_verify(
    pv.PRESENT, ref="abc123", ref_kind="commit", repo="o/r")
res = store.complete_claim(cid, EVIDENCE, project=P)
ok(res.get("completed") is True, "present: completed")
ok(res.get("status") == "In Review", "present: status In Review")
ok(res.get("push_verification", {}).get("status") == pv.PRESENT,
   "present: push_verification in response")
gs = store.get_task(tid, project=P).get("git_state") or {}
ok(gs.get("pushed_at"), "present: pushed_at stamped in git_state")
ok((gs.get("evidence") or {}).get("push_verification", {}).get("status") == pv.PRESENT,
   "present: verification persisted in git_state evidence")

# 3. UNVERIFIED (unreachable) -> allowed, warned, flagged
tid, cid = new_claim("unverified warns", 3)
store.push_verification.verify_push_evidence = stub_verify(
    pv.UNVERIFIED, reason="remote_unreachable", repo="o/r")
res = store.complete_claim(cid, EVIDENCE, project=P)
ok(res.get("completed") is True, "unverified: completion allowed")
ok(res.get("status") == "In Review", "unverified: advances to In Review")
ok(any("push_unverified" in w for w in res.get("warnings", [])),
   "unverified: push_unverified warning present")
ok(res.get("push_verification", {}).get("reason") == "remote_unreachable",
   "unverified: reason surfaced")

# 4. Docs completion with no git evidence -> skipped, unaffected
tid, cid = new_claim("docs skips", 4)
store.push_verification.verify_push_evidence = stub_verify(pv.SKIPPED, reason="no_git_evidence")
res = store.complete_claim(cid, '{"artifact_or_review_note": "reviewed"}', project=P)
ok(res.get("completed") is True, "docs: completes")
ok(res.get("status") == "In Review", "docs: In Review")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
