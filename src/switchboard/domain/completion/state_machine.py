"""Exact-head snapshot hydration for the Mission Bot.

Decision authority lives in ``switchboard.domain.mission_bot``. This module
only normalizes independently hydrated GitHub/Switchboard records into the
shared snapshot contract, plus a thin ``classify_completion`` projection for
legacy callers during cutover.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import re
from typing import Any, Mapping, Sequence

from switchboard.domain.mission_bot import reduce_mission
from switchboard.domain.mission_bot.outputs import MISSION_BOT_VERSION


COMPLETION_SNAPSHOT_SCHEMA = "switchboard.completion_snapshot.v1"
COMPLETION_DECISION_SCHEMA = "switchboard.completion_decision.v1"
COMPLETION_CLASSIFIER_VERSION = MISSION_BOT_VERSION

#: Branch-protection projection of Switchboard's *own* merge gate.
MERGE_AUTHORIZATION_CONTEXT = "Switchboard / merge authorization"

_CHECK_IDENTITY_FIELDS = frozenset({
    "name", "context", "check_name", "checkName",
})


def _map(value: Any) -> dict[str, Any]:
    return deepcopy(dict(value)) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _text_raw(value: Any) -> str:
    return str(value or "").strip()


def _head(value: Mapping[str, Any] | None) -> str:
    row = _map(value)
    return str(
        _map(row.get("head")).get("sha")
        or row.get("head_sha")
        or row.get("sha")
        or ""
    ).strip()


def _pr_url(pr: Mapping[str, Any]) -> str:
    return str(pr.get("url") or pr.get("html_url") or pr.get("href") or "").strip()


def _pr_identity(pr: Mapping[str, Any]) -> str:
    number = pr.get("number")
    url = _text_raw(pr.get("url"))
    if number not in (None, ""):
        return f"pr:{int(number)}"
    if url:
        return f"url:{url}"
    return ""


def _has_provenance(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (str, bytes)):
        return bool(str(value).strip())
    if isinstance(value, (int, float, bool)):
        return True
    if isinstance(value, Mapping):
        return any(_has_provenance(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_has_provenance(item) for item in value)
    return True


def _check_rows(source: Any) -> list[dict[str, Any]]:
    if isinstance(source, Mapping):
        if any(key in source for key in ("name", "context", "check_name", "state", "conclusion")):
            return [dict(source)]
        rows: list[dict[str, Any]] = []
        for key, value in source.items():
            if isinstance(value, Mapping):
                row = dict(value)
                row.setdefault("name", key)
                row.setdefault("context", key)
                rows.append(row)
            elif isinstance(value, str):
                rows.append({"name": key, "context": key, "state": value})
        return rows
    if isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
        return [dict(item) for item in source if isinstance(item, Mapping)]
    return []


def _check_timestamp(row: Mapping[str, Any]) -> float:
    for key in (
        "completedAt", "completed_at", "updatedAt", "updated_at",
        "startedAt", "started_at",
    ):
        raw = row.get(key)
        if not raw:
            continue
        try:
            text = str(raw).replace("Z", "+00:00")
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            continue
    return 0.0


def _check_provenance_richness(row: Mapping[str, Any]) -> int:
    return sum(
        1 for key, value in row.items()
        if key not in _CHECK_IDENTITY_FIELDS and _has_provenance(value)
    )


def _check_selection_key(row: Mapping[str, Any]) -> tuple[float, int, str]:
    canonical = json.dumps(
        dict(row),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return (
        _check_timestamp(row),
        _check_provenance_richness(row),
        canonical,
    )


def build_completion_snapshot(
    *,
    task: Mapping[str, Any] | None = None,
    github_pr: Mapping[str, Any] | None = None,
    required_status_contexts: Sequence[str] = (),
    status_contexts: Any = None,
    review: Mapping[str, Any] | None = None,
    merge_gate: Mapping[str, Any] | None = None,
    merge_queue: Mapping[str, Any] | None = None,
    work_session: Mapping[str, Any] | None = None,
    runner: Mapping[str, Any] | None = None,
    merge_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize independently hydrated records into the shared snapshot contract."""
    task_row, pr, gate = _map(task), _map(github_pr), _map(merge_gate)
    contexts: dict[str, dict[str, Any]] = {}
    sources = (
        pr.get("status_contexts"), pr.get("statusCheckRollup"), pr.get("checks"),
        gate.get("status_contexts"), status_contexts,
    )
    for source in sources:
        for row in _check_rows(source):
            name = str(
                row.get("name") or row.get("context") or row.get("check_name") or ""
            ).strip()
            current = contexts.get(name)
            if name and (
                current is None
                or _check_selection_key(row) > _check_selection_key(current)
            ):
                contexts[name] = row
    required = list(dict.fromkeys(
        str(item).strip() for item in
        [*(gate.get("required_status_contexts") or []), *required_status_contexts]
        if str(item).strip()
    ))
    board_git = _map(task_row.get("git_state"))
    board_pr = {
        "number": board_git.get("pr_number"),
        "url": board_git.get("pr_url"),
    }
    current_pr_url = (
        _pr_url(pr) or str(gate.get("pr_url") or "").strip()
        or str(board_git.get("pr_url") or "").strip()
    )
    return {
        "schema": COMPLETION_SNAPSHOT_SCHEMA,
        "task": task_row,
        "task_id": str(
            task_row.get("task_id") or gate.get("task_id") or ""
        ).strip().upper(),
        "board_status": str(task_row.get("status") or "").strip(),
        "board_head_sha": _head(board_git),
        "board_pr_number": board_git.get("pr_number"),
        "board_pr_url": board_git.get("pr_url"),
        "board_pr_identity": _pr_identity(board_pr),
        "github_pr": pr,
        "pr_number": pr.get("number") or gate.get("pr_number"),
        "pr_url": current_pr_url,
        "pr_identity": _pr_identity({
            "number": pr.get("number") or gate.get("pr_number"),
            "url": current_pr_url,
        }),
        "head_sha": _head(pr) or str(gate.get("head_sha") or "").strip(),
        "required_status_contexts": required,
        "status_contexts": contexts,
        "review": _map(review) or _map(gate.get("review_gate")),
        "merge_gate": gate,
        "findings": deepcopy(list(gate.get("findings") or [])),
        "merge_queue": _map(merge_queue),
        "work_session": _map(work_session),
        "runner": _map(runner),
        "merge_provenance": _map(merge_provenance),
        "dependency_state": _map(task_row.get("dependency_state")),
    }


