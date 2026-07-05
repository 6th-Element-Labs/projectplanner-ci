#!/usr/bin/env python3
"""HARDEN-28 claim-to-evidence verification regression."""
import os
import shutil
import subprocess
import tempfile

_TMP = tempfile.mkdtemp(prefix="evidence-claims-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP

import evidence_claims  # noqa: E402
import store  # noqa: E402

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def finding(report, task_id, code):
    return next(
        (item for item in report["findings"]
         if item.get("task_id") == task_id and item.get("code") == code),
        None,
    )


try:
    store.init_db(P)

    missing_comment = store.create_task(
        {"workstream_id": "HARDEN", "title": "missing comment evidence"},
        actor="test",
        project=P,
    )
    store.add_comment(
        missing_comment["task_id"],
        "codex/test",
        "Deliverable colour_authority_warnings.html is live on the review page.",
        project=P,
    )

    verified_comment = store.create_task(
        {"workstream_id": "HARDEN", "title": "verified comment evidence"},
        actor="test",
        project=P,
    )
    store.append_activity(
        "comment",
        "codex/test",
        {
            "text": "Deliverable report is in the repo.",
            "evidence_paths": ["docs/SWITCHBOARD-RUNBOOK.md"],
        },
        task_id=verified_comment["task_id"],
        project=P,
    )

    missing_completion = store.create_task(
        {"workstream_id": "HARDEN", "title": "missing completion evidence"},
        actor="test",
        project=P,
    )
    claim = store.claim_task(
        missing_completion["task_id"],
        "codex/HARDEN-28",
        actor="codex/HARDEN-28",
        project=P,
    )
    store.complete_claim(
        claim["claim_id"],
        evidence={"verification": "Generated page colour_authority_warnings.html"},
        actor="codex/HARDEN-28",
        project=P,
    )

    verified_completion = store.create_task(
        {"workstream_id": "HARDEN", "title": "verified completion evidence"},
        actor="test",
        project=P,
    )
    verified_claim = store.claim_task(
        verified_completion["task_id"],
        "codex/HARDEN-28",
        actor="codex/HARDEN-28",
        project=P,
    )
    store.complete_claim(
        verified_claim["claim_id"],
        evidence={
            "verification": "Generated report is in the repo.",
            "evidence_paths": ["docs/SWITCHBOARD-RUNBOOK.md"],
        },
        actor="codex/HARDEN-28",
        project=P,
    )

    report = store.reconcile(project=P)
    missing_comment_finding = finding(report, missing_comment["task_id"], "claim_without_evidence")
    ok(missing_comment_finding is not None, "comment artifact claim without evidence is reported")
    ok(
        missing_comment_finding["failure_class"] == "missing_data"
        and missing_comment_finding["severity"] == "medium",
        "comment missing-evidence finding is yellow/medium missing_data",
    )
    ok(
        finding(report, missing_completion["task_id"], "claim_without_evidence") is not None,
        "completion artifact claim without evidence is reported",
    )
    ok(
        finding(report, verified_comment["task_id"], "claim_evidence_missing") is None
        and finding(report, verified_comment["task_id"], "claim_without_evidence") is None,
        "comment with repo evidence path is not flagged",
    )
    ok(
        finding(report, verified_completion["task_id"], "claim_evidence_missing") is None
        and finding(report, verified_completion["task_id"], "claim_without_evidence") is None,
        "completion with repo evidence path is not flagged",
    )

    squash_merged = store.create_task(
        {"workstream_id": "BUG", "title": "squash-merged claim head disappeared"},
        actor="test",
        project=P,
    )
    squash_claim = store.claim_task(
        squash_merged["task_id"],
        "codex/BUG-26",
        actor="codex/BUG-26",
        project=P,
    )
    store.complete_claim(
        squash_claim["claim_id"],
        evidence={
            "branch": "codex/BUG-26-reconcile-squash",
            "head_sha": "0" * 40,
            "pr_url": "https://github.com/6th-Element-Labs/projectplanner/pull/91",
        },
        actor="codex/BUG-26",
        project=P,
    )
    store.mark_task_merged(
        squash_merged["task_id"],
        subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        pr_number=91,
        pr_url="https://github.com/6th-Element-Labs/projectplanner/pull/91",
        branch="codex/BUG-26-reconcile-squash",
        head_sha="0" * 40,
        actor="github-webhook",
        project=P,
    )
    squash_report = store.reconcile(project=P)
    ok(
        finding(squash_report, squash_merged["task_id"], "claim_evidence_missing") is None,
        "squash-merged Done task trusts merged_sha instead of missing pre-squash claim head",
    )

    direct = evidence_claims.evaluate_activity(
        {
            "task_id": "DIRECT-1",
            "actor": "test",
            "kind": "comment",
            "payload": {
                "text": "Artifact report is declared.",
                "evidence_urls": ["https://example.test/report"],
            },
            "created_at": 1,
        },
        os.path.dirname(os.path.abspath(__file__)),
    )
    ok(direct["status"] == "pass", "HTTP(S) evidence URL is accepted as declared evidence")

    bundle = store.audit_export(project=P)
    ok("evidence_claims" in bundle, "audit export includes claim-to-evidence reports")
    ok(
        bundle["summary"]["evidence_claim_status_counts"]["red"] >= 1
        and bundle["summary"]["evidence_claim_status_counts"]["pass"] >= 1,
        "audit export summarizes claim evidence status counts",
    )
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
