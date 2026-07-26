"""GitHub adapter for project-bound completion-PR readiness proof."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from switchboard.application.pr_ready import (
    pr_number_from_evidence,
    pr_repo_from_evidence,
)
from switchboard.storage.repositories import projects

RepositoryResolver = Callable[[str], str]
TokenResolver = Callable[[str], str]
GitHubTransport = Callable[..., tuple[int, Mapping[str, Any]]]


def _token_for_project(project: str) -> str:
    suffix = "".join(
        character if character.isalnum() else "_"
        for character in str(project or "").upper()
    )
    project_key = f"PM_GITHUB_TOKEN_{suffix}" if suffix else ""
    return str(
        (os.environ.get(project_key) if project_key else "")
        or os.environ.get("PM_GITHUB_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or os.environ.get("SWITCHBOARD_CI_GITHUB_TOKEN")
        or ""
    ).strip()


def _request(
    method: str,
    url: str,
    *,
    token: str,
    payload: Mapping[str, Any] | None = None,
) -> tuple[int, Mapping[str, Any]]:
    body = json.dumps(dict(payload)).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read().decode()
            decoded = json.loads(raw) if raw else {}
            return int(getattr(response, "status", 0) or response.getcode()), (
                decoded if isinstance(decoded, Mapping) else {}
            )
    except urllib.error.HTTPError as exc:
        try:
            decoded = json.loads(exc.read().decode())
        except (OSError, ValueError, json.JSONDecodeError):
            decoded = {}
        return int(exc.code or 0), decoded if isinstance(decoded, Mapping) else {}
    except (OSError, TimeoutError, urllib.error.URLError):
        return 0, {}


def _result(
    number: int,
    *,
    status: str,
    repository: str = "",
    is_draft: bool | None = None,
    message: str = "",
    error_code: str = "",
    failure_class: str = "",
) -> dict[str, Any]:
    return {
        "schema": "switchboard.pr_ready.v1",
        "pr_number": number or None,
        "repository": repository or None,
        "status": status,
        "is_draft": is_draft,
        "message": message,
        **({"error_code": error_code} if error_code else {}),
        **({"failure_class": failure_class} if failure_class else {}),
    }


class GitHubPullRequestReadyAdapter:
    """Mark a canonical-project GitHub PR ready, then re-read provider truth."""

    def __init__(
        self,
        *,
        repository_for_project: RepositoryResolver = projects.get_project_github_repo,
        token_for_project: TokenResolver = _token_for_project,
        transport: GitHubTransport = _request,
    ) -> None:
        self._repository_for_project = repository_for_project
        self._token_for_project = token_for_project
        self._transport = transport

    def __call__(
        self,
        evidence: Any,
        *,
        project: str,
        actor: str,
    ) -> dict[str, Any]:
        del actor  # authorization is enforced before the composition edge calls us
        number = pr_number_from_evidence(evidence)
        if number <= 0:
            return _result(number, status="skipped_no_pr")

        repository = str(self._repository_for_project(project) or "").strip()
        if not repository:
            return _result(
                number,
                status="error",
                error_code="canonical_repository_unavailable",
                failure_class="missing_data",
                message="The selected project has no canonical SCM repository.",
            )
        evidence_repo = pr_repo_from_evidence(evidence)
        if evidence_repo and evidence_repo.lower() != repository.lower():
            return _result(
                number,
                repository=repository,
                status="error",
                error_code="completion_pr_repository_mismatch",
                failure_class="unbound_identity",
                message="Completion PR does not belong to the selected project's canonical repository.",
            )

        token = self._token_for_project(project)
        if not token:
            return _result(
                number,
                repository=repository,
                status="error",
                error_code="scm_credential_unavailable",
                failure_class="missing_capability",
                message="No project-authorized GitHub credential is available to prove PR readiness.",
            )

        url = f"https://api.github.com/repos/{repository}/pulls/{number}"
        status_code, pull_request = self._transport(
            "GET", url, token=token, payload=None)
        if status_code != 200:
            return _result(
                number,
                repository=repository,
                status="error",
                error_code="completion_pr_read_failed",
                failure_class="provider_unavailable",
                message="GitHub did not return the completion PR.",
            )
        if pull_request.get("draft") is False:
            return _result(
                number,
                repository=repository,
                status="already_ready",
                is_draft=False,
                message="GitHub reports the PR ready for review.",
            )
        if pull_request.get("draft") is not True:
            return _result(
                number,
                repository=repository,
                status="error",
                error_code="completion_pr_draft_state_unknown",
                failure_class="missing_data",
                message="GitHub returned no authoritative draft state for the completion PR.",
            )

        node_id = str(pull_request.get("node_id") or "").strip()
        if not node_id:
            return _result(
                number,
                repository=repository,
                status="error",
                is_draft=True,
                error_code="completion_pr_node_id_missing",
                failure_class="missing_data",
                message="GitHub returned a draft PR without the node id required to mark it ready.",
            )
        mutation = """
mutation MarkPullRequestReady($pullRequestId: ID!) {
  markPullRequestReadyForReview(input: {pullRequestId: $pullRequestId}) {
    pullRequest { id isDraft }
  }
}
""".strip()
        mutation_status, mutation_result = self._transport(
            "POST",
            "https://api.github.com/graphql",
            token=token,
            payload={"query": mutation, "variables": {"pullRequestId": node_id}},
        )
        if mutation_status != 200 or mutation_result.get("errors"):
            return _result(
                number,
                repository=repository,
                status="error",
                is_draft=True,
                error_code="completion_pr_mark_ready_failed",
                failure_class="provider_rejected",
                message="GitHub rejected the request to mark the completion PR ready.",
            )

        reread_status, reread = self._transport(
            "GET", url, token=token, payload=None)
        if reread_status != 200 or reread.get("draft") is not False:
            return _result(
                number,
                repository=repository,
                status="error",
                is_draft=(reread.get("draft") if reread_status == 200 else None),
                error_code="completion_pr_ready_unproven",
                failure_class="failed_gate",
                message="GitHub did not confirm the PR as ready after the mutation.",
            )
        return _result(
            number,
            repository=repository,
            status="marked_ready",
            is_draft=False,
            message="GitHub confirmed the PR ready after mutation.",
        )


production_pr_ready_gateway = GitHubPullRequestReadyAdapter()


__all__ = ["GitHubPullRequestReadyAdapter", "production_pr_ready_gateway"]
