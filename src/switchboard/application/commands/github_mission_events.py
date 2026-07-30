"""Project durable GitHub observations into the Mission Bot v4 journal.

This command is deliberately append-only.  It records communication-plane facts and
never changes mission state, selects a role, starts capacity, arms merge, or requests
human attention.
"""
from __future__ import annotations

import hashlib
import json
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
        "ready_for_review", "auto_merge_enabled", "auto_merge_disabled", "enqueued",
        "dequeued", "closed", "review_requested", "review_request_removed", "locked",
        "unlocked",
    },
    "pull_request_review": {"submitted", "edited", "dismissed"},
    "pull_request_review_comment": {"created", "edited", "deleted"},
    "pull_request_review_thread": {"resolved", "unresolved"},
    "issue_comment": {"created", "edited", "deleted"},
    "merge_group": {"checks_requested"},
    "repository_ruleset": {"created", "edited", "deleted"},
    "branch_protection_rule": {"created", "edited", "deleted"},
    "repository": {"edited"},
}


def _task_ids_from_text(value: object) -> list[str]:
    import task_id_parser

    return task_id_parser.extract_task_ids(str(value or ""))


def _pr(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    return payload.get("pull_request") or {}


def _task_ids_for_sha(repository: MissionJournalRepository, project: str, sha: str) -> list[str]:
    return repository.task_ids_for_head(sha, project=project)


def _active_task_ids(repository: MissionJournalRepository, project: str) -> list[str]:
    return repository.active_task_ids(project=project)


def _identity(event: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    repo = str((payload.get("repository") or {}).get("full_name") or "").lower()
    action = str(payload.get("action") or "")
    pr = _pr(payload)
    number = pr.get("number") or (payload.get("issue") or {}).get("number")
    head_sha = str((pr.get("head") or {}).get("sha") or "")
    external_ref = str(pr.get("html_url") or "")
    material: dict[str, Any] = {"event": event, "action": action, "repo": repo}

    if event == "status":
        head_sha = str(payload.get("sha") or "")
        material.update(
            context=payload.get("context"), state=payload.get("state"),
            target_url=payload.get("target_url"),
        )
        external_ref = str(payload.get("target_url") or "")
    elif event in {"check_run", "check_suite"}:
        item = payload.get(event) or {}
        head_sha = str(item.get("head_sha") or "")
        material.update(
            id=item.get("id"), status=item.get("status"), conclusion=item.get("conclusion"),
            name=item.get("name") or (item.get("app") or {}).get("name"),
        )
        external_ref = str(item.get("html_url") or item.get("url") or "")
    elif event == "pull_request":
        material.update(
            number=number, head_sha=head_sha, base=(pr.get("base") or {}).get("ref"),
            draft=pr.get("draft"), merged=pr.get("merged"),
        )
    elif event == "pull_request_review":
        item = payload.get("review") or {}
        material.update(
            id=item.get("id"), state=item.get("state"), commit_id=item.get("commit_id"),
            body=item.get("body"),
        )
        external_ref = str(item.get("html_url") or "")
    elif event in {"pull_request_review_comment", "issue_comment"}:
        item = payload.get("comment") or {}
        material.update(id=item.get("id"), body=item.get("body"))
        external_ref = str(item.get("html_url") or "")
    elif event == "pull_request_review_thread":
        item = payload.get("thread") or {}
        material.update(id=item.get("id"), resolved=item.get("resolved"))
    elif event == "merge_group":
        item = payload.get("merge_group") or {}
        head_sha = str(item.get("head_sha") or "")
        material.update(head_sha=head_sha, base_sha=item.get("base_sha"), head_ref=item.get("head_ref"))
    elif event in {"repository_ruleset", "branch_protection_rule", "repository"}:
        item = payload.get(event) or payload.get("changes") or {}
        material.update(id=item.get("id"), changes=payload.get("changes") or item)

    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    return {
        "repo": repo, "action": action, "pr_number": int(number) if number else None,
        "head_sha": head_sha or None, "external_ref": external_ref,
        "material": material, "fingerprint": fingerprint,
    }


def _mapped_tasks(
    event: str, payload: Mapping[str, Any], identity: Mapping[str, Any],
    *, project: str, repository: MissionJournalRepository,
) -> list[str]:
    if event in {"status", "check_run", "check_suite"}:
        return _task_ids_for_sha(repository, project, str(identity.get("head_sha") or ""))
    if event in {"repository_ruleset", "branch_protection_rule", "repository"}:
        return _active_task_ids(repository, project)

    pr = _pr(payload)
    texts: Iterable[object] = (
        pr.get("title"), pr.get("body"), (pr.get("head") or {}).get("ref"),
        (payload.get("issue") or {}).get("title"), (payload.get("issue") or {}).get("body"),
    )
    found: list[str] = []
    for text in texts:
        found.extend(_task_ids_from_text(text))
    if event == "merge_group":
        for candidate in (payload.get("merge_group") or {}).get("pull_requests") or []:
            found.extend(_task_ids_from_text(candidate.get("head_ref")))
            found.extend(_task_ids_from_text(candidate.get("title")))
    return list(dict.fromkeys(found))


def project_delivery(
    event: str, payload: Mapping[str, Any], *, project: str,
    repository: MissionJournalRepository = default_mission_journal_repository,
) -> dict[str, Any]:
    """Append one material event per mapped mission; return an audit-friendly receipt."""
    action = str(payload.get("action") or "")
    if event in _MATERIAL_ACTIONS and action not in _MATERIAL_ACTIONS[event]:
        return {"action": "ignored", "reason": "non_material_action", "event": event}
    if event not in _MATERIAL_ACTIONS and event not in {"status", "check_run", "check_suite"}:
        return {"action": "ignored", "reason": "unsupported_event", "event": event}
    if event == "issue_comment" and not (payload.get("issue") or {}).get("pull_request"):
        return {"action": "ignored", "reason": "not_a_pull_request_comment", "event": event}

    identity = _identity(event, payload)
    tasks = _mapped_tasks(
        event, payload, identity, project=project, repository=repository,
    )
    appended = []
    for task_id in tasks:
        if repository.get_item(task_id, project=project) is None:
            continue
        event_row = repository.append_event(
            task_id, project=project, event_type="github_changed",
            source_plane="communication",
            idempotency_key=f"github-material:{task_id}:{identity['fingerprint']}",
            pr_number=identity["pr_number"], head_sha=identity["head_sha"],
            external_ref=identity["external_ref"], payload=identity["material"],
        )
        appended.append({
            "task_id": task_id, "sequence": event_row["sequence"],
            "created": event_row["created"],
        })
    return {"action": "github_mission_events_projected", "event": event, "events": appended}


def append_due_observations(
    *, project: str, now: float | None = None, due_after_s: float = 300,
    repository: MissionJournalRepository = default_mission_journal_repository,
) -> dict[str, Any]:
    """Append exactly one due event for each persisted WAITING timestamp."""
    timestamp = time.time() if now is None else now
    rows = repository.waiting_items_due(
        project=project, due_before=timestamp - due_after_s,
    )
    events = []
    for row in rows:
        waited_at = float(row["updated_at"])
        event = repository.append_event(
            str(row["task_id"]), project=project, event_type="observation_due",
            source_plane="coordination",
            idempotency_key=f"observation_due:{row['task_id']}:{waited_at:.6f}",
            occurred_at=timestamp, payload={"waited_at": waited_at, "due_after_s": due_after_s},
        )
        events.append({
            "task_id": row["task_id"], "sequence": event["sequence"],
            "created": event["created"],
        })
    return {"action": "observation_due_projected", "events": events}


__all__ = ["append_due_observations", "project_delivery"]
