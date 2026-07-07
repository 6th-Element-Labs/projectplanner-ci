#!/usr/bin/env python3
"""CI-MIRROR-3 external CI evidence surface and gate regressions."""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="external-ci-evidence-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    store.init_project_registry()
    store.init_db(P)
    store.create_project(
        "Private Product",
        project_id="private-product",
        github_repo="6th-Element-Labs/private-product",
        actor="test",
    )
    store.set_project_repo_topology(
        project="private-product",
        public_ci_repo="6th-Element-Labs/public-ci",
        public_ci_required_status_contexts="public-ci/full-suite",
    )

    source_sha = "abcdef1234567890abcdef1234567890abcdef12"
    task = store.create_task(
        {
            "workstream_id": "CIQA",
            "title": "external CI evidence",
            "description": "policy_profile:no_repo\nSynthetic external-CI evidence fixture.",
            "exit_criteria": "external_ci_passed required before merge",
        },
        actor="test",
        project=P,
    )
    missing = store.get_task(task["task_id"], project=P)
    ok(missing["external_ci"]["required"] is True and
       missing["external_ci"]["gate"]["status"] == "blocked",
       "task detail marks required external CI as blocked while missing")

    claim = store.claim_task(task["task_id"], "codex/CIQA-proof",
                             idem_key="ciqa-proof", project=P)
    completed = store.complete_claim(
        claim["claim_id"],
        {
            "branch": "codex/CIQA-proof",
            "head_sha": source_sha,
            "external_ci_required": True,
        },
        actor="test",
        project=P,
    )
    ok(completed["status"] == "In Review" and
       completed["review_gate"]["status"] == "blocked",
       "claim completion records a blocked external CI review gate")

    wrong_sha = "bbbbbb1234567890abcdef1234567890abcdef12"
    wrong_run = store.create_external_ci_run(
        {
            "source_project": "private-product",
            "source_branch": "codex/CIQA-proof",
            "source_sha": wrong_sha,
            "workflow": "strict.yml",
            "task_id": task["task_id"],
            "claim_id": claim["claim_id"],
            "agent_id": "codex/CIQA-proof",
        },
        actor="test",
        project=P,
    )
    store.update_external_ci_run(
        wrong_run["run_id"],
        {
            "status": "success",
            "conclusion": "success",
            "run_url": "https://github.com/6th-Element-Labs/public-ci/actions/runs/41",
            "result": {"tested_public_sha": "wrongpublic", "source_sha": wrong_sha},
        },
        actor="test",
        project=P,
    )
    still_blocked = store.get_task(task["task_id"], project=P)
    ok(still_blocked["external_ci"]["passed"] is False and
       still_blocked["external_ci"]["gate"]["status"] == "blocked",
       "external CI success for a different source SHA does not satisfy the gate")

    created = store.create_external_ci_run(
        {
            "source_project": "private-product",
            "source_branch": "codex/CIQA-proof",
            "source_sha": source_sha,
            "workflow": "strict.yml",
            "task_id": task["task_id"],
            "claim_id": claim["claim_id"],
            "agent_id": "codex/CIQA-proof",
        },
        actor="test",
        project=P,
    )
    updated = store.update_external_ci_run(
        created["run_id"],
        {
            "status": "success",
            "conclusion": "success",
            "run_url": "https://github.com/6th-Element-Labs/public-ci/actions/runs/42",
            "logs_url": "https://github.com/6th-Element-Labs/public-ci/actions/runs/42/logs",
            "result": {"tested_public_sha": "public123", "source_sha": source_sha},
        },
        actor="test",
        project=P,
    )
    ok(updated["status"] == "success", "external CI success is stored")

    detail = store.get_task(task["task_id"], project=P)
    ok(detail["external_ci"]["passed"] is True and
       detail["external_ci"]["gate"]["status"] == "passed",
       "task detail exposes passed external CI evidence")
    ok(detail["external_ci"]["latest"]["run_url"].endswith("/42") and
       detail["external_ci"]["source_sha"] == source_sha,
       "task detail exposes source SHA and public run URL")
    ok(detail["external_ci"]["source_repo"] == "6th-Element-Labs/private-product" and
       detail["external_ci"]["ci_repo"] == "6th-Element-Labs/public-ci" and
       detail["external_ci"]["status_context"] == "public-ci/full-suite" and
       detail["external_ci"]["evidence_only"] is True,
       "task detail exposes source_repo/source_sha to ci_repo/run/context as verification-only")
    listed = store.list_tasks(workstream="CIQA", project=P)[0]
    ok(listed["external_ci"]["status"] == "passed",
       "board/list task rows expose compact external CI status")

    deliverable = store.create_deliverable(
        {
            "id": "ci-proof-mission",
            "title": "CI proof mission",
            "status": "in_progress",
            "end_state": "A task can cite public CI evidence without becoming Done.",
        },
        actor="test",
        project=P,
    )
    linked = store.link_task_to_deliverable(
        deliverable["id"],
        P,
        task["task_id"],
        data={
            "role": "verification",
            "proof_required": {"external_ci_passed": True},
        },
        actor="test",
        project=P,
    )
    ok(linked["task_links"][0]["task"]["external_ci"]["passed"] is True,
       "deliverable task snapshot includes external CI evidence")
    ok(linked["progress"]["external_ci_required_count"] == 1 and
       linked["progress"]["external_ci_passed_count"] == 1 and
       linked["progress"]["done_with_proof_count"] == 0,
       "mission progress counts external CI proof without counting Done")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
