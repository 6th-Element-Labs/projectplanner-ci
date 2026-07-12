"""HARDEN-74 — event-driven CI gate requests (kill the 5-minute cron wait).

The prod PR gate ran on a 5-minute systemd timer, so a PR sat 0-5 minutes doing
nothing before CI even started. This module lets the GitHub PR webhook drop a tiny
request marker the instant a PR opens/updates; a systemd ``.path`` unit watches the
directory and fires the gate for exactly those PRs immediately (see
``deploy/projectplanner-ci-gate-request.path``). The periodic timer stays only as a
backstop for missed webhooks.

It is intentionally filesystem-only and FastAPI/store-free so the sandboxed web app
(which may write ``/var/lib/projectplanner`` but must not spawn git/gh) can enqueue a
request without running the heavy gate itself — the gate runs later, in its own
systemd sandbox, concurrently per request.

Marker protocol: one file ``pr-<n>.json`` per PR under the request dir, written
atomically (temp + rename). ``drain`` claims markers by removing them *before* the
gate runs, so a newer webhook that arrives mid-gate simply re-creates the marker and
re-triggers the ``.path`` unit — no event is silently lost.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = "switchboard.ci_gate_request.v1"

DEFAULT_REQUEST_DIR = "/var/lib/projectplanner/ci-gate/requests"


def is_event_driven_enabled() -> bool:
    """Feature flag: event-driven dispatch is off unless SWITCHBOARD_CI_EVENT_DRIVEN is
    truthy. Lets the code ship and deploy before the .path unit is installed."""
    return (os.environ.get("SWITCHBOARD_CI_EVENT_DRIVEN") or "").strip().lower() in (
        "1", "true", "yes", "on")


def request_dir(explicit: str = "") -> Path:
    """Resolve the request directory (arg > SWITCHBOARD_CI_REQUEST_DIR > default)."""
    return Path(explicit or os.environ.get("SWITCHBOARD_CI_REQUEST_DIR")
               or DEFAULT_REQUEST_DIR)


def _marker_path(directory: Path, pr_number: int) -> Path:
    return directory / f"pr-{int(pr_number)}.json"


def request_ci_gate(pr_number: int, *, repo: str = "", head_sha: str = "",
                    dir_override: str = "") -> Dict[str, Any]:
    """Drop (or overwrite) a request marker for one PR. Atomic: written to a temp file
    then renamed, so a concurrent reader never sees a half-written marker. Returns the
    marker payload. Raises only on a genuine filesystem error (callers wrap best-effort)."""
    directory = request_dir(dir_override)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": SCHEMA,
        "pr_number": int(pr_number),
        "repo": repo or "",
        "head_sha": head_sha or "",
        "requested_at": time.time(),
    }
    final = _marker_path(directory, pr_number)
    tmp = directory / f".pr-{int(pr_number)}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(tmp, final)  # atomic within the same directory
    return payload


def _read_marker(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or "pr_number" not in data:
        return None
    return data


def list_requests(dir_override: str = "") -> List[Dict[str, Any]]:
    """Pending request markers (read-only; does not clear them)."""
    directory = request_dir(dir_override)
    if not directory.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for path in sorted(directory.glob("pr-*.json")):
        marker = _read_marker(path)
        if marker:
            out.append(marker)
    return out


def drain(dir_override: str = "") -> List[Dict[str, Any]]:
    """Claim and return all pending requests, removing their markers first so a newer
    webhook arriving during the gate re-creates the marker (and re-fires the .path unit)
    instead of being lost. A malformed/stale marker is removed and skipped."""
    directory = request_dir(dir_override)
    if not directory.is_dir():
        return []
    claimed: List[Dict[str, Any]] = []
    for path in sorted(directory.glob("pr-*.json")):
        marker = _read_marker(path)
        try:
            path.unlink()  # claim before gating
        except FileNotFoundError:
            continue  # another drainer took it
        except OSError:
            continue
        if marker:
            claimed.append(marker)
    return claimed
