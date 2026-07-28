#!/usr/bin/env python3
"""BUG-214: authenticated remediation publication advances one PR from A to B."""
from __future__ import annotations

import os
import tempfile

tmp = tempfile.mkdtemp(prefix="bug214-head-advance-")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(tmp, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(tmp, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = os.path.join(tmp, "projects")

from path_setup import ROOT  # noqa: E402,F401

import store  # noqa: E402

P = "switchboard"
HEAD_A = "a" * 40
HEAD_B = "b" * 40
MERGED_SHA = "c" * 40
PR_URL = "https://github.com/6th-Element-Labs/projectplanner/pull/214"
BRANCH = "codex/BUG-214-remediation"

store.init_db(P)
task = store.create_task(
    {
        "task_id": "BUG-214-REPRO",
        "workstream_id": "BUG",
        "title": "advance remediation head",
        "status": "Not Started",
        "ui_impact": "no",
    },
    actor="bug214-test",
    project=P,
)
store.mark_task_pr_opened(
    task["task_id"], 214, PR_URL, BRANCH, HEAD_A,
    actor="github-webhook", project=P,
)

with store._conn(P) as c:
    publication_a = {
        "execution_id": "exec-implementation-a",
        "execution_generation": 1,
        "head_sha": HEAD_A,
        "pr_number": 214,
        "pr_url": PR_URL,
    }
    store._upsert_git_state(c, task["task_id"], {
        "evidence": {"execution_publication": publication_a},
    })
    current = store._load_git_state(c, task["task_id"])
    publication = {
        "execution_id": "exec-remediation-b",
        "execution_generation": 2,
        "head_sha": HEAD_B,
        "pr_number": 214,
        "pr_url": PR_URL,
    }
    evidence = {
        "branch": BRANCH,
        "head_sha": HEAD_B,
        "pr_number": 214,
        "pr_url": PR_URL,
        "execution_publication": publication,
    }
    updates = store._preserve_provider_pr_evidence(
        current,
        {**evidence, "evidence": evidence},
        evidence,
        execution_publication=publication,
    )
    advanced = store._upsert_git_state(c, task["task_id"], updates)

assert advanced["head_sha"] == HEAD_B, advanced
assert advanced["pr_number"] == 214
superseded = advanced["evidence"]["provider_evidence_superseded"]
assert superseded["previous"]["head_sha"] == HEAD_A
assert superseded["execution_id"] == "exec-remediation-b"
assert "provider_evidence_preserved" not in advanced["evidence"]

# Canonical merge provenance is terminal authority. A late generation-1
# completion must neither regress B nor rewrite any terminal proof.
with store._conn(P) as c:
    merged = store._upsert_git_state(c, task["task_id"], {
        "merged_sha": MERGED_SHA,
        "merged_at": 214.0,
        "in_main_content": True,
    })
    late_a_evidence = {
        "branch": BRANCH,
        "head_sha": HEAD_A,
        "pr_number": 214,
        "pr_url": PR_URL,
        "merged_sha": "d" * 40,
        "merged_at": 999.0,
    }
    late_updates = store._preserve_provider_pr_evidence(
        merged,
        {
            **late_a_evidence,
            "in_main_content": False,
            "evidence": late_a_evidence,
        },
        late_a_evidence,
        execution_publication=publication_a,
    )
    after_late_a = store._upsert_git_state(
        c, task["task_id"], late_updates)

assert after_late_a["head_sha"] == HEAD_B, after_late_a
assert after_late_a["merged_sha"] == MERGED_SHA
assert after_late_a["merged_at"] == 214.0
assert after_late_a["in_main_content"] is True
assert after_late_a["provenance_type"] == "github_pr_merged"
assert (
    after_late_a["evidence"]["execution_publication"]["execution_generation"]
    == 2
)
late = after_late_a["evidence"]["execution_publication_superseded"]
assert late["late_execution_generation"] == 1
assert late["current_execution_generation"] == 2

# Exact-head review now fences to B. A is historical and must be rejected.
review_b = store.record_review_verdict(
    {
        "task_id": task["task_id"],
        "pr_url": PR_URL,
        "head_sha": HEAD_B,
        "reviewer_principal": "bug214-reviewer",
        "status": "pass",
        "findings": [],
    },
    actor="bug214-reviewer",
    principal_id="principal-bug214-reviewer",
    project=P,
)
assert review_b["created"] is True, review_b

try:
    store.record_review_verdict(
        {
            "task_id": task["task_id"],
            "pr_url": PR_URL,
            "head_sha": HEAD_A,
            "reviewer_principal": "bug214-reviewer",
            "status": "pass",
            "findings": [],
        },
        actor="bug214-reviewer",
        principal_id="principal-bug214-reviewer",
        project=P,
    )
except Exception as exc:
    assert getattr(exc, "code", "") == "stale_review_head", exc
else:
    raise AssertionError("old head A review unexpectedly remained current")

print("BUG-214 remediation head advancement tests passed")
