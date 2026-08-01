#!/usr/bin/env python3
"""Host-local, project-independent repository workspace materialization.

An Execution Context is authoritative when present.  Context-less compatibility
wakes may use the enrolled host checkout only as a git object/source repository;
the provider CLI never runs in that shared checkout.  Both inputs produce the
same verified ``MaterializedWorkspace`` and use the same receipt and teardown
lifecycle.
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
    # Teardown deletes and renames directories, so it must know the boundary it
    # is allowed to act inside rather than inferring one from the path itself.
    workspace_root: Path | None = None


def _remaining_timeout(deadline: float | None, requested: float) -> float:
    timeout = max(0.01, float(requested))
    if deadline is None:
        return timeout
    remaining = float(deadline) - time.monotonic()
    if remaining <= 0:
        raise WorkspaceMaterializationError(
            "workspace_materialize_timeout",
            "repository workspace materialization deadline expired")
    return max(0.01, min(timeout, remaining))


def _run(args: list[str], *, cwd: Path | None = None,
         timeout: float = 120, deadline: float | None = None
         ) -> subprocess.CompletedProcess[str]:
    effective_timeout = _remaining_timeout(deadline, timeout)
    try:
        result = subprocess.run(
            args, cwd=str(cwd) if cwd else None, text=True, capture_output=True,
            timeout=effective_timeout, check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceMaterializationError(
            "workspace_materialize_timeout",
            "repository workspace materialization deadline expired",
            command=args[:2], timeout_seconds=effective_timeout) from exc
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


def _require_disjoint_roots(source_root: Path, workspace_root: Path) -> None:
    """Refuse a private-workspace root that overlaps the shared source checkout."""
    source_root = source_root.expanduser().resolve()
    workspace_root = workspace_root.expanduser().resolve()
    try:
        workspace_root.relative_to(source_root)
    except ValueError:
        pass
    else:
        raise WorkspaceMaterializationError(
            "connect_workspace_root_overlaps_repo",
            "private Connect workspace root is inside the host checkout",
            source_root=str(source_root), workspace_root=str(workspace_root))
    try:
        source_root.relative_to(workspace_root)
    except ValueError:
        return
    raise WorkspaceMaterializationError(
        "connect_workspace_root_overlaps_repo",
        "host checkout is inside the private Connect workspace root",
        source_root=str(source_root), workspace_root=str(workspace_root))


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
def _locked(path: Path, *, deadline: float | None = None) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                _remaining_timeout(deadline, 0.05)
                time.sleep(0.05)
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
                  checkout_sha: str,
                  quarantine_root: Path, *,
                  deadline: float | None = None) -> tuple[bool, Path | None]:
    created = False
    quarantined = None
    if cache_path.exists():
        try:
            actual = _run(
                ["git", "--git-dir", str(cache_path), "remote", "get-url", "origin"],
                deadline=deadline,
            ).stdout.strip()
            if _redacted_remote(actual) != _redacted_remote(remote):
                raise WorkspaceMaterializationError(
                    "repository_cache_origin_mismatch",
                    "repository cache origin disagrees with Execution Context")
            _run(["git", "--git-dir", str(cache_path), "fsck", "--no-dangling"],
                 timeout=300, deadline=deadline)
        except WorkspaceMaterializationError as exc:
            if exc.code == "workspace_materialize_timeout":
                raise
            quarantined = _quarantine(
                cache_path, quarantine_root, "invalid-repository-cache")
        except OSError:
            quarantined = _quarantine(
                cache_path, quarantine_root, "invalid-repository-cache")
    if not cache_path.exists():
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", "--mirror", remote, str(cache_path)],
             timeout=600, deadline=deadline)
        created = True
    _run(["git", "--git-dir", str(cache_path), "fetch", "--prune", "origin"],
         timeout=600, deadline=deadline)
    try:
        _run(["git", "--git-dir", str(cache_path), "cat-file", "-e",
              f"{base_sha}^{{commit}}"], deadline=deadline)
    except WorkspaceMaterializationError as exc:
        if exc.code == "workspace_materialize_timeout":
            raise
        raise WorkspaceMaterializationError(
            "base_sha_unreachable",
            "exact Execution Context base SHA is not present after fetch",
            base_sha=base_sha) from exc
    if checkout_sha != base_sha:
        try:
            _run(["git", "--git-dir", str(cache_path), "cat-file", "-e",
                  f"{checkout_sha}^{{commit}}"], deadline=deadline)
        except WorkspaceMaterializationError as exc:
            if exc.code == "workspace_materialize_timeout":
                raise
            raise WorkspaceMaterializationError(
                "checkout_sha_unreachable",
                "exact execution checkout SHA is not present after fetch",
                checkout_sha=checkout_sha) from exc
    return created, quarantined


def _check_workspace(path: Path, receipt_path: Path,
                     expected: Mapping[str, Any], *,
                     deadline: float | None = None) -> dict[str, Any]:
    """Prove one checkout is still the exact authorized workspace.

    Returns the receipt.  Raises the typed refusal that names the first thing
    that disagrees, so callers can report *why* rather than a bare boolean.
    """
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkspaceMaterializationError(
            "workspace_receipt_unreadable",
            "workspace receipt is missing or unreadable",
            receipt_path=str(receipt_path)) from exc
    if not isinstance(receipt, dict) or receipt.get("schema") != RECEIPT_SCHEMA:
        raise WorkspaceMaterializationError(
            "workspace_receipt_invalid", "workspace receipt schema is invalid")
    if receipt.get("revoked_at"):
        raise WorkspaceMaterializationError(
            "workspace_revoked", "workspace was revoked and may not be reused",
            revoked_reason=str(receipt.get("revoked_reason") or ""))
    for key, value in expected.items():
        if receipt.get(key) != value:
            raise WorkspaceMaterializationError(
                "workspace_receipt_mismatch",
                "workspace receipt disagrees with the launch workspace identity",
                field=key)
    if not path.is_dir():
        raise WorkspaceMaterializationError(
            "workspace_missing", "authorized workspace no longer exists",
            workspace_path=str(path))
    head = _run(
        ["git", "rev-parse", "HEAD"], cwd=path, deadline=deadline).stdout.strip()
    branch = _run(
        ["git", "branch", "--show-current"], cwd=path,
        deadline=deadline).stdout.strip()
    origin = _run(
        ["git", "remote", "get-url", "origin"], cwd=path,
        deadline=deadline).stdout.strip()
    expected_head = str(expected.get("checkout_sha") or expected["base_sha"])
    if head != expected_head:
        code = (
            "workspace_exact_head_mismatch"
            if expected.get("checkout_sha")
            else "workspace_head_mismatch"
        )
        raise WorkspaceMaterializationError(
            code,
            "workspace HEAD is not the exact execution checkout SHA",
            checkout_sha=expected_head)
    if branch != expected["branch"]:
        raise WorkspaceMaterializationError(
            "workspace_branch_mismatch", "workspace is on the wrong branch",
            branch=str(expected["branch"]))
    if _redacted_remote(origin) != _redacted_remote(expected["remote"]):
        raise WorkspaceMaterializationError(
            "workspace_origin_mismatch",
            "workspace origin disagrees with the Execution Context repository")
    return receipt


def _git_common_dir(path: Path, *, deadline: float | None = None) -> Path:
    raw = _run(
        ["git", "rev-parse", "--git-common-dir"], cwd=path,
        deadline=deadline).stdout.strip()
    common = Path(raw)
    if not common.is_absolute():
        common = path / common
    return common.resolve()


def _host_worktree_static_identity(
    *, project_id: str, task_id: str, execution_id: str, generation: int,
    branch: str, source_repo_root: str | Path, workspace_root: str | Path,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Validate the host-derived compatibility input and resolve private paths."""
    project = _safe_part(project_id, "project_id")
    task_id = str(task_id or "").strip().upper()
    if not task_id:
        raise WorkspaceMaterializationError(
            "invalid_workspace_identity", "task_id is required")
    execution_id = str(execution_id or "").strip()
    execution_part = _safe_part(execution_id, "execution_id")
    try:
        generation = int(generation)
    except (TypeError, ValueError) as exc:
        raise WorkspaceMaterializationError(
            "invalid_execution_generation",
            "Connect execution generation must be an integer") from exc
    if generation <= 0:
        raise WorkspaceMaterializationError(
            "invalid_execution_generation",
            "Connect execution generation must be positive")
    workspace_part = f"{execution_part}-g{generation}"
    branch = str(branch or "").strip()
    if not branch or branch.startswith("-") or ".." in branch or " " in branch:
        raise WorkspaceMaterializationError(
            "invalid_workspace_branch", "workspace branch is unsafe")

    if not str(source_repo_root or "").strip():
        raise WorkspaceMaterializationError(
            "legacy_source_repo_invalid",
            "host checkout path is required")
    if not str(workspace_root or "").strip():
        raise WorkspaceMaterializationError(
            "workspace_root_missing",
            "private Connect workspace root is required")
    source_root = Path(source_repo_root).expanduser().resolve()
    workspace_root_path = Path(workspace_root).expanduser().resolve()
    _require_disjoint_roots(source_root, workspace_root_path)
    if not source_root.is_dir():
        raise WorkspaceMaterializationError(
            "legacy_source_repo_invalid",
            "host checkout is not an available directory",
            source_repo_root=str(source_root))
    try:
        source_common_dir = _git_common_dir(source_root, deadline=deadline)
        source_head = _run(
            ["git", "rev-parse", "HEAD"], cwd=source_root,
            deadline=deadline).stdout.strip().lower()
        remote = _run(
            ["git", "remote", "get-url", "origin"], cwd=source_root,
            deadline=deadline).stdout.strip()
    except WorkspaceMaterializationError as exc:
        if exc.code == "workspace_materialize_timeout":
            raise
        raise WorkspaceMaterializationError(
            "legacy_source_repo_invalid",
            "host checkout is not a usable git worktree",
            source_repo_root=str(source_root), cause=exc.code) from exc
    if not _SHA.fullmatch(source_head):
        raise WorkspaceMaterializationError(
            "legacy_source_repo_invalid",
            "host checkout HEAD is not a full commit SHA",
            source_repo_root=str(source_root))
    _redacted_remote(remote)

    workspace_path = _inside(
        workspace_root_path,
        workspace_root_path / project / _safe_part(task_id, "task_id")
        / workspace_part,
    )
    receipt_path = _inside(
        workspace_root_path,
        workspace_root_path / ".receipts" / project
        / _safe_part(task_id, "task_id") / f"{workspace_part}.json",
    )
    return {
        "project_id": str(project_id),
        "task_id": task_id,
        "execution_id": execution_id,
        "generation": generation,
        "branch": branch,
        "source_root": source_root,
        "source_common_dir": source_common_dir,
        "source_head": source_head,
        "remote": remote,
        "workspace_root": workspace_root_path,
        "workspace_path": workspace_path,
        "receipt_path": receipt_path,
    }


