"""Pull-model CI dispatch relay (CI-3 / CI-6).

On PR open/update the canonical webhook handler fires one authenticated
``repository_dispatch`` to the public CI repo carrying ``{pr, head_sha}``.
Stateless — no git, no disk on the Plan VM — so it cannot reproduce the
2026-07-12 bare-mirror failure class.

Feature-flagged via ``SWITCHBOARD_CI_PULL_MODEL`` until CI-6 flip is complete.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict

SCHEMA = "switchboard.ci_verify_dispatch.v1"
DEFAULT_CI_REPO = "6th-Element-Labs/projectplanner-ci"
DEFAULT_CANONICAL_REPO = "6th-Element-Labs/projectplanner"
DEFAULT_EVENT_TYPE = "verify-pr"


def is_pull_model_enabled() -> bool:
    return (os.environ.get("SWITCHBOARD_CI_PULL_MODEL") or "").strip().lower() in (
        "1", "true", "yes", "on")


def ci_repo(explicit: str = "") -> str:
    return (
        explicit
        or os.environ.get("SWITCHBOARD_CI_VERIFY_REPO")
        or DEFAULT_CI_REPO
    ).strip()


def canonical_repo(explicit: str = "") -> str:
    return (
        explicit
        or os.environ.get("SWITCHBOARD_CI_REPO")
        or os.environ.get("PM_GITHUB_REPO_SWITCHBOARD")
        or os.environ.get("PM_GITHUB_REPO")
        or DEFAULT_CANONICAL_REPO
    ).strip()


def _token(explicit: str = "") -> str:
    return (
        explicit
        or os.environ.get("SWITCHBOARD_CI_DISPATCH_TOKEN")
        or os.environ.get("SWITCHBOARD_CI_GITHUB_TOKEN")
        or os.environ.get("PM_GITHUB_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or ""
    ).strip()


def _github_request(method: str, path: str, *, token: str,
                    body: Dict[str, Any] | None = None) -> Any:
    url = path if path.startswith("https://") else f"https://api.github.com/{path.lstrip('/')}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method.upper())
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"GitHub API {method} {url} failed: HTTP {exc.code} {detail}"
        ) from exc


def dispatch_verify(
    pr_number: int,
    *,
    head_sha: str = "",
    repo: str = "",
    ci_repo_name: str = "",
    token: str = "",
    event_type: str = "",
) -> Dict[str, Any]:
    """Fire ``repository_dispatch`` on the public CI repo for one PR head."""
    pr = int(pr_number)
    source_repo = canonical_repo(repo)
    target = ci_repo(ci_repo_name)
    tok = _token(token)
    if not tok:
        raise RuntimeError("A GitHub token is required to dispatch pull-model CI.")
    owner, name = target.split("/", 1)
    payload = {
        "event_type": (event_type or os.environ.get("SWITCHBOARD_CI_VERIFY_EVENT")
                       or DEFAULT_EVENT_TYPE).strip(),
        "client_payload": {
            "schema": SCHEMA,
            "pr": pr,
            "head_sha": (head_sha or "").strip(),
            "repo": source_repo,
        },
    }
    _github_request(
        "POST",
        f"repos/{owner}/{name}/dispatches",
        token=tok,
        body=payload,
    )
    return {
        "schema": SCHEMA,
        "dispatched": True,
        "ci_repo": target,
        "canonical_repo": source_repo,
        "pr": pr,
        "head_sha": (head_sha or "").strip(),
        "event_type": payload["event_type"],
    }
