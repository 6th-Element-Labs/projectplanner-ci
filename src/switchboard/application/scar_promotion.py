"""Promote production completion scars into hermetic conformance scenarios.

This is deliberately a read-only projection over ``decision_records``.  It does
not route work or alter completion state; the background-job adapter only writes
reviewable scenario artifacts into a caller-supplied checkout.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from switchboard.domain.completion.effects import plan_effect
from switchboard.domain.completion.state_machine import classify_completion

SCHEMA = "switchboard.completion_conformance.scenario.v1"
TIMING = {
    "ci_delay_seconds": 0,
    "never_report": False,
    "hang_seconds": 0,
    "close_pr_while_runner_live": False,
    "move_head_during_assessment": False,
}
_BAD_ACTION = re.compile(r"\b(?:override|bypass|force|manual)\b", re.I)


def scar_kind(episode: Mapping[str, Any], *, repeat_threshold: int = 8) -> str:
    """Return the production-scar class, or ``""`` for an ordinary episode."""
    if str(episode.get("divergence_class") or "") == "unsafe":
        return "unsafe_shadow_divergence"
    if int(episode.get("tick_count") or 0) >= repeat_threshold:
        return "repair_loop"
    action = str(episode.get("human_action") or "")
    if episode.get("human_intervened") and _BAD_ACTION.search(action):
        return "operator_override"
    if episode.get("human_intervened") and episode.get("route") != "human":
        return "unexpected_human_escalation"
    return ""


def select_scars(
    episodes: Iterable[Mapping[str, Any]], *, repeat_threshold: int = 8,
) -> list[dict[str, Any]]:
    selected = []
    for episode in episodes:
        kind = scar_kind(episode, repeat_threshold=repeat_threshold)
        if kind and episode.get("snapshot_retained", True):
            selected.append({**dict(episode), "scar_kind": kind})
    return selected


def _ci(snapshot: Mapping[str, Any]) -> tuple[str, str, str]:
    contexts = list(snapshot.get("status_contexts") or [])
    required = list(snapshot.get("required_status_contexts") or [])
    context = str(required[0] if required else "Switchboard CI / VM gate")
    row = next(
        (item for item in contexts if str(item.get("context") or item.get("name")) == context),
        None,
    )
    if not row:
        return "missing", context, "unknown"
    state = str(row.get("state") or row.get("status") or row.get("conclusion") or "").lower()
    if state in {"success", "passed", "pass", "ok", "neutral", "skipped"}:
        state = "pass"
    elif state in {"failure", "failed"}:
        state = "fail"
    elif state in {"requested", "queued", "in_progress", "waiting", "expected"}:
        state = "pending"
    return state, context, str(row.get("failure_attribution") or "unknown")


def world_from_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Lift a normalized completion snapshot while dropping tenant identifiers."""
    pr = dict(snapshot.get("github_pr") or snapshot.get("pr") or {})
    review = dict(snapshot.get("review") or {})
    queue = dict(snapshot.get("merge_queue") or {})
    runner = dict(snapshot.get("runner") or {})
    ci, ci_context, attribution = _ci(snapshot)
    review_status = str(review.get("status") or "").lower()
    if not review_status:
        review_status = "missing"
    elif review_status not in {"passed", "changes_requested"}:
        review_status = "passed" if review_status in {"pass", "approved"} else "changes_requested"
    queue_state = str(queue.get("state") or "").lower()
    queue_value = (
        "queued" if queue_state in {"queued", "awaiting_checks", "mergeable"}
        else "unmergeable" if queue_state == "unmergeable"
        else "locked" if queue_state == "locked"
        else "ejected" if queue.get("prior_enqueue_verified")
        else "none"
    )
    runner_role = str(runner.get("role") or "none").lower()
    exact_head = str(snapshot.get("head_sha") or "")
    runner_head = str(runner.get("head_sha") or "")
    world: dict[str, Any] = {
        "draft": bool(pr.get("draft")),
        "ci": ci,
        "ci_context": ci_context,
        "ci_attribution": attribution,
        "review": review_status,
        "mergeable": pr.get("mergeable"),
        "merge_state_status": str(
            pr.get("mergeStateStatus") or pr.get("merge_state_status") or "UNKNOWN"
        ).upper(),
        "queue": queue_value,
        "runner": {
            "live": bool(runner.get("live")),
            "role": runner_role if runner_role in {"review_merge", "remediation"} else "none",
            "head": "same" if runner_head and runner_head == exact_head else (
                "stale" if runner_head else "none"
            ),
        },
    }
    pr_state = str(pr.get("state") or "open").lower()
    if pr_state != "open":
        world["pr_state"] = pr_state
    return world


