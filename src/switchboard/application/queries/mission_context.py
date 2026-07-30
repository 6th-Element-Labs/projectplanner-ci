"""Bounded Mission Bot v4 context and history read models."""
from __future__ import annotations

from typing import Any, Callable

from switchboard.application.commands.merge_gate import _github_commit_statuses
from switchboard.application.queries import review_verdicts
from switchboard.storage.repositories import provenance as provenance_repository
from switchboard.storage.repositories import runner as runner_repository
from switchboard.storage.repositories import tasks as tasks_repository
from switchboard.storage.repositories.mission_journal import (
    MissionJournalRepository,
    default_mission_journal_repository,
)


def _attempt(source: str, function: Callable[[], Any],
             missing: list[str], default: Any) -> Any:
    try:
        value = function()
    except Exception:
        missing.append(source)
        return default
    if value is None:
        missing.append(source)
        return default
    return value


def list_history(
    task_id: str, *, project: str, after_sequence: int = 0, limit: int = 50,
    repository: MissionJournalRepository = default_mission_journal_repository,
) -> dict[str, Any]:
    events = repository.list_events(
        task_id, project=project, after_sequence=after_sequence, limit=limit,
    )
    return {
        "schema": "switchboard.mission_history.v4",
        "project": project,
        "task_id": task_id,
        "events": events,
        "next_cursor": events[-1]["sequence"] if events else int(after_sequence),
        "has_more": len(events) == max(1, min(int(limit), 200)),
    }


def get(
    task_id: str, *, project: str, recent_limit: int = 20,
    repository: MissionJournalRepository = default_mission_journal_repository,
) -> dict[str, Any]:
    """Hydrate authoritative current facts without deriving a route."""
    missing: list[str] = []
    mission = _attempt(
        "mission_journal", lambda: repository.get_item(task_id, project=project),
        missing, {},
    )
    task = _attempt(
        "task_store",
        lambda: tasks_repository.get_task(task_id, project=project),
        missing,
        {},
    )
    git_state = dict(task.get("git_state") or {})
    head_sha = str(git_state.get("head_sha") or "")
    pr_url = str(git_state.get("pr_url") or "")
    if not pr_url:
        missing.append("github_pr")
    github: dict[str, Any] = {
        "repo": ((task.get("project_context") or {}).get("repo_role_guide") or {})
                .get("done_authority", {}).get("repo"),
        "pr": {
            "number": git_state.get("pr_number"),
            "url": pr_url or None,
            "head_sha": head_sha or None,
        } if pr_url else None,
        "required_context": None,
    }
    if pr_url:
        repo = str(github.get("repo") or "")
        pr_number = int(git_state.get("pr_number") or 0)
        token = _attempt(
            "github_token",
            lambda: provenance_repository._github_token(repo),
            missing, "",
        )
        live_pr = _attempt(
            "github_live_pr",
            lambda: provenance_repository._github_pr(repo, pr_number, token=token),
            missing, None,
        )
        if live_pr:
            head_sha = str((live_pr.get("head") or {}).get("sha") or "")
            github["pr"] = {
                "number": live_pr.get("number"),
                "url": live_pr.get("html_url"),
                "state": str(live_pr.get("state") or "").upper() or None,
                "is_draft": live_pr.get("draft"),
                "head_sha": head_sha or None,
                "base_branch": (live_pr.get("base") or {}).get("ref"),
                "base_sha": (live_pr.get("base") or {}).get("sha"),
                "mergeable": live_pr.get("mergeable"),
                "merge_state_status": live_pr.get("mergeable_state"),
                "review_decision": None,
                "auto_merge_armed": bool(live_pr.get("auto_merge")),
                "queue": live_pr.get("mergeQueueEntry"),
            }
            statuses = _attempt(
                "github_required_context",
                lambda: _github_commit_statuses(repo, head_sha, token=token),
                missing, [],
            )
            required_names = list(
                ((task.get("external_ci") or {}).get("required_status_contexts") or [])
            )
            required_name = required_names[0] if required_names else ""
            required = next(
                (status for status in statuses if status.get("context") == required_name),
                None,
            ) if required_name else None
            github["required_context"] = {
                "name": required_name,
                "state": (required or {}).get("state"),
                "target_url": (required or {}).get("target_url"),
            } if required_name else None
            if required_name and required is None:
                missing.append("github_required_context")
    runners = _attempt(
        "runner_sessions",
        lambda: runner_repository.list_runner_sessions(task_id=task_id, project=project),
        missing, [],
    )
    live_runners = [
        row for row in runners
        if row.get("status") in {"running", "stopping"} and not row.get("stale")
    ]
    verdict = _attempt(
        "switchboard_review",
        lambda: review_verdicts.get_for(task_id, project=project, head_sha=head_sha),
        missing, None,
    ) if head_sha else None
    findings = _attempt(
        "review_findings",
        lambda: review_verdicts.list_findings_for(
            task_id, project=project, head_sha=head_sha, state="open",
        ),
        missing, [],
    ) if head_sha else []
    external_ci = dict(task.get("external_ci") or {})
    terminal = (
        {"kind": git_state.get("provenance_type"), "ref": git_state.get("merged_sha")}
        if git_state.get("merged_sha") else None
    )
    recent = repository.list_events(
        task_id, project=project, after_sequence=max(
            0, int(mission.get("latest_sequence") or 0) - max(1, min(recent_limit, 50))
        ), limit=max(1, min(recent_limit, 50)),
    ) if mission else []
    missing = list(dict.fromkeys(missing))
    return {
        "schema": "switchboard.mission_context.v4",
        "project": project,
        "task_id": task_id,
        "mission": mission or None,
        "current": {
            "task_status": task.get("status"),
            "dependencies_satisfied": bool(
                (task.get("dependency_state") or {}).get("satisfied", False)
            ),
            "github": github,
            "external_ci": external_ci or None,
            "switchboard_review": verdict,
            "open_findings": findings,
            "runner": {
                "live": bool(live_runners),
                "execution_id": (
                    ((live_runners[0].get("execution") or {}).get("execution_id"))
                    if live_runners else None
                ),
            },
            "terminal_provenance": terminal,
        },
        "recent_history": recent,
        "history_cursor": int(mission.get("latest_sequence") or 0),
        "context_complete": not missing,
        "missing_sources": missing,
    }
