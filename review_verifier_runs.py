"""Checkpointed verifier-job manifests for Switchboard review/audit workflows.

The forge-style review path fans findings out to skeptic lenses such as
verify/repro/impact. This module keeps that fan-out resumable and auditable:
completed jobs are reused, failed or missing jobs are explicit, and reports can
fail closed when load-bearing findings lack required verifier coverage.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


SCHEMA = "switchboard.review_verifier_run.v1"
DEFAULT_LENSES = ("verify", "repro", "impact")
COMPLETED_STATUSES = {"completed", "confirmed", "refuted", "not_applicable"}
RETRYABLE_STATUSES = {"pending", "running", "token_limit", "rate_limit", "error", "missing"}
LOAD_BEARING_SEVERITIES = {"critical", "high"}
TOKEN_LIMIT_MARKERS = (
    "token limit",
    "token-limit",
    "context length",
    "context window",
    "maximum context",
    "rate limit",
    "rate-limit",
)


def _stable_id(parts: Iterable[Any], *, prefix: str) -> str:
    raw = "\x1f".join(str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def _finding_id(finding: Mapping[str, Any], index: int) -> str:
    for key in ("finding_id", "id", "code"):
        value = finding.get(key)
        if value:
            return str(value)
    return _stable_id([
        finding.get("dimension", ""),
        finding.get("file", ""),
        finding.get("line", ""),
        finding.get("description", finding.get("title", "")),
        index,
    ], prefix="finding")


def _is_load_bearing(finding: Mapping[str, Any]) -> bool:
    if "load_bearing" in finding:
        return bool(finding.get("load_bearing"))
    severity = str(finding.get("severity", "")).strip().lower()
    return severity in LOAD_BEARING_SEVERITIES


def normalize_finding(finding: Mapping[str, Any], index: int) -> Dict[str, Any]:
    """Return the stable finding shape stored in verifier checkpoints."""
    normalized = dict(finding)
    normalized["finding_id"] = _finding_id(finding, index)
    normalized["dimension"] = str(normalized.get("dimension") or "general")
    normalized["severity"] = str(normalized.get("severity") or "unknown").lower()
    normalized["load_bearing"] = _is_load_bearing(normalized)
    return normalized


def make_verifier_job(finding: Mapping[str, Any], lens: str) -> Dict[str, Any]:
    finding_id = str(finding["finding_id"])
    dimension = str(finding.get("dimension") or "general")
    return {
        "job_id": _stable_id([finding_id, lens], prefix="verifier"),
        "finding_id": finding_id,
        "dimension": dimension,
        "lens": lens,
        "status": "pending",
        "attempts": 0,
        "updated_at": None,
        "error": None,
        "failure_class": None,
        "transcript_path": None,
        "result": None,
    }


def create_verifier_run(
    findings: Sequence[Mapping[str, Any]],
    *,
    run_id: str = "",
    lenses: Sequence[str] = DEFAULT_LENSES,
    required_lenses: Sequence[str] = DEFAULT_LENSES,
    required_dimensions: Sequence[str] = (),
    metadata: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a deterministic manifest for all finding/lens verifier jobs."""
    normalized_findings = [normalize_finding(finding, index) for index, finding in enumerate(findings)]
    jobs = [
        make_verifier_job(finding, lens)
        for finding in normalized_findings
        for lens in lenses
    ]
    return {
        "schema": SCHEMA,
        "run_id": run_id or _stable_id(
            [finding["finding_id"] for finding in normalized_findings] + list(lenses),
            prefix="review",
        ),
        "created_at": time.time(),
        "updated_at": time.time(),
        "lenses": list(lenses),
        "required_lenses": list(required_lenses),
        "required_dimensions": list(required_dimensions),
        "metadata": dict(metadata or {}),
        "findings": normalized_findings,
        "jobs": jobs,
        "summary": summarize_verifier_run({
            "findings": normalized_findings,
            "jobs": jobs,
            "required_lenses": list(required_lenses),
            "required_dimensions": list(required_dimensions),
        }),
    }


def _job_index(manifest: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        str(job.get("job_id")): dict(job)
        for job in manifest.get("jobs") or []
        if job.get("job_id")
    }


