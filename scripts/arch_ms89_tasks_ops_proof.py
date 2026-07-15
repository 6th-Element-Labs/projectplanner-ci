#!/usr/bin/env python3
"""ARCH-MS-89 Tasks cut ops proof harness.

Measures Go/No-Go inputs for ARCH-MS-90 without enabling a live Tasks process cut:

1. Multi-process SQLite contention (Tasks writer + monolith sibling on one project DB)
2. Second-uvicorn RSS/CPU budget vs interactive-slice MemoryLow (skeleton stand-in)
3. Day-one Tasks API parity (401 never 403 for unauthenticated writes; binding fail-closed)
4. Caddy Tasks cutover/rollback drill artifacts present (dry checklist)

Usage::

    python scripts/arch_ms89_tasks_ops_proof.py
    python scripts/arch_ms89_tasks_ops_proof.py --json
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from http.client import HTTPConnection
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

INTERACTIVE_MEMORY_LOW_MIB = 250  # deploy/projectplanner-interactive.slice MemoryLow
SECOND_UVICORN_RSS_BUDGET_MIB = 80  # soft budget for a cut-out Tasks/skeleton process
CONTENTION_ROUNDS = 40
CONTENTION_LOCK_ERROR_CEILING = 0
PROJECT = "switchboard"


def _ok(report: Dict[str, Any], name: str, passed: bool, **detail: Any) -> None:
    report["checks"][name] = passed
    report["details"][name] = {"ok": passed, **detail}


def _env_for_tmp(tmp: Path) -> None:
    os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(tmp / "project_registry.db")
    os.environ["PM_DB_PATH"] = str(tmp / "maxwell.db")
    os.environ["PM_HELM_DB_PATH"] = str(tmp / "helm.db")
    os.environ["PM_SWITCHBOARD_DB_PATH"] = str(tmp / "switchboard.db")
    os.environ["PM_AUTH_MODE"] = "dev-open"
    os.environ["PM_JWT_SECRET"] = "arch-ms89-ops-proof-secret"


def _worker_tasks(db_dir: str, rounds: int, out_q: mp.Queue) -> None:
    tmp = Path(db_dir)
    _env_for_tmp(tmp)
    errors: List[str] = []
    ok_n = 0
    t0 = time.perf_counter()
    try:
        from switchboard.api.tasks_port_adapters import configure_tasks_ports
        from switchboard.services.tasks import deps as tasks_deps
        import store

        configure_tasks_ports()
        for i in range(rounds):
            try:
                created = store.create_task(
                    {
                        "workstream_id": "ARCH-MS",
                        "title": f"ops-proof-tasks-{os.getpid()}-{i}",
                        "status": "Not Started",
                    },
                    actor=f"cursor/ops-tasks-{os.getpid()}",
                    project=PROJECT,
                )
                if not created or not created.get("task_id"):
                    errors.append(f"create_failed:{i}")
                    continue
                tid = created["task_id"]
                tasks_deps.board().add_comment(
                    tid,
                    f"cursor/ops-tasks-{os.getpid()}",
                    f"comment-{i}",
                    project=PROJECT,
                    hydrate_task=False,
                )
                rows = tasks_deps.board().list_tasks(project=PROJECT)
                if not isinstance(rows, list):
                    errors.append(f"list_failed:{i}")
                else:
                    ok_n += 1
            except Exception as exc:
                msg = str(exc)
                if "database is locked" in msg.lower() or "locked" in msg.lower():
                    errors.append(f"lock:{msg}")
                else:
                    errors.append(f"err:{type(exc).__name__}:{msg}")
    except Exception as exc:
        errors.append(f"setup:{type(exc).__name__}:{exc}")
    out_q.put({
        "role": "tasks",
        "ok": ok_n,
        "errors": errors,
        "elapsed_s": time.perf_counter() - t0,
        "pid": os.getpid(),
    })


def _worker_monolith(db_dir: str, rounds: int, out_q: mp.Queue) -> None:
    """Sibling process writing the same project SQLite (activity / meta)."""
    tmp = Path(db_dir)
    _env_for_tmp(tmp)
    errors: List[str] = []
    ok_n = 0
    t0 = time.perf_counter()
    try:
        import store
        from switchboard.storage.repositories import activity as activity_repo

        for i in range(rounds):
            try:
                # Same DB file, non-Tasks-owned meta + unscoped activity — models the
                # monolith sibling that remains co-located after a Tasks process cut.
                activity_repo.append_activity(
                    "ops.proof.ping",
                    f"cursor/ops-mono-{os.getpid()}",
                    {"i": i, "role": "monolith"},
                    task_id=None,
                    project=PROJECT,
                )
                store.set_meta(
                    f"ops_proof_{os.getpid()}_{i}", {"i": i}, project=PROJECT)
                ok_n += 1
            except Exception as exc:
                msg = str(exc)
                if "database is locked" in msg.lower() or "locked" in msg.lower():
                    errors.append(f"lock:{msg}")
                else:
                    errors.append(f"err:{type(exc).__name__}:{msg}")
    except Exception as exc:
        errors.append(f"setup:{type(exc).__name__}:{exc}")
    out_q.put({
        "role": "monolith",
        "ok": ok_n,
        "errors": errors,
        "elapsed_s": time.perf_counter() - t0,
        "pid": os.getpid(),
    })


def run_sqlite_contention(rounds: int = CONTENTION_ROUNDS) -> Dict[str, Any]:
    tmp = Path(tempfile.mkdtemp(prefix="arch-ms89-contention-"))
    _env_for_tmp(tmp)
    import store
    from switchboard.api.tasks_port_adapters import configure_tasks_ports

    configure_tasks_ports()
    store.init_project_registry()
    store.create_project("Switchboard", project_id=PROJECT, actor="ops-proof")
    store.init_db(PROJECT)

    ctx = mp.get_context("spawn")
    q: mp.Queue = ctx.Queue()
    procs = [
        ctx.Process(target=_worker_tasks, args=(str(tmp), rounds, q)),
        ctx.Process(target=_worker_monolith, args=(str(tmp), rounds, q)),
    ]
    for p in procs:
        p.start()
    results = [q.get(timeout=180) for _ in procs]
    for p in procs:
        p.join(timeout=30)
        if p.is_alive():
            p.terminate()
    lock_errors = sum(
        1 for r in results for e in r.get("errors", []) if e.startswith("lock:")
    )
    other_errors = [
        e for r in results for e in r.get("errors", [])
        if not e.startswith("lock:")
    ]
    total_ok = sum(int(r.get("ok") or 0) for r in results)
    passed = lock_errors <= CONTENTION_LOCK_ERROR_CEILING and not other_errors and total_ok > 0
    shutil.rmtree(tmp, ignore_errors=True)
    return {
        "ok": passed,
        "rounds_per_worker": rounds,
        "lock_errors": lock_errors,
        "lock_error_ceiling": CONTENTION_LOCK_ERROR_CEILING,
        "other_errors": other_errors[:20],
        "total_ok": total_ok,
        "workers": results,
        "verdict": (
            "pass_shared_project_sqlite_under_short_load"
            if passed else
            "no_go_multi_process_sqlite_contention"
        ),
    }


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def run_second_uvicorn_budget(timeout_s: float = 25.0) -> Dict[str, Any]:
    """Skeleton uvicorn stand-in until ARCH-MS-90 lands services/tasks create_app."""
    port = _free_port()
    env = os.environ.copy()
    env["SWITCHBOARD_SKELETON_PORT"] = str(port)
    env["SWITCHBOARD_SKELETON_HOST"] = "127.0.0.1"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), str(SRC)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn",
         "switchboard.services._skeleton.app:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    rss_kib = 0
    healthy = False
    err = ""
    deadline = time.time() + timeout_s
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                err = (proc.stderr.read() if proc.stderr else "")[-1500:]
                break
            try:
                conn = HTTPConnection("127.0.0.1", port, timeout=1.0)
                conn.request("GET", "/health")
                resp = conn.getresponse()
                body = resp.read()
                conn.close()
                if resp.status == 200 and b"ok" in body:
                    healthy = True
                    break
            except OSError:
                time.sleep(0.15)
        if healthy:
            try:
                status = Path(f"/proc/{proc.pid}/status").read_text(encoding="utf-8")
                for line in status.splitlines():
                    if line.startswith("VmRSS:"):
                        rss_kib = int(line.split()[1])
                        break
            except Exception:
                try:
                    out = subprocess.check_output(
                        ["ps", "-o", "rss=", "-p", str(proc.pid)], text=True)
                    rss_kib = int(out.strip() or "0")
                except Exception as exc:
                    err = f"rss_sample_failed:{exc}"
        rss_mib = rss_kib / 1024.0 if rss_kib else 0.0
        within_budget = healthy and rss_mib > 0 and rss_mib <= SECOND_UVICORN_RSS_BUDGET_MIB
        headroom_mib = INTERACTIVE_MEMORY_LOW_MIB - rss_mib
        return {
            "ok": within_budget,
            "healthy": healthy,
            "port": port,
            "rss_mib": round(rss_mib, 2),
            "budget_mib": SECOND_UVICORN_RSS_BUDGET_MIB,
            "interactive_memory_low_mib": INTERACTIVE_MEMORY_LOW_MIB,
            "headroom_vs_memory_low_mib": round(headroom_mib, 2),
            "stand_in": "switchboard.services._skeleton",
            "error": err[:500] if err else None,
            "verdict": (
                "pass_second_uvicorn_within_soft_budget"
                if within_budget else
                "measure_failed_or_over_budget"
            ),
        }
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def run_status_parity() -> Dict[str, Any]:
    """Day-one Tasks surface: unauthenticated writes → 401 (never 403); binding fail-closed."""
    tmp = Path(tempfile.mkdtemp(prefix="arch-ms89-parity-"))
    _env_for_tmp(tmp)
    os.environ["PM_AUTH_MODE"] = "required"

    from fastapi import FastAPI, HTTPException, Request
    from fastapi.testclient import TestClient

    from switchboard.api.tasks_port_adapters import configure_tasks_ports
    from switchboard.api.routers.tasks import create_router
    from switchboard.services.tasks import binding as tasks_binding
    import store

    configure_tasks_ports()
    store.init_project_registry()
    store.create_project("Switchboard", project_id=PROJECT, actor="ops-proof")
    store.init_db(PROJECT)

    def resolve_project(project: str) -> str:
        return project or PROJECT

    def resolve_principal(request: Request, project: str, scopes, dev_actor: str = "web"):
        # Simulate production-required auth: missing bearer → 401 (never 403).
        auth_header = (request.headers.get("authorization") or "").strip()
        if not auth_header:
            raise HTTPException(401, "authentication required")
        return {
            "id": "parity-principal",
            "display_name": "parity-principal",
            "kind": "user",
            "scopes": list(scopes or ()),
        }

    app = FastAPI()
    app.include_router(create_router(
        resolve_project=resolve_project,
        resolve_principal=resolve_principal,
    ))
    client = TestClient(app, raise_server_exceptions=False)

    cases: List[Dict[str, Any]] = []

    r = client.get("/api/tasks", params={"project": PROJECT})
    cases.append({
        "name": "list_tasks_unauthenticated_read",
        "status": r.status_code,
        "expect_family": "200",
        "ok": r.status_code == 200,
    })

    r = client.post(
        "/api/tasks",
        params={"project": PROJECT},
        json={"workstream_id": "ARCH-MS", "title": "parity create"},
    )
    cases.append({
        "name": "create_task_unauthenticated",
        "status": r.status_code,
        "expect_family": "401",
        "ok": r.status_code == 401,
    })

    # Authenticated path must not return 403 for a present principal (Access/grants
    # use 403). Store/schema failures may be 4xx/5xx — still never 403 for auth miss.
    r = client.post(
        "/api/tasks",
        params={"project": PROJECT},
        headers={"Authorization": "Bearer parity-token"},
        json={"workstream_id": "ARCH-MS", "title": "parity create ok"},
    )
    cases.append({
        "name": "create_task_authenticated_not_403",
        "status": r.status_code,
        "expect_family": "not_403",
        "ok": r.status_code != 403,
    })

    # Fail-closed write-binding (ports) — naked env token denied.
    binding_ok = False
    try:
        tasks_binding.require_write_binding(
            "env-mcp-token", project=PROJECT, task_id="ARCH-MS-89")
    except tasks_binding.WriteBindingError as exc:
        binding_ok = exc.payload.get("failure_class") == "unbound_identity"
    cases.append({
        "name": "write_binding_fail_closed",
        "status": "denied" if binding_ok else "accepted",
        "expect_family": "unbound_identity",
        "ok": binding_ok,
    })

    shutil.rmtree(tmp, ignore_errors=True)
    failed = [c for c in cases if not c["ok"]]
    never_403 = all(
        (not isinstance(c.get("status"), int)) or int(c["status"]) != 403
        for c in cases
    )
    return {
        "ok": not failed and never_403,
        "cases": cases,
        "failed": failed,
        "never_403": never_403,
        "contract": {
            "unauthenticated_write": "401 (never 403)",
            "authenticated_present": "must not be 403 (reserved for Access denies)",
            "write_binding": "unbound env-token → WriteBindingError / unbound_identity",
        },
        "verdict": "pass_day_one_tasks_parity_contract" if not failed and never_403
        else "parity_contract_failed",
    }


def run_caddy_drill_artifacts() -> Dict[str, Any]:
    tasks_fragment = ROOT / "deploy" / "skeleton" / "Caddyfile.tasks-fragment.example"
    runbook = ROOT / "docs" / "runbooks" / "tasks-caddy-cutover-rollback.md"
    live_caddy = ROOT / "deploy" / "Caddyfile"
    unit_example = ROOT / "deploy" / "tasks" / "switchboard-tasks.service.example"
    live_text = live_caddy.read_text(encoding="utf-8") if live_caddy.is_file() else ""
    # Live Tasks cut must NOT be present yet (yellow light until Go + ARCH-MS-92).
    live_has_tasks_cut = (
        "handle /api/tasks" in live_text and "8122" in live_text
        and "127.0.0.1:8122" in live_text
    )
    checklist = [
        "Do not apply live Caddy Tasks cut until independence G6 + ARCH-MS-90/91 parity",
        "Fragment example under deploy/skeleton/Caddyfile.tasks-fragment.example",
        "Unit example under deploy/tasks/switchboard-tasks.service.example (:8122)",
        "Rollback drill documented in docs/runbooks/tasks-caddy-cutover-rollback.md",
        "Carve monolith siblings …/dispatch …/chat …/review_* (Auth me* analogue)",
    ]
    ok = (
        tasks_fragment.is_file()
        and runbook.is_file()
        and unit_example.is_file()
        and live_caddy.is_file()
        and not live_has_tasks_cut  # fail if someone prematurely cut live traffic
    )
    return {
        "ok": ok,
        "tasks_fragment": str(tasks_fragment.relative_to(ROOT)),
        "runbook": str(runbook.relative_to(ROOT)),
        "unit_example": str(unit_example.relative_to(ROOT)),
        "live_caddy_has_tasks_cut": live_has_tasks_cut,
        "checklist": checklist,
        "verdict": (
            "pass_drill_artifacts_without_premature_live_cut"
            if ok else
            "missing_artifacts_or_premature_live_cut"
        ),
    }


def build_report() -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "schema": "switchboard.arch_ms89_tasks_ops_proof.v1",
        "task": "ARCH-MS-89",
        "ok": False,
        "checks": {},
        "details": {},
        "go_no_go": {},
    }
    contention = run_sqlite_contention()
    _ok(report, "sqlite_contention", contention["ok"], **contention)

    budget = run_second_uvicorn_budget()
    _ok(report, "second_uvicorn_budget", budget["ok"], **budget)

    parity = run_status_parity()
    _ok(report, "status_parity_day_one", parity["ok"], **parity)

    caddy = run_caddy_drill_artifacts()
    _ok(report, "caddy_drill_artifacts", caddy["ok"], **caddy)

    # Ports + fail-closed binding already gated by ARCH-MS-87/88; re-check package files.
    ports = ROOT / "src" / "switchboard" / "services" / "tasks" / "ports.py"
    binding = ROOT / "src" / "switchboard" / "services" / "tasks" / "binding.py"
    gate = ROOT / "docs" / "TASKS-INDEPENDENCE-GATE.md"
    g4_ok = ports.is_file() and binding.is_file() and gate.is_file()
    _ok(report, "ports_and_gate_docs_present", g4_ok,
        ports=str(ports.relative_to(ROOT)),
        binding=str(binding.relative_to(ROOT)),
        gate=str(gate.relative_to(ROOT)))

    report["ok"] = all(report["checks"].values())
    if not contention["ok"]:
        recommendation = "No-Go"
        rationale = (
            "Multi-process project SQLite writers hit lock/other errors under short load. "
            "Path B (keep Tasks in-process) is the safe Phase 3 exit."
        )
        verdict = "nogo"
    elif not budget["ok"]:
        recommendation = "No-Go"
        rationale = "Second uvicorn budget measurement failed or exceeded soft RSS budget."
        verdict = "nogo"
    elif report["ok"]:
        recommendation = "Conditional Go"
        rationale = (
            "Hermetic harnesses passed (contention, RSS budget, day-one parity, Caddy drill "
            "artifacts without premature live cut). Operator must still confirm production "
            "soak and explicit G6 before ARCH-MS-90."
        )
        verdict = "go"
    else:
        recommendation = "No-Go"
        rationale = "One or more ops-proof checks failed. Path B No-Go is valid."
        verdict = "nogo"
    report["go_no_go"] = {
        "recommendation": recommendation,
        "verdict": verdict,
        "rationale": rationale,
        "g5_ops_proof": "measured" if report["ok"] else "failed",
        "operator_g6_required": True,
        "path_b_nogo_valid": True,
    }
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        mp.set_start_method("spawn")
    except RuntimeError:
        pass
    report = build_report()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print("ARCH-MS-89 Tasks cut ops proof")
        for name, passed in report["checks"].items():
            print(("  PASS  " if passed else "  FAIL  ") + name)
        gng = report["go_no_go"]
        print(
            f"Go/No-Go recommendation: {gng.get('recommendation')} "
            f"(verdict={gng.get('verdict')}) — {gng.get('rationale')}"
        )
        print("OK" if report["ok"] else "FAIL")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