def _host_worktree_expected(
    resolved: Mapping[str, Any], *, base_sha: str | None = None,
) -> dict[str, Any]:
    return {
        "project_id": resolved["project_id"],
        "task_id": resolved["task_id"],
        "execution_id": resolved["execution_id"],
        "generation": resolved["generation"],
        "source": "repo_root",
        "isolation": "host_worktree",
        "workspace_backend": "git_worktree",
        "source_repo_root": str(resolved["source_root"]),
        "source_git_common_dir": str(resolved["source_common_dir"]),
        "remote": resolved["remote"],
        "base_sha": str(base_sha or resolved["source_head"]),
        "branch": resolved["branch"],
    }


def _verify_host_worktree_common_dir(
    workspace_path: Path, expected: Mapping[str, Any], *,
    deadline: float | None = None,
) -> None:
    common = _git_common_dir(workspace_path, deadline=deadline)
    if common != Path(str(expected["source_git_common_dir"])).resolve():
        raise WorkspaceMaterializationError(
            "workspace_git_common_dir_mismatch",
            "private workspace is not a worktree of the enrolled host checkout")


def _remove_host_worktree(source_root: Path, workspace_path: Path, *,
                          deadline: float | None = None) -> None:
    """Remove one registered worktree without deleting its task branch."""
    try:
        result = subprocess.run(
            ["git", "worktree", "remove", "--force", str(workspace_path)],
            cwd=str(source_root), text=True, capture_output=True, check=False,
            timeout=_remaining_timeout(deadline, 120),
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceMaterializationError(
            "workspace_materialize_timeout",
            "repository workspace materialization deadline expired",
            command=["git", "worktree"], timeout_seconds=exc.timeout) from exc
    if result.returncode and workspace_path.exists():
        raise WorkspaceMaterializationError(
            "git_worktree_remove_failed",
            "private Connect worktree could not be removed",
            returncode=result.returncode, stderr=(result.stderr or "")[-2000:])
    _run(
        ["git", "worktree", "prune"], cwd=source_root, deadline=deadline)


def materialize_host_worktree(
    *, project_id: str, task_id: str, execution_id: str, generation: int,
    branch: str, source_repo_root: str | Path, workspace_root: str | Path,
    timeout_s: float | None = None,
) -> MaterializedWorkspace:
    """Create or recover a private worktree for a context-less Connect wake.

    ``source_repo_root`` contributes committed git objects only.  It grants no
    execution, coordination, provider, SCM, or Done authority.
    """
    deadline = (
        time.monotonic() + max(0.01, float(timeout_s))
        if timeout_s is not None else None)
    resolved = _host_worktree_static_identity(
        project_id=project_id, task_id=task_id, execution_id=execution_id,
        generation=generation, branch=branch,
        source_repo_root=source_repo_root, workspace_root=workspace_root,
        deadline=deadline)
    workspace_path = resolved["workspace_path"]
    receipt_path = resolved["receipt_path"]
    workspace_root_path = resolved["workspace_root"]
    quarantine_root = _inside(
        workspace_root_path, workspace_root_path / ".quarantine")
    lock_key = hashlib.sha256(
        str(resolved["source_common_dir"]).encode()).hexdigest()[:20]
    lock_path = workspace_root_path / ".locks" / f"{lock_key}.lock"

    with _locked(lock_path, deadline=deadline):
        existing_receipt: dict[str, Any] = {}
        try:
            parsed = json.loads(receipt_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                existing_receipt = parsed
        except (OSError, ValueError):
            pass
        base_sha = str(existing_receipt.get("base_sha") or resolved["source_head"])
        if not _SHA.fullmatch(base_sha):
            raise WorkspaceMaterializationError(
                "workspace_receipt_invalid",
                "host worktree receipt has an invalid base SHA")
        expected = _host_worktree_expected(resolved, base_sha=base_sha)
        if workspace_path.exists():
            if _workspace_valid(
                    workspace_path, receipt_path, expected, deadline=deadline):
                _verify_host_worktree_common_dir(
                    workspace_path, expected, deadline=deadline)
                return MaterializedWorkspace(
                    workspace_path, resolved["branch"], base_sha,
                    resolved["source_common_dir"], receipt_path,
                    existing_receipt, reused=True,
                    workspace_root=workspace_root_path)
            try:
                _remove_host_worktree(
                    resolved["source_root"], workspace_path, deadline=deadline)
            except WorkspaceMaterializationError as exc:
                if exc.code == "workspace_materialize_timeout":
                    raise
                _quarantine(
                    workspace_path, quarantine_root, "stale-host-worktree")
        workspace_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            try:
                branch_exists = subprocess.run(
                    ["git", "show-ref", "--verify", "--quiet",
                     f"refs/heads/{resolved['branch']}"],
                    cwd=str(resolved["source_root"]), check=False,
                    timeout=_remaining_timeout(deadline, 120),
                    env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                ).returncode == 0
            except subprocess.TimeoutExpired as exc:
                raise WorkspaceMaterializationError(
                    "workspace_materialize_timeout",
                    "repository workspace materialization deadline expired",
                    command=["git", "show-ref"],
                    timeout_seconds=exc.timeout) from exc
            if branch_exists:
                branch_sha = _run(
                    ["git", "rev-parse", resolved["branch"]],
                    cwd=resolved["source_root"], deadline=deadline).stdout.strip()
                if branch_sha != base_sha:
                    raise WorkspaceMaterializationError(
                        "workspace_branch_base_mismatch",
                        "existing execution branch is not at the recorded base SHA",
                        branch=resolved["branch"], base_sha=base_sha)
                args = [
                    "git", "worktree", "add", str(workspace_path),
                    resolved["branch"],
                ]
            else:
                args = [
                    "git", "worktree", "add", "-b", resolved["branch"],
                    str(workspace_path), base_sha,
                ]
            _run(
                args, cwd=resolved["source_root"], timeout=600,
                deadline=deadline)
            receipt = {
                "schema": RECEIPT_SCHEMA,
                **expected,
                "workspace_path": str(workspace_path),
                "created_at": time.time(),
            }
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = receipt_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(receipt, sort_keys=True), encoding="utf-8")
            temporary.replace(receipt_path)
            checked = _check_workspace(
                workspace_path, receipt_path, expected, deadline=deadline)
            _verify_host_worktree_common_dir(
                workspace_path, expected, deadline=deadline)
            return MaterializedWorkspace(
                workspace_path, resolved["branch"], base_sha,
                resolved["source_common_dir"], receipt_path, checked,
                workspace_root=workspace_root_path)
        except Exception:
            if workspace_path.exists():
                try:
                    _remove_host_worktree(
                        resolved["source_root"], workspace_path,
                        deadline=deadline)
                except WorkspaceMaterializationError:
                    _quarantine(
                        workspace_path, quarantine_root,
                        "host-materialization-failed")
            raise


def verify_host_worktree(
    *, project_id: str, task_id: str, execution_id: str, generation: int,
    branch: str, source_repo_root: str | Path, workspace_root: str | Path,
) -> MaterializedWorkspace:
    """Re-prove the private host worktree immediately before process spawn."""
    resolved = _host_worktree_static_identity(
        project_id=project_id, task_id=task_id, execution_id=execution_id,
        generation=generation, branch=branch,
        source_repo_root=source_repo_root, workspace_root=workspace_root)
    try:
        receipt = json.loads(
            resolved["receipt_path"].read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkspaceMaterializationError(
            "workspace_receipt_unreadable",
            "workspace receipt is missing or unreadable",
            receipt_path=str(resolved["receipt_path"])) from exc
    base_sha = str((receipt or {}).get("base_sha") or "")
    if not _SHA.fullmatch(base_sha):
        raise WorkspaceMaterializationError(
            "workspace_receipt_invalid",
            "host worktree receipt has an invalid base SHA")
    expected = _host_worktree_expected(resolved, base_sha=base_sha)
    receipt = _check_workspace(
        resolved["workspace_path"], resolved["receipt_path"], expected)
    _verify_host_worktree_common_dir(resolved["workspace_path"], expected)
    return MaterializedWorkspace(
        resolved["workspace_path"], resolved["branch"], base_sha,
        resolved["source_common_dir"], resolved["receipt_path"], receipt,
        reused=True, workspace_root=resolved["workspace_root"])


def _workspace_valid(path: Path, receipt_path: Path,
                     expected: Mapping[str, Any], *,
                     deadline: float | None = None) -> bool:
    try:
        _check_workspace(path, receipt_path, expected, deadline=deadline)
    except WorkspaceMaterializationError as exc:
        if exc.code == "workspace_materialize_timeout":
            raise
        return False
    except (OSError, ValueError):
        return False
    return True


def _resolved_identity(
    execution_context: Mapping[str, Any], *, task_id: str, execution_id: str,
    branch: str, cache_root: str | Path, workspace_root: str | Path,
    remote_url: str = "",
) -> dict[str, Any]:
    """Validate one launch request and derive its exact paths and receipt fields.

    Materialization and pre-process verification must agree on every one of
    these values, so both derive them here rather than restating them.
    """
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
    checkout_sha = str(context.get("checkout_sha") or base_sha).strip().lower()
    if not _SHA.fullmatch(checkout_sha):
        raise WorkspaceMaterializationError(
            "execution_context_checkout_invalid",
            "exact execution checkout SHA is required")
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
        "checkout_sha": checkout_sha,
        "branch": branch,
    }
    return {
        "cache_root": cache_root_path,
        "workspace_root": workspace_root_path,
        "quarantine_root": quarantine_root,
        "cache_key": key,
        "cache_path": cache_path,
        "workspace_path": workspace_path,
        "receipt_path": receipt_path,
        "remote": remote,
        "base_sha": base_sha,
        "checkout_sha": checkout_sha,
        "branch": branch,
        "expected": expected,
    }


def materialize(
    execution_context: Mapping[str, Any], *, task_id: str, execution_id: str,
    branch: str, cache_root: str | Path, workspace_root: str | Path,
    remote_url: str = "", timeout_s: float | None = None,
) -> MaterializedWorkspace:
    """Create or recover one exact isolated checkout and durable receipt."""
    deadline = (
        time.monotonic() + max(0.01, float(timeout_s))
        if timeout_s is not None else None)
    resolved = _resolved_identity(
        execution_context, task_id=task_id, execution_id=execution_id,
        branch=branch, cache_root=cache_root, workspace_root=workspace_root,
        remote_url=remote_url)
    cache_root_path = resolved["cache_root"]
    quarantine_root = resolved["quarantine_root"]
    key = resolved["cache_key"]
    cache_path = resolved["cache_path"]
    workspace_path = resolved["workspace_path"]
    receipt_path = resolved["receipt_path"]
    remote = resolved["remote"]
    base_sha = resolved["base_sha"]
    checkout_sha = resolved["checkout_sha"]
    branch = resolved["branch"]
    expected = resolved["expected"]

    lock_path = cache_root_path / ".locks" / f"{key}.lock"
    with _locked(lock_path, deadline=deadline):
        cache_created, cache_quarantined = _ensure_cache(
            cache_path, remote, base_sha, checkout_sha, quarantine_root,
            deadline=deadline)
        if workspace_path.exists():
            if _workspace_valid(
                    workspace_path, receipt_path, expected, deadline=deadline):
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                return MaterializedWorkspace(
                    workspace_path, branch, checkout_sha, cache_path,
                    receipt_path, receipt, reused=True,
                    workspace_root=resolved["workspace_root"])
            _quarantine(workspace_path, quarantine_root, "stale-workspace")
        workspace_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _run(["git", "clone", "--no-checkout",
                  str(cache_path), str(workspace_path)], timeout=600,
                 deadline=deadline)
            _run(
                ["git", "remote", "set-url", "origin", remote],
                cwd=workspace_path, deadline=deadline)
            _run(
                ["git", "checkout", "-b", branch, checkout_sha],
                cwd=workspace_path, deadline=deadline)
            head = _run(
                ["git", "rev-parse", "HEAD"], cwd=workspace_path,
                deadline=deadline).stdout.strip()
            if head != checkout_sha:
                raise WorkspaceMaterializationError(
                    "workspace_exact_head_mismatch",
                    "materialized workspace did not checkout exact execution SHA")
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
                workspace_path, branch, head, cache_path, receipt_path, receipt,
                workspace_root=resolved["workspace_root"])
        except Exception:
            _quarantine(workspace_path, quarantine_root, "materialization-failed")
            raise


