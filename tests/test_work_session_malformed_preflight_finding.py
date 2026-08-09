"""A malformed persisted preflight finding stays visible instead of crashing reads."""

from __future__ import annotations

from path_setup import ROOT  # noqa: F401

from switchboard.storage.repositories.work_sessions import _work_session_health


def test_string_preflight_finding_becomes_explicit_blocker() -> None:
    health = _work_session_health({
        "work_session_id": "worksession-malformed",
        "project_id": "maxwell",
        "task_id": "REPORT-17",
        "agent_id": "agent/codex/report-17",
        "status": "active",
        "dirty_status": "clean",
        "conflict_marker_count": 0,
        "storage_mode": "worktree",
        "worktree_path": "/tmp/report-17",
        "hygiene": {
            "repo_preflight": {
                "ok": False,
                "verdict": "deny",
                "findings": ["repository materialization mismatch"],
            },
        },
    }, now=123.0)

    malformed = next(
        finding for finding in health["findings"]
        if finding["code"] == "malformed_repo_preflight_finding"
    )
    assert health["status"] == "unsafe"
    assert malformed["blocking"] is True
    assert malformed["failure_class"] == "invalid_input"
    assert malformed["message"] == "repository materialization mismatch"