def merge_checkpoint(base: Mapping[str, Any], checkpoint: Mapping[str, Any]) -> Dict[str, Any]:
    """Merge prior job results into a fresh manifest with the same deterministic ids."""
    merged = dict(base)
    previous = _job_index(checkpoint)
    jobs = []
    for job in base.get("jobs") or []:
        old = previous.get(str(job.get("job_id")))
        if old:
            current = dict(job)
            current.update(old)
            jobs.append(current)
        else:
            jobs.append(dict(job))
    merged["jobs"] = jobs
    merged["created_at"] = checkpoint.get("created_at") or base.get("created_at")
    merged["updated_at"] = time.time()
    merged["summary"] = summarize_verifier_run(merged)
    return merged


def load_checkpoint(path: str | Path) -> Dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA:
        raise ValueError(f"unsupported verifier checkpoint schema: {data.get('schema')}")
    return data


def save_checkpoint(manifest: Mapping[str, Any], path: str | Path) -> None:
    """Atomically write a checkpoint manifest."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(manifest)
    payload["updated_at"] = time.time()
    payload["summary"] = summarize_verifier_run(payload)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(target.parent), delete=False) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temp_name = handle.name
    os.replace(temp_name, target)


def classify_verifier_error(message: str) -> Dict[str, str]:
    lowered = (message or "").lower()
    if any(marker in lowered for marker in TOKEN_LIMIT_MARKERS):
        return {"status": "token_limit", "failure_class": "context_limit"}
    return {"status": "error", "failure_class": "verifier_error"}


def record_job_result(
    manifest: Mapping[str, Any],
    job_id: str,
    *,
    status: str,
    result: Optional[Mapping[str, Any]] = None,
    error: str = "",
    transcript_path: str = "",
    failure_class: str = "",
) -> Dict[str, Any]:
    """Return a manifest copy with one job result recorded."""
    updated = dict(manifest)
    jobs = []
    found = False
    for job in manifest.get("jobs") or []:
        current = dict(job)
        if current.get("job_id") == job_id:
            found = True
            current["status"] = status
            current["result"] = dict(result or {}) if result is not None else None
            current["error"] = error or None
            current["transcript_path"] = transcript_path or current.get("transcript_path")
            current["failure_class"] = failure_class or current.get("failure_class")
            current["attempts"] = int(current.get("attempts") or 0) + 1
            current["updated_at"] = time.time()
        jobs.append(current)
    if not found:
        raise KeyError(f"unknown verifier job_id: {job_id}")
    updated["jobs"] = jobs
    updated["updated_at"] = time.time()
    updated["summary"] = summarize_verifier_run(updated)
    return updated


def record_job_failure(
    manifest: Mapping[str, Any],
    job_id: str,
    *,
    error: str,
    transcript_path: str = "",
) -> Dict[str, Any]:
    classified = classify_verifier_error(error)
    return record_job_result(
        manifest,
        job_id,
        status=classified["status"],
        error=error,
        transcript_path=transcript_path,
        failure_class=classified["failure_class"],
    )


def resume_jobs(manifest: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Return only missing or retryable verifier jobs; completed jobs are skipped."""
    jobs = []
    for job in manifest.get("jobs") or []:
        status = str(job.get("status") or "missing")
        if status not in COMPLETED_STATUSES:
            retry = dict(job)
            retry["resume_reason"] = "missing" if status == "missing" else status
            jobs.append(retry)
    return jobs


def shard_jobs(jobs: Sequence[Mapping[str, Any]], *, shard_size: int = 12) -> List[List[Dict[str, Any]]]:
    if shard_size <= 0:
        raise ValueError("shard_size must be positive")
    materialized = [dict(job) for job in jobs]
    return [
        materialized[index:index + shard_size]
        for index in range(0, len(materialized), shard_size)
    ]


def _coverage_by_finding(manifest: Mapping[str, Any]) -> Dict[str, Dict[str, str]]:
    coverage: Dict[str, Dict[str, str]] = {}
    for job in manifest.get("jobs") or []:
        finding_id = str(job.get("finding_id") or "")
        lens = str(job.get("lens") or "")
        if finding_id and lens:
            coverage.setdefault(finding_id, {})[lens] = str(job.get("status") or "missing")
    return coverage


