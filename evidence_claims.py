"""Verify narrative deliverable claims against declared evidence.

Switchboard comments are useful coordination, but they are not proof by
themselves. This module scans task activity for artifact/report/page/server
claims and checks whether the activity also declares repo-accessible evidence:
relative paths, HTTP(S) URLs, or reachable git refs.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence
from urllib.parse import urlparse


SCHEMA = "switchboard.evidence_claims.v1"

CLAIM_KIND_PREFIXES = (
    "comment",
    "task.claim.completed",
    "task.done_blocked",
    "task.offline_verified",
    "task.offline_evidence_corrected",
)
COMPLETION_KINDS = {
    "task.claim.completed",
    "task.done_blocked",
    "task.offline_verified",
    "task.offline_evidence_corrected",
}
CLAIM_KEYWORDS = (
    "artifact",
    "deliverable",
    "generated page",
    "review page",
    "audit page",
    "report",
    "server wiring",
    "run log",
)
FILE_RE = re.compile(
    r"(?<![\w:/.-])([A-Za-z0-9_.@/-]+\."
    r"(?:html|htm|json|md|pdf|png|jpe?g|svg|csv|tsv|ya?ml|sqlite|db|txt|log))"
    r"(?![\w.-])",
    re.I,
)
URL_RE = re.compile(r"https?://[^\s`'\"<>),]+", re.I)
PATH_KEYS = {
    "evidence_path",
    "evidence_paths",
    "artifact_path",
    "artifact_paths",
    "report_path",
    "report_paths",
    "file",
    "files",
    "path",
    "paths",
    "proof_file",
    "proof_files",
}
URL_KEYS = {
    "evidence_url",
    "evidence_urls",
    "artifact_url",
    "artifact_urls",
    "report_url",
    "report_urls",
    "url",
    "urls",
    "pr_url",
}
REF_KEYS = {
    "evidence_ref",
    "evidence_refs",
    "git_ref",
    "git_refs",
    "commit_sha",
    "commit_shas",
    "head_sha",
    "merged_sha",
}
BRANCH_KEYS = {
    "branch",
    "branches",
    "git_branch",
    "head_ref",
    "head_branch",
}


def _flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return " ".join(_flatten_text(v) for v in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return " ".join(_flatten_text(v) for v in value)
    return str(value)


def _payload(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"text": raw}
    return raw


def _as_list(value: Any) -> List[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return [value]


def _collect_declared(value: Any) -> Dict[str, List[str]]:
    declared = {"paths": [], "urls": [], "refs": [], "branches": []}

    def visit(item: Any, key: str = "") -> None:
        if isinstance(item, Mapping):
            for child_key, child in item.items():
                visit(child, str(child_key).strip().lower())
            return
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            for child in item:
                visit(child, key)
            return
        values = [str(v).strip() for v in _as_list(item) if str(v).strip()]
        if not values:
            return
        if key in PATH_KEYS:
            declared["paths"].extend(values)
        elif key in URL_KEYS:
            declared["urls"].extend(values)
        elif key in REF_KEYS:
            declared["refs"].extend(values)
        elif key in BRANCH_KEYS:
            declared["branches"].extend(values)

    visit(value)
    for bucket in declared:
        seen = set()
        deduped = []
        for item in declared[bucket]:
            if item not in seen:
                seen.add(item)
                deduped.append(item)
        declared[bucket] = deduped
    return declared


def _claimed_artifacts(text: str) -> List[str]:
    urls = set(URL_RE.findall(text or ""))
    artifacts: List[str] = []
    for match in FILE_RE.finditer(text or ""):
        value = match.group(1).rstrip(".,;:")
        if any(value in url for url in urls):
            continue
        if value not in artifacts:
            artifacts.append(value)
    return artifacts


def _claim_keywords(text: str) -> List[str]:
    lower = (text or "").lower()
    return [keyword for keyword in CLAIM_KEYWORDS if keyword in lower]


def _path_check(repo_root: Path, value: str) -> Dict[str, Any]:
    raw = (value or "").strip()
    if not raw:
        return {"type": "path", "value": value, "status": "missing", "detail": "empty path"}
    path = Path(raw)
    if path.is_absolute():
        return {
            "type": "path",
            "value": raw,
            "status": "missing",
            "detail": "absolute paths are not repo-accessible evidence",
        }
    resolved = (repo_root / path).resolve()
    try:
        resolved.relative_to(repo_root.resolve())
    except ValueError:
        return {
            "type": "path",
            "value": raw,
            "status": "missing",
            "detail": "path escapes repository root",
        }
    if resolved.exists():
        return {"type": "path", "value": raw, "status": "pass", "detail": str(resolved)}
    return {"type": "path", "value": raw, "status": "missing", "detail": "path not found"}


def _url_check(value: str) -> Dict[str, Any]:
    raw = (value or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return {"type": "url", "value": raw, "status": "pass", "detail": "declared HTTP(S) evidence URL"}
    return {"type": "url", "value": raw, "status": "missing", "detail": "URL must be HTTP(S)"}


def _normalize_branch_ref(branch: str) -> str:
    raw = (branch or "").strip()
    if not raw:
        return ""
    if raw.startswith("refs/heads/"):
        return raw
    if raw.startswith("origin/"):
        return f"refs/heads/{raw[len('origin/'):]}"
    return f"refs/heads/{raw}"


def _remote_branch_sha(repo_root: Path, branch: str) -> str:
    """Return origin tip SHA for a branch via ls-remote, or '' if unreachable."""
    ref = _normalize_branch_ref(branch)
    if not ref:
        return ""
    try:
        proc = subprocess.run(
            ["git", "ls-remote", "--exit-code", "--heads", "origin", ref],
            cwd=str(repo_root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    first = (proc.stdout.splitlines() or [""])[0].strip()
    if not first:
        return ""
    return first.split()[0].strip().lower()


def _ref_check(repo_root: Path, value: str,
               batched: Mapping[str, Dict[str, Any]] | None = None,
               branches: Sequence[str] | None = None) -> Dict[str, Any]:
    raw = (value or "").strip()
    if not raw:
        return {"type": "ref", "value": value, "status": "missing", "detail": "empty ref"}
    if batched is not None and raw in batched:
        result = dict(batched[raw])
        if result.get("status") == "pass" or not branches:
            return result
        # Batched local miss: still allow origin tip equality below.
    else:
        proc = subprocess.run(
            ["git", "cat-file", "-e", f"{raw}^{{commit}}"],
            cwd=str(repo_root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if proc.returncode == 0:
            return {
                "type": "ref",
                "value": raw,
                "status": "pass",
                "detail": "git commit is reachable",
            }
    # Control-plane clones often lack task branches. Accept origin tip equality when the
    # claim also declared the branch (BUG-117).
    want = raw.lower()
    for branch in branches or []:
        remote_sha = _remote_branch_sha(repo_root, branch)
        if remote_sha and remote_sha == want:
            return {
                "type": "ref",
                "value": raw,
                "status": "pass",
                "detail": f"git commit is reachable on origin ({_normalize_branch_ref(branch)})",
            }
    return {"type": "ref", "value": raw, "status": "missing", "detail": "git commit/ref not reachable"}


def _batch_ref_checks(repo_root: Path, refs: Iterable[str],
                      branches: Sequence[str] | None = None) -> Dict[str, Dict[str, Any]]:
    unique = list(dict.fromkeys((ref or "").strip() for ref in refs if (ref or "").strip()))
    if not unique:
        return {}
    expressions = [f"{ref}^{{commit}}" for ref in unique]
    proc = subprocess.run(
        ["git", "cat-file", "--batch-check=%(objectname) %(objecttype)"],
        cwd=str(repo_root), text=True, input="\n".join(expressions) + "\n",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    lines = proc.stdout.splitlines()
    results: Dict[str, Dict[str, Any]] = {}
    missing: List[str] = []
    for index, raw in enumerate(unique):
        line = lines[index] if index < len(lines) else ""
        reachable = bool(re.fullmatch(r"[0-9a-fA-F]{40,64} commit", line.strip()))
        if reachable:
            results[raw] = {
                "type": "ref", "value": raw,
                "status": "pass",
                "detail": "git commit is reachable",
            }
        else:
            missing.append(raw)
    if missing and branches:
        remote_by_branch = {
            branch: _remote_branch_sha(repo_root, branch) for branch in branches if branch
        }
        for raw in missing:
            want = raw.lower()
            matched_branch = next(
                (branch for branch, sha in remote_by_branch.items() if sha == want),
                "",
            )
            if matched_branch:
                results[raw] = {
                    "type": "ref",
                    "value": raw,
                    "status": "pass",
                    "detail": (
                        "git commit is reachable on origin "
                        f"({_normalize_branch_ref(matched_branch)})"
                    ),
                }
            else:
                results[raw] = {
                    "type": "ref", "value": raw,
                    "status": "missing",
                    "detail": "git commit/ref not reachable",
                }
    else:
        for raw in missing:
            results[raw] = {
                "type": "ref", "value": raw,
                "status": "missing",
                "detail": "git commit/ref not reachable",
            }
    return results


def _evidence_checks(declared: Mapping[str, List[str]], repo_root: Path,
                     batched_refs: Mapping[str, Dict[str, Any]] | None = None) -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []
    branches = list(declared.get("branches") or [])
    for value in declared.get("paths") or []:
        checks.append(_path_check(repo_root, value))
    for value in declared.get("urls") or []:
        checks.append(_url_check(value))
    for value in declared.get("refs") or []:
        checks.append(
            _ref_check(repo_root, value, batched=batched_refs, branches=branches)
        )
    return checks


def _kind_is_claim_relevant(kind: str) -> bool:
    return any((kind or "").startswith(prefix) for prefix in CLAIM_KIND_PREFIXES)


def evaluate_activity(activity: Mapping[str, Any], repo_root: str | Path,
                      batched_refs: Mapping[str, Dict[str, Any]] | None = None) -> Dict[str, Any]:
    """Return a claim/evidence report for one activity row, or {} if irrelevant."""
    kind = str(activity.get("kind") or "")
    if not _kind_is_claim_relevant(kind):
        return {}
    payload = _payload(activity.get("payload"))
    text = _flatten_text(payload)
    artifacts = _claimed_artifacts(text)
    keywords = _claim_keywords(text)
    declared = _collect_declared(payload)
    # Branches are reachability hints for refs, not standalone evidence.
    has_declared = any(declared.get(key) for key in ("paths", "urls", "refs"))
    if not artifacts and not keywords and not has_declared:
        return {}

    checks = _evidence_checks(declared, Path(repo_root), batched_refs=batched_refs)
    missing = [check for check in checks if check.get("status") != "pass"]
    if checks and not missing:
        status = "pass"
        severity = "low"
        code = "claim_evidence_verified"
        detail = "Declared evidence is repo-accessible or an HTTP(S) URL."
    elif checks and missing:
        status = "red"
        severity = "high"
        code = "claim_evidence_missing"
        detail = "Claim declares evidence, but one or more evidence entries are missing or invalid."
    elif kind in COMPLETION_KINDS:
        status = "red"
        severity = "high"
        code = "claim_without_evidence"
        detail = "Completion/offline evidence claims an artifact or report without declared evidence paths, URLs, or refs."
    else:
        status = "yellow"
        severity = "medium"
        code = "claim_without_evidence"
        detail = "Comment claims an artifact or deliverable without declared evidence paths, URLs, or refs."

    claim_text = " ".join(text.split())
    return {
        "schema": SCHEMA,
        "task_id": activity.get("task_id"),
        "actor": activity.get("actor"),
        "activity_kind": kind,
        "created_at": activity.get("created_at"),
        "status": status,
        "severity": severity,
        "code": code,
        "failure_class": "missing_data" if status != "pass" else None,
        "detail": detail,
        "claim": {
            "text": claim_text[:500],
            "artifacts": artifacts,
            "keywords": keywords,
        },
        "declared_evidence": declared,
        "evidence_checks": checks,
    }


def evaluate_activities(activities: Iterable[Mapping[str, Any]],
                        repo_root: str | Path) -> List[Dict[str, Any]]:
    materialized = list(activities)
    refs: List[str] = []
    branches: List[str] = []
    for activity in materialized:
        if _kind_is_claim_relevant(str(activity.get("kind") or "")):
            declared = _collect_declared(_payload(activity.get("payload")))
            refs.extend(declared.get("refs") or [])
            branches.extend(declared.get("branches") or [])
    batched_refs = _batch_ref_checks(Path(repo_root), refs, branches=branches)
    reports = []
    for activity in materialized:
        report = evaluate_activity(activity, repo_root, batched_refs=batched_refs)
        if report:
            reports.append(report)
    return reports


def summarize_reports(reports: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    counts = {"pass": 0, "yellow": 0, "red": 0}
    materialized = list(reports)
    for report in materialized:
        status = str(report.get("status") or "")
        if status in counts:
            counts[status] += 1
    return {
        "schema": SCHEMA,
        "claim_count": len(materialized),
        "status_counts": counts,
        "ok": counts["red"] == 0,
    }
