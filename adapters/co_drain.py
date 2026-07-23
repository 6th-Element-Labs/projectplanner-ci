#!/usr/bin/env python3
"""CO worker drain protocol for planned scale-in and Spot interruption (CO-4).

The module is deliberately usable from both ephemeral AWS workers and persistent
Agent Hosts.  A durable request marker stops new wake claims, active supervised
processes are snapshotted and interrupted, task worktrees are checkpointed and
pushed, provider leases are released, runtime homes are purged, and only a
redacted receipt is published back to Switchboard.

Raw provider credentials, auth capsules, log tails, and credential identifiers
never enter the receipt.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


REQUEST_SCHEMA = "switchboard.co_drain.request.v1"
RECEIPT_SCHEMA = "switchboard.co_drain.receipt.v1"
DEFAULT_REQUEST_PATH = "/run/switchboard-co/drain-request.json"
DEFAULT_RECEIPT_PATH = "/run/switchboard-co/drain-receipt.json"
TERMINAL_RUNNER_STATES = {
    "completed", "failed", "cancelled", "expired", "lost", "killed", "exited", "stopped",
}
ALLOWED_REASONS = {
    "planned_scale_in", "spot_interruption", "rebalance_recommendation",
    "persistent_host_removal", "operator_request",
}
SAFE_BRANCH = re.compile(r"^[A-Za-z0-9._/@+\-]{1,180}$")
SECRET_PATH_PARTS = {
    "auth.json", ".credentials", "credentials.json", "provider-runtimes",
    ".aws", ".ssh", ".gnupg",
}
SECRET_SUFFIXES = {".pem", ".p12", ".pfx", ".key"}
SECRET_CONTENT = re.compile(
    rb"(?:-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----|"
    rb"AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|"
    rb"sk-(?:proj|ant|live)-[A-Za-z0-9_-]{20,})"
)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def normalize_request(payload: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else float(now)
    reason = str(payload.get("reason") or "").strip().lower()
    if reason not in ALLOWED_REASONS:
        raise ValueError("unsupported CO drain reason")
    request_id = str(payload.get("request_id") or "").strip()
    if not request_id:
        request_id = "drain-" + uuid.uuid4().hex[:16]
    if not re.fullmatch(r"drain-[A-Za-z0-9._-]{8,80}", request_id):
        raise ValueError("invalid CO drain request id")
    requested_at = float(payload.get("requested_at") or now)
    deadline = float(payload.get("deadline") or (requested_at + 90))
    if deadline <= requested_at:
        raise ValueError("CO drain deadline must follow requested_at")
    termination_kind = str(payload.get("termination_kind") or (
        "persistent_host" if reason == "persistent_host_removal" else "ephemeral_instance"
    )).strip()
    if termination_kind not in {"ephemeral_instance", "persistent_host"}:
        raise ValueError("invalid CO drain termination kind")
    return {
        "schema": REQUEST_SCHEMA,
        "request_id": request_id,
        "reason": reason,
        "termination_kind": termination_kind,
        "requested_at": requested_at,
        "deadline": deadline,
    }


def request_path(path: str | Path | None = None) -> Path:
    return Path(path or os.environ.get("PM_CO_DRAIN_REQUEST_PATH") or DEFAULT_REQUEST_PATH)


def receipt_path(path: str | Path | None = None) -> Path:
    return Path(path or os.environ.get("PM_CO_DRAIN_RECEIPT_PATH") or DEFAULT_RECEIPT_PATH)


def write_request(payload: Mapping[str, Any], path: str | Path | None = None) -> dict[str, Any]:
    request = normalize_request(payload)
    _atomic_json(request_path(path), request)
    return request


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def read_request(path: str | Path | None = None) -> dict[str, Any] | None:
    value = _read_json(request_path(path))
    if not value or value.get("schema") != REQUEST_SCHEMA:
        return None
    try:
        return normalize_request(value)
    except (TypeError, ValueError):
        return None


def read_receipt(path: str | Path | None = None) -> dict[str, Any] | None:
    value = _read_json(receipt_path(path))
    return value if value and value.get("schema") == RECEIPT_SCHEMA else None


def write_receipt(payload: Mapping[str, Any], path: str | Path | None = None) -> None:
    if payload.get("schema") != RECEIPT_SCHEMA:
        raise ValueError("invalid CO drain receipt schema")
    _atomic_json(receipt_path(path), payload)


def _imds_request(method: str, path: str, headers: Mapping[str, str]) -> tuple[int, str]:
    request = urllib.request.Request(
        "http://169.254.169.254" + path, method=method, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=0.25) as response:
            return int(response.status), response.read(65536).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), ""
    except (OSError, urllib.error.URLError):
        return 0, ""


def detect_ec2_interruption(
        requester: Callable[[str, str, Mapping[str, str]], tuple[int, str]] = _imds_request,
        *, now: float | None = None) -> dict[str, Any] | None:
    """Return a drain request for an IMDS Spot action/rebalance notice.

    IMDS probing is opt-in so persistent hosts and local tests never contact the
    link-local metadata address accidentally.
    """
    if str(os.environ.get("PM_CO_DRAIN_IMDS") or "").lower() not in {"1", "true", "yes", "on"}:
        return None
    now = time.time() if now is None else float(now)
    status, token = requester(
        "PUT", "/latest/api/token", {"X-aws-ec2-metadata-token-ttl-seconds": "60"})
    if status != 200 or not token:
        return None
    headers = {"X-aws-ec2-metadata-token": token}
    status, body = requester("GET", "/latest/meta-data/spot/instance-action", headers)
    if status == 200 and body:
        try:
            action = json.loads(body)
        except json.JSONDecodeError:
            action = {}
        deadline = now + 110
        value = str(action.get("time") or "")
        if value:
            try:
                from datetime import datetime
                deadline = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
        seed = f"spot:{value}:{now:.0f}"
        return normalize_request({
            "request_id": "drain-" + hashlib.sha256(seed.encode()).hexdigest()[:16],
            "reason": "spot_interruption", "requested_at": now,
            "deadline": max(now + 1, deadline), "termination_kind": "ephemeral_instance",
        }, now=now)
    status, body = requester(
        "GET", "/latest/meta-data/events/recommendations/rebalance", headers)
    if status == 200 and body:
        seed = f"rebalance:{body}:{now:.0f}"
        return normalize_request({
            "request_id": "drain-" + hashlib.sha256(seed.encode()).hexdigest()[:16],
            "reason": "rebalance_recommendation", "requested_at": now,
            "deadline": now + 110, "termination_kind": "ephemeral_instance",
        }, now=now)
    return None


def discover_request(
        *, path: str | Path | None = None,
        detector: Callable[[], dict[str, Any] | None] = detect_ec2_interruption) -> dict[str, Any] | None:
    current = read_request(path)
    if current:
        return current
    detected = detector()
    if detected:
        return write_request(detected, path)
    return None


def _git(cwd: Path, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), text=True, capture_output=True,
        timeout=timeout, check=False)


def _nul_names(completed: subprocess.CompletedProcess[str]) -> list[str]:
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or "git path query failed").strip())
    return [item for item in completed.stdout.split("\0") if item]


def _secret_path(name: str) -> bool:
    path = Path(name)
    lowered = [part.lower() for part in path.parts]
    if path.name.lower() == ".env" or (
            path.name.lower().startswith(".env.") and path.name.lower() != ".env.example"):
        return True
    return any(part in SECRET_PATH_PARTS for part in lowered) or path.suffix.lower() in SECRET_SUFFIXES


def _secret_content(path: Path) -> bool:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
            return path.is_symlink()
        return bool(SECRET_CONTENT.search(path.read_bytes()))
    except OSError:
        return True


def _test_evidence(work_session: Mapping[str, Any]) -> tuple[bool, str | None]:
    candidates = [
        work_session.get("executed_test_run"),
        (work_session.get("hygiene") or {}).get("executed_test_run"),
        (work_session.get("env") or {}).get("executed_test_run"),
    ]
    run = next((item for item in candidates if isinstance(item, Mapping)), None)
    if not run:
        return False, None
    passed = bool(run.get("executed")) and str(run.get("status") or "").lower() in {
        "success", "passed", "pass",
    }
    return passed, str(run.get("output_hash") or run.get("artifact_hash") or "") or None


def checkpoint_work_session(work_session: Mapping[str, Any], *, task_id: str,
                            request_id: str, workspace_root: str | Path) -> dict[str, Any]:
    """Freeze work into a safe commit and prove the exact head exists on origin."""
    session_id = str(work_session.get("work_session_id") or "")
    raw_path = str(work_session.get("worktree_path") or work_session.get("clone_path") or "")
    root = Path(workspace_root).expanduser().resolve()
    path = Path(raw_path).expanduser().resolve() if raw_path else None
    base = {
        "work_session_id": session_id,
        "status": "blocked",
        "pushed": False,
        "credential_material_included": False,
    }
    if not path:
        return {**base, "error_code": "work_session_workspace_missing"}
    try:
        path.relative_to(root)
    except ValueError:
        return {**base, "error_code": "work_session_workspace_outside_root"}
    if not (path / ".git").exists():
        # Git worktrees use a .git file; clones use a directory. Both satisfy exists().
        return {**base, "error_code": "work_session_workspace_not_git"}
    branch_result = _git(path, ["rev-parse", "--abbrev-ref", "HEAD"])
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else ""
    if (not SAFE_BRANCH.fullmatch(branch or "") or branch in {"main", "master"}
            or str(task_id).upper() not in branch.upper()):
        return {**base, "branch": branch, "error_code": "checkpoint_branch_invalid"}
    diff_check = _git(path, ["diff", "--check"])
    if diff_check.returncode != 0:
        return {**base, "branch": branch, "error_code": "checkpoint_diff_check_failed"}
    tracked = _nul_names(_git(path, ["diff", "--name-only", "-z", "HEAD"]))
    untracked = _nul_names(_git(path, ["ls-files", "--others", "--exclude-standard", "-z"]))
    changed = sorted(dict.fromkeys(tracked + untracked))
    unsafe = [name for name in changed if _secret_path(name)]
    unsafe += [name for name in changed if not _secret_path(name) and (path / name).exists()
               and _secret_content(path / name)]
    if unsafe:
        return {
            **base, "branch": branch, "error_code": "checkpoint_secret_scan_failed",
            "changed_file_count": len(changed), "unsafe_file_count": len(set(unsafe)),
        }
    committed = False
    if changed:
        staged = _git(path, ["add", "-u"])
        if staged.returncode == 0 and untracked:
            staged = _git(path, ["add", "--", *untracked])
        if staged.returncode != 0 or _git(path, ["diff", "--cached", "--check"]).returncode != 0:
            return {**base, "branch": branch, "error_code": "checkpoint_stage_failed"}
        has_staged = _git(path, ["diff", "--cached", "--quiet"]).returncode == 1
        if has_staged:
            commit = _git(path, [
                "-c", "user.name=Switchboard CO Drain",
                "-c", "user.email=co-drain@switchboard.invalid",
                "commit", "-m", f"checkpoint({task_id}): drain {request_id}",
            ], timeout=60)
            if commit.returncode != 0:
                return {**base, "branch": branch, "error_code": "checkpoint_commit_failed"}
            committed = True
    head_result = _git(path, ["rev-parse", "HEAD"])
    head = head_result.stdout.strip() if head_result.returncode == 0 else ""
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        return {**base, "branch": branch, "error_code": "checkpoint_head_missing"}
    pushed = _git(path, ["push", "-u", "origin", branch], timeout=120)
    if pushed.returncode != 0:
        return {
            **base, "branch": branch, "head_sha": head,
            "committed": committed, "error_code": "checkpoint_push_failed",
        }
    remote = _git(path, ["ls-remote", "--heads", "origin", f"refs/heads/{branch}"], timeout=60)
    remote_head = (remote.stdout.strip().split() or [""])[0] if remote.returncode == 0 else ""
    tests_present, tests_hash = _test_evidence(work_session)
    return {
        **base,
        "status": "checkpointed",
        "branch": branch,
        "head_sha": head,
        "remote_head_sha": remote_head,
        "pushed": remote_head == head,
        "committed": committed,
        "changed_file_count": len(changed),
        "git_diff_check": True,
        "test_evidence_present": tests_present,
        "test_evidence_hash": tests_hash,
    }


def purge_runtime_residue(runtime_root: str | Path | None) -> dict[str, Any]:
    if not runtime_root:
        return {"purged": True, "residue_count": 0, "root_configured": False}
    candidate = Path(runtime_root).expanduser()
    if candidate.is_symlink():
        return {"purged": False, "residue_count": -1, "error_code": "runtime_root_symlink"}
    root = candidate.resolve()
    if root == Path(root.anchor) or len(root.parts) < 4:
        return {"purged": False, "residue_count": -1, "error_code": "runtime_root_unsafe"}
    try:
        if root.exists():
            for child in list(root.iterdir()):
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    try:
                        child.unlink()
                    except OSError:
                        pass
        residue = list(root.iterdir()) if root.exists() else []
    except OSError:
        return {"purged": False, "residue_count": -1, "error_code": "runtime_purge_failed"}
    return {"purged": not residue, "residue_count": len(residue), "root_configured": True}


def _select_work_session(runner: Mapping[str, Any],
                         work_sessions: Iterable[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    metadata = runner.get("metadata") or {}
    wanted = str(metadata.get("work_session_id") or "")
    rows = list(work_sessions)
    if wanted:
        return next((row for row in rows if row.get("work_session_id") == wanted), None)
    active = [row for row in rows if row.get("status") == "active"
              and row.get("task_id") == runner.get("task_id")
              and (not runner.get("agent_id") or row.get("agent_id") == runner.get("agent_id"))]
    return sorted(active, key=lambda row: float(row.get("created_at") or 0), reverse=True)[0] \
        if active else None


def _safe_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = value.get("last_snapshot") or value.get("snapshot") or value
    return {key: snapshot.get(key) for key in (
        "captured_at", "runner_session_id", "agent_id", "task_id", "claim_id",
        "branch", "head_sha",
    ) if snapshot.get(key) not in (None, "")}


def drain_host(
        request: Mapping[str, Any], inventory: Mapping[str, Any], *,
        runners: Iterable[Mapping[str, Any]],
        work_sessions: Iterable[Mapping[str, Any]],
        supervisor: Callable[[str, str, Mapping[str, Any] | None], Mapping[str, Any]],
        release_lease: Callable[[str, str], Mapping[str, Any]],
        publish_host: Callable[[str, Mapping[str, Any]], Mapping[str, Any] | None],
        update_runner: Callable[[Mapping[str, Any]], Mapping[str, Any] | None] | None = None,
        workspace_root: str | Path | None = None,
        runtime_root: str | Path | None = None,
        now: float | None = None) -> dict[str, Any]:
    """Execute one idempotent host drain and publish a redacted durable receipt."""
    now = time.time() if now is None else float(now)
    request = normalize_request(request, now=now)
    host_id = str(inventory.get("host_id") or "")
    rows = list(runners)
    sessions = list(work_sessions)
    try:
        publish_host("draining", {
            "active_sessions": len(rows),
            "drain": {"request_id": request["request_id"], "reason": request["reason"],
                      "requested_at": request["requested_at"], "deadline": request["deadline"]},
        })
    except Exception:
        pass
    runner_receipts: list[dict[str, Any]] = []
    failures: list[str] = []
    for runner in rows:
        status = str(runner.get("status") or "").lower()
        if runner.get("stale") or status in TERMINAL_RUNNER_STATES:
            continue
        runner_id = str(runner.get("runner_session_id") or "")
        try:
            snapshot_raw = dict(supervisor("snapshot", runner_id, None) or {})
        except Exception:
            snapshot_raw = {"error": "runner_snapshot_failed"}
        safe_snapshot = _safe_snapshot(snapshot_raw)
        if snapshot_raw.get("error"):
            failures.append(f"{runner_id}:runner_snapshot_failed")
        metadata = runner.get("metadata") or {}
        if update_runner:
            try:
                update_runner({
                    **dict(runner), "status": "draining", "last_snapshot": safe_snapshot,
                    "metadata": {**dict(metadata),
                                 "drain_request_id": request["request_id"]},
                })
            except Exception:
                failures.append(f"{runner_id}:runner_draining_report_failed")
        try:
            killed = dict(supervisor(
                "lease_stop", runner_id, {
                    "reason": f"host drain: {request['reason']}"}) or {})
        except Exception:
            killed = {"error": "runner_stop_failed", "alive": True}
        session = _select_work_session(runner, sessions)
        if session:
            try:
                checkpoint = checkpoint_work_session(
                    session, task_id=str(
                        runner.get("task_id") or session.get("task_id") or ""),
                    request_id=request["request_id"],
                    workspace_root=workspace_root or os.environ.get("PM_WORKSPACE_ROOT")
                    or "/var/lib/projectplanner/workspaces")
            except Exception:
                checkpoint = {
                    "work_session_id": session.get("work_session_id"),
                    "status": "blocked", "pushed": False,
                    "error_code": "checkpoint_unexpected_failure",
                    "credential_material_included": False,
                }
        elif runner.get("claim_id"):
            checkpoint = {
                "status": "blocked", "pushed": False,
                "error_code": "active_claim_work_session_missing",
                "credential_material_included": False,
            }
        else:
            checkpoint = {
                "status": "not_applicable", "pushed": True,
                "credential_material_included": False,
            }
        lease_id = str(metadata.get("credential_lease_id") or "")
        lease_state = "not_bound"
        if lease_id:
            try:
                release = dict(release_lease(lease_id, "host_drain") or {})
                lease_state = str(release.get("state") or "release_failed")
            except Exception:
                lease_state = "release_failed"
        stopped = killed.get("alive") is False and not killed.get("error")
        if not stopped:
            failures.append(f"{runner_id}:runner_stop_failed")
        if not checkpoint.get("pushed"):
            failures.append(f"{runner_id}:{checkpoint.get('error_code') or 'checkpoint_failed'}")
        if lease_state not in {"not_bound", "released", "fenced", "expired"}:
            failures.append(f"{runner_id}:credential_release_failed")
        runner_receipt = {
            "runner_session_id": runner_id,
            "task_id": runner.get("task_id"),
            "claim_id": runner.get("claim_id"),
            "agent_id": runner.get("agent_id"),
            "stopped": stopped,
            "snapshot": safe_snapshot,
            "checkpoint": checkpoint,
            "credential": {
                "provider": metadata.get("provider"),
                "account_affinity_id": metadata.get("account_affinity_id"),
                "lease_state": lease_state,
                "credential_identifiers_redacted": True,
            },
        }
        runner_receipts.append(runner_receipt)
        if update_runner:
            try:
                update_runner({
                    **dict(runner), "status": "killed" if stopped else "failed",
                    "last_snapshot": safe_snapshot,
                    "metadata": {**dict(metadata), "drain_request_id": request["request_id"],
                                 "drain_checkpoint": checkpoint},
                })
            except Exception:
                failures.append(f"{runner_id}:runner_terminal_report_failed")
    residue = purge_runtime_residue(
        runtime_root if runtime_root is not None
        else os.environ.get("PM_PROVIDER_RUNTIME_ROOT"))
    if not residue.get("purged"):
        failures.append("provider_runtime_residue")
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "request_id": request["request_id"],
        "host_id": host_id,
        "reason": request["reason"],
        "termination_kind": request["termination_kind"],
        "status": "drained" if not failures else "drain_failed",
        "no_new_claims": True,
        "runner_count": len(runner_receipts),
        "runners": runner_receipts,
        "provider_runtime_residue": residue,
        "credential_values_redacted": True,
        "failures": failures,
        "completed_at": now,
    }
    try:
        published = publish_host(receipt["status"], {
            "active_sessions": 0 if not failures else len(failures),
            "drain_receipt": receipt,
        })
    except Exception:
        published = None
    receipt["durable_acknowledged"] = bool(published and not published.get("error"))
    return receipt


def inventory_for_drain(inventory: Mapping[str, Any]) -> dict[str, Any]:
    value = json.loads(json.dumps(dict(inventory)))
    value["policy"] = {**(value.get("policy") or {}), "allow_work": False,
                       "allow_global_claim": False, "mode": "draining"}
    for runtime in value.get("runtimes") or []:
        runtime["policy"] = {**(runtime.get("policy") or {}), "allow_work": False,
                             "allow_global_claim": False, "mode": "draining"}
    value["capacity"] = {**(value.get("capacity") or {}), "draining": True}
    return value


__all__ = [
    "REQUEST_SCHEMA", "RECEIPT_SCHEMA", "checkpoint_work_session", "detect_ec2_interruption",
    "discover_request", "drain_host", "inventory_for_drain", "normalize_request",
    "purge_runtime_residue", "read_receipt", "read_request", "write_receipt", "write_request",
]
