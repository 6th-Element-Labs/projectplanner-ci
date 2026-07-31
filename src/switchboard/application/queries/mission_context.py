"""Bounded read models for the staged Mission Bot mission protocol.

These queries expose facts owned by their respective ADR-0008 planes.  They do
not infer lifecycle state, choose a role, wake capacity, or authorize merge.
"""
from __future__ import annotations

from typing import Any, Callable

from switchboard.application.commands.merge_gate import _github_commit_statuses
from switchboard.application.queries import review_verdicts
from switchboard.storage.repositories import projects as projects_repository
from switchboard.storage.repositories import provenance as provenance_repository
from switchboard.storage.repositories import runner as runner_repository
from switchboard.storage.repositories import tasks as tasks_repository
from switchboard.storage.repositories.mission_journal import (
    MissionJournalRepository,
    default_mission_journal_repository,
)


def _attempt(
    source: str,
    function: Callable[[], Any],
    missing: list[str],
    details: list[dict[str, str]],
    default: Any,
) -> Any:
    try:
        value = function()
    except Exception as exc:
        missing.append(source)
        details.append({
            "source": source,
            "error_type": type(exc).__name__,
            "message": str(exc),
        })
        return default
    if value is None:
        missing.append(source)
        details.append({
            "source": source,
            "error_type": "not_found",
            "message": f"{source} returned no record",
        })
        return default
    return value


def list_history(
    task_id: str,
    *,
    project: str,
    after_sequence: int = 0,
    limit: int = 50,
    repository: MissionJournalRepository = default_mission_journal_repository,
) -> dict[str, Any]:
    page_size = max(1, min(int(limit), 200))
    events = repository.list_events(
        task_id,
        project=project,
        after_sequence=after_sequence,
        limit=page_size + 1,
    )
    has_more = len(events) > page_size
    page = events[:page_size]
    return {
        "schema": "switchboard.mission_history.v4",
        "project": project,
        "task_id": task_id,
        "events": page,
        "next_cursor": (
            int(page[-1]["sequence"]) if page else int(after_sequence)
        ),
        "has_more": has_more,
    }


