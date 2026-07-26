#!/usr/bin/env python3
"""Is the fleet on the GitHub App yet, and how much budget is left?

Run on the box after configuring the App (source the env first — the key path and
App id live in /opt/projectplanner/.env):

    cd /opt/projectplanner && sudo bash -c 'set -a && . ./.env && set +a && \\
        .venv/bin/python scripts/github_app_doctor.py'

Prints, per canonical repo, which credential resolves and what that credential's
real remaining budget is — read from the `x-ratelimit-*` headers of an actual
request, never from /rate_limit, which reported 4,962 remaining during the
2026-07-26 outage while every real call was returning 0.

No secrets are printed: tokens are shown as a prefix and a length.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import github_app_auth  # noqa: E402


def redact(token: str) -> str:
    if not token:
        return "(none)"
    return f"{token[:4]}…len={len(token)}"


def probe(token: str) -> dict:
    """One cheap authenticated call; report the rate-limit headers it comes back with."""
    request = urllib.request.Request("https://api.github.com/rate_limit")
    request.add_header("Accept", "application/vnd.github+json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            headers = response.headers
            status = response.status
    except urllib.error.HTTPError as exc:
        headers, status = exc.headers, exc.code
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "http": status,
        "limit": headers.get("x-ratelimit-limit"),
        "remaining": headers.get("x-ratelimit-remaining"),
        "used": headers.get("x-ratelimit-used"),
        "reset_in_s": (int(headers.get("x-ratelimit-reset") or 0) - int(__import__("time").time())
                       if headers.get("x-ratelimit-reset") else None),
    }


def main() -> int:
    print("== configuration ==")
    status = github_app_auth.status()
    print(json.dumps(status, indent=2, sort_keys=True))
    if not status["configured"]:
        print("\nNo GitHub App configured — the fleet is still on the shared PAT.")
        print(f"Set {github_app_auth.APP_ID_ENV} and "
              f"{github_app_auth.PRIVATE_KEY_PATH_ENV}; see docs/GITHUB-APP-RATE-LIMIT.md")

    try:
        import store
        repos = sorted(store.list_canonical_repos() or [])
    except Exception as exc:  # noqa: BLE001
        print(f"\n(could not list canonical repos: {exc})")
        repos = [r for r in (os.environ.get("PM_GITHUB_REPO") or "").split(",") if r]

    print("\n== credential per canonical repo ==")
    seen_tokens = {}
    on_app = []
    for repo in repos:
        github_app_auth.reset_cache()
        token = github_app_auth.resolve_token(repo=repo)
        source = github_app_auth.token_source() or "(none)"
        error = github_app_auth.last_error()
        if source == "github_app":
            on_app.append(repo)
        print(f"\n{repo}")
        print(f"  source     : {source}")
        print(f"  token      : {redact(token)}")
        if error:
            print(f"  last_error : {error}")
        if not token:
            print("  budget     : n/a (no credential)")
            continue
        if token not in seen_tokens:
            seen_tokens[token] = probe(token)
        budget = seen_tokens[token]
        print(f"  budget     : {json.dumps(budget, sort_keys=True)}")
        remaining = budget.get("remaining")
        if remaining is not None and str(remaining).isdigit() and int(remaining) == 0:
            print("  ** EXHAUSTED — every GitHub read will 403 until reset **")

    print("\n== verdict ==")
    if on_app and len(on_app) == len(repos):
        print("Every canonical repo mints an installation token; each install has its own budget.")
        return 0
    if on_app:
        missing = [r for r in repos if r not in on_app]
        print(f"Partly migrated. Still on the shared PAT: {', '.join(missing)}")
        print("Install the App on those owners, or add them to "
              f"{github_app_auth.INSTALLATION_MAP_ENV}.")
        return 1
    print("Fleet is still drawing on the shared PAT budget.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
