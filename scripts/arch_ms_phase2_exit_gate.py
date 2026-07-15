#!/usr/bin/env python3
"""Fail-closed ARCH-MS Phase 2 exit audit (ADR-0011 Decision 5 / ARCH-MS-74).

Phase 2 closes when **either** Path A (Auth process cut) **or** Path B
(documented No-Go, Auth stays in-process) is fully evidenced. Half-cuts fail.

Shared fail-closed checks (always required):
  - Phase 1 exit gate still green
  - No dual-auth implementation markers in live code
  - Tasks cut **or** Tasks readiness artifact present
  - Architecture rails present (charter ADR + service skeleton)

Path A — Auth cut (Go):
  - Independence verdict artifact records ``go``
  - Auth service package + non-example systemd unit present
  - Production Caddy routes ``/api/auth*``
  - Auth cutover/rollback playbook present

Path B — Documented No-Go:
  - Independence verdict artifact records ``nogo``
  - Auth remains in-process (no live Auth unit / Caddy Auth path cut)
  - No-Go rationale + measured-evidence pointers present
  - No half-cut network façade

Initially the live tree may report ``passed=false`` until 2B0/2B/2C land — that
is expected (board AC). The harness must still be importable and fail closed.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parents[1]

SCHEMA = "switchboard.arch_ms_phase2_exit.v1"

# Machine-checkable evidence paths (later 2B0/2B/2C tasks fill these in).
INDEPENDENCE_VERDICT = "docs/phase2/auth_independence_verdict.json"
NOGO_RATIONALE = "docs/phase2/auth_nogo_rationale.md"
TASKS_READINESS = "docs/phase2/tasks_readiness.md"
AUTH_CUT_PLAYBOOK = "docs/phase2/auth_cut_playbook.md"

AUTH_SERVICE_PACKAGE = "src/switchboard/services/auth/app.py"
AUTH_SERVICE_UNIT = "deploy/switchboard-auth.service"
AUTH_SERVICE_UNIT_EXAMPLE = "deploy/switchboard-auth.service.example"

CHARTER_ADR = "docs/decisions/0011-phase2-process-strangler.md"
SKELETON_APP = "src/switchboard/services/_skeleton/app.py"
PHASE1_GATE = "scripts/arch_ms_phase1_exit_gate.py"

# Live code surfaces scanned for dual-auth / legacy cutover markers.
DUAL_AUTH_SCAN_PATHS = (
    "app.py",
    "app_impl.py",
    "auth.py",
    "mcp_server.py",
    "mcp_server_impl.py",
    "src/switchboard/api/routers/auth",
    "static",
)
DUAL_AUTH_MARKERS = (
    "PM_GLOBAL_AUTH",
    "legacy_per_project_login",
    "LEGACY_PROJECT_LOGIN",
)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _exists(root: Path, rel: str) -> bool:
    return (root / rel).is_file()


def _load_json(path: Path) -> Dict[str, Any]:
    data = json.loads(_read_text(path))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected JSON object")
    return data


def _scan_dual_auth_markers(root: Path) -> List[str]:
    """Return live-code hits for dual-auth markers (docs/ are ignored)."""
    hits: List[str] = []
    for rel in DUAL_AUTH_SCAN_PATHS:
        path = root / rel
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            candidates = sorted(path.rglob("*"))
        else:
            continue
        for file_path in candidates:
            if not file_path.is_file():
                continue
            if file_path.suffix not in {".py", ".html", ".js", ".css", ""}:
                continue
            try:
                text = _read_text(file_path)
            except (OSError, UnicodeDecodeError):
                continue
            for marker in DUAL_AUTH_MARKERS:
                if marker in text:
                    hits.append(f"{file_path.relative_to(root)}:{marker}")
    return hits


def _caddy_routes_api_auth(root: Path) -> Dict[str, Any]:
    """Detect production Caddy routing for ``/api/auth*`` (Path A cutover)."""
    caddy = root / "deploy" / "Caddyfile"
    if not caddy.is_file():
        return {"present": False, "routes_api_auth": False, "snippet": None}
    text = _read_text(caddy)
    # Match handle/path blocks that mention /api/auth (not comments-only).
    live_lines = [
        line for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    joined = "\n".join(live_lines)
    routes = bool(
        re.search(r"(?m)^\s*handle\s+/api/auth", joined)
        or re.search(r"/api/auth\*", joined)
    )
    return {
        "present": True,
        "routes_api_auth": routes,
        "snippet": next(
            (line.strip() for line in live_lines if "/api/auth" in line),
            None,
        ),
    }


def _run_phase1_gate(root: Path) -> Dict[str, Any]:
    """Subprocess the Phase 1 exit gate against ``root`` when possible.

    The Phase 1 script is rooted at its own file location for ``ROOT``, so for
    the live tree we execute it directly. Fixture trees that are not the real
    repo cannot re-root Phase 1 without importing it — those use an importable
    override via ``phase1_passed`` injection in tests through ``build_report``.
    """
    script = root / PHASE1_GATE
    if not script.is_file():
        return {"ran": False, "passed": False, "error": "phase1_gate_missing"}
    # Only trust the subprocess result when evaluating the real checkout; fixture
    # roots that copy the script still execute against the script's baked ROOT.
    if root.resolve() != ROOT.resolve():
        return {
            "ran": False,
            "passed": False,
            "error": "phase1_subprocess_only_valid_on_live_root",
            "skipped_for_fixture": True,
        }
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=root,
        text=True,
        capture_output=True,
    )
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "ran": True,
            "passed": False,
            "returncode": proc.returncode,
            "error": (proc.stdout or proc.stderr or "invalid_json")[:500],
        }
    return {
        "ran": True,
        "passed": bool(report.get("passed")),
        "returncode": proc.returncode,
        "schema": report.get("schema"),
    }


def _tasks_cut_or_readiness(root: Path) -> Dict[str, Any]:
    tasks_service = _exists(root, "src/switchboard/services/tasks/app.py")
    readiness = _exists(root, TASKS_READINESS)
    return {
        "tasks_service_present": tasks_service,
        "readiness_artifact_present": readiness,
        "ok": tasks_service or readiness,
        "readiness_path": TASKS_READINESS,
    }


def _independence_verdict(root: Path) -> Dict[str, Any]:
    path = root / INDEPENDENCE_VERDICT
    if not path.is_file():
        return {
            "present": False,
            "verdict": None,
            "path": INDEPENDENCE_VERDICT,
        }
    try:
        data = _load_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "present": True,
            "verdict": None,
            "path": INDEPENDENCE_VERDICT,
            "error": f"{type(exc).__name__}: {exc}",
        }
    raw = str(data.get("verdict") or data.get("decision") or "").strip().lower()
    # Normalize common spellings.
    if raw in {"go", "yes", "cut"}:
        verdict = "go"
    elif raw in {"nogo", "no-go", "no_go", "keep-in-process", "keep_in_process"}:
        verdict = "nogo"
    else:
        verdict = raw or None
    return {
        "present": True,
        "verdict": verdict,
        "path": INDEPENDENCE_VERDICT,
        "raw": data,
    }


def _auth_process_artifacts(root: Path) -> Dict[str, Any]:
    package = _exists(root, AUTH_SERVICE_PACKAGE)
    unit = _exists(root, AUTH_SERVICE_UNIT)
    unit_example = _exists(root, AUTH_SERVICE_UNIT_EXAMPLE)
    return {
        "package_present": package,
        "unit_present": unit,
        "unit_example_present": unit_example,
        "package_path": AUTH_SERVICE_PACKAGE,
        "unit_path": AUTH_SERVICE_UNIT,
        "ok_for_path_a": package and unit,
    }


def _half_cut_detected(
    *,
    auth_artifacts: Dict[str, Any],
    caddy: Dict[str, Any],
    verdict: Optional[str],
) -> bool:
    """True when Auth looks process-cut without a recorded Go (forbidden)."""
    live_cut_signals = bool(
        auth_artifacts.get("unit_present") or caddy.get("routes_api_auth")
    )
    if not live_cut_signals:
        return False
    return verdict != "go"


def build_report(
    root: Optional[Path] = None,
    *,
    phase1_passed: Optional[bool] = None,
) -> Dict[str, Any]:
    """Return the Phase 2 exit evidence report.

    ``phase1_passed`` lets fixture tests inject Phase 1 status without
    re-rooting the Phase 1 script's baked ``ROOT``.
    """
    root = root or ROOT
    verdict_info = _independence_verdict(root)
    verdict = verdict_info.get("verdict")
    auth_artifacts = _auth_process_artifacts(root)
    caddy = _caddy_routes_api_auth(root)
    dual_hits = _scan_dual_auth_markers(root)
    tasks = _tasks_cut_or_readiness(root)
    half_cut = _half_cut_detected(
        auth_artifacts=auth_artifacts, caddy=caddy, verdict=verdict if isinstance(verdict, str) else None
    )

    if phase1_passed is None:
        phase1 = _run_phase1_gate(root)
        phase1_ok = bool(phase1.get("passed"))
    else:
        phase1 = {"ran": False, "passed": bool(phase1_passed), "injected": True}
        phase1_ok = bool(phase1_passed)

    rails = {
        "charter_adr_present": _exists(root, CHARTER_ADR),
        "skeleton_present": _exists(root, SKELETON_APP),
        "phase1_gate_script_present": _exists(root, PHASE1_GATE),
    }
    rails_ok = all(rails.values())

    no_dual_auth = not dual_hits
    playbook_present = _exists(root, AUTH_CUT_PLAYBOOK)
    nogo_rationale_present = _exists(root, NOGO_RATIONALE)

    # Shared checks from the board task text.
    shared_checks = {
        "phase1_exit_green": phase1_ok,
        "no_dual_auth_markers": no_dual_auth,
        "tasks_cut_or_readiness": bool(tasks["ok"]),
        "architecture_rails_present": rails_ok,
        "no_half_cut_network_facade": not half_cut,
    }

    path_a_checks = {
        "independence_verdict_go": verdict == "go",
        "auth_service_artifacts_present": bool(auth_artifacts["ok_for_path_a"]),
        "caddy_routes_api_auth": bool(caddy.get("routes_api_auth")),
        "auth_cut_playbook_present": playbook_present,
    }
    path_a = all(path_a_checks.values()) and all(shared_checks.values())

    path_b_checks = {
        "independence_verdict_nogo": verdict == "nogo",
        "nogo_rationale_present": nogo_rationale_present,
        "auth_remains_in_process": (
            not auth_artifacts.get("unit_present")
            and not caddy.get("routes_api_auth")
        ),
    }
    path_b = all(path_b_checks.values()) and all(shared_checks.values())

    checks = {
        **shared_checks,
        "path_a_auth_cut": path_a,
        "path_b_documented_nogo": path_b,
        "exit_path_satisfied": path_a or path_b,
    }

    return {
        "schema": SCHEMA,
        "charter": "ADR-0011 Decision 5",
        "task_id": "ARCH-MS-74",
        "independence": verdict_info,
        "auth_process_artifacts": auth_artifacts,
        "caddy": caddy,
        "dual_auth_marker_hits": dual_hits,
        "tasks": tasks,
        "architecture_rails": rails,
        "phase1": phase1,
        "half_cut_detected": half_cut,
        "path_a_checks": path_a_checks,
        "path_b_checks": path_b_checks,
        "paths": {
            "path_a_auth_cut": path_a,
            "path_b_documented_nogo": path_b,
        },
        "evidence_paths": {
            "independence_verdict": INDEPENDENCE_VERDICT,
            "nogo_rationale": NOGO_RATIONALE,
            "tasks_readiness": TASKS_READINESS,
            "auth_cut_playbook": AUTH_CUT_PLAYBOOK,
            "auth_service_package": AUTH_SERVICE_PACKAGE,
            "auth_service_unit": AUTH_SERVICE_UNIT,
        },
        "checks": checks,
        "passed": bool(checks["exit_path_satisfied"]),
    }


def main() -> int:
    try:
        report = build_report()
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        report = {
            "schema": SCHEMA,
            "passed": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
