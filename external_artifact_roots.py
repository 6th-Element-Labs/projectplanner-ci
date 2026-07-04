"""External artifact/root provenance checks for review and build workflows.

Review jobs sometimes consume temp worktrees, generated reports, or uploaded
artifacts that are outside the repository under review. This module makes those
inputs explicit and fail-closed: required roots must exist and be versioned,
attached, URL-backed, or deliberately marked non-reproducible before a workflow
can present a green report.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence
from urllib.parse import urlparse


SCHEMA = "switchboard.external_artifact_roots.v1"
REPRODUCIBLE_PROVENANCE = {"repo", "versioned", "attached", "url", "git_ref"}
NON_REPRODUCIBLE_PROVENANCE = {"non_reproducible"}


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_list(value: Any) -> List[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return [value]


def _git(repo: Path, args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _git_out(repo: Path, args: List[str]) -> str:
    result = _git(repo, args)
    return result.stdout.strip() if result.returncode == 0 else ""


def _inside_repo(path: Path, repo_root: Path) -> bool:
    try:
        path.resolve().relative_to(repo_root.resolve())
        return True
    except ValueError:
        return False


def _git_root(path: Path) -> str:
    if not path.exists():
        return ""
    cwd = path if path.is_dir() else path.parent
    result = _git(cwd, ["rev-parse", "--show-toplevel"])
    return result.stdout.strip() if result.returncode == 0 else ""


def _git_head(path: Path) -> str:
    root = _git_root(path)
    if not root:
        return ""
    return _git_out(Path(root), ["rev-parse", "HEAD"])


def _git_dirty(path: Path) -> bool:
    root = _git_root(path)
    if not root:
        return False
    return bool(_git_out(Path(root), ["status", "--porcelain", "--untracked-files=all"]))


def _url_ok(value: str) -> bool:
    parsed = urlparse(value or "")
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _classify_path(path_value: str, repo_root: Path) -> Dict[str, Any]:
    raw = str(path_value or "").strip()
    path = Path(raw)
    resolved = path if path.is_absolute() else (repo_root / path)
    resolved = resolved.resolve()
    exists = resolved.exists()
    in_repo = exists and _inside_repo(resolved, repo_root)
    git_root = _git_root(resolved) if exists else ""
    git_head = _git_head(resolved) if exists else ""
    dirty = _git_dirty(resolved) if exists and git_root else False
    if in_repo:
        source_class = "repo"
    elif git_root and git_head:
        source_class = "external_versioned"
    elif raw.startswith(("/tmp/", "/private/tmp/", "/var/tmp/")):
        source_class = "external_temp"
    elif path.is_absolute():
        source_class = "external_path"
    else:
        source_class = "repo"
    return {
        "kind": "path",
        "value": raw,
        "resolved": str(resolved),
        "exists": exists,
        "source_class": source_class,
        "git_root": git_root or None,
        "git_head": git_head or None,
        "git_dirty": dirty,
    }


def _classify_url(url: str) -> Dict[str, Any]:
    raw = str(url or "").strip()
    parsed = urlparse(raw)
    return {
        "kind": "url",
        "value": raw,
        "exists": _url_ok(raw),
        "source_class": "external_url" if _url_ok(raw) else "invalid_url",
        "host": parsed.netloc or None,
    }


def _classify_ref(ref: str, repo_root: Path) -> Dict[str, Any]:
    raw = str(ref or "").strip()
    ok = bool(raw) and _git(repo_root, ["cat-file", "-e", f"{raw}^{{commit}}"]).returncode == 0
    return {
        "kind": "ref",
        "value": raw,
        "exists": ok,
        "source_class": "git_ref" if ok else "missing_ref",
    }


def normalize_inputs(inputs: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for index, item in enumerate(inputs):
        record = dict(item)
        input_id = str(record.get("input_id") or record.get("id") or f"input-{index + 1}")
        kind = str(record.get("kind") or ("url" if record.get("url") else "ref" if record.get("ref") else "path")).lower()
        value = record.get("value")
        if value is None:
            value = record.get(kind) or record.get("path") or record.get("url") or record.get("ref")
        normalized.append({
            "input_id": input_id,
            "kind": kind,
            "value": str(value or ""),
            "role": record.get("role") or record.get("usage") or "input",
            "required": _as_bool(record.get("required"), True),
            "provenance": str(record.get("provenance") or record.get("source") or "").strip().lower(),
            "attached": _as_bool(record.get("attached"), False),
            "non_reproducible": _as_bool(record.get("non_reproducible"), False),
            "reason": str(record.get("reason") or record.get("non_reproducible_reason") or "").strip(),
            "finding_ids": [str(v) for v in _as_list(record.get("finding_ids") or record.get("finding_id"))],
        })
    return normalized


def evaluate_input(record: Mapping[str, Any], repo_root: str | Path) -> Dict[str, Any]:
    repo = Path(repo_root).resolve()
    kind = str(record.get("kind") or "path").lower()
    value = str(record.get("value") or "")
    if kind == "url":
        classified = _classify_url(value)
    elif kind == "ref":
        classified = _classify_ref(value, repo)
    else:
        classified = _classify_path(value, repo)

    provenance = str(record.get("provenance") or "").lower()
    if record.get("attached"):
        provenance = provenance or "attached"
    if record.get("non_reproducible"):
        provenance = provenance or "non_reproducible"
    required = _as_bool(record.get("required"), True)

    findings: List[Dict[str, Any]] = []
    if required and not classified.get("exists"):
        findings.append({
            "severity": "high",
            "code": "external_root_missing",
            "detail": "Required external input is missing or unreachable.",
            "blocking": True,
        })
    if classified.get("exists") and classified.get("source_class") == "external_temp":
        if provenance not in REPRODUCIBLE_PROVENANCE and provenance not in NON_REPRODUCIBLE_PROVENANCE:
            findings.append({
                "severity": "high",
                "code": "external_temp_root_unprovenanced",
                "detail": "External temp input must be versioned, attached, URL-backed, or declared non-reproducible.",
                "blocking": True,
            })
    if classified.get("exists") and classified.get("source_class") == "external_versioned":
        if classified.get("git_dirty") and provenance != "attached":
            findings.append({
                "severity": "medium",
                "code": "external_versioned_root_dirty",
                "detail": "External git input has uncommitted or untracked changes.",
                "blocking": False,
            })
    if provenance == "non_reproducible" and not record.get("reason"):
        findings.append({
            "severity": "medium",
            "code": "non_reproducible_without_reason",
            "detail": "Non-reproducible inputs need an explicit reason.",
            "blocking": False,
        })

    blocking = [finding for finding in findings if finding.get("blocking")]
    status = "red" if blocking else ("yellow" if findings or provenance == "non_reproducible" else "pass")
    if not required and not classified.get("exists"):
        status = "yellow"
    return {
        **dict(record),
        **classified,
        "provenance": provenance or None,
        "status": status,
        "findings": findings,
    }


def run_external_artifact_preflight(
    inputs: Iterable[Mapping[str, Any]],
    repo_root: str | Path,
    *,
    workflow_id: str = "",
    project: str = "",
) -> Dict[str, Any]:
    """Evaluate declared external roots before review/build artifacts are created."""
    evaluated = [evaluate_input(record, repo_root) for record in normalize_inputs(inputs)]
    findings: List[Dict[str, Any]] = []
    for item in evaluated:
        for finding in item.get("findings") or []:
            findings.append({
                **finding,
                "input_id": item.get("input_id"),
                "value": item.get("value"),
                "source_class": item.get("source_class"),
            })
    blocking = [finding for finding in findings if finding.get("blocking")]
    status = "red" if blocking else ("yellow" if findings or any(i.get("status") == "yellow" for i in evaluated) else "pass")
    source_counts: Dict[str, int] = {}
    for item in evaluated:
        source = str(item.get("source_class") or "unknown")
        source_counts[source] = source_counts.get(source, 0) + 1
    return {
        "schema": SCHEMA,
        "workflow_id": workflow_id,
        "project": project,
        "repo_root": str(Path(repo_root).resolve()),
        "checked_at": time.time(),
        "ok": status == "pass",
        "status": status,
        "source_counts": source_counts,
        "inputs": evaluated,
        "findings": findings,
    }


def attribute_findings(findings: Iterable[Mapping[str, Any]],
                       preflight: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Annotate review findings with repo/external-temp/external-versioned source classes."""
    by_finding: Dict[str, List[str]] = {}
    for item in preflight.get("inputs") or []:
        source = str(item.get("source_class") or "unknown")
        for finding_id in item.get("finding_ids") or []:
            by_finding.setdefault(str(finding_id), []).append(source)
    out = []
    for finding in findings:
        current = dict(finding)
        finding_id = str(current.get("finding_id") or current.get("id") or current.get("code") or "")
        sources = by_finding.get(finding_id) or ["repo"]
        current["source_classes"] = sorted(set(sources))
        current["external_state"] = any(source.startswith("external") for source in sources)
        out.append(current)
    return out


def format_external_roots_header(report: Mapping[str, Any]) -> str:
    lines = [
        "Switchboard external artifact roots preflight",
        f"schema={SCHEMA}",
        f"status={report.get('status')} ok={report.get('ok')}",
        f"workflow_id={report.get('workflow_id') or '(unspecified)'}",
        f"project={report.get('project') or '(unspecified)'}",
        f"repo_root={report.get('repo_root')}",
        f"source_counts={json.dumps(report.get('source_counts') or {}, sort_keys=True)}",
    ]
    for finding in report.get("findings") or []:
        lines.append(
            f"- [{finding.get('severity')}] {finding.get('input_id')} "
            f"{finding.get('code')}: {finding.get('detail')}"
        )
    return "\n".join(lines) + "\n"


def load_inputs(path: str | Path) -> List[Dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, Mapping):
        return normalize_inputs(data.get("inputs") or [])
    return normalize_inputs(data)