def get(
    task_id: str,
    *,
    project: str,
    recent_limit: int = 20,
    repository: MissionJournalRepository = default_mission_journal_repository,
) -> dict[str, Any]:
    """Hydrate current authority facts without deriving a lifecycle route."""
    missing: list[str] = []
    details: list[dict[str, str]] = []
    mission = _attempt(
        "mission_journal",
        lambda: repository.get_item(task_id, project=project),
        missing,
        details,
        None,
    )
    # The restored protocol is opt-in. A task without a mission row is inert:
    # do not touch GitHub, task state, review evidence, or Capacity.
    if not mission:
        return {
            "schema": "switchboard.mission_context.v4",
            "project": project,
            "task_id": task_id,
            "mission": None,
            "current": None,
            "recent_history": [],
            "history_cursor": 0,
            "context_complete": False,
            "missing_sources": ["mission_journal"],
            "missing_source_details": details or [{
                "source": "mission_journal",
                "error_type": "not_found",
                "message": "mission_journal returned no record",
            }],
        }

    task = _attempt(
        "task_store",
        lambda: tasks_repository.get_task(task_id, project=project),
        missing,
        details,
        {},
    )
    topology = _attempt(
        "repo_topology",
        lambda: projects_repository.get_project_repo_topology(project),
        missing,
        details,
        {},
    )
    canonical = dict((topology.get("roles") or {}).get("canonical") or {})
    repo = str(canonical.get("repo") or "")
    if not repo:
        missing.append("canonical_repo")
        details.append({
            "source": "canonical_repo",
            "error_type": "not_configured",
            "message": "canonical repository is not configured",
        })

    git_state = dict(task.get("git_state") or {})
    stored_head = str(git_state.get("head_sha") or "")
    head_sha = stored_head
    pr_number = int(git_state.get("pr_number") or 0)
    pr_url = str(git_state.get("pr_url") or "")
    github: dict[str, Any] = {
        "repo": repo or None,
        "pr": ({
            "number": pr_number or None,
            "url": pr_url or None,
            "head_sha": stored_head or None,
        } if pr_number or pr_url else None),
        "required_contexts": [],
    }
    if pr_number and repo:
        token = _attempt(
            "github_token",
            lambda: provenance_repository._github_token(repo),
            missing,
            details,
            "",
        )
        live_pr = _attempt(
            "github_live_pr",
            lambda: provenance_repository._github_pr(repo, pr_number, token=token),
            missing,
            details,
            None,
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
                "auto_merge_armed": bool(live_pr.get("auto_merge")),
            }
            statuses = _attempt(
                "github_commit_statuses",
                lambda: _github_commit_statuses(repo, head_sha, token=token),
                missing,
                details,
                [],
            )
            required_names = list(
                ((task.get("external_ci") or {}).get("required_status_contexts") or [])
                or canonical.get("required_status_contexts")
                or []
            )
            by_name = {
                str(status.get("context") or ""): status for status in statuses
            }
            github["required_contexts"] = [{
                "name": name,
                "state": (by_name.get(name) or {}).get("state"),
                "target_url": (by_name.get(name) or {}).get("target_url"),
            } for name in required_names]
            missing_contexts = [name for name in required_names if name not in by_name]
            if missing_contexts:
                missing.append("github_required_contexts")
                details.append({
                    "source": "github_required_contexts",
                    "error_type": "not_found",
                    "message": "missing exact-head contexts: " + ", ".join(missing_contexts),
                })
    elif pr_url or pr_number:
        missing.append("github_live_pr")
        details.append({
            "source": "github_live_pr",
            "error_type": "identity_incomplete",
            "message": "live PR hydration requires canonical repo and PR number",
        })

    runners = _attempt(
        "runner_sessions",
        lambda: runner_repository.list_runner_sessions(
            task_id=task_id, project=project,
        ),
        missing,
        details,
        [],
    )
    live_runners = [
        row for row in runners
        if str(row.get("status") or "").lower() in {"running", "stopping"}
        and not row.get("stale")
    ]
    runner_identities = [{
        "runner_session_id": row.get("runner_session_id"),
        "host_id": row.get("host_id"),
        "agent_id": row.get("agent_id"),
        "runtime": row.get("runtime"),
        "status": row.get("status"),
        "execution": row.get("execution"),
    } for row in live_runners]
    verdict = _attempt(
        "switchboard_review",
        lambda: review_verdicts.get_for(
            task_id, project=project, head_sha=head_sha,
        ),
        missing,
        details,
        None,
    ) if head_sha else None
    findings = _attempt(
        "review_findings",
        lambda: review_verdicts.list_findings_for(
            task_id, project=project, head_sha=head_sha, state="open",
        ),
        missing,
        details,
        [],
    ) if head_sha else []
    terminal = None
    if git_state.get("merged_sha"):
        terminal = {
            "kind": git_state.get("provenance_type") or "github_merge",
            "ref": git_state.get("merged_sha"),
        }
    recent_size = max(1, min(int(recent_limit), 50))
    latest = int(mission.get("latest_sequence") or 0)
    recent = repository.list_events(
        task_id,
        project=project,
        after_sequence=max(0, latest - recent_size),
        limit=recent_size,
    )
    unique_missing = list(dict.fromkeys(missing))
    return {
        "schema": "switchboard.mission_context.v4",
        "project": project,
        "task_id": task_id,
        "mission": mission,
        "current": {
            "task_status": task.get("status"),
            "dependency_state": task.get("dependency_state"),
            "github": github,
            "external_ci": task.get("external_ci") or None,
            "switchboard_review": verdict,
            "open_findings": findings,
            "runner": {
                "live": bool(live_runners),
                "sessions": runner_identities,
            },
            "terminal_provenance": terminal,
        },
        "recent_history": recent,
        "history_cursor": latest,
        "context_complete": not unique_missing,
        "missing_sources": unique_missing,
        "missing_source_details": details,
    }
