"""Append bounded GitHub observations to an existing dormant mission.

This is a communication-to-evidence adapter only. It cannot create a mission,
change mission state, choose a role, request capacity, authorize merge, stamp
Done, or ask a human.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Iterable, Mapping
from typing import Any

from switchboard.storage.repositories.mission_journal import (
    MissionJournalRepository,
    default_mission_journal_repository,
)


_MATERIAL_ACTIONS = {
    "pull_request": {
        "opened", "reopened", "synchronize", "edited", "converted_to_draft",
        "ready_for_review", "auto_merge_enabled", "auto_merge_disabled",
        "enqueued", "dequeued", "closed", "review_requested",
        "review_request_removed", "locked", "unlocked",
    },
    "pull_request_review": {"submitted", "edited", "dismissed"},
    "pull_request_review_comment": {"created", "edited", "deleted"},
    "pull_request_review_thread": {"resolved", "unresolved"},
    "issue_comment": {"created", "edited", "deleted"},
    "check_run": {"created", "rerequested", "requested_action", "completed"},
    "check_suite": {"requested", "rerequested", "completed"},
    "merge_group": {"checks_requested"},
    "repository_ruleset": {"created", "edited", "deleted"},
    "branch_protection_rule": {"created", "edited", "deleted"},
    "repository": {"edited"},
}
_STATUS_EVENTS = frozenset({"status", "check_run", "check_suite"})
_POLICY_EVENTS = frozenset({
    "repository_ruleset", "branch_protection_rule", "repository",
})
_MERGE_QUEUE_PR = re.compile(r"(?:^|/)pr-(\d+)(?:-|/|$)")


class GithubMissionProjectionError(ValueError):
    """A material provider event is missing required durable identity."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _task_ids_from_text(value: object) -> list[str]:
    import task_id_parser

    return task_id_parser.extract_task_ids(str(value or ""))


