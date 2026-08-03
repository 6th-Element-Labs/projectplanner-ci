"""Capacity-owned materialization for bounded verification profile names."""

from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any, Callable, Mapping

from .execution_assignment import SWITCHBOARD_CI_VERIFICATION_PROFILE


RECEIPT_SCHEMA = "switchboard.verification_toolchain_receipt.v1"
PROFILE_SPEC = {
    "profile": SWITCHBOARD_CI_VERIFICATION_PROFILE,
    "python": ">=3.12",
    "lockfile": "uv.lock",
    "requirements": ["requirements.txt", "requirements-ci.txt"],
    "entrypoint": "scripts/switchboard_ci.sh",
    "browser": "chromium",
}


class VerificationRuntimeError(RuntimeError):
    """One assigned profile cannot be proven on this physical host."""

    code = "verification_runtime_unavailable"

    def __init__(self, cause: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = {"diagnostic_cause": str(cause), **details}

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, **self.details}


def failure(cause: str, message: str, **details: Any) -> VerificationRuntimeError:
    return VerificationRuntimeError(cause, message, **details)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as exc:
        raise failure(
            "verification_artifact_unreadable",
            "a locked verification artifact is unreadable",
            path=str(path), observed_error=f"{type(exc).__name__}: {exc}",
        ) from exc
    return "sha256:" + digest.hexdigest()


def _normalized_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", str(value or "")).lower()


def _marker_applies(expression: str) -> bool:
    """Evaluate the small, data-only PEP 508 marker subset in uv exports."""
    environment = {
        "implementation_name": str(sys.implementation.name),
        "os_name": str(os.name),
        "platform_machine": platform.machine(),
        "platform_python_implementation": platform.python_implementation(),
        "platform_release": platform.release(),
        "platform_system": platform.system(),
        "platform_version": platform.version(),
        "python_full_version": platform.python_version(),
        "python_version": ".".join(map(str, sys.version_info[:2])),
        "sys_platform": str(sys.platform),
    }
    try:
        tree = ast.parse(str(expression or ""), mode="eval")
    except (SyntaxError, ValueError) as exc:
        raise failure(
            "dependency_lock_unreadable",
            "a dependency environment marker is malformed",
            marker=str(expression or ""),
        ) from exc

    def evaluate(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            values = [bool(evaluate(value)) for value in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not bool(evaluate(node.operand))
        if isinstance(node, ast.Name) and node.id in environment:
            return environment[node.id]
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Compare) and len(node.ops) == len(node.comparators):
            left = evaluate(node.left)
            for operator, comparator in zip(node.ops, node.comparators):
                right = evaluate(comparator)
                if isinstance(operator, ast.Eq):
                    matched = left == right
                elif isinstance(operator, ast.NotEq):
                    matched = left != right
                elif isinstance(operator, ast.In):
                    matched = left in right
                elif isinstance(operator, ast.NotIn):
                    matched = left not in right
                else:
                    raise TypeError(type(operator).__name__)
                if not matched:
                    return False
                left = right
            return True
        raise TypeError(type(node).__name__)

    try:
        return bool(evaluate(tree))
    except (KeyError, TypeError, ValueError) as exc:
        raise failure(
            "dependency_lock_unreadable",
            "a dependency environment marker uses an unsupported expression",
            marker=str(expression or ""),
            observed_error=f"{type(exc).__name__}: {exc}",
        ) from exc


def _requirements_pins(workspace: Path) -> tuple[dict[str, str], dict[str, str]]:
    pin_re = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;]+)(?:\s*;\s*(.+))?$")
    pins: dict[str, str] = {}
    digests: dict[str, str] = {}
    for relative in PROFILE_SPEC["requirements"]:
        path = workspace / relative
        if not path.is_file():
            raise failure(
                "dependency_lock_missing",
                "a canonical dependency export is missing",
                path=relative,
            )
        digests[relative] = _file_digest(path)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise failure(
                "dependency_lock_unreadable",
                "a canonical dependency export is unreadable",
                path=relative,
                observed_error=f"{type(exc).__name__}: {exc}",
            ) from exc
        for raw in lines:
            if not raw.strip() or raw.lstrip().startswith("#") or raw[0].isspace():
                continue
            match = pin_re.fullmatch(raw.strip())
            if not match:
                raise failure(
                    "dependency_lock_unreadable",
                    "a canonical dependency export is malformed",
                    path=relative, requirement=raw,
                )
            requirement_name, version, marker = match.groups()
            if marker and not _marker_applies(marker):
                continue
            name = _normalized_distribution_name(requirement_name)
            if name in pins and pins[name] != version:
                raise failure(
                    "dependency_lock_mismatch",
                    "dependency exports disagree on an exact version",
                    dependency=name, expected=pins[name], observed=version,
                )
            pins[name] = version
    return pins, digests


