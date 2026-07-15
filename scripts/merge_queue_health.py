#!/usr/bin/env python3
"""Off-box liveness monitor for the native merge queue.

Failure mode this guards: GitHub tests each merge group's head SHA and lands the PR only once
``Switchboard CI / VM gate`` posts on that SHA — which happens only after the Plan VM receives
the ``merge_group`` webhook and drives the scratchpad mirror. If the box is down/wedged (or the
webhook subscription / wiring breaks), the gate never posts and EVERY queued PR hangs until the
60-min ``check_response_timeout``. That makes the box a single point of failure for all merges.

This monitor runs on GitHub-hosted runners (NOT the box), so it detects the hang even when the
box is completely dead, and pages via the same labelled-issue path as the uptime probe.

A merge group is STUCK if it formed longer than ``MQ_STUCK_MIN`` ago and its VM-gate status is
still pending/missing (never terminal). verify.yml runs ~2-3 min, so a group pending >15 min
means nobody is posting the gate. A ``failure``/``success`` status is a *posted* result — the
box IS answering — so those are healthy from a liveness standpoint (a red suite is the queue's
problem to re-split, not an outage).
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Dict, List

REPO = os.environ.get("MQ_HEALTH_REPO", "6th-Element-Labs/projectplanner")
CONTEXT = os.environ.get("MQ_STATUS_CONTEXT", "Switchboard CI / VM gate")
STUCK_MIN = float(os.environ.get("MQ_STUCK_MIN", "15"))

# Terminal (posted) states — the box IS answering. Only pending/missing means "no one posted".
_TERMINAL = ("success", "failure", "error")


def evaluate_groups(groups: List[Dict[str, Any]], *, now_epoch: float,
                    stuck_min: float = STUCK_MIN) -> Dict[str, Any]:
    """Pure verdict over merge groups. Each group is
    ``{ref, sha, formed_epoch, gate_state}``. Stuck = formed > ``stuck_min`` ago AND the
    gate state is not terminal (i.e. pending/missing — nobody posted). Kept pure so the
    decision logic is unit-tested without the GitHub API."""
    reasons: List[str] = []
    detail: List[Dict[str, Any]] = []
    for g in groups:
        age_min = (now_epoch - float(g["formed_epoch"])) / 60.0
        state = g.get("gate_state") or "missing"
        stuck = age_min > stuck_min and state not in _TERMINAL
        detail.append({"ref": g["ref"], "sha": g["sha"], "age_min": round(age_min, 1),
                       "gate_state": state, "stuck": stuck})
        if stuck:
            reasons.append(
                f"{g['ref'].split('/')[-1]}: gate '{state}' for {age_min:.0f} min "
                f"(> {stuck_min:.0f}) — box not posting on the merge-group SHA")
    return {"stuck": bool(reasons), "reasons": reasons, "groups": detail,
            "threshold_min": stuck_min, "context": CONTEXT, "repo": REPO}


def _gh_json(path: str) -> Any:
    out = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=True)
    return json.loads(out.stdout) if out.stdout.strip() else None


def _iso_epoch(iso: str) -> float:
    """Parse a GitHub ISO-8601 timestamp to epoch seconds, tolerant of a trailing ``Z`` or an
    explicit offset (git commit dates can carry either) so a format quirk never turns into a
    false 'stuck' alert."""
    from datetime import datetime, timezone
    text = (iso or "").strip()
    if not text:
        raise ValueError("empty timestamp")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)  # handles +00:00 / -08:00 on 3.7+
    except ValueError:
        dt = datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def fetch_groups() -> List[Dict[str, Any]]:
    """The live merge groups (``gh-readonly-queue/*`` refs) with each one's formation time
    and current VM-gate state, via ``gh api`` (uses the ambient GH_TOKEN)."""
    refs = _gh_json(f"/repos/{REPO}/git/matching-refs/heads/gh-readonly-queue") or []
    groups: List[Dict[str, Any]] = []
    for r in refs:
        sha = r["object"]["sha"]
        commit = _gh_json(f"/repos/{REPO}/git/commits/{sha}") or {}
        formed = _iso_epoch(commit.get("committer", {}).get("date", "") or
                            commit.get("author", {}).get("date", ""))
        status = _gh_json(f"/repos/{REPO}/commits/{sha}/status") or {}
        gate = next((s["state"] for s in status.get("statuses", [])
                     if s.get("context") == CONTEXT), "missing")
        groups.append({"ref": r["ref"], "sha": sha, "formed_epoch": formed, "gate_state": gate})
    return groups


def main() -> int:
    from time import time
    try:
        groups = fetch_groups()
    except Exception as exc:  # a monitor that can't read the queue is itself an alert
        print(json.dumps({"stuck": True, "reasons": [f"monitor error: {exc}"], "groups": []}))
        return 1
    verdict = evaluate_groups(groups, now_epoch=time())
    print(json.dumps(verdict, indent=2, sort_keys=True))
    return 1 if verdict["stuck"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
