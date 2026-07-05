#!/usr/bin/env python3
"""REPO-4 public mirror publication evidence regressions."""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="publication-evidence-")
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
        public_repo="6th-Element-Labs/private-product-public",
        public_publish_scripts="scripts/publish-public-mirror.sh",
    )

    source_sha = "abcdef1234567890abcdef1234567890abcdef12"
    public_sha = "1234567890abcdef1234567890abcdef12345678"
    task = store.create_task(
        {
            "workstream_id": "PUBLISH",
            "title": "public mirror package",
            "exit_criteria": "publication_evidence required before release",
        },
        actor="test",
        project=P,
    )
    missing = store.get_task(task["task_id"], project=P)
    ok(missing["publication"]["required"] is True and
       missing["publication"]["gate"]["status"] == "blocked",
       "task detail marks required publication evidence as blocked while missing")

    claim = store.claim_task(task["task_id"], "codex/PUBLISH-proof",
                             idem_key="publish-proof", project=P)
    completed = store.complete_claim(
        claim["claim_id"],
        {
            "branch": "codex/PUBLISH-proof",
            "head_sha": source_sha,
            "publication_required": True,
        },
        actor="test",
        project=P,
    )
    ok(completed["status"] == "In Review" and
       completed["publication"]["gate"]["status"] == "blocked",
       "claim completion records a blocked publication review gate")

    stale_proof = store.create_publication_evidence(
        {
            "source_project": "private-product",
            "source_sha": "bbbbbb1234567890abcdef1234567890abcdef12",
            "public_ref": "refs/heads/main",
            "public_sha": public_sha,
            "guard_status": "passed",
            "task_id": task["task_id"],
            "claim_id": claim["claim_id"],
            "agent_id": "codex/PUBLISH-proof",
        },
        actor="test",
        project=P,
    )
    ok(stale_proof["public_repo"] == "6th-Element-Labs/private-product-public" and
       stale_proof["script"] == "scripts/publish-public-mirror.sh",
       "publication evidence defaults public repo and script from repo_topology")
    still_blocked = store.get_task(task["task_id"], project=P)
    ok(still_blocked["publication"]["status"] == "stale" and
       still_blocked["publication"]["passed"] is False,
       "publication proof for a different source SHA is stale, not passing")

    proof = store.create_publication_evidence(
        {
            "source_project": "private-product",
            "source_sha": source_sha,
            "public_ref": "refs/heads/main",
            "public_sha": public_sha,
            "public_tag": "v0.1.0",
            "guard_status": "passed",
            "guard": {"leak_check": "passed", "runtime_guard": "passed"},
            "artifact_url": "https://example.test/public-mirror.log",
            "task_id": task["task_id"],
            "claim_id": claim["claim_id"],
            "agent_id": "codex/PUBLISH-proof",
        },
        actor="test",
        project=P,
    )
    ok(proof["guard_status"] == "passed" and
       proof["guard"]["leak_check"] == "passed",
       "publication evidence records guard/leak result")

    detail = store.get_task(task["task_id"], project=P)
    ok(detail["status"] == "In Review",
       "publication evidence does not mark code work Done")
    ok(detail["publication"]["passed"] is True and
       detail["publication"]["gate"]["status"] == "passed",
       "task detail exposes passed publication evidence")
    ok(detail["publication"]["source_sha"] == source_sha and
       detail["publication"]["public_repo"] == "6th-Element-Labs/private-product-public" and
       detail["publication"]["public_sha"] == public_sha and
       detail["publication"]["artifact_url"].endswith("public-mirror.log"),
       "task detail exposes source/public repo/ref/SHA/artifact proof")

    wrong_public_repo = store.create_publication_evidence(
        {
            "source_project": "private-product",
            "source_sha": source_sha,
            "public_repo": "6th-Element-Labs/wrong-public",
            "public_ref": "refs/heads/main",
            "guard_status": "passed",
        },
        actor="test",
        project=P,
    )
    ok(wrong_public_repo.get("error") ==
       "public_repo must match repo_topology.roles.public.repo",
       "wrong public mirror repo fails closed when topology declares the role")

    deliverable = store.create_deliverable(
        {
            "id": "publish-mission",
            "title": "Publish mission",
            "status": "in_progress",
        },
        actor="test",
        project=P,
    )
    linked = store.link_task_to_deliverable(
        deliverable["id"],
        P,
        task["task_id"],
        data={"role": "release", "proof_required": {"publication_evidence": True}},
        actor="test",
        project=P,
    )
    ok(linked["task_links"][0]["task"]["publication"]["passed"] is True,
       "deliverable task snapshot includes publication evidence")
    ok(linked["progress"]["publication_required_count"] == 1 and
       linked["progress"]["publication_passed_count"] == 1 and
       linked["progress"]["done_with_proof_count"] == 0,
       "mission progress counts publication proof without counting Done")

    stale_task = store.create_task(
        {"workstream_id": "PUBLISH", "title": "stale publication drift"},
        actor="test",
        project=P,
    )
    stale_claim = store.claim_task(stale_task["task_id"], "codex/PUBLISH-stale",
                                  idem_key="publish-stale", project=P)
    stale_source = "cccccc1234567890abcdef1234567890abcdef12"
    store.complete_claim(
        stale_claim["claim_id"],
        {"branch": "codex/PUBLISH-stale", "head_sha": stale_source},
        actor="test",
        project=P,
    )
    store.create_publication_evidence(
        {
            "source_project": "private-product",
            "source_sha": "dddddd1234567890abcdef1234567890abcdef12",
            "public_ref": "refs/heads/main",
            "guard_status": "passed",
            "task_id": stale_task["task_id"],
        },
        actor="test",
        project=P,
    )
    report = store.reconcile(project=P)
    ok(any(f["code"] == "publish_drift_stale_public_mirror" and
           f["task_id"] == stale_task["task_id"] and
           f["failure_class"] == "stale_branch"
           for f in report["findings"]),
       "reconcile reports stale public mirror as publish drift")
    ok(not any(f["task_id"] == stale_task["task_id"] and
               f["code"] in {"merged_sha_not_found", "merged_sha_not_on_canonical_main"}
               for f in report["findings"]),
       "stale public mirror is not reported as merge drift")

    export = store.audit_export(project=P)
    ok(export["summary"]["publication_evidence_count"] == 3 and
       len(export["publication_evidence"]) == 3,
       "audit export includes publication evidence")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