def classify_completion(
    current_run: Mapping[str, Any] | None,
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Legacy projection of the Mission Bot command onto the old decision shape.

    ``current_run`` is ignored — Mission Bot never reads stored outcomes as
    authority. New code should call ``reduce_mission`` directly.
    """
    del current_run
    command = reduce_mission(snapshot)
    dossier = command.get("dossier") if isinstance(command.get("dossier"), Mapping) else {}
    decision = {
        "schema": COMPLETION_DECISION_SCHEMA,
        "state": command.get("state"),
        "route": command.get("route"),
        "reason_code": command.get("reason_code"),
        "desired_role": command.get("desired_role"),
        "retry_policy": "none",
        "board_projection": command.get("board_projection"),
        "effect": command.get("effect"),
        "mission_output": command.get("output"),
        "acceptance_findings": list(dossier.get("acceptance_findings") or []),
    }
    # COORD-51 §3.3: omit empty failing-context identity. A decision that never
    # looked at a failing check must not claim it found none.
    failing_contexts = [
        str(name) for name in list(dossier.get("failing_contexts") or []) if str(name).strip()
    ]
    if failing_contexts:
        decision["failing_contexts"] = failing_contexts
        decision["failing_check_url"] = dossier.get("failing_check_url") or ""
        decision["failing_run_attempt"] = int(dossier.get("failing_run_attempt") or 0)
        decision["failing_check_summary"] = dossier.get("failing_check_summary") or ""
    missing_artifact = dossier.get("missing_artifact")
    if isinstance(missing_artifact, Mapping) and missing_artifact:
        decision["missing_artifact"] = dict(missing_artifact)
    return decision


# Silence unused-import lint for helpers retained for snapshot parity with
# historical modules that imported math/re from this file indirectly.
_ = (math, re)


__all__ = [
    "COMPLETION_CLASSIFIER_VERSION",
    "COMPLETION_DECISION_SCHEMA",
    "COMPLETION_SNAPSHOT_SCHEMA",
    "MERGE_AUTHORIZATION_CONTEXT",
    "build_completion_snapshot",
    "classify_completion",
]
