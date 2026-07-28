"""Immutable Execution Context bindings for PR and merge provenance."""
from __future__ import annotations

import re
import time
import uuid
from typing import Any, Mapping

from switchboard.application.commands.execution_context import (
    ExecutionContextError,
    verify_digest,
)

_PR_URL = re.compile(r"^https://github\.com/([^/]+/[^/]+)/pull/(\d+)(?:/|$)", re.I)


class ExecutionPublicationError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message,
                "failure_class": "failed_gate", **self.details}


def _required(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ExecutionPublicationError(
            "execution_publication_missing_field",
            f"Execution publication requires {field}.", field=field)
    return result


def build_binding(*, project: str, task_id: str, execution_id: str,
                  execution_context: Mapping[str, Any],
                  branch: str, head_sha: str, pr_number: int,
                  pr_url: str) -> dict[str, Any]:
    """Validate a publish event against the signed execution authority."""
    try:
        verify_digest(execution_context)
    except ExecutionContextError as exc:
        raise ExecutionPublicationError(exc.code, exc.message, **exc.details) from exc
    context = dict(execution_context)
    repository = _required(context.get("repository"), "repository")
    default_branch = _required(context.get("default_branch"), "default_branch")
    base_sha = _required(context.get("base_sha"), "base_sha").lower()
    head_sha = _required(head_sha, "head_sha").lower()
    branch = _required(branch, "branch")
    execution_id = _required(execution_id, "execution_id")
    pr_url = _required(pr_url, "pr_url")
    match = _PR_URL.match(pr_url)
    try:
        pr_number = int(pr_number)
    except (TypeError, ValueError) as exc:
        raise ExecutionPublicationError(
            "execution_publication_pr_mismatch", "PR number must be positive.") from exc
    mismatches = {}
    expected = {
        "project_id": str(project),
        "task_id": str(task_id).upper(),
        "repository": repository.lower(),
        "pr_number": pr_number,
    }
    actual = {
        "project_id": str(context.get("project_id") or ""),
        "task_id": str(context.get("task_id") or "").upper(),
        "repository": (match.group(1).lower() if match else ""),
        "pr_number": (int(match.group(2)) if match else 0),
    }
    for field, expected_value in expected.items():
        if actual[field] != expected_value:
            mismatches[field] = {"expected": expected_value, "actual": actual[field]}
    if mismatches:
        raise ExecutionPublicationError(
            "execution_publication_context_mismatch",
            "PR publication disagrees with its Execution Context.",
            mismatches=mismatches)
    scm = dict(context.get("scm") or {})
    return {
        "schema": "switchboard.execution_publication.v1",
        "publication_id": f"execpub-{uuid.uuid4().hex[:16]}",
        "project_id": str(project),
        "task_id": str(task_id).upper(),
        "execution_id": execution_id,
        "execution_generation": int(context.get("generation") or 0),
        "repo_role": _required(context.get("repo_role"), "repo_role"),
        "repository": repository,
        "default_branch": default_branch,
        "base_sha": base_sha,
        "branch": branch,
        "head_sha": head_sha,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "scm_connection_ref": _required(
            scm.get("connection_reference"), "scm.connection_reference"),
        "context_digest": _required(context.get("digest"), "digest"),
    }


def persist_in(c, binding: Mapping[str, Any]) -> dict[str, Any]:
    now = time.time()
    value = dict(binding)
    existing = c.execute(
        "SELECT * FROM execution_publications "
        "WHERE project_id=? AND task_id=? AND execution_id=? AND head_sha=?",
        (value["project_id"], value["task_id"], value["execution_id"],
         value["head_sha"])).fetchone()
    if existing:
        current = dict(existing)
        immutable = (
            "execution_id", "execution_generation", "repo_role", "repository",
            "default_branch", "base_sha", "branch", "head_sha", "pr_number",
            "pr_url", "scm_connection_ref", "context_digest",
        )
        drift = {field: {"stored": current[field], "incoming": value[field]}
                 for field in immutable if current[field] != value[field]}
        if drift:
            raise ExecutionPublicationError(
                "execution_publication_immutable_mismatch",
                "Published execution binding is immutable.", mismatches=drift)
        return current
    c.execute(
        "INSERT INTO execution_publications("
        "publication_id,project_id,task_id,execution_id,execution_generation,"
        "repo_role,repository,default_branch,base_sha,branch,head_sha,pr_number,"
        "pr_url,scm_connection_ref,context_digest,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        tuple(value[field] for field in (
            "publication_id", "project_id", "task_id", "execution_id",
            "execution_generation", "repo_role", "repository", "default_branch",
            "base_sha", "branch", "head_sha", "pr_number", "pr_url",
            "scm_connection_ref", "context_digest")) + (now, now))
    return dict(c.execute(
        "SELECT * FROM execution_publications WHERE publication_id=?",
        (value["publication_id"],)).fetchone())


def get_for_task_in(c, project: str, task_id: str) -> dict[str, Any] | None:
    row = c.execute(
        "SELECT * FROM execution_publications WHERE project_id=? AND task_id=? "
        "ORDER BY updated_at DESC, created_at DESC LIMIT 1",
        (project, task_id)).fetchone()
    return dict(row) if row else None


def validate_event(binding: Mapping[str, Any], *, project: str, repository: str,
                   pr_number: int, branch: str = "", head_sha: str = "",
                   base_branch: str = "",
                   allow_head_advance: bool = False) -> dict[str, Any]:
    checks = {
        "project_id": (binding.get("project_id"), project),
        "repository": (str(binding.get("repository") or "").lower(),
                       str(repository or "").lower()),
        "pr_number": (int(binding.get("pr_number") or 0), int(pr_number or 0)),
    }
    if branch:
        checks["branch"] = (binding.get("branch"), branch)
    if head_sha:
        checks["head_sha"] = (str(binding.get("head_sha") or "").lower(),
                              str(head_sha).lower())
    if base_branch:
        checks["default_branch"] = (binding.get("default_branch"), base_branch)
    mismatches = {field: {"bound": values[0], "event": values[1]}
                  for field, values in checks.items() if values[0] != values[1]}
    if (
        allow_head_advance
        and set(mismatches) == {"head_sha"}
        and str(head_sha or "").strip()
    ):
        return {
            "valid": True,
            "head_advanced": True,
            "bound_head_sha": str(binding.get("head_sha") or "").lower(),
            "event_head_sha": str(head_sha).lower(),
        }
    if mismatches:
        raise ExecutionPublicationError(
            "execution_publication_event_mismatch",
            "GitHub event disagrees with the persisted Execution Context binding.",
            mismatches=mismatches)
    return {"valid": True, "head_advanced": False}