def _locked_dependency_receipt(workspace: Path) -> dict[str, Any]:
    lock_path = workspace / PROFILE_SPEC["lockfile"]
    if not lock_path.is_file():
        raise failure(
            "dependency_lock_missing",
            "the canonical dependency lock is missing",
            path=PROFILE_SPEC["lockfile"],
        )
    try:
        lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
        packages = {
            _normalized_distribution_name(item["name"]): str(item["version"])
            for item in lock["package"]
            if item.get("name") and item.get("version")
        }
    except (OSError, KeyError, TypeError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise failure(
            "dependency_lock_unreadable",
            "the canonical dependency lock cannot be decoded",
            path=PROFILE_SPEC["lockfile"],
            observed_error=f"{type(exc).__name__}: {exc}",
        ) from exc
    if str(lock.get("requires-python") or "") != ">=3.12":
        raise failure(
            "dependency_lock_mismatch",
            "the dependency lock no longer declares the profile Python floor",
            expected=">=3.12", observed=str(lock.get("requires-python") or ""),
        )
    pins, export_digests = _requirements_pins(workspace)
    observed: dict[str, str] = {}
    for name, expected in sorted(pins.items()):
        locked = packages.get(name)
        if locked != expected:
            raise failure(
                "dependency_lock_mismatch",
                "a dependency export disagrees with uv.lock",
                dependency=name, expected=locked, observed=expected,
            )
        try:
            installed = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise failure(
                "dependency_lock_mismatch",
                "a locked verification dependency is not installed",
                dependency=name, expected=expected, observed="missing",
            ) from exc
        except Exception as exc:
            raise failure(
                "dependency_lock_unreadable",
                "the verification environment cannot report a dependency version",
                dependency=name, expected=expected,
                observed_error=f"{type(exc).__name__}: {exc}",
            ) from exc
        if installed != expected:
            raise failure(
                "dependency_lock_mismatch",
                "the verification environment disagrees with the dependency lock",
                dependency=name, expected=expected, observed=installed,
            )
        observed[name] = installed
    environment_digest = hashlib.sha256(json.dumps(
        observed, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return {
        "path": PROFILE_SPEC["lockfile"],
        "lock_digest": _file_digest(lock_path),
        "requires_python": ">=3.12",
        "export_digests": export_digests,
        "verified_package_count": len(observed),
        "environment_digest": "sha256:" + environment_digest,
    }


def _playwright_receipt() -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
        version = importlib.metadata.version("playwright")
        with sync_playwright() as runtime:
            executable = Path(runtime.chromium.executable_path)
            if not executable.is_file() or not os.access(executable, os.X_OK):
                raise FileNotFoundError(str(executable))
            browser = runtime.chromium.launch(headless=True)
            browser.close()
    except Exception as exc:
        raise failure(
            "playwright_unavailable",
            "Playwright Chromium is unavailable to the locked verification runtime",
            observed_error=f"{type(exc).__name__}: {exc}",
        ) from exc
    return {
        "browser": "chromium",
        "playwright_version": version,
        "executable": str(executable),
        "launch_verified": True,
    }


def prove(
    profile: str,
    workspace: Any,
    execution_context: Mapping[str, Any],
    assignment: Mapping[str, Any],
    *,
    python_runtime_provider: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> tuple[Mapping[str, Any], dict[str, Any]]:
    """Materialize one known profile into a bounded pre-spawn receipt."""
    profile = str(profile or "").strip().lower()
    if profile != SWITCHBOARD_CI_VERIFICATION_PROFILE:
        raise failure(
            "verification_profile_unsupported",
            "the Agent Host does not implement the assigned verification profile",
            profile=profile,
        )
    root = Path(workspace.path).resolve()
    python_runtime = python_runtime_provider(execution_context)
    if not isinstance(python_runtime, Mapping) or not python_runtime.get(
        "python_executable"
    ):
        raise failure(
            "python_executable_unavailable",
            "the assigned verification profile has no proven Python runtime",
        )
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=str(root), capture_output=True, text=True, timeout=15,
            check=False, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise failure(
            "test_environment_unverifiable",
            "the isolated verification workspace cannot be inspected",
            observed_error=f"{type(exc).__name__}: {exc}",
        ) from exc
    if status.returncode:
        raise failure(
            "test_environment_unverifiable",
            "git could not verify the isolated workspace",
            observed_error=(status.stderr or status.stdout or "")[-2000:],
        )
    dirty = [line for line in status.stdout.splitlines() if line.strip()]
    if dirty:
        raise failure(
            "test_environment_dirty",
            "the isolated verification workspace is not clean",
            dirty_paths=dirty[:50],
        )
    entrypoint_relative = PROFILE_SPEC["entrypoint"]
    entrypoint = root / entrypoint_relative
    if not entrypoint.is_file() or not os.access(entrypoint, os.X_OK):
        raise failure(
            "canonical_test_entrypoint_unavailable",
            "the canonical Switchboard CI entrypoint is unavailable",
            path=entrypoint_relative,
        )
    dependencies = _locked_dependency_receipt(root)
    playwright = _playwright_receipt()
    spec_digest = hashlib.sha256(json.dumps(
        PROFILE_SPEC, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "profile": profile,
        "profile_digest": "sha256:" + spec_digest,
        "assignment": {
            "assignment_id": str(assignment.get("assignment_id") or ""),
            "execution_id": str(assignment.get("execution_id") or ""),
            "generation": int(assignment.get("generation") or 0),
            "context_digest": str(
                (assignment.get("workspace_assignment") or {}).get(
                    "context_digest") or ""),
        },
        "workspace": {
            "head_sha": str(workspace.head_sha or ""),
            "authority_digest": str(
                (workspace.receipt or {}).get("authority_digest") or ""),
            "clean": True,
            "isolated": True,
        },
        "python": {
            key: value for key, value in python_runtime.items()
            if key != "environment"
        },
        "dependencies": dependencies,
        "playwright": playwright,
        "entrypoint": {
            "path": entrypoint_relative,
            "digest": _file_digest(entrypoint),
            "executable": True,
        },
        "verified_at": time.time(),
    }
    signed = dict(receipt)
    signed.pop("verified_at", None)
    receipt["receipt_digest"] = "sha256:" + hashlib.sha256(json.dumps(
        signed, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return python_runtime, receipt