def summarize_verifier_run(manifest: Mapping[str, Any]) -> Dict[str, Any]:
    jobs = [dict(job) for job in manifest.get("jobs") or []]
    total = len(jobs)
    completed = sum(1 for job in jobs if str(job.get("status")) in COMPLETED_STATUSES)
    token_limit = sum(1 for job in jobs if str(job.get("status")) == "token_limit")
    errors = sum(1 for job in jobs if str(job.get("status")) in {"error", "rate_limit", "token_limit"})
    missing = sum(1 for job in jobs if str(job.get("status") or "missing") in RETRYABLE_STATUSES)
    by_lens: Dict[str, Dict[str, Any]] = {}
    for job in jobs:
        lens = str(job.get("lens") or "unknown")
        item = by_lens.setdefault(lens, {"total": 0, "completed": 0, "errors": 0, "token_limit": 0})
        item["total"] += 1
        status = str(job.get("status") or "missing")
        if status in COMPLETED_STATUSES:
            item["completed"] += 1
        if status in {"error", "rate_limit", "token_limit"}:
            item["errors"] += 1
        if status == "token_limit":
            item["token_limit"] += 1
    for item in by_lens.values():
        item["completion_ratio"] = item["completed"] / item["total"] if item["total"] else 1.0

    required_lenses = list(manifest.get("required_lenses") or DEFAULT_LENSES)
    required_dimensions = {
        str(dimension)
        for dimension in (manifest.get("required_dimensions") or [])
    }
    coverage = _coverage_by_finding(manifest)
    unverified_load_bearing: List[Dict[str, Any]] = []
    for finding in manifest.get("findings") or []:
        if not finding.get("load_bearing"):
            continue
        dimension = str(finding.get("dimension") or "general")
        if required_dimensions and dimension not in required_dimensions:
            continue
        finding_id = str(finding.get("finding_id"))
        statuses = coverage.get(finding_id, {})
        missing_lenses = [
            lens for lens in required_lenses
            if statuses.get(lens) not in COMPLETED_STATUSES
        ]
        if missing_lenses:
            unverified_load_bearing.append({
                "finding_id": finding_id,
                "dimension": dimension,
                "severity": finding.get("severity"),
                "missing_lenses": missing_lenses,
            })

    fail_closed = bool(unverified_load_bearing)
    return {
        "total_jobs": total,
        "completed_jobs": completed,
        "completion_ratio": completed / total if total else 1.0,
        "missing_or_retryable_jobs": missing,
        "error_jobs": errors,
        "token_limit_jobs": token_limit,
        "by_lens": by_lens,
        "unverified_load_bearing": unverified_load_bearing,
        "fail_closed": fail_closed,
        "status": "red" if fail_closed else ("yellow" if errors or missing else "pass"),
    }


def format_verifier_summary(manifest: Mapping[str, Any]) -> str:
    summary = summarize_verifier_run(manifest)
    lines = [
        "Switchboard verifier coverage",
        f"schema={SCHEMA}",
        f"run_id={manifest.get('run_id')}",
        f"status={summary['status']} fail_closed={summary['fail_closed']}",
        (
            f"completion={summary['completed_jobs']}/{summary['total_jobs']} "
            f"({summary['completion_ratio']:.1%})"
        ),
        f"token_limit_jobs={summary['token_limit_jobs']} error_jobs={summary['error_jobs']}",
    ]
    for lens, item in sorted((summary.get("by_lens") or {}).items()):
        lines.append(
            f"- {lens}: {item['completed']}/{item['total']} "
            f"({item['completion_ratio']:.1%}), errors={item['errors']}, token_limit={item['token_limit']}"
        )
    for finding in summary.get("unverified_load_bearing") or []:
        lines.append(
            "- unverified_load_bearing "
            f"{finding['finding_id']} missing={','.join(finding['missing_lenses'])}"
        )
    return "\n".join(lines) + "\n"
