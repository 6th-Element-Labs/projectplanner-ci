"""SIMPLIFY-8 — one CI surface: ``verify(sha) -> status``.

The public contract is intentionally small:

```text
verify(sha) -> {pending|green|red, url, contexts, stall?}
```

Mirror branches, scratchpad dispatch, polling, and janitors stay behind this
adapter. Callers (coordinator, stewards, UI, MCP/REST) must not touch those
internals directly.
"""
from __future__ import annotations

import os
import re
from typing import Any, Callable, Dict, List, Mapping, Optional

from constants import DEFAULT_PROJECT, VERIFY_CI_SCHEMA

GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
SURFACE_STATUSES = frozenset({"pending", "green", "red"})
STALL_STAGES = frozenset({"dispatch", "run", "callback"})

GREEN_RUN = frozenset({"success", "succeeded", "passed", "pass", "green"})
RED_RUN = frozenset({"failure", "failed", "error", "cancelled", "canceled", "timed_out"})
PENDING_RUN = frozenset({
    "requested", "mirrored", "triggered", "pending", "queued", "running", "in_progress",
})

DISPATCH_FAILURES = frozenset({"mirror_sync_failed", "workflow_trigger_failed"})
RUN_FAILURES = frozenset({"workflow_poll_failed", "workflow_failed"})


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalize_sha(sha: str) -> str:
    cleaned = (sha or "").strip().lower()
    if not cleaned or not GIT_SHA_RE.fullmatch(cleaned):
        raise ValueError("sha must be a git object id (7-64 hex chars)")
    return cleaned


def status_from_run(row: Mapping[str, Any] | None) -> str:
    """Map one ``external_ci_runs`` row onto the public surface status."""
    if not row:
        return "pending"
    conclusion = _norm(row.get("conclusion"))
    status = _norm(row.get("status"))
    if conclusion in GREEN_RUN or status in GREEN_RUN:
        return "green"
    if conclusion in RED_RUN or status in RED_RUN:
        return "red"
    if status in PENDING_RUN or not conclusion:
        return "pending"
    return "pending"


def stall_from_run(row: Mapping[str, Any] | None, *, surface: str) -> Optional[str]:
    """Attribute a non-green surface to dispatch / run / callback."""
    if surface == "green":
        return None
    if not row:
        return "dispatch"
    failure = _norm(row.get("failure_class"))
    status = _norm(row.get("status"))
    if failure in DISPATCH_FAILURES or status in {"requested", "mirrored"}:
        return "dispatch"
    if failure in RUN_FAILURES or status in {"triggered", "running"}:
        return "run"
    if surface == "red":
        # Terminal workflow failure is attributed to the run stage.
        return "run"
    # Board evidence exists but required GitHub status callback is absent.
    if surface == "pending" and status in GREEN_RUN:
        return "callback"
    if surface == "pending":
        if status in {"requested", "mirrored"}:
            return "dispatch"
        if status in {"triggered", "running"}:
            return "run"
        return "callback"
    return "dispatch"