def _scenario_id(episode: Mapping[str, Any], world: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(world, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:10]
    kind = re.sub(r"[^a-z0-9]+", "_", str(episode["scar_kind"]).lower()).strip("_")
    return f"scar_{kind}_{digest}"


def predict_route_from_spec(
    snapshot: Mapping[str, Any], world: Mapping[str, Any],
) -> str:
    """Apply the documented state-machine precedence without calling production.

    This is intentionally small and fail-closed: an unmodelled scar is skipped
    rather than promoted using the classifier as its own oracle.
    """
    task = dict(snapshot.get("task") or {})
    pr_state = str(world.get("pr_state") or "open")
    if str(task.get("status") or "").lower() in {"done", "cancelled", "canceled"}:
        return "none"
    if pr_state == "merged":
        return "reconcile"
    if pr_state == "closed":
        return "coordination_retry" if world.get("reopen_authorized") else "human"
    if world["queue"] == "queued":
        return "wait"
    if world["queue"] == "unmergeable":
        return (
            "remediation"
            if world["ci_attribution"] == "product" else "coordination_retry"
        )
    if world["queue"] == "locked":
        queue = dict(snapshot.get("merge_queue") or {})
        return "coordination_retry" if queue.get("retry_exhausted") else "wait"
    ci = str(world["ci"])
    if ci in {"fail"}:
        return (
            "remediation"
            if world["ci_attribution"] == "product" else "coordination_retry"
        )
    if ci in {"error", "timed_out", "cancelled", "canceled", "stale", "startup_failure"}:
        return "coordination_retry"
    if ci == "action_required":
        return "human"
    if world["draft"]:
        return "review_merge"
    if ci == "pending":
        return "wait"
    if ci == "missing":
        return "coordination_retry"
    if world["review"] == "missing":
        return "review_merge"
    if world["review"] == "changes_requested":
        review = dict(snapshot.get("review") or {})
        if review.get("retry_exhausted"):
            return "human"
        findings = list(review.get("findings") or [])
        return (
            "remediation"
            if findings and all(
                str(item.get("finding_class") or "") == "automatic"
                for item in findings
            )
            else "human"
        )
    if world["mergeable"] is False or world["merge_state_status"] == "DIRTY":
        return "remediation"
    if world["merge_state_status"] == "BEHIND":
        return "coordination_retry"
    if world["mergeable"] is None or world["merge_state_status"] == "UNKNOWN":
        return "wait"
    # Merge-gate findings are already typed by the documented mapping.
    findings = list((snapshot.get("merge_gate") or {}).get("findings") or [])
    if findings:
        routes = {
            str(item.get("route") or item.get("suggested_route") or "")
            for item in findings
        } - {""}
        if len(routes) == 1:
            return routes.pop()
        raise ValueError("merge-gate finding family is not spec-predictable")
    return "review_merge"


def build_scenario(episode: Mapping[str, Any]) -> dict[str, Any]:
    """Build one gold candidate and fail closed if replay no longer reproduces."""
    snapshot = dict(episode.get("snapshot") or {})
    if snapshot.get("schema") != "switchboard.completion_snapshot.v1":
        raise ValueError("episode has no retained completion_snapshot.v1")
    world = world_from_snapshot(snapshot)
    spec_route = predict_route_from_spec(snapshot, world)
    decision = classify_completion(None, snapshot)
    plan = plan_effect(decision, snapshot, run={"run_id": "scar-promotion", "state_version": 1})
    route = str(decision.get("route") or "")
    # The recorded route is the incident's historical fact.  A mismatch means the
    # current classifier/spec has moved; do not silently bless a different scar.
    recorded = str(episode.get("route") or "")
    if route != spec_route or (recorded and route != recorded):
        raise ValueError(
            "route mismatch: "
            f"spec={spec_route}, recorded={recorded or '(none)'}, current={route}"
        )
    terminal = "human" if route == "human" else (
        "reconcile_done" if route == "reconcile" else
        "merged" if route == "none" else "blocked"
    )
    expected = {
        "terminal": terminal,
        "reason_code": str(decision.get("reason_code") or ""),
        "role_sequence": [decision["desired_role"]] if decision.get("desired_role") else [],
        "route": route,
        "effect": str(plan.get("effect") or ""),
    }
    if str(episode.get("divergence_class") or "") == "unsafe":
        expected["normalized_action"] = str(episode.get("thin_action") or "")
        expected["shadow_divergence_class"] = "unsafe"
    return {
        "schema": SCHEMA,
        "id": _scenario_id(episode, world),
        "expect": expected,
        "world": world,
        "timing": dict(TIMING),
    }


def promote(
    episodes: Iterable[Mapping[str, Any]], output_dir: Path, *,
    repeat_threshold: int = 8,
) -> dict[str, Any]:
    """Write idempotent candidates and return a PR-ready change manifest."""
    output_dir.mkdir(parents=True, exist_ok=True)
    written, skipped = [], []
    for episode in select_scars(episodes, repeat_threshold=repeat_threshold):
        try:
            scenario = build_scenario(episode)
            path = output_dir / f"{scenario['id']}.json"
            payload = json.dumps(scenario, indent=2, sort_keys=True) + "\n"
            if not path.exists() or path.read_text(encoding="utf-8") != payload:
                path.write_text(payload, encoding="utf-8")
            written.append(str(path))
        except ValueError as exc:
            skipped.append({"record_id": episode.get("record_id"), "reason": str(exc)})
    day = time.strftime("%Y-%m-%d", time.gmtime())
    return {
        "schema": "switchboard.scar_promotion.v1",
        "written": written,
        "skipped": skipped,
        "pull_request": {
            "branch": f"automation/completion-scars-{day}",
            "title": f"test(conformance): lock down completion scars {day}",
            "body": "Automated promotion of retained bad completion-decision episodes.",
            "ready": bool(written),
        },
    }


def open_pull_request(repo_root: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Commit only promoted files in a dedicated checkout and open a draft PR."""
    files = [str(Path(path).resolve()) for path in manifest.get("written") or []]
    if not files:
        return {"opened": False, "reason": "no_scenarios"}
    root = repo_root.resolve()
    if any(root not in Path(path).parents for path in files):
        raise ValueError("promoted file is outside repo_root")

    def run(*args: str) -> str:
        completed = subprocess.run(
            list(args), cwd=root, check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return completed.stdout.strip()

    if run("git", "status", "--porcelain"):
        # The generated files are expected to be dirty; anything else is not.
        changed = {
            str((root / line[3:]).resolve())
            for line in run("git", "status", "--porcelain").splitlines()
        }
        if changed - set(files):
            raise ValueError("repo_root contains unrelated changes")
    pr = dict(manifest.get("pull_request") or {})
    branch = str(pr["branch"])
    run("git", "switch", "-C", branch)
    run("git", "add", "--", *[str(Path(path).relative_to(root)) for path in files])
    staged = run("git", "diff", "--cached", "--name-only")
    if not staged:
        return {"opened": False, "reason": "already_promoted", "branch": branch}
    run("git", "commit", "-m", str(pr["title"]))
    run("git", "push", "-u", "origin", branch)
    url = run(
        "gh", "pr", "create", "--draft", "--title", str(pr["title"]),
        "--body", str(pr["body"]), "--head", branch,
    )
    return {"opened": True, "branch": branch, "url": url}
