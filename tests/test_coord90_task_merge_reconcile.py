#!/usr/bin/env python3
"""COORD-90: exact-task merge reconciliation and terminal attention cleanup."""
from __future__ import annotations

import json
import os
import tempfile
import time

tmp = tempfile.mkdtemp(prefix="coord90-")
os.environ["PM_DB_PATH"] = os.path.join(tmp, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(tmp, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(tmp, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(tmp, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = tmp

from path_setup import ROOT  # noqa: E402,F401

import store  # noqa: E402
from db.connection import _conn  # noqa: E402
from switchboard.application.commands.reconcile_task_merge import execute  # noqa: E402
from switchboard.storage.repositories.attention import (  # noqa: E402
    create_attention_request_in,
)
from switchboard.storage.repositories.provenance import (  # noqa: E402
    _upsert_git_state,
    task_merge_reconcile_subject,
)

P = "switchboard"
REPO = "6th-Element-Labs/projectplanner"
PR_991_MERGE = "24921910ef2fa472d5bca4b0d5647be1f1d19940"
PR_990_MERGE = "0b781b4115df704aa21f6956c4e8b4d4b79908e3"
CURRENT_MAIN = "f" * 40
store.init_db(P)
store.set_meta("github_repo", REPO, project=P)
store.set_meta("canonical_main_sha", CURRENT_MAIN, project=P)


def make_task(status: str, pr_number: int, stored_head: str) -> dict:
    task = store.create_task(
        {"workstream_id": "COORD", "title": f"reconcile PR {pr_number}",
         "status": status},
        actor="test", project=P,
    )
    with _conn(P) as c:
        _upsert_git_state(c, task["task_id"], {
            "pr_number": pr_number,
            "pr_url": f"https://github.com/{REPO}/pull/{pr_number}",
            "branch": f"codex/{task['task_id']}-stale",
            "head_sha": stored_head,
            "evidence": {"pr_number": pr_number},
        })
    return task


blocked = make_task("Blocked", 991, "1" * 40)
not_started = make_task("Not Started", 990, "2" * 40)
unrelated = make_task("In Review", 989, "3" * 40)


def add_attention(task_id: str, suffix: str) -> str:
    with _conn(P) as c:
        result = create_attention_request_in(c, {
            "request_id": f"attention-{suffix}",
            "task_id": task_id,
            "provider": "codex",
            "provider_request_id": f"provider-{suffix}",
            "schema_version": "provider.attention.v1",
            "prompt": "obsolete after merge",
            "choices": [{"id": "continue"}],
            "idempotency_key": f"idem-{suffix}",
        }, project=P, actor="test")
    return result["request"]["request_id"]


blocked_attention = add_attention(blocked["task_id"], "blocked")
unrelated_attention = add_attention(unrelated["task_id"], "unrelated")

# A completion-provider attention wake is communication state and must be
# terminalized without invoking completion or starting a runtime.
with _conn(P) as c:
    now = time.time()
    # The known production failure shape includes an immutable execution
    # publication on an older remediation head. Fresh exact-PR merge truth must
    # repair the task without weakening repository/PR/default-branch checks.
    c.execute(
        "INSERT INTO execution_publications("
        "publication_id,project_id,task_id,execution_id,execution_generation,"
        "repo_role,repository,default_branch,base_sha,branch,head_sha,pr_number,"
        "pr_url,scm_connection_ref,context_digest,created_at,updated_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "execpub-stale-blocked", P, blocked["task_id"], "exec-stale", 1,
            "canonical", REPO, "master", "0" * 40,
            f"codex/{blocked['task_id']}-stale", "1" * 40, 991,
            f"https://github.com/{REPO}/pull/991", "scm-test",
            "sha256:test", now, now,
        ),
    )
    c.execute(
        "INSERT INTO attention_decisions("
        "decision_id, request_id, request_version, idempotency_key, decision_hash, "
        "choice_json, actor, created_at) VALUES (?,?,?,?,?,?,?,?)",
        ("decision-blocked", blocked_attention, 1, "decision-idem", "hash",
         json.dumps({"id": "continue"}), "test", now),
    )
    c.execute(
        "INSERT INTO attention_completion_wakes("
        "wake_id, project_id, request_id, decision_id, task_id, completion_run_id, "
        "state_version, head_sha, pr_number, choice_id, status, attempt_count, "
        "available_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?,?)",
        ("wake-blocked", P, blocked_attention, "decision-blocked",
         blocked["task_id"], "run-blocked", 1, "1" * 40, 991, "continue",
         "pending", now, now, now),
    )
    for wake_id, status, runner_session_id, wake_task_id in (
        ("wake-execution-pending", "pending", None, blocked["task_id"]),
        ("wake-execution-pre-runner", "claimed", None, blocked["task_id"]),
        ("wake-execution-live", "claimed", "runner-live", blocked["task_id"]),
        ("wake-execution-other-task", "pending", None, unrelated["task_id"]),
    ):
        c.execute(
            "INSERT INTO wake_intents(wake_id, source, reason, selector_json, "
            "policy_json, status, requested_at, task_id, runner_session_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (wake_id, "connect", "merge cleanup fixture", "{}", "{}", status,
             now, wake_task_id, runner_session_id),
        )
    personal_wake_id = "wake-execution-pre-runner"
    c.execute(
        "UPDATE wake_intents SET claimed_by_host=?, policy_json=? WHERE wake_id=?",
        (
            "host-personal",
            json.dumps({
                "execution_mode": "personal_agent_host",
                "require_exact_host_binding": True,
                "account_binding": {
                    "claim_id": "claim-personal",
                    "work_session_id": "ws-personal",
                },
                "execution_binding": {
                    "execution_connection_id": "connection-personal",
                    "runner_session_id": "runner-personal",
                    "host_id": "host-personal",
                    "host_principal_id": "principal-personal",
                    "agent_id": "agent-personal",
                    "source_sha": "source-personal",
                },
            }, sort_keys=True),
            personal_wake_id,
        ),
    )
    c.execute(
        "INSERT INTO personal_execution_connections("
        "execution_connection_id, wake_id, task_id, claim_id, work_session_id, "
        "runner_session_id, host_id, host_principal_id, agent_id, source_sha, "
        "status, created_at, expires_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "connection-personal", personal_wake_id, blocked["task_id"],
            "claim-personal", "ws-personal", "runner-personal", "host-personal",
            "principal-personal", "agent-personal", "source-personal", "reserved",
            now, now + 3600, now,
        ),
    )

live = {
    991: {
        "merged_at": "2026-07-27T01:00:00Z",
        "merge_commit_sha": PR_991_MERGE,
        "html_url": f"https://github.com/{REPO}/pull/991",
        "head": {"ref": "codex/COORD-85-fix", "sha": "a" * 40},
        "base": {"ref": "master", "repo": {"default_branch": "master"}},
    },
    990: {
        "merged_at": "2026-07-27T00:00:00Z",
        "merge_commit_sha": PR_990_MERGE,
        "html_url": f"https://github.com/{REPO}/pull/990",
        "head": {"ref": "codex/COORD-86-fix", "sha": "b" * 40},
        "base": {"ref": "master", "repo": {"default_branch": "master"}},
    },
}
fetches: list[tuple[str, int]] = []


def github_pr(repo: str, pr_number: int, token: str = "") -> dict | None:
    fetches.append((repo, pr_number))
    return live.get(pr_number)


store._github_pr = github_pr
store._github_token = lambda repo="": "test-token"


def reconcile(task_id: str) -> dict:
    return execute(
        task_id,
        project=P,
        actor="test/reconcile",
        load_subject=task_merge_reconcile_subject,
        canonical_repo_for=store.get_project_github_repo,
        fetch_pull_request=lambda repo, number: store._github_pr(
            repo, number, token=store._github_token(repo)),
        mark_merged=store.mark_task_merged,
    )


first = reconcile(blocked["task_id"])
second = reconcile(not_started["task_id"])
assert first["status"] == "Done", first
assert first["git_state"]["merged_sha"] == PR_991_MERGE, first
assert first["attention_cleanup"] == {
    "schema": "switchboard.task_attention_terminalization.v1",
    "task_id": blocked["task_id"],
    "reason": "canonical_merge_observed",
    "requests_cancelled": 1,
    "completion_wakes_cancelled": 1,
    "changed": True,
}, first
assert first["execution_wake_cleanup"] == {
    "schema": "switchboard.task_execution_wake_terminalization.v1",
    "task_id": blocked["task_id"],
    "reason": "canonical_merge_observed",
    "wakes_cancelled": 2,
    "execution_leases_released": 0,
    "effects_voided": 0,
    "wakes_blocked": 0,
    "changed": True,
}, first
assert second["status"] == "Done", second
assert second["git_state"]["merged_sha"] == PR_990_MERGE, second
assert fetches == [(REPO, 991), (REPO, 990)], fetches
# Reconciling historical merges must never rewind the observed default-branch tip.
assert store.get_meta("canonical_main_sha", project=P) == CURRENT_MAIN

with _conn(P) as c:
    before_replay = int(c.execute(
        "SELECT COUNT(*) FROM activity").fetchone()[0])
    unrelated_before = dict(c.execute(
        "SELECT status, version FROM attention_requests WHERE request_id=?",
        (unrelated_attention,),
    ).fetchone())
    wake = c.execute(
        "SELECT status FROM attention_completion_wakes WHERE wake_id='wake-blocked'",
    ).fetchone()
    assert wake["status"] == "cancelled", dict(wake)
    execution_wakes = {
        row["wake_id"]: row["status"] for row in c.execute(
            "SELECT wake_id, status FROM wake_intents WHERE wake_id LIKE 'wake-execution-%'"
        ).fetchall()
    }
    assert execution_wakes == {
        "wake-execution-pending": "cancelled",
        "wake-execution-pre-runner": "cancelled",
        "wake-execution-live": "claimed",
        "wake-execution-other-task": "pending",
    }, execution_wakes
    connection = c.execute(
        "SELECT status FROM personal_execution_connections "
        "WHERE execution_connection_id='connection-personal'",
    ).fetchone()
    assert connection["status"] == "cancelled", dict(connection)

replay = reconcile(blocked["task_id"])
assert replay["idempotent"] is True, replay
assert replay["attention_cleanup"]["changed"] is False, replay

with _conn(P) as c:
    after_replay = int(c.execute(
        "SELECT COUNT(*) FROM activity").fetchone()[0])
    unrelated_after = dict(c.execute(
        "SELECT status, version FROM attention_requests WHERE request_id=?",
        (unrelated_attention,),
    ).fetchone())
assert after_replay == before_replay, (before_replay, after_replay)
assert unrelated_after == unrelated_before
assert fetches == [(REPO, 991), (REPO, 990), (REPO, 991)], fetches
assert store.get_meta("canonical_main_sha", project=P) == CURRENT_MAIN

print("PASS: COORD-90 task-scoped merge reconcile is fresh, terminal, isolated, and idempotent")
