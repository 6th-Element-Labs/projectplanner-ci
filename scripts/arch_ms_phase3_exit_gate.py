#!/usr/bin/env python3
"""Fail-closed ARCH-MS Phase 3 exit audit (ADR-0012 Decision 5 / ARCH-MS-86).

Phase 3 closes when **either** Path A (Tasks process cut) **or** Path B
(documented No-Go, Tasks stays in-process) is fully evidenced. Half-cuts fail.

Shared fail-closed checks (always required):
  - Phase 2 exit gate still green
  - ADR-0012 charter present
  - No network-wrap while root ``store`` imports remain in Tasks service code
  - No half-cut network façade (live unit/Caddy without recorded Go)

Path A — Tasks cut (Go):
  - Independence verdict artifact records ``go`` **and** authorizes process cut
    (operator G6 when ``operator_g6_required`` / Conditional Go)
  - Tasks service package + non-example systemd unit present
  - Production Caddy routes ``/api/tasks*`` (+ claim-only TXP)
  - Dual-strip: monolith sets ``PM_TASKS_HTTP_PRIMARY=service``
  - Tasks cutover/rollback playbook present

Path B — Documented No-Go:
  - Independence verdict artifact records ``nogo``
  - Tasks remains in-process (no live Tasks unit / Caddy Tasks path cut)
  - No-Go rationale + measured-evidence pointers present
  - No half-cut network façade

Initially the live tree may report ``passed=false`` until 3B0/3B land — that
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

SCHEMA = "switchboard.arch_ms_phase3_exit.v1"

# Machine-checkable evidence paths (later 3B0/3B tasks fill these in).
INDEPENDENCE_VERDICT = "docs/phase3/tasks_independence_verdict.json"
NOGO_RATIONALE = "docs/phase3/tasks_nogo_rationale.md"
TASKS_CUT_PLAYBOOK = "docs/phase3/tasks_cut_playbook.md"

TASKS_SERVICE_PACKAGE = "src/switchboard/services/tasks/app.py"
TASKS_SERVICE_DIR = "src/switchboard/services/tasks"
TASKS_SERVICE_UNIT = "deploy/switchboard-tasks.service"
TASKS_SERVICE_UNIT_EXAMPLE = "deploy/switchboard-tasks.service.example"

CHARTER_ADR = "docs/decisions/0012-phase3-tasks-process-strangler.md"
PHASE2_GATE = "scripts/arch_ms_phase2_exit_gate.py"
MONOLITH_UNIT = "deploy/projectplanner.service"

DUAL_STRIP_ENV = "PM_TASKS_HTTP_PRIMARY=service"

# Root store coupling — network wrapping with these remaining is forbidden.
STORE_IMPORT_RE = re.compile(
    r"(?m)^\s*(?:from\s+store(?:\s+import|\s*\.)|import\s+store\b)"
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


def _caddy_live_lines(root: Path) -> Dict[str, Any]:
    caddy = root / "deploy" / "Caddyfile"
    if not caddy.is_file():
        return {"present": False, "live_text": "", "snippet_tasks": None, "snippet_claim": None}
    text = _read_text(caddy)
    live_lines = [
        line for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    joined = "\n".join(live_lines)
    return {
        "present": True,
        "live_text": joined,
        "snippet_tasks": next(
            (line.strip() for line in live_lines if "/api/tasks" in line),
            None,
        ),
        "snippet_claim": next(
            (
                line.strip()
                for line in live_lines
                if "/txp/v1/claim" in line or "claim_next" in line
            ),
            None,
        ),
    }


def _caddy_routes_tasks(root: Path) -> Dict[str, Any]:
    """Detect production Caddy routing for day-one Tasks surface."""
    info = _caddy_live_lines(root)
    if not info["present"]:
        return {
            "present": False,
            "routes_api_tasks": False,
            "routes_claim_txp": False,
            "snippet": None,
        }
    joined = info["live_text"]
    routes_tasks = bool(
        re.search(r"(?m)^\s*handle\s+/api/tasks", joined)
        or re.search(r"/api/tasks\*", joined)
    )
    routes_claim = bool(
        re.search(r"/txp/v1/claim", joined)
        or re.search(r"claim_next", joined)
    )
    return {
        "present": True,
        "routes_api_tasks": routes_tasks,
        "routes_claim_txp": routes_claim,
        "snippet": info["snippet_tasks"] or info["snippet_claim"],
    }


def _dual_strip_present(root: Path) -> Dict[str, Any]:
    """Detect monolith dual-strip for Tasks HTTP (Auth analogue)."""
    hits: List[str] = []
    candidates = (
        MONOLITH_UNIT,
        "deploy/projectplanner.service.example",
        "app.py",
        "app_impl.py",
    )
    for rel in candidates:
        path = root / rel
        if not path.is_file():
            continue
        try:
            text = _read_text(path)
        except (OSError, UnicodeDecodeError):
            continue
        if DUAL_STRIP_ENV in text or "PM_TASKS_HTTP_PRIMARY" in text:
            # Require the production value for Path A; mere mention is noted.
            if DUAL_STRIP_ENV in text:
                hits.append(rel)
            elif "PM_TASKS_HTTP_PRIMARY" in text:
                hits.append(f"{rel}:mentioned")
    live = [h for h in hits if not h.endswith(":mentioned")]
    return {
        "env": DUAL_STRIP_ENV,
        "hits": hits,
        "ok": bool(live),
    }


def _scan_store_imports_in_tasks(root: Path) -> List[str]:
    """Return Tasks-service files that still import root ``store``."""
    service_dir = root / TASKS_SERVICE_DIR
    if not service_dir.is_dir():
        return []
    hits: List[str] = []
    for file_path in sorted(service_dir.rglob("*.py")):
        try:
            text = _read_text(file_path)
        except (OSError, UnicodeDecodeError):
            continue
        if STORE_IMPORT_RE.search(text):
            hits.append(str(file_path.relative_to(root)))
    return hits


def _run_phase2_gate(root: Path) -> Dict[str, Any]:
    """Subprocess the Phase 2 exit gate against the live checkout when possible."""
    script = root / PHASE2_GATE
    if not script.is_file():
        return {"ran": False, "passed": False, "error": "phase2_gate_missing"}
    if root.resolve() != ROOT.resolve():
        return {
            "ran": False,
            "passed": False,
            "error": "phase2_subprocess_only_valid_on_live_root",
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


def _independence_verdict(root: Path) -> Dict[str, Any]:
    path = root / INDEPENDENCE_VERDICT
    if not path.is_file():
        return {
            "present": False,
            "verdict": None,
            "process_cut_authorized": False,
            "path": INDEPENDENCE_VERDICT,
        }
    try:
        data = _load_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "present": True,
            "verdict": None,
            "process_cut_authorized": False,
            "path": INDEPENDENCE_VERDICT,
            "error": f"{type(exc).__name__}: {exc}",
        }
    raw = str(data.get("verdict") or data.get("decision") or "").strip().lower()
    if raw in {"go", "yes", "cut"}:
        verdict = "go"
    elif raw in {"nogo", "no-go", "no_go", "keep-in-process", "keep_in_process"}:
        verdict = "nogo"
    else:
        verdict = raw or None
    authorized = _process_cut_authorized(data, verdict)
    return {
        "present": True,
        "verdict": verdict,
        "process_cut_authorized": authorized,
        "path": INDEPENDENCE_VERDICT,
        "raw": data,
    }


def _process_cut_authorized(data: Optional[Dict[str, Any]], verdict: Optional[str]) -> bool:
    """True only when independence Go authorizes ARCH-MS-90+ traffic cut.

    A Conditional Go (``operator_g6_required`` still true / ``decision=conditional_go``
    without ``inputs.G6_operator_go``) must **not** authorize live unit/Caddy cuts —
    otherwise ``no_half_cut_network_facade`` would fail open before operator G6.
    Bare ``{"verdict":"go"}`` remains authorized (fixtures / full operator Go).
    """
    if verdict != "go":
        return False
    if not isinstance(data, dict):
        return True
    inputs = data.get("inputs") if isinstance(data.get("inputs"), dict) else {}
    if data.get("operator_g6_required") is True and not inputs.get("G6_operator_go"):
        return False
    decision = str(data.get("decision") or "").strip().lower().replace("_", "-")
    if decision == "conditional-go" and not inputs.get("G6_operator_go"):
        return False
    return True


def _tasks_process_artifacts(root: Path) -> Dict[str, Any]:
    package = _exists(root, TASKS_SERVICE_PACKAGE)
    unit = _exists(root, TASKS_SERVICE_UNIT)
    unit_example = _exists(root, TASKS_SERVICE_UNIT_EXAMPLE)
    return {
        "package_present": package,
        "unit_present": unit,
        "unit_example_present": unit_example,
        "package_path": TASKS_SERVICE_PACKAGE,
        "unit_path": TASKS_SERVICE_UNIT,
        "ok_for_path_a": package and unit,
    }


def _half_cut_detected(
    *,
    tasks_artifacts: Dict[str, Any],
    caddy: Dict[str, Any],
    process_cut_authorized: bool,
) -> bool:
    """True when Tasks looks process-cut without authorized Go (forbidden)."""
    live_cut_signals = bool(
        tasks_artifacts.get("unit_present") or caddy.get("routes_api_tasks")
    )
    if not live_cut_signals:
        return False
    return not process_cut_authorized


def _network_wrap_with_store(
    *,
    store_import_hits: List[str],
    tasks_artifacts: Dict[str, Any],
    caddy: Dict[str, Any],
) -> bool:
    """True when Tasks is network-facing while root store imports remain."""
    if not store_import_hits:
        return False
    return bool(
        tasks_artifacts.get("unit_present") or caddy.get("routes_api_tasks")
    )


def build_report(
    root: Optional[Path] = None,
    *,
    phase2_passed: Optional[bool] = None,
) -> Dict[str, Any]:
    """Return the Phase 3 exit evidence report.

    ``phase2_passed`` lets fixture tests inject Phase 2 status without
    re-rooting the Phase 2 script's baked ``ROOT``.
    """
    root = root or ROOT
    verdict_info = _independence_verdict(root)
    verdict = verdict_info.get("verdict")
    process_cut_authorized = bool(verdict_info.get("process_cut_authorized"))
    tasks_artifacts = _tasks_process_artifacts(root)
    caddy = _caddy_routes_tasks(root)
    dual_strip = _dual_strip_present(root)
    store_hits = _scan_store_imports_in_tasks(root)
    half_cut = _half_cut_detected(
        tasks_artifacts=tasks_artifacts,
        caddy=caddy,
        process_cut_authorized=process_cut_authorized,
    )
    network_wrap = _network_wrap_with_store(
        store_import_hits=store_hits,
        tasks_artifacts=tasks_artifacts,
        caddy=caddy,
    )

    if phase2_passed is None:
        phase2 = _run_phase2_gate(root)
        phase2_ok = bool(phase2.get("passed"))
    else:
        phase2 = {"ran": False, "passed": bool(phase2_passed), "injected": True}
        phase2_ok = bool(phase2_passed)

    rails = {
        "charter_adr_present": _exists(root, CHARTER_ADR),
        "phase2_gate_script_present": _exists(root, PHASE2_GATE),
    }
    rails_ok = all(rails.values())

    playbook_present = _exists(root, TASKS_CUT_PLAYBOOK)
    nogo_rationale_present = _exists(root, NOGO_RATIONALE)

    shared_checks = {
        "phase2_exit_green": phase2_ok,
        "adr_0012_present": bool(rails["charter_adr_present"]),
        "architecture_rails_present": rails_ok,
        "no_half_cut_network_facade": not half_cut,
        "no_network_wrap_with_store_imports": not network_wrap,
    }

    path_a_checks = {
        "independence_verdict_go": process_cut_authorized,
        "tasks_service_artifacts_present": bool(tasks_artifacts["ok_for_path_a"]),
        "caddy_routes_api_tasks": bool(caddy.get("routes_api_tasks")),
        "caddy_routes_claim_txp": bool(caddy.get("routes_claim_txp")),
        "dual_strip_present": bool(dual_strip.get("ok")),
        "tasks_cut_playbook_present": playbook_present,
    }
    path_a = all(path_a_checks.values()) and all(shared_checks.values())

    path_b_checks = {
        "independence_verdict_nogo": verdict == "nogo",
        "nogo_rationale_present": nogo_rationale_present,
        "tasks_remains_in_process": (
            not tasks_artifacts.get("unit_present")
            and not caddy.get("routes_api_tasks")
        ),
    }
    path_b = all(path_b_checks.values()) and all(shared_checks.values())

    checks = {
        **shared_checks,
        "path_a_tasks_cut": path_a,
        "path_b_documented_nogo": path_b,
        "exit_path_satisfied": path_a or path_b,
    }

    return {
        "schema": SCHEMA,
        "charter": "ADR-0012 Decision 5",
        "task_id": "ARCH-MS-86",
        "independence": verdict_info,
        "tasks_process_artifacts": tasks_artifacts,
        "caddy": caddy,
        "dual_strip": dual_strip,
        "store_import_hits": store_hits,
        "architecture_rails": rails,
        "phase2": phase2,
        "half_cut_detected": half_cut,
        "network_wrap_with_store_imports": network_wrap,
        "path_a_checks": path_a_checks,
        "path_b_checks": path_b_checks,
        "paths": {
            "path_a_tasks_cut": path_a,
            "path_b_documented_nogo": path_b,
        },
        "evidence_paths": {
            "independence_verdict": INDEPENDENCE_VERDICT,
            "nogo_rationale": NOGO_RATIONALE,
            "tasks_cut_playbook": TASKS_CUT_PLAYBOOK,
            "tasks_service_package": TASKS_SERVICE_PACKAGE,
            "tasks_service_unit": TASKS_SERVICE_UNIT,
            "charter_adr": CHARTER_ADR,
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