def _pr(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    return payload.get("pull_request") or {}


def _identity(event: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    repository = str((payload.get("repository") or {}).get("full_name") or "").lower()
    action = str(payload.get("action") or "")
    pull_request = _pr(payload)
    number = (
        pull_request.get("number")
        or payload.get("number")
        or (payload.get("issue") or {}).get("number")
    )
    head_sha = str((pull_request.get("head") or {}).get("sha") or "")
    external_ref = str(pull_request.get("html_url") or "")
    object_id: str | int | None = number
    status_context = ""
    status_state = ""
    review_id: str | int | None = None
    review_state = ""
    queue_entry_id: str | int | None = None
    queue_state = ""
    merge_group_sha = ""
    policy_ref = ""
    provider_status = ""
    provider_conclusion = ""
    material: dict[str, Any] = {
        "event": event,
        "action": action,
        "repository": repository,
    }

    if event == "status":
        head_sha = str(payload.get("sha") or "")
        status_context = str(payload.get("context") or "")
        status_state = str(payload.get("state") or "")
        external_ref = str(payload.get("target_url") or "")
        object_id = status_context or head_sha
        material.update(
            head_sha=head_sha,
            status_context=status_context,
            status_state=status_state,
            target_url=external_ref,
        )
    elif event in {"check_run", "check_suite"}:
        item = payload.get(event) or {}
        head_sha = str(item.get("head_sha") or "")
        object_id = item.get("id")
        status_context = str(
            item.get("name") or (item.get("app") or {}).get("name") or ""
        )
        provider_status = str(item.get("status") or "")
        provider_conclusion = str(item.get("conclusion") or "")
        status_state = provider_conclusion or provider_status
        external_ref = str(item.get("html_url") or item.get("url") or "")
        material.update(
            id=object_id,
            head_sha=head_sha,
            status_context=status_context,
            status_state=status_state,
            target_url=external_ref,
        )
    elif event == "pull_request":
        if action in {"enqueued", "dequeued"}:
            queue_entry_id = number
            queue_state = action
        material.update(
            number=number,
            head_sha=head_sha,
            base=(pull_request.get("base") or {}).get("ref"),
            draft=pull_request.get("draft"),
            merged=pull_request.get("merged"),
            title=pull_request.get("title"),
            body=pull_request.get("body"),
        )
    elif event == "pull_request_review":
        item = payload.get("review") or {}
        object_id = item.get("id")
        review_id = object_id
        review_state = str(item.get("state") or "")
        external_ref = str(item.get("html_url") or "")
        material.update(
            id=object_id,
            state=review_state,
            commit_id=item.get("commit_id"),
            body=item.get("body"),
        )
    elif event in {"pull_request_review_comment", "issue_comment"}:
        item = payload.get("comment") or {}
        object_id = item.get("id")
        external_ref = str(item.get("html_url") or "")
        material.update(id=object_id, body=item.get("body"))
    elif event == "pull_request_review_thread":
        item = payload.get("thread") or {}
        object_id = item.get("id")
        review_id = object_id
        review_state = "resolved" if item.get("resolved") else "unresolved"
        material.update(id=object_id, resolved=item.get("resolved"))
    elif event == "merge_group":
        item = payload.get("merge_group") or {}
        head_sha = str(item.get("head_sha") or "")
        merge_group_sha = head_sha
        object_id = str(item.get("head_ref") or head_sha)
        queue_entry_id = object_id
        queue_state = action
        material.update(
            head_sha=head_sha,
            base_sha=item.get("base_sha"),
            head_ref=item.get("head_ref"),
        )
    elif event in _POLICY_EVENTS:
        item = payload.get(event) or payload.get("changes") or {}
        object_id = item.get("id") if isinstance(item, Mapping) else None
        policy_ref = str(object_id or repository)
        material.update(id=object_id, changes=payload.get("changes") or item)

    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    evidence: dict[str, str | int] = {
        "material_fingerprint": fingerprint,
        "object_type": event,
    }
    for key, value in (
        ("repository", repository),
        ("event_action", action),
        ("object_id", object_id),
        ("status_context", status_context),
        ("status_state", status_state),
        ("target_url", external_ref),
        ("review_id", review_id),
        ("review_state", review_state),
        ("queue_entry_id", queue_entry_id),
        ("queue_state", queue_state),
        ("merge_group_sha", merge_group_sha),
        ("policy_ref", policy_ref),
    ):
        if isinstance(value, int) and not isinstance(value, bool):
            evidence[key] = value
        elif isinstance(value, str) and value.strip():
            evidence[key] = value
    return {
        "pr_number": number,
        "head_sha": head_sha or None,
        "external_ref": external_ref,
        "fingerprint": fingerprint,
        "payload": evidence,
        "provider_status": provider_status,
        "provider_conclusion": provider_conclusion,
    }


def _validate_identity(event: str, identity: Mapping[str, Any]) -> None:
    evidence = identity.get("payload") or {}
    missing: list[str] = []
    if not evidence.get("repository"):
        missing.append("repository.full_name")
    if event in _STATUS_EVENTS:
        if not identity.get("head_sha"):
            missing.append("head_sha")
        if event in {"check_run", "check_suite"} and not evidence.get("object_id"):
            missing.append(f"{event}.id")
        if not evidence.get("status_context"):
            missing.append("status_context")
        if not evidence.get("status_state"):
            missing.append("status_state")
        if (
            event in {"check_run", "check_suite"}
            and identity.get("provider_status") == "completed"
            and not identity.get("provider_conclusion")
        ):
            missing.append(f"{event}.conclusion")
    elif event == "merge_group":
        if not identity.get("head_sha"):
            missing.append("merge_group.head_sha")
    elif event in _POLICY_EVENTS:
        if not evidence.get("policy_ref"):
            missing.append("policy_ref")
    else:
        if not identity.get("pr_number"):
            missing.append("pull_request.number")
        if event in {
            "pull_request_review", "pull_request_review_comment",
            "pull_request_review_thread", "issue_comment",
        } and not evidence.get("object_id"):
            missing.append(f"{event}.id")
        if event == "pull_request" and not identity.get("head_sha"):
            missing.append("pull_request.head.sha")
    pr_number = identity.get("pr_number")
    if pr_number is not None and (
        isinstance(pr_number, bool)
        or not isinstance(pr_number, int)
        or pr_number <= 0
    ):
        missing.append("positive pull_request.number")
    if missing:
        raise GithubMissionProjectionError(
            "github_material_identity_missing",
            f"{event} is missing required material identity: {', '.join(missing)}",
        )


def _mapped_tasks(
    event: str,
    payload: Mapping[str, Any],
    identity: Mapping[str, Any],
    *,
    project: str,
    repository: MissionJournalRepository,
) -> list[str]:
    if event in _STATUS_EVENTS:
        return repository.task_ids_for_head(
            str(identity.get("head_sha") or ""), project=project,
        )
    if event in _POLICY_EVENTS:
        return repository.active_task_ids(project=project)

    pull_request = _pr(payload)
    texts: Iterable[object] = (
        pull_request.get("title"),
        pull_request.get("body"),
        (pull_request.get("head") or {}).get("ref"),
        (payload.get("issue") or {}).get("title"),
        (payload.get("issue") or {}).get("body"),
    )
    found: list[str] = []
    pr_number = identity.get("pr_number")
    if isinstance(pr_number, int) and not isinstance(pr_number, bool):
        found.extend(repository.task_ids_for_pr_number(pr_number, project=project))
    for text in texts:
        found.extend(_task_ids_from_text(text))
    if event == "merge_group":
        merge_group = payload.get("merge_group") or {}
        for candidate in merge_group.get("pull_requests") or []:
            found.extend(_task_ids_from_text(candidate.get("head_ref")))
            found.extend(_task_ids_from_text(candidate.get("title")))
            number = candidate.get("number")
            if isinstance(number, int) and not isinstance(number, bool):
                found.extend(repository.task_ids_for_pr_number(number, project=project))
        head_ref = str(merge_group.get("head_ref") or "")
        for match in _MERGE_QUEUE_PR.finditer(head_ref):
            found.extend(repository.task_ids_for_pr_number(
                int(match.group(1)), project=project,
            ))
    return list(dict.fromkeys(found))


def project_delivery(
    event: str,
    payload: Mapping[str, Any],
    *,
    project: str,
    repository: MissionJournalRepository = default_mission_journal_repository,
) -> dict[str, Any]:
    """Append one deduplicated fact per existing mapped mission."""
    action = str(payload.get("action") or "")
    if event in _MATERIAL_ACTIONS and action not in _MATERIAL_ACTIONS[event]:
        return {"action": "ignored", "reason": "non_material_action", "event": event}
    if event not in _MATERIAL_ACTIONS and event not in _STATUS_EVENTS:
        return {"action": "ignored", "reason": "unsupported_event", "event": event}
    if event == "issue_comment" and not (payload.get("issue") or {}).get("pull_request"):
        return {
            "action": "ignored",
            "reason": "not_a_pull_request_comment",
            "event": event,
        }

    identity = _identity(event, payload)
    _validate_identity(event, identity)
    task_ids = _mapped_tasks(
        event,
        payload,
        identity,
        project=project,
        repository=repository,
    )
    appended = []
    missing_missions = []
    for task_id in task_ids:
        if repository.get_item(task_id, project=project) is None:
            missing_missions.append(task_id)
            continue
        event_row = repository.append_event(
            task_id,
            project=project,
            event_type="github_changed",
            source_plane="communication",
            idempotency_key=f"github-material:{task_id}:{identity['fingerprint']}",
            pr_number=identity["pr_number"],
            head_sha=identity["head_sha"],
            external_ref=str(identity["external_ref"] or ""),
            payload=identity["payload"],
        )
        appended.append({
            "task_id": task_id,
            "sequence": event_row["sequence"],
            "created": event_row["created"],
        })
    if appended:
        action = "github_mission_events_projected"
        reason = ""
    elif missing_missions:
        action = "github_mission_events_inert"
        reason = "mission_not_found"
    else:
        action = "github_mission_events_inert"
        reason = "no_mapped_task"
    receipt = {
        "action": action,
        "event": event,
        "events": appended,
        "mapped_task_ids": task_ids,
        "missing_mission_task_ids": missing_missions,
    }
    if reason:
        receipt["reason"] = reason
    return receipt


def append_due_observations(
    *,
    project: str,
    now: float | None = None,
    due_after_s: float = 300,
    task_id: str = "",
    repository: MissionJournalRepository = default_mission_journal_repository,
) -> dict[str, Any]:
    """Append one passive backstop fact per persisted WAITING timestamp."""
    timestamp = time.time() if now is None else now
    rows = repository.waiting_items_due(
        project=project,
        due_before=timestamp - due_after_s,
        task_id=task_id,
    )
    events = []
    for row in rows:
        wait_started_at = float(row["updated_at"])
        event_row = repository.append_event(
            str(row["task_id"]),
            project=project,
            event_type="observation_due",
            source_plane="coordination",
            idempotency_key=(
                f"observation_due:{row['task_id']}:{wait_started_at:.6f}"
            ),
            occurred_at=timestamp,
            payload={
                "wait_started_at": wait_started_at,
                "due_at": wait_started_at + due_after_s,
            },
        )
        events.append({
            "task_id": row["task_id"],
            "sequence": event_row["sequence"],
            "created": event_row["created"],
        })
    return {"action": "observation_due_projected", "events": events}


__all__ = [
    "GithubMissionProjectionError", "append_due_observations", "project_delivery",
]