def verify(
    execution_context: Mapping[str, Any], *, task_id: str, execution_id: str,
    branch: str, cache_root: str | Path, workspace_root: str | Path,
    remote_url: str = "",
) -> MaterializedWorkspace:
    """Re-prove an authorized workspace immediately before a process starts.

    ``materialize`` proves the checkout when it is created; this proves it again
    at the last moment before a provider CLI is given the directory, so a
    workspace that was deleted, rewound, re-pointed, or revoked in between
    refuses the launch instead of silently running somewhere else.
    """
    resolved = _resolved_identity(
        execution_context, task_id=task_id, execution_id=execution_id,
        branch=branch, cache_root=cache_root, workspace_root=workspace_root,
        remote_url=remote_url)
    receipt = _check_workspace(
        resolved["workspace_path"], resolved["receipt_path"],
        resolved["expected"])
    return MaterializedWorkspace(
        resolved["workspace_path"], resolved["branch"],
        resolved["checkout_sha"],
        resolved["cache_path"], resolved["receipt_path"], receipt, reused=True,
        workspace_root=resolved["workspace_root"])


def revoke(workspace: MaterializedWorkspace, *, reason: str,
           quarantine: bool = False) -> dict[str, Any]:
    """Terminally deny further writes to one materialized workspace.

    The receipt is stamped first and the directory removed second, so a crash
    between the two still leaves a revoked receipt that ``verify`` and
    ``materialize`` both refuse to reuse.  Revoking twice is a no-op.
    """
    try:
        receipt = json.loads(workspace.receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        receipt = dict(workspace.receipt or {})
    already = bool(receipt.get("revoked_at"))
    if not already:
        receipt["revoked_at"] = time.time()
        receipt["revoked_reason"] = str(reason or "revoked")
        try:
            workspace.receipt_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = workspace.receipt_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(receipt, sort_keys=True), encoding="utf-8")
            temporary.replace(workspace.receipt_path)
        except OSError as exc:
            raise WorkspaceMaterializationError(
                "workspace_revocation_unrecorded",
                "workspace revocation could not be persisted",
                receipt_path=str(workspace.receipt_path)) from exc
    removed = cleanup(workspace, quarantine=quarantine, reason=reason)
    return {"revoked": True, "already_revoked": already,
            "reason": str(reason or "revoked"), **removed}


