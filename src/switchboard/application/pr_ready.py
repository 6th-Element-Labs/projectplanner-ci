"""Provider-neutral completion-PR readiness contract.

The application layer identifies PR evidence and records the readiness proof
returned by an injected SCM adapter. It does not resolve credentials, invoke a
provider CLI, or make network requests.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, MutableMapping, Protocol


class PullRequestReadyGateway(Protocol):
    """SCM edge that proves a completion PR is non-draft."""

    def __call__(
        self,
        evidence: Any,
        *,
        project: str,
        actor: str,
    ) -> dict[str, Any]:
        ...


def evidence_mapping(evidence: Any) -> dict[str, Any]:
    if isinstance(evidence, Mapping):
        return dict(evidence)
    if isinstance(evidence, str) and evidence.strip():
        try:
            parsed = json.loads(evidence)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def pr_number_from_evidence(evidence: Any) -> int:
    data = evidence_mapping(evidence)
    raw = data.get("pr_number") or data.get("number")
    if raw not in (None, ""):
        try:
            number = int(raw)
        except (TypeError, ValueError):
            number = 0
        if number > 0:
            return number
    url = str(data.get("pr_url") or data.get("url") or "")
    match = re.search(r"/pull/(\d+)(?:/|$|\?|#)", url)
    return int(match.group(1)) if match else 0


def pr_repo_from_evidence(evidence: Any) -> str:
    data = evidence_mapping(evidence)
    explicit = str(data.get("repo") or data.get("repository") or "").strip()
    if explicit:
        return explicit.removesuffix(".git")
    url = str(data.get("pr_url") or data.get("url") or "")
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/pull/\d+", url)
    return match.group(1) if match else ""


def unavailable_pr_ready_result(evidence: Any) -> dict[str, Any]:
    """Fail closed when a PR exists but no SCM readiness adapter was injected."""
    number = pr_number_from_evidence(evidence)
    return {
        "schema": "switchboard.pr_ready.v1",
        "pr_number": number or None,
        "status": "adapter_unavailable" if number else "skipped_no_pr",
        "is_draft": None,
        "error_code": "pr_ready_adapter_unavailable" if number else "",
        "failure_class": "missing_capability" if number else "",
        "message": (
            "No project-bound SCM adapter is configured to prove PR readiness."
            if number else ""
        ),
    }


def pr_ready_is_proven(
    result: Mapping[str, Any],
    *,
    pr_number: int,
) -> bool:
    """Only an explicit, successful non-draft observation is completion proof."""
    try:
        observed_number = int(result.get("pr_number") or 0)
    except (TypeError, ValueError):
        observed_number = 0
    return (
        result.get("schema") == "switchboard.pr_ready.v1"
        and observed_number == pr_number
        and result.get("status") in {"already_ready", "marked_ready"}
        and result.get("is_draft") is False
    )


def attach_pr_ready_evidence(
    evidence: Any,
    pr_ready: Mapping[str, Any],
) -> Any:
    """Return evidence with readiness proof attached, preserving JSON-string input."""
    if pr_ready.get("status") == "skipped_no_pr":
        return evidence
    data = evidence_mapping(evidence)
    out: MutableMapping[str, Any] = dict(data)
    out["pr_ready"] = dict(pr_ready)
    if isinstance(evidence, str):
        return json.dumps(out)
    return dict(out)


__all__ = [
    "PullRequestReadyGateway",
    "attach_pr_ready_evidence",
    "evidence_mapping",
    "pr_number_from_evidence",
    "pr_ready_is_proven",
    "pr_repo_from_evidence",
    "unavailable_pr_ready_result",
]
