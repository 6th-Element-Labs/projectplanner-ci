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
        {
            "workstream_id": "BUG",
            "title": "squash-merged claim head disappeared",
            "description": "policy_profile:no_repo\nSynthetic reconcile fixture for historical PR evidence.",
        },
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

    # BUG-117: full reconcile backfills a merged PR to Done in the same pass. Evidence
    # claim checks must not emit claim_evidence_missing for the unfetched PR head after
    # that successful merge backfill (control-plane clones often lack task branches).
    simultaneous = store.create_task(
        {
            "workstream_id": "BUG",
            "title": "simultaneous merge backfill vs claim evidence",
            "description": "policy_profile:no_repo\nBUG-117 fixture: In Review claim before reconcile backfill.",
        },
        actor="test",
        project=P,
    )
    simultaneous_claim = store.claim_task(
        simultaneous["task_id"],
        "codex/BUG-117",
        actor="codex/BUG-117",
        project=P,
    )
    # Synthetic SHA absent from the local object DB (control-plane miss).
    unreachable_head = "0" * 40
    simultaneous_pr_url = (
        "https://github.com/6th-Element-Labs/projectplanner/pull/690"
    )
    store.complete_claim(
        simultaneous_claim["claim_id"],
        evidence={
            "branch": "codex/BUG-116-direct-cli-intake",
            "head_sha": unreachable_head,
            "pr_url": simultaneous_pr_url,
        },
        actor="codex/BUG-117",
        project=P,
    )
    merge_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    store.set_project_github_repo("6th-Element-Labs/projectplanner", project=P)
    original_fetch = store._fetch_github_prs
    original_token = store._github_token
    original_orphan = getattr(store, "_orphan_merge_discovery_findings", None)

    def fake_fetch_prs(pr_keys, token=""):
        out = {}
        for repo, pr_number in pr_keys:
            if int(pr_number) == 690:
                out[(repo, 690)] = {
                    "merged_at": "2026-07-20T12:00:00Z",
                    "merge_commit_sha": merge_sha,
                    "html_url": simultaneous_pr_url,
                    "base": {"ref": "master", "repo": {"default_branch": "master"}},
                    "head": {
                        "ref": "codex/BUG-116-direct-cli-intake",
                        "sha": unreachable_head,
                    },
                }
        return out, {"github_prs_fetch": "mocked"}

    store._fetch_github_prs = fake_fetch_prs
    store._github_token = lambda: "tok"
    # Keep this fixture focused on the recorded-PR backfill path.
    store._orphan_merge_discovery_findings = (
        lambda *a, **k: ([], [], {"orphan_merge_discovery": "skipped_test"})
    )
    try:
        simultaneous_report = store.reconcile(project=P)
    finally:
        store._fetch_github_prs = original_fetch
        store._github_token = original_token
        if original_orphan is not None:
            store._orphan_merge_discovery_findings = original_orphan
    ok(
        any(
            item.get("task_id") == simultaneous["task_id"]
            for item in (simultaneous_report.get("backfilled") or [])
        ),
        "full reconcile backfills the merged PR to Done",
    )
    ok(
        finding(simultaneous_report, simultaneous["task_id"], "claim_evidence_missing")
        is None,
        "merged-PR backfill does not emit false claim_evidence_missing for unfetched PR head",
    )

    # Remote-reachable PR head (ls-remote) must pass even when the local clone lacks the object.
    remote_branch = "codex/BUG-117-remote-reachable"
    remote_sha = "abcdef0123456789abcdef0123456789abcdef01"
    real_run_remote = evidence_claims.subprocess.run

    def remote_aware_run(*args, **kwargs):
        argv = list(args[0]) if args else []
        joined = " ".join(argv)
        if "--batch-check=%(objectname) %(objecttype)" in joined:
            lines = []
            for expression in (kwargs.get("input") or "").splitlines():
                ref = expression.replace("^{commit}", "").strip()
                if ref == remote_sha:
                    lines.append(f"{ref} missing")
                elif ref:
                    lines.append(f"{ref} commit")
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="\n".join(lines) + "\n", stderr=""
            )
        if "cat-file" in argv and remote_sha in joined:
            return subprocess.CompletedProcess(args=argv, returncode=1, stdout="", stderr="")
        if argv[:2] == ["git", "ls-remote"] or (len(argv) >= 2 and argv[0] == "git" and "ls-remote" in argv):
            return subprocess.CompletedProcess(
                args=argv,
                returncode=0,
                stdout=f"{remote_sha}\trefs/heads/{remote_branch}\n",
                stderr="",
            )
        return real_run_remote(*args, **kwargs)

    evidence_claims.subprocess.run = remote_aware_run
    try:
        remote_report = evidence_claims.evaluate_activity(
            {
                "task_id": "BUG-117-REMOTE",
                "actor": "test",
                "kind": "task.claim.completed",
                "payload": {
                    "evidence": {
                        "branch": remote_branch,
                        "head_sha": remote_sha,
                        "pr_url": simultaneous_pr_url,
                    }
                },
                "created_at": 1,
            },
            os.path.dirname(os.path.abspath(__file__)),
        )
    finally:
        evidence_claims.subprocess.run = real_run_remote
    ok(
        remote_report.get("status") == "pass",
        "claim head SHA reachable on origin via ls-remote is accepted without local object",
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

    real_run = evidence_claims.subprocess.run
    batch_calls = []

    def counting_run(*args, **kwargs):
        if "--batch-check=%(objectname) %(objecttype)" in args[0]:
            batch_calls.append(args[0])
        return real_run(*args, **kwargs)

    evidence_claims.subprocess.run = counting_run
    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        batch_reports = evidence_claims.evaluate_activities([
            {"task_id": "BATCH-1", "actor": "test", "kind": "comment",
             "payload": {"text": "Artifact report", "evidence_refs": [head]}},
            {"task_id": "BATCH-2", "actor": "test", "kind": "comment",
             "payload": {"text": "Artifact report", "evidence_refs": [head, "0" * 40]}},
        ], os.path.dirname(os.path.abspath(__file__)))
    finally:
        evidence_claims.subprocess.run = real_run
    ok(len(batch_calls) == 1, "multiple historical refs use one git cat-file batch")
    ok(batch_reports[0]["status"] == "pass" and batch_reports[1]["status"] == "red",
       "batched ref checks preserve reachable and missing evidence verdicts")

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
