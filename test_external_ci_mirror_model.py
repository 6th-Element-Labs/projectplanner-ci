#!/usr/bin/env python3
"""Self-contained tests for external CI mirror run model."""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="external-ci-model-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    store.init_project_registry()
    store.init_db("switchboard")
    source_project = store.create_project(
        "Private Product",
        project_id="private-product",
        github_repo="6th-Element-Labs/private-product",
        actor="test",
    )
    ok(source_project.get("created") is True,
       "source project is physically created with a GitHub repo")

    task = store.create_task({"workstream_id": "CIQA", "title": "private branch proof"},
                             actor="test", project="switchboard")
    source_sha = "abcdef1234567890abcdef1234567890abcdef12"
    created = store.create_external_ci_run(
        {
            "source_project": "private-product",
            "source_branch": "codex/CIQA-1-proof",
            "source_sha": source_sha,
            "mirror_repo": "6th-Element-Labs/public-ci",
            "workflow": "strict.yml",
            "task_id": task["task_id"],
            "claim_id": "claim-123",
            "agent_id": "codex/CIQA-1-proof",
            "request": {"reason": "actions quota exhausted in private repo"},
        },
        actor="test",
        project="switchboard",
    )
    ok(created["source_repo"] == "6th-Element-Labs/private-product",
       "source_repo defaults from the source project")
    ok(created["mirror_branch"] == f"ci/{task['task_id']}/{source_sha[:12]}",
       "mirror branch is deterministic and task/SHA scoped")
    ok(created["status"] == "requested" and created["effect_key"].startswith("effect-"),
       "create_external_ci_run records requested state and side-effect key")
    ok(created["request"]["reason"].startswith("actions quota"),
       "request metadata round-trips")

    effects = store.list_external_effects(effect_type="external_ci_mirror",
                                          task_id=task["task_id"],
                                          project="switchboard")
    ok(len(effects) == 1 and effects[0]["target"] == "6th-Element-Labs/public-ci",
       "external CI run reserves exactly one side-effect ledger row")
    ok(effects[0]["resource"] == created["mirror_branch"],
       "side-effect resource is the disposable public mirror branch")

    duplicate = store.create_external_ci_run(
        {
            "source_project": "private-product",
            "source_branch": "codex/CIQA-1-proof",
            "source_sha": source_sha,
            "mirror_repo": "6th-Element-Labs/public-ci",
            "workflow": "strict.yml",
            "task_id": task["task_id"],
            "claim_id": "claim-123",
            "agent_id": "codex/CIQA-1-proof",
            "request": {"reason": "actions quota exhausted in private repo"},
        },
        actor="test",
        project="switchboard",
    )
    ok(duplicate["run_id"] == created["run_id"] and duplicate["idempotent"] is True,
       "duplicate mirror request returns the existing run")

    updated = store.update_external_ci_run(
        created["run_id"],
        {
            "status": "success",
            "conclusion": "success",
            "run_url": "https://github.com/6th-Element-Labs/public-ci/actions/runs/42",
            "logs_url": "https://github.com/6th-Element-Labs/public-ci/actions/runs/42/logs",
            "artifacts": [{"name": "strict-log", "url": "https://example.test/log.txt"}],
            "result": {"tested_public_sha": "1234567"},
        },
        actor="test",
        project="switchboard",
    )
    ok(updated["status"] == "success" and updated["completed_at"] is not None,
       "terminal success records completion timestamp")
    ok(updated["artifacts"][0]["name"] == "strict-log" and
       updated["result"]["tested_public_sha"] == "1234567",
       "result artifacts and readback evidence round-trip")

    listed = store.list_external_ci_runs(task_id=task["task_id"], project="switchboard")
    ok(len(listed) == 1 and listed[0]["run_id"] == created["run_id"],
       "list_external_ci_runs filters by task")

    unknown_project = store.create_external_ci_run(
        {
            "source_project": "missing-project",
            "source_sha": source_sha,
            "mirror_repo": "6th-Element-Labs/public-ci",
            "workflow": "strict.yml",
        },
        project="switchboard",
    )
    ok(unknown_project.get("error", "").startswith("unknown source project"),
       "unknown source project fails closed")

    bad_sha = store.create_external_ci_run(
        {
            "source_project": "private-product",
            "source_sha": "not-a-sha",
            "mirror_repo": "6th-Element-Labs/public-ci",
            "workflow": "strict.yml",
        },
        project="switchboard",
    )
    ok(bad_sha.get("error") == "source_sha must be a 7-64 character hex Git SHA",
       "invalid source SHA fails closed")

    bad_branch = store.create_external_ci_run(
        {
            "source_project": "private-product",
            "source_sha": source_sha,
            "mirror_repo": "6th-Element-Labs/public-ci",
            "mirror_branch": "feature/not-ci",
            "workflow": "strict.yml",
        },
        project="switchboard",
    )
    ok(bad_branch.get("error") == "mirror_branch must be under ci/",
       "mirror branch is constrained to disposable ci/ namespace")

    export = store.audit_export(project="switchboard")
    ok(export["summary"]["external_ci_run_count"] == 1 and
       export["external_ci_runs"][0]["source_sha"] == source_sha,
       "audit export includes external CI mirror runs")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
