"""Mark a completion PR non-draft before complete_claim persists In Review.

Server-side counterpart to ``adapters.switchboard_core.ensure_pr_ready``
(ADAPTER-30 / BREAKDOWN 5). Codex workers call MCP ``complete_claim`` directly
and never hit the local adapter exit path, so the application command must
enforce the same contract.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any, Mapping, MutableMapping, Optional


def _evidence_mapping(evidence: Any) -> dict[str, Any]:
    if isinstance(evidence, Mapping):
        return dict(evidence)
    if isinstance(evidence, str) and evidence.strip():
        try:
            parsed = json.loads(evidence)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _pr_number_from_evidence(evidence: Any) -> int:
    data = _evidence_mapping(evidence)
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


def _pr_repo_from_evidence(evidence: Any, repo: str = "") -> str:
    if repo:
        return str(repo).strip()
    data = _evidence_mapping(evidence)
    explicit = str(data.get("repo") or data.get("repository") or "").strip()
    if explicit:
        return explicit
    url = str(data.get("pr_url") or data.get("url") or "")
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/pull/\d+", url)
    return match.group(1) if match else ""


def ensure_pr_ready(
    evidence: Any,
    *,
    repo: str = "",
    token: str = "",
    cwd: Optional[str] = None,
) -> dict[str, Any]:
    """Mark the worker's PR non-draft before merge authorization.

    Idempotent: an already-ready PR is a no-op. Missing PR evidence is skipped so
    offline/docs completions are unchanged.
    """
    number = _pr_number_from_evidence(evidence)
    result: dict[str, Any] = {
        "schema": "switchboard.pr_ready.v1",
        "pr_number": number or None,
        "status": "skipped_no_pr",
        "is_draft": None,
        "message": "",
    }
    if number <= 0:
        return result

    repo_name = _pr_repo_from_evidence(evidence, repo=repo)
    env = dict(os.environ)
    if token:
        env["GH_TOKEN"] = token
    view_cmd = ["gh", "pr", "view", str(number), "--json", "isDraft,number"]
    if repo_name:
        view_cmd.extend(["--repo", repo_name])
    try:
        view = subprocess.run(
            view_cmd, text=True, capture_output=True, check=False, env=env,
            cwd=cwd or None, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result.update({
            "status": "error",
            "is_draft": True,
            "message": f"gh pr view failed: {exc}",
        })
        return result
    if view.returncode != 0:
        result.update({
            "status": "error",
            "is_draft": True,
            "message": (view.stderr or view.stdout or "gh pr view failed").strip()[:500],
        })
        return result
    try:
        payload = json.loads(view.stdout or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    is_draft = bool(payload.get("isDraft"))
    result["is_draft"] = is_draft
    if not is_draft:
        result.update({"status": "already_ready", "message": "PR is already ready for review"})
        return result

    ready_cmd = ["gh", "pr", "ready", str(number)]
    if repo_name:
        ready_cmd.extend(["--repo", repo_name])
    try:
        ready = subprocess.run(
            ready_cmd, text=True, capture_output=True, check=False, env=env,
            cwd=cwd or None, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result.update({
            "status": "error",
            "is_draft": True,
            "message": f"gh pr ready failed: {exc}",
        })
        return result
    stderr = (ready.stderr or "").strip()
    stdout = (ready.stdout or "").strip()
    already = "already ready" in stderr.lower() or "already ready" in stdout.lower()
    if ready.returncode != 0 and not already:
        result.update({
            "status": "error",
            "is_draft": True,
            "message": (stderr or stdout or "gh pr ready failed")[:500],
        })
        return result
    result.update({
        "status": "already_ready" if already else "marked_ready",
        "is_draft": False,
        "message": (stdout or stderr or "PR marked ready")[:500],
    })
    return result


def attach_pr_ready_evidence(
    evidence: Any,
    pr_ready: Mapping[str, Any],
) -> Any:
    """Return evidence with ``pr_ready`` attached, preserving input type when string.

    Offline/docs completions with no PR stay untouched (``skipped_no_pr``).
    """
    if pr_ready.get("status") == "skipped_no_pr":
        return evidence
    data = _evidence_mapping(evidence)
    out: MutableMapping[str, Any] = dict(data)
    out["pr_ready"] = dict(pr_ready)
    if isinstance(evidence, str):
        return json.dumps(out)
    return dict(out)
