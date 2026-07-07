#!/usr/bin/env python3
"""SESSION-12 — PR provenance gate decision logic.

Proves the CI chokepoint catches fleet PRs that bypassed the board workflow while
exempting human/operator and docs-only PRs, honoring SESSION-9 policy profiles,
and staying non-blocking in warn mode.
"""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="pr-provenance-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
for _k in ("SWITCHBOARD_CI_CLAIM_GATE_MODE", "SWITCHBOARD_CI_FLEET_BRANCH_PREFIXES",
           "SWITCHBOARD_CI_FLEET_AUTHORS"):
    os.environ.pop(_k, None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pr_provenance_gate  # noqa: E402
import store  # noqa: E402

HOME = "qa-prov-home"
REPO = "example/qa-prov-repo"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def _pr(number, *, branch, title="", body="", author="cursoragent", head_sha=None):
    return {
        "number": number,
        "title": title,
        "body": body,
        "user": {"login": author},
        "head": {"ref": branch, "sha": head_sha or ("h" * 40)},
        "base": {"ref": "main"},
    }


def evaluate(pr, **kw):
    kw.setdefault("mode", "warn")
    kw.setdefault("record_activity", False)
    return pr_provenance_gate.evaluate_pr_provenance(pr, repo=REPO, **kw)


try:
    store.init_project_registry()
    store.create_project("Prov Home", project_id=HOME, actor="test")
    store.init_db(HOME)
    store.set_project_github_repo(REPO, project=HOME)
    store.set_project_repo_topology(project=HOME, canonical_repo=REPO)

    # Coverage fixtures ------------------------------------------------------
    reviewed = store.create_task({"workstream_id": "CODE", "title": "code merge path in progress"},
                                 actor="test", project=HOME)
    store.update_task(reviewed["task_id"], {"status": "In Review"}, actor="test", project=HOME)

    sessioned = store.create_task({"workstream_id": "CODE", "title": "code work with a session"},
                                  actor="test", project=HOME)
    ws = store.create_work_session({
        "task_id": sessioned["task_id"], "agent_id": "cursor/agent-1", "repo_role": "canonical",
        "storage_mode": "worktree", "worktree_path": "/tmp/ws/agent-1",
        "branch": f"cursor/{sessioned['task_id']}-work",
        "base_sha": "b" * 40, "status": "active",
    }, actor="test", project=HOME)
    ok(not ws.get("error"), f"work session fixture created ({ws.get('error') or 'ok'})")

    # An enforced (code_strict) task: a fleet PR that references it but never
    # claimed it must be flagged. Switchboard defaults code tasks to docs_review,
    # so enforcement is opt-in per task via the policy_profile tag.
    naked = store.create_task({"workstream_id": "CODE", "title": "merge code path",
                               "description": "policy_profile: code_strict"},
                              actor="test", project=HOME)
    docs = store.create_task({"workstream_id": "DOCS", "title": "write the runbook",
                              "description": "policy_profile: docs_review"},
                             actor="test", project=HOME)

    # 1. covered by status -----------------------------------------------------
    r = evaluate(_pr(1, branch=f"cursor/{reviewed['task_id']}-x",
                     title=f"{reviewed['task_id']}: ship it"))
    ok(r["ok"] and not r["would_block"] and r["state"] == "success",
       "fleet PR for an In Review task passes (covered by status)")
    ok(any(x["task_id"] == reviewed["task_id"] for x in r["resolved"]),
       "resolved lists the referenced board task")

    # 2. covered by work session ----------------------------------------------
    r = evaluate(_pr(2, branch=f"cursor/{sessioned['task_id']}-work",
                     title=f"{sessioned['task_id']}: work"))
    ok(r["ok"] and not r["would_block"] and r.get("covered_by", "").startswith(sessioned["task_id"]),
       "fleet PR with an active Work Session passes (covered by work_session)")

    # 3. uncovered code_strict task -> would block ----------------------------
    warn = evaluate(_pr(3, branch=f"codex/{naked['task_id']}-go",
                        title=f"{naked['task_id']}: sneaky merge"))
    ok(warn["would_block"] and warn["reason"] == "uncovered_tasks",
       "uncovered code task is flagged would_block")
    ok(warn["ok"] and warn["state"] == "success" and
       warn["context_description"].startswith("WARN"),
       "warn mode surfaces the block in the description but posts success")
    enf = evaluate(_pr(3, branch=f"codex/{naked['task_id']}-go",
                       title=f"{naked['task_id']}: sneaky merge"), mode="enforce")
    ok(not enf["ok"] and enf["state"] == "failure",
       "enforce mode posts failure for the same uncovered task")

    # 4. no task reference at all ---------------------------------------------
    r = evaluate(_pr(4, branch="codex/mystery-refactor", title="refactor things"), mode="enforce")
    ok(not r["ok"] and r["reason"] == "no_task_reference" and r["state"] == "failure",
       "fleet PR with no task reference blocks in enforce mode")

    # 5. references a task id that is not on any board for this repo -----------
    r = evaluate(_pr(5, branch="codex/GHOST-9-x", title="GHOST-9: not real"), mode="enforce")
    ok(not r["ok"] and r["reason"] == "task_not_on_board",
       "reference to an unknown task id blocks")

    # 6. non-fleet (operator) branch is exempt --------------------------------
    r = evaluate(_pr(6, branch="fix/some-operator-fix", title="fix: a thing",
                     author="StevenRidder"))
    ok(r["ok"] and r["exempt"] and r["reason"] == "non_fleet_pr",
       "operator (non-fleet) PR is exempt")

    # 7. docs-only change is exempt even for a fleet branch -------------------
    r = evaluate(_pr(7, branch=f"codex/{naked['task_id']}-docs",
                     title=f"{naked['task_id']}: docs"),
                 changed_paths=["docs/guide.md", "README.md", "plan-docs/x.md"])
    ok(r["ok"] and r["exempt"] and r["reason"] == "docs_only_change",
       "docs-only fleet PR is exempt")
    r = evaluate(_pr(7, branch=f"codex/{naked['task_id']}-mix",
                     title=f"{naked['task_id']}: mixed"),
                 changed_paths=["docs/guide.md", "store.py"])
    ok(r["would_block"], "a code file among docs removes the docs-only exemption")

    # 8. uncovered but NON-enforced profile (docs_review) does not block -------
    r = evaluate(_pr(8, branch=f"codex/{docs['task_id']}-x",
                     title=f"{docs['task_id']}: notes"))
    ok(r["reason"] == "uncovered_tasks" and not r["would_block"],
       "uncovered task on a non-enforced profile warns but does not block")

    # 9. mode off short-circuits ----------------------------------------------
    r = evaluate(_pr(9, branch=f"codex/{naked['task_id']}-x",
                     title=f"{naked['task_id']}: x"), mode="off")
    ok(r["ok"] and r["reason"] == "gate_disabled" and not r["resolved"],
       "mode=off returns success without touching the board")

    # 10. activity trail for repeat-offender surfacing (violations only, deduped)
    violator = _pr(10, branch=f"codex/{naked['task_id']}-y", title=f"{naked['task_id']}: y",
                   head_sha="v" * 40)
    for _ in range(3):  # timer re-runs must not spam the log
        pr_provenance_gate.evaluate_pr_provenance(
            violator, repo=REPO, mode="warn", record_activity=True, activity_project=HOME)
    with store._conn(HOME) as c:
        n_violation = c.execute(
            "SELECT COUNT(*) FROM activity WHERE kind='pr.provenance_gate'").fetchone()[0]
    ok(n_violation == 1, "violation is logged exactly once across repeated ticks (deduped by sha)")
    # A clean/covered PR must NOT write an activity row.
    pr_provenance_gate.evaluate_pr_provenance(
        _pr(11, branch=f"cursor/{reviewed['task_id']}-z", title=f"{reviewed['task_id']}: z"),
        repo=REPO, mode="warn", record_activity=True, activity_project=HOME)
    with store._conn(HOME) as c:
        n_after = c.execute(
            "SELECT COUNT(*) FROM activity WHERE kind='pr.provenance_gate'").fetchone()[0]
    ok(n_after == 1, "clean passes do not write activity rows")

    # 11. env-configured fleet prefixes ---------------------------------------
    r = evaluate(_pr(11, branch="wip/anything", title="no task"),
                 fleet_branch_prefixes=["wip/"], mode="enforce")
    ok(not r["ok"] and r["fleet"], "custom fleet branch prefix is honored")

    # 12. per-repo mode resolution (primary enforce, others warn) --------------
    PRIMARY = "example/primary-repo"
    for _k in ("SWITCHBOARD_CI_CLAIM_GATE_MODE", "SWITCHBOARD_CI_CLAIM_GATE_MODE_DEFAULT",
               "SWITCHBOARD_CI_CLAIM_GATE_MODES"):
        os.environ.pop(_k, None)
    os.environ["SWITCHBOARD_CI_CLAIM_GATE_MODE"] = "enforce"
    ok(pr_provenance_gate.resolve_mode(PRIMARY, PRIMARY) == "enforce",
       "primary repo uses the configured enforce mode")
    ok(pr_provenance_gate.resolve_mode("other/repo", PRIMARY) == "warn",
       "a non-primary canonical repo defaults to warn")
    os.environ["SWITCHBOARD_CI_CLAIM_GATE_MODES"] = "Other/Repo=enforce"
    ok(pr_provenance_gate.resolve_mode("other/repo", PRIMARY) == "enforce",
       "per-repo override (case-insensitive) wins over the default")
    os.environ["SWITCHBOARD_CI_CLAIM_GATE_MODE_DEFAULT"] = "off"
    ok(pr_provenance_gate.resolve_mode("third/repo", PRIMARY) == "off",
       "SWITCHBOARD_CI_CLAIM_GATE_MODE_DEFAULT tunes non-primary repos")
    for _k in ("SWITCHBOARD_CI_CLAIM_GATE_MODE", "SWITCHBOARD_CI_CLAIM_GATE_MODE_DEFAULT",
               "SWITCHBOARD_CI_CLAIM_GATE_MODES"):
        os.environ.pop(_k, None)

    # 13. registry-driven repo discovery (auto-covers new projects) -----------
    store.create_project("Prov Sibling", project_id="qa-prov-sib", actor="test")
    store.init_db("qa-prov-sib")
    store.set_project_repo_topology(project="qa-prov-sib", canonical_repo=REPO)  # shares REPO
    store.create_project("Prov Other", project_id="qa-prov-other", actor="test")
    store.init_db("qa-prov-other")
    store.set_project_repo_topology(project="qa-prov-other", canonical_repo="example/other-repo")
    store.create_project("Prov NoRepo", project_id="qa-prov-norepo", actor="test")
    store.init_db("qa-prov-norepo")
    repos = store.list_canonical_repos()
    ok(REPO in repos and set(repos[REPO]) >= {HOME, "qa-prov-sib"},
       "shared canonical repo maps to all its projects, listed once")
    ok(repos.get("example/other-repo") == ["qa-prov-other"],
       "a distinct project's canonical repo is discovered")
    ok(all("qa-prov-norepo" not in v for v in repos.values()),
       "a project with no canonical repo is excluded")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\npr provenance gate: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