def _context_rows(
        required: List[str],
        *,
        surface: str,
        run: Mapping[str, Any] | None,
        status_reader: Callable[[str], Mapping[str, Any]] | None = None,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for context in required:
        live = status_reader(context) if status_reader else None
        if live and live.get("state"):
            state = _norm(live.get("state"))
            url = live.get("target_url") or live.get("url") or (run or {}).get("run_url")
        elif surface == "green":
            state = "success"
            url = (run or {}).get("run_url")
        elif surface == "red":
            state = "failure"
            url = (run or {}).get("run_url")
        else:
            state = "pending"
            url = (run or {}).get("run_url")
        rows.append({
            "context": context,
            "state": state,
            "url": url or None,
        })
    return rows


def _required_contexts(project: str, run: Mapping[str, Any] | None = None) -> List[str]:
    from switchboard.storage.repositories import external_ci as external_ci_repo

    from_run = list((run or {}).get("required_status_contexts") or [])
    if from_run:
        return [str(c).strip() for c in from_run if str(c).strip()]
    contract = external_ci_repo._external_ci_topology_contract(project)
    contexts = list(contract.get("required_status_contexts") or [])
    if contexts:
        return [str(c).strip() for c in contexts if str(c).strip()]
    single = (run or {}).get("status_context") or contract.get("status_context")
    if single:
        return [str(single).strip()]
    env = (os.environ.get("SWITCHBOARD_CI_STATUS_CONTEXT") or "").strip()
    return [env] if env else ["Switchboard CI / VM gate"]


def _select_run(rows: List[Mapping[str, Any]], sha: str) -> Optional[Dict[str, Any]]:
    exact = [dict(r) for r in rows if _norm(r.get("source_sha")) == sha]
    pool = exact or [dict(r) for r in rows]
    if not pool:
        return None
    # Prefer newest by updated_at; list_external_ci_runs already orders DESC.
    return pool[0]


def _apply_callback_stall(
        result: Dict[str, Any],
        *,
        contexts: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """If board says green but required GH contexts are not success, stall=callback."""
    if result.get("status") != "green":
        return result
    if not contexts:
        return result
    missing = [c for c in contexts if _norm(c.get("state")) not in {"success", "success_with_warnings"}]
    if not missing:
        return result
    pending_only = all(_norm(c.get("state")) in {"pending", "expected", ""} for c in missing)
    result["status"] = "pending" if pending_only else "red"
    result["stall"] = "callback"
    result["stall_detail"] = (
        "Required GitHub status callback missing or not success for: "
        + ", ".join(c["context"] for c in missing)
    )
    return result


def _ensure_dispatch(
        sha: str,
        *,
        project: str,
        source_path: str = "",
        task_id: str = "",
        pr_number: int = 0,
        repo: str = "",
        source_fetch_ref: str = "",
) -> Dict[str, Any]:
    """Hidden ensure path — owns scratchpad / mirror dispatch."""
    import ci_scratchpad_dispatch as csd

    path = (source_path or csd.source_checkout_path()).strip()
    if pr_number:
        return csd.try_dispatch_scratchpad(
            int(pr_number),
            head_sha=sha,
            repo=repo,
            project=project,
            source_path=path,
        )
    fetch_ref = (source_fetch_ref or sha).strip()
    label = f"verify-{(task_id or sha)[:24]}".replace("/", "-")
    try:
        out = csd.dispatch_scratchpad_ref(
            sha,
            fetch_ref,
            label=label,
            repo=repo,
            project=project,
            source_path=path,
            dry_run=False,
        )
        out.setdefault("skip_reason", None)
        return out
    except Exception as exc:
        return {
            "dispatched": False,
            "skip_reason": str(exc),
            "head_sha": sha,
            "error": str(exc),
        }


def verify(
        sha: str,
        *,
        project: str = DEFAULT_PROJECT,
        ensure: bool = False,
        source_path: str = "",
        task_id: str = "",
        pr_number: int = 0,
        repo: str = "",
        source_fetch_ref: str = "",
        actor: str = "system",
        status_reader: Callable[[str], Mapping[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Read (and optionally ensure) CI status for one source SHA.

    Manual re-verify is this function with ``ensure=True`` and exactly a SHA.
    """
    from switchboard.storage.repositories import external_ci as external_ci_repo

    try:
        normalized = _normalize_sha(sha)
    except ValueError as exc:
        return {
            "schema": VERIFY_CI_SCHEMA,
            "ok": False,
            "error": str(exc),
            "error_code": "invalid_sha",
            "status": "red",
            "url": None,
            "contexts": [],
            "stall": "dispatch",
            "stall_detail": str(exc),
            "sha": (sha or "").strip() or None,
            "project": project,
            "actor": actor,
        }

    ensured_payload: Optional[Dict[str, Any]] = None
    if ensure:
        ensured_payload = _ensure_dispatch(
            normalized,
            project=project,
            source_path=source_path,
            task_id=task_id,
            pr_number=int(pr_number or 0),
            repo=repo,
            source_fetch_ref=source_fetch_ref,
        )

    rows = external_ci_repo.list_external_ci_runs(
        source_sha=normalized, project=project)
    if task_id and not rows:
        rows = external_ci_repo.list_external_ci_runs(
            task_id=task_id, project=project)
        rows = [r for r in rows if _norm(r.get("source_sha")).startswith(normalized[:7])]
    run = _select_run(rows, normalized)

    if run is None and not ensure:
        required = _required_contexts(project, None)
        contexts = _context_rows(required, surface="pending", run=None, status_reader=status_reader)
        return {
            "schema": VERIFY_CI_SCHEMA,
            "ok": True,
            "sha": normalized,
            "status": "pending",
            "url": None,
            "contexts": contexts,
            "stall": "dispatch",
            "stall_detail": "No external CI run recorded for this SHA (never dispatched).",
            "run_id": None,
            "failure_class": None,
            "ensured": False,
            "project": project,
            "actor": actor,
            "task_id": task_id or None,
        }

    if run is None and ensure:
        # Ensure attempted but no board row yet — surface the dispatch stall.
        required = _required_contexts(project, None)
        contexts = _context_rows(required, surface="pending", run=None, status_reader=status_reader)
        detail = None
        if ensured_payload:
            detail = (
                ensured_payload.get("skip_reason")
                or ensured_payload.get("error")
                or ensured_payload.get("message")
            )
        return {
            "schema": VERIFY_CI_SCHEMA,
            "ok": bool(ensured_payload and ensured_payload.get("dispatched")),
            "sha": normalized,
            "status": "pending",
            "url": (ensured_payload or {}).get("run_url"),
            "contexts": contexts,
            "stall": "dispatch",
            "stall_detail": detail or "Ensure dispatch did not create a board CI run.",
            "run_id": (ensured_payload or {}).get("run_id"),
            "failure_class": "mirror_sync_failed" if ensured_payload and not ensured_payload.get("dispatched") else None,
            "ensured": True,
            "ensure_result": ensured_payload,
            "project": project,
            "actor": actor,
            "task_id": task_id or None,
        }

    surface = status_from_run(run)
    stall = stall_from_run(run, surface=surface)
    required = _required_contexts(project, run)
    contexts = _context_rows(
        required, surface=surface, run=run, status_reader=status_reader)

    result: Dict[str, Any] = {
        "schema": VERIFY_CI_SCHEMA,
        "ok": True,
        "sha": normalized,
        "status": surface,
        "url": run.get("run_url") or run.get("logs_url"),
        "contexts": contexts,
        "stall": stall,
        "stall_detail": run.get("failure_reason") if stall else None,
        "run_id": run.get("run_id"),
        "failure_class": run.get("failure_class"),
        "ensured": bool(ensure),
        "project": project,
        "actor": actor,
        "task_id": task_id or run.get("task_id") or None,
        "latest_run": {
            "run_id": run.get("run_id"),
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "source_sha": run.get("source_sha"),
            "failure_class": run.get("failure_class"),
        },
    }
    if ensure:
        result["ensure_result"] = ensured_payload
    result = _apply_callback_stall(result, contexts=contexts)
    if result.get("status") not in SURFACE_STATUSES:
        result["status"] = "pending"
    if result.get("stall") and result["stall"] not in STALL_STAGES:
        result["stall"] = "dispatch"
    return result


def execute_mapping_result(
        data: Mapping[str, Any],
        *,
        actor: str = "system",
        status_reader: Callable[[str], Mapping[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Adapter entry used by REST/MCP."""
    payload = dict(data or {})
    return verify(
        str(payload.get("sha") or payload.get("source_sha") or ""),
        project=str(payload.get("project") or DEFAULT_PROJECT),
        ensure=bool(payload.get("ensure")),
        source_path=str(payload.get("source_path") or ""),
        task_id=str(payload.get("task_id") or ""),
        pr_number=int(payload.get("pr_number") or 0),
        repo=str(payload.get("repo") or ""),
        source_fetch_ref=str(payload.get("source_fetch_ref") or ""),
        actor=actor,
        status_reader=status_reader,
    )


__all__ = [
    "GIT_SHA_RE",
    "SURFACE_STATUSES",
    "STALL_STAGES",
    "status_from_run",
    "stall_from_run",
    "verify",
    "execute_mapping_result",
]
