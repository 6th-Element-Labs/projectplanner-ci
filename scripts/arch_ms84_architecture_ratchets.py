#!/usr/bin/env python3
"""ARCH-MS-84 architecture ratchets — import direction, ceilings, typed bodies, ruff.

Reads ``perf/arch_ms84_ratchet_baseline.json`` and fails closed when a measured
count exceeds its committed ceiling. Ceilings turn one way: lower the committed
number in the same PR that shrinks the measured set.

Usage::

    python scripts/arch_ms84_architecture_ratchets.py
    python scripts/arch_ms84_architecture_ratchets.py --json
    python scripts/arch_ms84_architecture_ratchets.py --ruff-changed --base origin/master
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "perf" / "arch_ms84_ratchet_baseline.json"
AUTH_PKG = ROOT / "src" / "switchboard" / "api" / "routers" / "auth"
TASKS_PKG = ROOT / "src" / "switchboard" / "services" / "tasks"
STORE_IMPORT_RE = re.compile(r"^\s*(import\s+store\b|from\s+store\b)")
BODY_DICT_RE = re.compile(r"body:\s*dict\s*=\s*Body\b")


def _load_baseline() -> Dict[str, Any]:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _iter_py(paths: Iterable[Path]) -> Iterable[Path]:
    for path in paths:
        if path.is_file() and path.suffix == ".py":
            yield path
            continue
        if not path.is_dir():
            continue
        for p in path.rglob("*.py"):
            if any(part in {".git", "venv", ".venv", "__pycache__", "node_modules"}
                   for part in p.parts):
                continue
            yield p


def _package_forbidden_hits(package: Path, forbidden: Sequence[str]) -> List[str]:
    forbidden_set = frozenset(forbidden)
    hits: List[str] = []
    if not package.is_dir():
        return hits
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    if root in forbidden_set:
                        hits.append(f"{path.relative_to(ROOT)}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    continue
                mod = node.module or ""
                root = mod.split(".", 1)[0]
                if root in forbidden_set:
                    hits.append(f"{path.relative_to(ROOT)}: from {mod} import …")
    return hits


def _auth_forbidden_hits(forbidden: Sequence[str]) -> List[str]:
    return _package_forbidden_hits(AUTH_PKG, forbidden)


def _tasks_forbidden_hits(forbidden: Sequence[str]) -> List[str]:
    return _package_forbidden_hits(TASKS_PKG, forbidden)


def _store_import_files(src_root: Path) -> List[str]:
    found: List[str] = []
    for path in _iter_py([src_root]):
        text = path.read_text(encoding="utf-8")
        if any(STORE_IMPORT_RE.search(line) for line in text.splitlines()):
            found.append(str(path.relative_to(ROOT)))
    return sorted(found)


def _wildcard_sites(src_root: Path) -> List[str]:
    sites: List[str] = []
    for path in _iter_py([src_root]):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
                mod = node.module or ""
                sites.append(f"{path.relative_to(ROOT)}:{node.lineno} from {mod} import *")
    return sites


def _untyped_body_lines(routers_root: Path) -> Tuple[List[str], List[str]]:
    all_hits: List[str] = []
    auth_hits: List[str] = []
    for path in _iter_py([routers_root]):
        rel = str(path.relative_to(ROOT))
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if BODY_DICT_RE.search(line):
                item = f"{rel}:{i}"
                all_hits.append(item)
                if "routers/auth/" in rel.replace("\\", "/"):
                    auth_hits.append(item)
    return all_hits, auth_hits


def _changed_py_files(base_ref: str) -> List[str]:
    cmd = ["git", "-C", str(ROOT), "diff", "--name-only", "--diff-filter=ACMR",
           f"{base_ref}...HEAD", "--", "*.py"]
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Fall back to unstaged+staged vs HEAD when no merge-base range works.
        try:
            out = subprocess.check_output(
                ["git", "-C", str(ROOT), "diff", "--name-only", "--diff-filter=ACMR",
                 "HEAD", "--", "*.py"],
                text=True, stderr=subprocess.DEVNULL)
        except (subprocess.CalledProcessError, FileNotFoundError):
            return []
    return [line.strip() for line in out.splitlines() if line.strip().endswith(".py")]


def _run_ruff(paths: Sequence[str]) -> Dict[str, Any]:
    if not paths:
        return {"skipped": True, "reason": "no_changed_python_files", "ok": True}
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "ruff", "check", *paths],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as exc:  # pragma: no cover
        return {"skipped": True, "reason": f"ruff_unavailable:{exc}", "ok": True}
    if proc.returncode == 2 and "No module named ruff" in (proc.stderr or ""):
        return {"skipped": True, "reason": "ruff_not_installed", "ok": True}
    return {
        "skipped": False,
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "paths": list(paths),
        "stdout": (proc.stdout or "")[-4000:],
        "stderr": (proc.stderr or "")[-2000:],
    }


def build_report(*, ruff_changed: bool = False, base_ref: str = "origin/master") -> Dict[str, Any]:
    baseline = _load_baseline()
    scopes = baseline["scopes"]
    checks: Dict[str, bool] = {}
    details: Dict[str, Any] = {}

    forbidden = scopes["auth_forbidden_imports"]["forbidden_root_modules"]
    auth_hits = _auth_forbidden_hits(forbidden)
    auth_ok = len(auth_hits) <= int(scopes["auth_forbidden_imports"]["ceiling"])
    checks["auth_forbidden_imports"] = auth_ok
    details["auth_forbidden_imports"] = {
        "count": len(auth_hits),
        "ceiling": scopes["auth_forbidden_imports"]["ceiling"],
        "hits": auth_hits,
    }

    tasks_scope = scopes.get("tasks_forbidden_imports") or {}
    tasks_forbidden = tasks_scope.get("forbidden_root_modules") or [
        "store", "auth", "notify", "dispatch", "agent",
        "app_impl", "mcp_server", "mcp_server_impl",
    ]
    tasks_hits = _tasks_forbidden_hits(tasks_forbidden)
    tasks_ceiling = int(tasks_scope.get("ceiling", 0))
    tasks_ok = len(tasks_hits) <= tasks_ceiling
    checks["tasks_forbidden_imports"] = tasks_ok
    details["tasks_forbidden_imports"] = {
        "count": len(tasks_hits),
        "ceiling": tasks_ceiling,
        "hits": tasks_hits,
    }

    store_files = _store_import_files(ROOT / "src")
    store_ceiling = int(scopes["store_import_files_src"]["ceiling"])
    store_ok = len(store_files) <= store_ceiling
    checks["store_import_files_src"] = store_ok
    details["store_import_files_src"] = {
        "count": len(store_files),
        "ceiling": store_ceiling,
        "measured_baseline": scopes["store_import_files_src"].get("measured"),
        "files": store_files,
    }

    wild = _wildcard_sites(ROOT / "src")
    wild_ceiling = int(scopes["wildcard_import_sites_src"]["ceiling"])
    wild_ok = len(wild) <= wild_ceiling
    checks["wildcard_import_sites_src"] = wild_ok
    details["wildcard_import_sites_src"] = {
        "count": len(wild),
        "ceiling": wild_ceiling,
        "sites": wild,
    }

    body_all, body_auth = _untyped_body_lines(ROOT / "src" / "switchboard" / "api" / "routers")
    body_ceiling = int(scopes["untyped_body_dict_routers"]["ceiling"])
    auth_body_ceiling = int(scopes["untyped_body_dict_routers"]["auth_package_ceiling"])
    body_ok = len(body_all) <= body_ceiling and len(body_auth) <= auth_body_ceiling
    checks["untyped_body_dict_routers"] = body_ok
    details["untyped_body_dict_routers"] = {
        "count": len(body_all),
        "ceiling": body_ceiling,
        "auth_count": len(body_auth),
        "auth_ceiling": auth_body_ceiling,
        "hits": body_all[:40],
        "auth_hits": body_auth,
    }

    ruff_result: Dict[str, Any]
    if ruff_changed or os.environ.get("ARCH_MS84_RUFF", "").strip() in {"1", "true", "yes"}:
        changed = _changed_py_files(base_ref)
        # Always include paths that exist in the worktree.
        existing = [p for p in changed if (ROOT / p).is_file()]
        ruff_result = _run_ruff(existing)
        checks["ruff_changed"] = bool(ruff_result.get("ok"))
    else:
        ruff_result = {"skipped": True, "reason": "ruff_not_requested", "ok": True}
        checks["ruff_changed"] = True
    details["ruff"] = ruff_result

    ok = all(checks.values())
    return {
        "schema": "switchboard.arch_ms84_architecture_ratchet_report.v1",
        "task": "ARCH-MS-84",
        "ok": ok,
        "checks": checks,
        "details": details,
        "baseline_path": str(BASELINE_PATH.relative_to(ROOT)),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--ruff-changed", action="store_true",
                        help="Run ruff check on Python files changed vs --base")
    parser.add_argument("--base", default="origin/master",
                        help="git merge-base ref for --ruff-changed (default: origin/master)")
    args = parser.parse_args(argv)
    report = build_report(ruff_changed=args.ruff_changed, base_ref=args.base)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("ARCH-MS-84 architecture ratchets")
        for name, passed in report["checks"].items():
            detail = report["details"].get(name) or report["details"].get(
                "ruff" if name == "ruff_changed" else name, {})
            count = detail.get("count")
            ceiling = detail.get("ceiling")
            extra = ""
            if count is not None and ceiling is not None:
                extra = f" ({count}/{ceiling})"
            if name == "ruff_changed":
                ruff = report["details"]["ruff"]
                if ruff.get("skipped"):
                    extra = f" (skipped:{ruff.get('reason')})"
                else:
                    extra = f" (exit={ruff.get('exit_code')})"
            print(("  PASS  " if passed else "  FAIL  ") + name + extra)
        print("OK" if report["ok"] else "FAIL")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
