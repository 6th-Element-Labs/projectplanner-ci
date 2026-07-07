#!/usr/bin/env python3
"""SESSION-9 session policy profile regressions."""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="session-policy-profiles-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

AGENT = "codex/SESSION-9-policy-profiles"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def task(project, title, description="", workstream="SESSION", order=1):
    return store.create_task(
        {
            "workstream_id": workstream,
            "title": title,
            "description": description,
            "sort_order": order,
        },
        actor="test",
        project=project,
    )


def session_payload(project, task_id, profile="code_strict", dirty="clean"):
    return {
        "task_id": task_id,
        "agent_id": AGENT,
        "runtime": "codex",
        "repo_role": "canonical",
        "branch": f"codex/{task_id}-policy-profiles",
        "upstream": "origin/main" if project == "helm" else "origin/master",
        "base_sha": "base-ok",
        "head_sha": "head-ok",
        "worktree_path": f"/tmp/{task_id.lower()}-policy-profiles",
        "storage_mode": "worktree",
        "status": "active",
        "dirty_status": dirty,
        "conflict_marker_count": 0,
        "policy_profile": profile,
        "hygiene": {
            "repo_preflight": {
                "schema": "switchboard.repo_preflight.v1",
                "ok": True,
                "verdict": "pass",
                "repo_role": "canonical",
                "branch": f"codex/{task_id}-policy-profiles",
                "head_sha": "head-ok",
                "findings": [],
            },
        },
    }


def github_pr(task_id):
    return {
        "number": 909,
        "html_url": "https://github.com/StevenRidder/Helm/pull/909",
        "draft": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "base": {"ref": "main"},
        "head": {"ref": f"codex/{task_id}-policy-profiles", "sha": "head-ok"},
        "status_contexts": {"helm-ci/full-suite": "success"},
    }


try:
    store.init_project_registry()
    store.init_db("switchboard")
    store.init_db("helm")
    store.register_agent(AGENT, "codex", lane="SESSION", project="switchboard")
    store.register_agent(AGENT, "codex", lane="ENGINE", project="helm")

    profiles = store.get_session_policy_profiles("helm")
    ok(profiles["schema"] == store.SESSION_POLICY_PROFILE_SCHEMA and
       profiles["defaults"]["code_task_default_profile"] == "code_strict" and
       "docs_review" in profiles["profiles"],
       "project exposes named session policy profiles and Helm code default")

    agreement = store.get_working_agreement(project="helm")
    ok(agreement["session_policy_profiles"]["defaults"]["code_task_default_profile"] == "code_strict" and
       agreement["work_session_contract"]["policy_profiles"]["schema"] == store.SESSION_POLICY_PROFILE_SCHEMA,
       "working agreement exposes profile defaults and Work Session contract")

    code_task = task(
        "helm",
        "Build C++ renderer API",
        description="Implement code, tests, branch, PR, and CI for Helm runtime.",
        workstream="ENGINE",
        order=10,
    )
    missing_code = store.claim_task(
        code_task["task_id"],
        AGENT,
        actor="test",
        project="helm",
    )
    ok(missing_code["claimed"] is False and
       missing_code["reason"] == "work_session_required" and
       missing_code["work_session"]["policy_profile"] == "code_strict",
       "Helm code-like task defaults to code_strict and requires Work Session")

    docs_task = task(
        "switchboard",
        "Document policy profile rollout",
        description="policy_profile:docs_review\nReview and update docs only.",
        order=20,
    )
    docs_claim = store.claim_task(docs_task["task_id"], AGENT, actor="test",
                                  project="switchboard")
    ok(docs_claim["claimed"] is True and
       docs_claim["work_session"]["status"] == "not_required" and
       docs_claim["work_session"]["policy_profile"] == "docs_review",
       "docs_review override remains claimable without Work Session")

    docs_pre = store.pre_tool_check({
        "task_id": docs_task["task_id"],
        "agent_id": AGENT,
        "tool_name": "Edit",
        "tool_input": {"file_path": "docs/MCP.md"},
        "policy_profile": "docs_review",
    }, actor=AGENT, project="switchboard")
    ok(docs_pre["decision"] == "warn" and docs_pre["ok"] is True and
       docs_pre["policy_profile"] == "docs_review",
       "docs_review pre_tool_check warns instead of denying missing Work Session")

    strict_claim = store.claim_task(
        code_task["task_id"],
        AGENT,
        work_session=session_payload("helm", code_task["task_id"]),
        session_policy_profile="code_strict",
        actor="test",
        override_identity_risk=True,
        project="helm",
    )
    ok(strict_claim["claimed"] is True and strict_claim["work_session_id"],
       "code_strict claim accepts clean task-scoped Work Session")

    completed = store.complete_claim(
        strict_claim["claim_id"],
        evidence={
            "branch": f"codex/{code_task['task_id']}-policy-profiles",
            "head_sha": "head-ok",
            "pr_url": "https://github.com/StevenRidder/Helm/pull/909",
            "pr_number": 909,
            "tests": ["python3 test_session_policy_profiles.py"],
            "git_diff_check": "clean",
        },
        actor="test",
        project="helm",
    )
    ok(completed["completed"] is True and
       completed["work_session_gate"]["policy_profile"] == "code_strict",
       "complete_claim records the enforcing policy profile")

    store.mark_task_pr_opened(
        code_task["task_id"], 909, "https://github.com/StevenRidder/Helm/pull/909",
        f"codex/{code_task['task_id']}-policy-profiles", "head-ok",
        actor="github-webhook", project="helm")
    merge_without_session = store.merge_gate({
        "task_id": code_task["task_id"],
        "agent_id": AGENT,
        "repo": "StevenRidder/Helm",
        "target_branch": "main",
        "branch": f"codex/{code_task['task_id']}-policy-profiles",
        "head_sha": "head-ok",
        "pr_url": "https://github.com/StevenRidder/Helm/pull/909",
        "pr_number": 909,
        "github_pr": github_pr(code_task["task_id"]),
        "status_contexts": {"helm-ci/full-suite": "success"},
    }, actor="test", project="helm")
    ok(merge_without_session["ok"] is False and
       merge_without_session["policy_profile"] == "code_strict" and
       any(f["code"] == "work_session_required" for f in merge_without_session["findings"]),
       "merge_gate derives Work Session requirement from code_strict policy")

    merge_with_session = store.merge_gate({
        "task_id": code_task["task_id"],
        "agent_id": AGENT,
        "claim_id": strict_claim["claim_id"],
        "work_session_id": strict_claim["work_session_id"],
        "repo": "StevenRidder/Helm",
        "target_branch": "main",
        "branch": f"codex/{code_task['task_id']}-policy-profiles",
        "head_sha": "head-ok",
        "pr_url": "https://github.com/StevenRidder/Helm/pull/909",
        "pr_number": 909,
        "github_pr": github_pr(code_task["task_id"]),
        "status_contexts": {"helm-ci/full-suite": "success"},
    }, actor="test", project="helm")
    ok(merge_with_session["ok"] is True and
       merge_with_session["work_session_required"] is True,
       "merge_gate passes when code_strict Work Session and CI evidence are present")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
