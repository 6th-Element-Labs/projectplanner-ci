#!/usr/bin/env python3
"""Host-local, project-independent repository workspace materialization.

Execution Context is the only authority accepted here.  The application checkout,
current git root, and legacy PM_REPO_* variables are deliberately irrelevant.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit, urlunsplit


_SHA = re.compile(r"^[0-9a-f]{40}$")
_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
RECEIPT_SCHEMA = "switchboard.repository_workspace_receipt.v1"


class WorkspaceMaterializationError(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, **self.details}


@dataclass(frozen=True)
class MaterializedWorkspace:
    path: Path
    branch: str
    head_sha: str
    cache_path: Path
    receipt_path: Path
    receipt: dict[str, Any]
    reused: bool = False


def _run(args: list[str], *, cwd: Path | None = None,
         timeout: int = 120) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args, cwd=str(cwd) if cwd else None, text=True, capture_output=True,
        timeout=timeout, check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode:
        raise WorkspaceMaterializationError(
            "git_command_failed", f"{args[0]} {args[1]} failed",
            command=args[:2], returncode=result.returncode,
            stderr=(result.stderr or "")[-2000:])
    return result


def _safe_part(value: str, label: str) -> str:
    value = str(value or "").strip()
    safe = _SAFE.sub("-", value).strip(".-")
    if not safe or safe in {".", ".."}:
        raise WorkspaceMaterializationError(
            "invalid_workspace_identity", f"{label} is not safe")
    return safe[:96]


def _inside(root: Path, candidate: Path) -> Path:
    root = root.expanduser().resolve()
    candidate = candidate.expanduser().resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise WorkspaceMaterializationError(
            "workspace_path_escape", "workspace path escapes configured root",
            root=str(root), path=str(candidate)) from exc
    return candidate


def _redacted_remote(remote: str) -> str:
    remote = str(remote or "").strip()
    if remote.startswith("git@"):
        return remote.removesuffix(".git").lower()
    parsed = urlsplit(remote)
    if parsed.scheme == "file" and parsed.path.startswith("/"):
        return urlunsplit(("file", "", parsed.path.removesuffix(".git"), "", ""))
    if parsed.scheme not in {"http", "https", "ssh"} or not parsed.hostname:
        raise WorkspaceMaterializationError(
            "invalid_repository_remote", "repository remote is not an allowed URL")
    host = parsed.hostname.lower()
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit((parsed.scheme.lower(), host + port,
                       parsed.path.removesuffix(".git"), "", "")).lower()


def repository_remote(repository: str, remote_url: str = "") -> str:
    repository = str(repository or "").strip()
    if not _SLUG.fullmatch(repository):
        raise WorkspaceMaterializationError(
            "invalid_repository", "Execution Context repository must be owner/name")
    expected_suffix = repository.lower().removesuffix(".git")
    if remote_url:
        parsed = urlsplit(str(remote_url).strip())
        if parsed.username or parsed.password:
            raise WorkspaceMaterializationError(
                "repository_remote_contains_credential",
                "repository credentials must not be embedded in the remote URL")
        normalized = _redacted_remote(remote_url)
        path = (normalized.split(":", 1)[1] if normalized.startswith("git@")
                else urlsplit(normalized).path.lstrip("/"))
        if not path.removesuffix(".git").lower().endswith(expected_suffix):
            raise WorkspaceMaterializationError(
                "repository_remote_mismatch",
                "remote URL disagrees with Execution Context repository")
        return str(remote_url).strip()
    return f"https://github.com/{repository}.git"


def _cache_key(repository: str) -> str:
    return hashlib.sha256(repository.lower().encode()).hexdigest()[:20]


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _quarantine(path: Path, quarantine_root: Path, reason: str) -> Path | None:
    if not path.exists() and not path.is_symlink():
        return None
    quarantine_root.mkdir(parents=True, exist_ok=True)
    stamp = f"{int(time.time() * 1000)}-{os.getpid()}"
    target = _inside(
        quarantine_root,
        quarantine_root / f"{path.name}-{_safe_part(reason, 'reason')}-{stamp}",
    )
    path.rename(target)
    return target


def _ensure_cache(cache_path: Path, remote: str, base_sha: str,
                  quarantine_root: Path) -> tuple[bool, Path | None]:
    created = False
    quarantined = None
    if cache_path.exists():
        try:
            actual = _run(
                ["git", "--git-dir", str(cache_path), "remote", "get-url", "origin"]
            ).stdout.strip()
            if _redacted_remote(actual) != _redacted_remote(remote):
                raise WorkspaceMaterializationError(
                    "repository_cache_origin_mismatch",
                    "repository cache origin disagrees with Execution Context")
            _run(["git", "--git-dir", str(cache_path), "fsck", "--no-dangling"],
                 timeout=300)
        except (WorkspaceMaterializationError, OSError):
            quarantined = _quarantine(
                cache_path, quarantine_root, "invalid-repository-cache")
    if not cache_path.exists():
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", "--mirror", remote, str(cache_path)], timeout=600)
        created = True
    _run(["git", "--git-dir", str(cache_path), "fetch", "--prune", "origin"],
         timeout=600)
    try:
        _run(["git", "--git-dir", str(cache_path), "cat-file", "-e",
              f"{base_sha}^{{commit}}"])
    except WorkspaceMaterializationError as exc:
        raise WorkspaceMaterializationError(
            "base_sha_unreachable",
            "exact Execution Context base SHA is not present after fetch",
            base_sha=base_sha) from exc
    return created, quarantined


def _workspace_valid(path: Path, receipt_path: Path,
                     expected: Mapping[str, Any]) -> bool:
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        head = _run(["git", "rev-parse", "HEAD"], cwd=path).stdout.strip()
        branch = _run(["git", "branch", "--show-current"], cwd=path).stdout.strip()
        origin = _run(["git", "remote", "get-url", "origin"], cwd=path).stdout.strip()
        return (
            receipt.get("schema") == RECEIPT_SCHEMA
            and all(receipt.get(key) == value for key, value in expected.items())
            and head == expected["base_sha"]
            and branch == expected["branch"]
            and _redacted_remote(origin) == _redacted_remote(expected["remote"])
        )
    except (OSError, ValueError, WorkspaceMaterializationError):
        return False


def materialize(
    execution_context: Mapping[str, Any], *, task_id: str, execution_id: str,
    branch: str, cache_root: str | Path, workspace_root: str | Path,
    remote_url: str = "",
) -> MaterializedWorkspace:
    """Create or recover one exact isolated checkout and durable receipt."""
    context = dict(execution_context or {})
    if context.get("schema") != "switchboard.execution_context.v1":
        raise WorkspaceMaterializationError(
            "execution_context_invalid", "Execution Context schema is required")
    project = _safe_part(str(context.get("project_id") or ""), "project_id")
    context_task = str(context.get("task_id") or "").strip().upper()
    task_id = str(task_id or "").strip().upper()
    if not task_id or context_task != task_id:
        raise WorkspaceMaterializationError(
            "execution_context_task_mismatch",
            "Execution Context task disagrees with launch task")
    base_sha = str(context.get("base_sha") or "").strip().lower()
    if not _SHA.fullmatch(base_sha):
        raise WorkspaceMaterializationError(
            "execution_context_base_invalid", "exact base SHA is required")
    isolation = str((context.get("workspace") or {}).get("isolation") or "")
    if isolation not in {"worktree", "clone"}:
        raise WorkspaceMaterializationError(
            "workspace_isolation_unsupported",
            "Execution Context requires unsupported workspace isolation")
    repository = str(context.get("repository") or "")
    remote = repository_remote(repository, remote_url)
    branch = str(branch or "").strip()
    if not branch or branch.startswith("-") or ".." in branch or " " in branch:
        raise WorkspaceMaterializationError(
            "invalid_workspace_branch", "workspace branch is unsafe")

    cache_root_path = Path(cache_root).expanduser().resolve()
    workspace_root_path = Path(workspace_root).expanduser().resolve()
    quarantine_root = _inside(
        workspace_root_path, workspace_root_path / ".quarantine")
    key = _cache_key(repository)
    cache_path = _inside(cache_root_path, cache_root_path / f"{key}.git")
    execution_part = _safe_part(execution_id, "execution_id")
    workspace_path = _inside(
        workspace_root_path,
        workspace_root_path / project / _safe_part(task_id, "task_id")
        / execution_part,
    )
    receipt_path = _inside(
        workspace_root_path,
        workspace_root_path / ".receipts" / project
        / _safe_part(task_id, "task_id") / f"{execution_part}.json",
    )
    expected = {
        "project_id": str(context.get("project_id")),
        "task_id": task_id,
        "execution_id": str(execution_id),
        "generation": int(context.get("generation") or 0),
        "authority_digest": str(context.get("authority_digest") or ""),
        "context_digest": str(context.get("digest") or ""),
        "repository": repository,
        "remote": remote,
        "base_sha": base_sha,
        "branch": branch,
    }

    lock_path = cache_root_path / ".locks" / f"{key}.lock"
    with _locked(lock_path):
        cache_created, cache_quarantined = _ensure_cache(
            cache_path, remote, base_sha, quarantine_root)
        if workspace_path.exists():
            if _workspace_valid(workspace_path, receipt_path, expected):
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                return MaterializedWorkspace(
                    workspace_path, branch, base_sha, cache_path,
                    receipt_path, receipt, reused=True)
            _quarantine(workspace_path, quarantine_root, "stale-workspace")
        workspace_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _run(["git", "clone", "--no-checkout",
                  str(cache_path), str(workspace_path)], timeout=600)
            _run(["git", "remote", "set-url", "origin", remote], cwd=workspace_path)
            _run(["git", "checkout", "-b", branch, base_sha], cwd=workspace_path)
            head = _run(["git", "rev-parse", "HEAD"], cwd=workspace_path).stdout.strip()
            if head != base_sha:
                raise WorkspaceMaterializationError(
                    "workspace_head_mismatch",
                    "materialized workspace did not checkout exact base SHA")
            receipt = {
                "schema": RECEIPT_SCHEMA,
                **expected,
                "cache_key": key,
                "cache_created": cache_created,
                "cache_quarantined": (
                    str(cache_quarantined) if cache_quarantined else None),
                "workspace_path": str(workspace_path),
                "created_at": time.time(),
            }
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = receipt_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(receipt, sort_keys=True), encoding="utf-8")
            temporary.replace(receipt_path)
            return MaterializedWorkspace(
                workspace_path, branch, head, cache_path, receipt_path, receipt)
        except Exception:
            _quarantine(workspace_path, quarantine_root, "materialization-failed")
            raise


def cleanup(workspace: MaterializedWorkspace, *, quarantine: bool = False,
            reason: str = "completed") -> dict[str, Any]:
    root = workspace.path.parents[2]
    if quarantine:
        target = _quarantine(workspace.path, root / ".quarantine", reason)
        return {"cleaned": False, "quarantined": str(target) if target else None}
    if workspace.path.exists():
        shutil.rmtree(workspace.path)
    return {"cleaned": True, "quarantined": None}
