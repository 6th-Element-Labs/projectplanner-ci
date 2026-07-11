#!/usr/bin/env python3
"""Remote push verification for claim completion (ENFORCE: silent-failed-push leak).

Before this module, ``complete_claim`` stamped ``pushed_at`` from the mere
presence of ``head_sha`` in agent-supplied evidence (store.py) and the managed
loop fabricated a ``remote_ref`` without pushing (adapters/switchboard_core.py).
A branch that was committed locally but never pushed was therefore recorded as
pushed and moved to In Review -- so worked tasks silently never landed on the
board.

``verify_push_evidence`` proves the branch/head_sha actually exists on the
canonical remote via the GitHub API (no local clone required, so it is safe to
call from the web/control-plane process). Policy, per operator decision
"fail-closed, warn on unreachable":

  * ``present``     -- ref proven on the remote              -> completion proceeds
  * ``absent``      -- remote reachable, ref is NOT there    -> FAIL CLOSED
                       (``stale_branch``); the committed-but-unpushed leak.
  * ``unverified``  -- remote unreachable / no token / rate-limited -> WARN and
                       allow (never wedge a legitimate completion on a network
                       blip), but leave an auditable ``push_verification`` signal
                       so reconcile/monitors can re-check.
  * ``skipped``     -- no git evidence to verify (docs/offline) or already merged.

The GitHub call is injected (``request_fn``) so this is fully unit-testable
offline, and so the caller can run it OUTSIDE the sqlite transaction (network
I/O must never hold the write lock on the shared box).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

SCHEMA = "switchboard.push_verification.v1"

PRESENT = "present"
ABSENT = "absent"
UNVERIFIED = "unverified"
SKIPPED = "skipped"

# Same token precedence used by orphan_merge_discovery / reconcile.
_TOKEN_ENV_VARS = ("PM_GITHUB_TOKEN", "GITHUB_TOKEN", "SWITCHBOARD_CI_GITHUB_TOKEN")


def github_token_from_env(env: Optional[Mapping[str, str]] = None) -> str:
    env = env if env is not None else os.environ
    for name in _TOKEN_ENV_VARS:
        val = (env.get(name) or "").strip()
        if val:
            return val
    return ""


def _default_request_fn(url: str, token: str = "") -> Tuple[int, Any]:
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=8) as resp:
        body = resp.read().decode() or "{}"
        return int(getattr(resp, "status", 200) or 200), json.loads(body)


def _result(status: str, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"status": status, "schema": SCHEMA}
    out.update(extra)
    return out


def _branch_from_ref(remote_ref: str) -> str:
    """Extract a branch name from a remote_ref; '' if it is not a branch push."""
    ref = (remote_ref or "").strip()
    if not ref:
        return ""
    for prefix in ("refs/heads/", "refs/remotes/origin/", "origin/"):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    if ref.startswith("refs/"):
        return ""  # tags / other ref namespaces are not a branch push
    return ref


def verify_push_evidence(
    evidence: Mapping[str, Any],
    repo: str,
    token: str = "",
    *,
    request_fn: Optional[Callable[[str, str], Tuple[int, Any]]] = None,
) -> Dict[str, Any]:
    """Return a push-verification result dict for completion evidence.

    ``repo`` is the canonical ``owner/name`` slug. ``request_fn(url, token)``
    must return ``(http_status, json_body)`` or raise ``urllib.error.HTTPError``
    for 4xx/5xx and ``urllib.error.URLError`` for transport failures.
    """
    evidence = evidence or {}

    # Already-merged work is proven by merge provenance elsewhere -- nothing to push-check.
    if str(evidence.get("merged_sha") or "").strip():
        return _result(SKIPPED, reason="already_merged")

    head_sha = str(evidence.get("head_sha") or "").strip()
    branch = str(evidence.get("branch") or "").strip()
    remote_ref = str(evidence.get("remote_ref") or "").strip()
    pr_url = str(evidence.get("pr_url") or "").strip()
    pr_number = evidence.get("pr_number")

    # No git evidence at all -> docs/offline completion; nothing to verify.
    if not (head_sha or branch or remote_ref):
        return _result(SKIPPED, reason="no_git_evidence")

    repo = (repo or "").strip()
    if not repo:
        return _result(UNVERIFIED, reason="no_canonical_repo",
                       detail="project has no canonical repo configured")

    request_fn = request_fn or _default_request_fn

    # Prefer verifying the exact commit -- the strongest proof the pushed head is
    # on the remote. Fall back to the branch ref, then a remote_ref-derived branch.
    if head_sha:
        kind, ref_value = "commit", head_sha
        url = f"https://api.github.com/repos/{repo}/commits/{head_sha}"
    else:
        ref_branch = branch or _branch_from_ref(remote_ref)
        if ref_branch:
            kind, ref_value = "branch", ref_branch
            url = ("https://api.github.com/repos/"
                   f"{repo}/branches/{urllib.parse.quote(ref_branch, safe='')}")
        elif pr_url or pr_number:
            # Only an unparseable remote_ref, but a real PR exists -> trust the PR.
            return _result(PRESENT, reason="pr_evidence",
                           verified_ref=remote_ref or pr_url)
        else:
            return _result(UNVERIFIED, reason="unparseable_ref", detail=remote_ref)

    try:
        code, _body = request_fn(url, token)
        code = int(code or 0)
    except urllib.error.HTTPError as e:
        code = int(getattr(e, "code", 0) or 0)
        if code in (404, 422):
            return _result(ABSENT, reason=f"{kind}_not_on_remote", repo=repo,
                           ref_kind=kind, ref=ref_value, http_status=code)
        return _result(UNVERIFIED, reason="github_error", repo=repo,
                       ref_kind=kind, ref=ref_value, http_status=code)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return _result(UNVERIFIED, reason="remote_unreachable", repo=repo,
                       ref_kind=kind, ref=ref_value, detail=str(e))
    except Exception as e:  # never let verification crash a completion
        return _result(UNVERIFIED, reason="verification_error", repo=repo,
                       ref_kind=kind, ref=ref_value, detail=str(e))

    if 200 <= code < 300:
        return _result(PRESENT, repo=repo, ref_kind=kind, ref=ref_value,
                       verified_ref=ref_value, http_status=code)
    if code in (404, 422):
        return _result(ABSENT, reason=f"{kind}_not_on_remote", repo=repo,
                       ref_kind=kind, ref=ref_value, http_status=code)
    return _result(UNVERIFIED, reason="github_error", repo=repo,
                   ref_kind=kind, ref=ref_value, http_status=code)