def safe_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Project a receipt down to identity a central registry may safely hold.

    Credentials never reach a receipt, but host cache and quarantine paths do.
    Those describe the operator's machine rather than the execution, so they are
    reduced to booleans and the remote is republished in redacted form.
    """
    receipt = dict(receipt or {})
    projection = {
        key: receipt.get(key) for key in (
            "schema", "project_id", "task_id", "execution_id", "generation",
            "authority_digest", "context_digest", "repository", "base_sha",
            "checkout_sha",
            "branch", "workspace_path", "created_at", "revoked_at",
            "revoked_reason", "source", "isolation", "workspace_backend",
        ) if receipt.get(key) is not None
    }
    remote = str(receipt.get("remote") or "")
    if remote:
        try:
            projection["remote"] = _redacted_remote(remote)
        except WorkspaceMaterializationError:
            projection["remote"] = ""
    projection["cache_created"] = bool(receipt.get("cache_created"))
    projection["cache_quarantined"] = bool(receipt.get("cache_quarantined"))
    return projection


def cleanup(workspace: MaterializedWorkspace, *, quarantine: bool = False,
            reason: str = "completed") -> dict[str, Any]:
    """Remove or quarantine one workspace, never anything outside its root.

    Teardown is driven by a durable receipt that outlives the process which
    wrote it.  A receipt naming a path outside the configured workspace root is
    refused rather than obeyed — this deletes directories, so "trust the file"
    is not an acceptable posture.
    """
    root = (Path(workspace.workspace_root) if workspace.workspace_root
            else workspace.path.parents[2])
    path = _inside(root, workspace.path)
    try:
        receipt = json.loads(
            workspace.receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        receipt = dict(workspace.receipt or {})
    if receipt.get("workspace_backend") == "git_worktree":
        source_root = Path(str(receipt.get("source_repo_root") or "")).resolve()
        expected_common = Path(
            str(receipt.get("source_git_common_dir") or "")).resolve()
        if not str(receipt.get("source_repo_root") or "") or not source_root.is_dir():
            raise WorkspaceMaterializationError(
                "legacy_source_repo_invalid",
                "host checkout is unavailable during worktree teardown")
        if _git_common_dir(source_root) != expected_common:
            raise WorkspaceMaterializationError(
                "workspace_git_common_dir_mismatch",
                "host checkout changed before worktree teardown")
        _remove_host_worktree(source_root, path)
        return {"cleaned": True, "quarantined": None}
    if quarantine:
        target = _quarantine(path, root / ".quarantine", reason)
        return {"cleaned": False, "quarantined": str(target) if target else None}
    if path.exists():
        shutil.rmtree(path)
    return {"cleaned": True, "quarantined": None}
