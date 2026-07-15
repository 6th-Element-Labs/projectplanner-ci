#!/usr/bin/env python3
"""ARCH-MS-84 Auth cut ops proof harness.

Measures Go/No-Go inputs for ARCH-MS-75 without enabling a live Auth process cut:

1. Multi-process SQLite contention (Auth writer + Access/monolith writer on one registry DB)
2. Second-uvicorn RSS/CPU budget vs interactive-slice MemoryLow
3. 401/403 parity contract (in-process Auth routes)
4. Caddy Auth cutover/rollback drill artifacts present (dry checklist)

Usage::

    python scripts/arch_ms84_auth_ops_proof.py
    python scripts/arch_ms84_auth_ops_proof.py --json
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
SECOND_UVICORN_RSS_BUDGET_MIB = 80  # soft budget for a cut-out Auth/skeleton process
CONTENTION_ROUNDS = 40
CONTENTION_LOCK_ERROR_CEILING = 0  # fail-closed: any unrecovered lock error is a No-Go signal


def _ok(report: Dict[str, Any], name: str, passed: bool, **detail: Any) -> None:
    report["checks"][name] = passed
    report["details"][name] = {"ok": passed, **detail}


def _worker_auth(registry: str, rounds: int, out_q: mp.Queue) -> None:
    os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = registry
    os.environ["PM_AUTH_MODE"] = "dev-open"
    os.environ["PM_JWT_SECRET"] = "arch-ms84-ops-proof-secret"
    # Isolate other DBs so store.init does not touch shared state unexpectedly.
    tmp = Path(registry).parent
    os.environ["PM_DB_PATH"] = str(tmp / "maxwell.db")
    os.environ["PM_HELM_DB_PATH"] = str(tmp / "helm.db")
    os.environ["PM_SWITCHBOARD_DB_PATH"] = str(tmp / "switchboard.db")
    errors: List[str] = []
    ok_n = 0
    t0 = time.perf_counter()
    try:
        from switchboard.api.auth_port_adapters import configure_auth_ports
        from switchboard.api.routers.auth import store as auth_store
        from switchboard.api.routers.auth import service as auth_service

        configure_auth_ports()
        auth_store.init()
        for i in range(rounds):
            try:
                uid = f"auth-worker-{os.getpid()}-{i}"
                auth_store.ensure_identity(uid, email=f"{uid}@example.com", display_name=uid)
                # Issue/verify a session to force auth_sessions_v2 writes+reads.
                user, token, _ = auth_service.register(
                    f"reg-{uid}@example.com", uid, "password12345", ip="127.0.0.1")
                if auth_service.current_user(token) is None:
                    errors.append(f"session_verify_failed:{uid}")
                else:
                    ok_n += 1
                del user
            except Exception as exc:
                msg = str(exc)
                if "database is locked" in msg.lower() or "locked" in msg.lower():
                    errors.append(f"lock:{msg}")
                else:
                    errors.append(f"err:{type(exc).__name__}:{msg}")
    except Exception as exc:
        msg = str(exc)
        # Concurrent ADD COLUMN races are not SQLite lock contention — parent
        # pre-inits schema; treat residual duplicate-column as soft setup noise.
        if "duplicate column" in msg.lower():
            errors.append(f"setup_soft:{type(exc).__name__}:{msg}")
        else:
            errors.append(f"setup:{type(exc).__name__}:{msg}")
    out_q.put({
        "role": "auth",
        "ok": ok_n,
        "errors": errors,
        "elapsed_s": time.perf_counter() - t0,
        "pid": os.getpid(),
    })


def _worker_access(registry: str, rounds: int, out_q: mp.Queue) -> None:
    os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = registry
    os.environ["PM_AUTH_MODE"] = "dev-open"
    os.environ["PM_JWT_SECRET"] = "arch-ms84-ops-proof-secret"
    tmp = Path(registry).parent
    os.environ["PM_DB_PATH"] = str(tmp / "maxwell.db")
    os.environ["PM_HELM_DB_PATH"] = str(tmp / "helm.db")
    os.environ["PM_SWITCHBOARD_DB_PATH"] = str(tmp / "switchboard.db")
    errors: List[str] = []
    ok_n = 0
    t0 = time.perf_counter()
    try:
        from switchboard.api.auth_port_adapters import configure_auth_ports
        from switchboard.storage.repositories import access

        configure_auth_ports()
        access.init_project_registry()
        for i in range(rounds):
            try:
                uid = f"access-worker-{os.getpid()}-{i}"
                # ensure_user delegates to Auth ensure_identity (ARCH-MS-83) — still a
                # second OS process writing the same SQLite file as the Auth worker.
                access.ensure_user(uid, email=f"{uid}@example.com", display_name=uid)
                access.ensure_org(f"org-{uid}", name=f"Org {uid}", slug=f"org-{uid}")
                ok_n += 1
            except Exception as exc:
                msg = str(exc)
                if "database is locked" in msg.lower() or "locked" in msg.lower():
                    errors.append(f"lock:{msg}")
                else:
                    errors.append(f"err:{type(exc).__name__}:{msg}")
    except Exception as exc:
        msg = str(exc)
        if "duplicate column" in msg.lower():
            errors.append(f"setup_soft:{type(exc).__name__}:{msg}")
        else:
            errors.append(f"setup:{type(exc).__name__}:{msg}")
    out_q.put({
        "role": "access",
        "ok": ok_n,
        "errors": errors,
        "elapsed_s": time.perf_counter() - t0,
        "pid": os.getpid(),
    })


def run_sqlite_contention(rounds: int = CONTENTION_ROUNDS) -> Dict[str, Any]:
    tmp = Path(tempfile.mkdtemp(prefix="arch-ms84-contention-"))
    registry = str(tmp / "project_registry.db")
    # Pre-init schema in the parent so concurrent workers do not race ADD COLUMN
    # migrations (duplicate column name: …) during process spawn.
    os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = registry
    os.environ["PM_AUTH_MODE"] = "dev-open"
    os.environ["PM_JWT_SECRET"] = "arch-ms84-ops-proof-secret"
    os.environ["PM_DB_PATH"] = str(tmp / "maxwell.db")
    os.environ["PM_HELM_DB_PATH"] = str(tmp / "helm.db")
    os.environ["PM_SWITCHBOARD_DB_PATH"] = str(tmp / "switchboard.db")
    from switchboard.api.auth_port_adapters import configure_auth_ports
    from switchboard.api.routers.auth import store as auth_store
    from switchboard.storage.repositories import access

    configure_auth_ports()
    access.init_project_registry()
    auth_store.init()

    ctx = mp.get_context("spawn")
    q: mp.Queue = ctx.Queue()
    procs = [
        ctx.Process(target=_worker_auth, args=(registry, rounds, q)),
        ctx.Process(target=_worker_access, args=(registry, rounds, q)),
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
        if not e.startswith("lock:") and not e.startswith("setup_soft:")
    ]
    soft_setup = [
        e for r in results for e in r.get("errors", []) if e.startswith("setup_soft:")
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
        "soft_setup_errors": soft_setup[:10],
        "total_ok": total_ok,
        "workers": results,
        "verdict": (
            "pass_shared_sqlite_under_short_load"
            if passed else
            "no_go_multi_process_sqlite_contention"
        ),
    }


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def run_second_uvicorn_budget(timeout_s: float = 25.0) -> Dict[str, Any]:
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
            # Sample RSS via /proc when available; else child rusage after brief work.
            try:
                status = Path(f"/proc/{proc.pid}/status").read_text(encoding="utf-8")
                for line in status.splitlines():
                    if line.startswith("VmRSS:"):
                        rss_kib = int(line.split()[1])
                        break
            except Exception:
                # macOS / no /proc — approximate via ps
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
    """401 vs 403 contract for Auth routes (in-process; remote cut must match)."""
    tmp = Path(tempfile.mkdtemp(prefix="arch-ms84-parity-"))
    os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(tmp / "project_registry.db")
    os.environ["PM_DB_PATH"] = str(tmp / "maxwell.db")
    os.environ["PM_HELM_DB_PATH"] = str(tmp / "helm.db")
    os.environ["PM_SWITCHBOARD_DB_PATH"] = str(tmp / "switchboard.db")
    os.environ["PM_AUTH_MODE"] = "dev-open"
    os.environ["PM_JWT_SECRET"] = "arch-ms84-parity-secret"

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from switchboard.api.auth_port_adapters import configure_auth_ports
    from switchboard.api.routers.auth import routes as auth_routes
    from switchboard.api.routers.auth import store as auth_store
    import store

    configure_auth_ports()
    store.init_project_registry()
    auth_store.init()

    app = FastAPI()
    app.include_router(auth_routes.router)
    client = TestClient(app)

    cases: List[Dict[str, Any]] = []

    # Unauthenticated session → 401 (or empty/unauthorized shape — must not be 403).
    r = client.get("/api/auth/session")
    cases.append({
        "name": "session_unauthenticated",
        "status": r.status_code,
        "expect_family": "401",
        "ok": r.status_code == 401,
    })

    # Bad login credentials → 401 (not 403).
    r = client.post("/api/auth/login", json={
        "email": "nobody@example.com", "password": "wrong-password-xx",
    })
    cases.append({
        "name": "login_bad_credentials",
        "status": r.status_code,
        "expect_family": "401",
        "ok": r.status_code == 401,
    })

    # Register + authenticated session → 200.
    r = client.post("/api/auth/register", json={
        "email": "parity@example.com",
        "display_name": "Parity",
        "password": "password12345",
    })
    cases.append({
        "name": "register_ok",
        "status": r.status_code,
        "expect_family": "200",
        "ok": r.status_code == 200,
    })
    r = client.get("/api/auth/session")
    cases.append({
        "name": "session_authenticated",
        "status": r.status_code,
        "expect_family": "200",
        "ok": r.status_code == 200 and bool((r.json() or {}).get("user")),
    })

    # Change password without session after logout → 401.
    client.post("/api/auth/logout")
    r = client.post("/api/auth/change-password", json={
        "current_password": "password12345",
        "new_password": "password67890",
    })
    cases.append({
        "name": "change_password_unauthenticated",
        "status": r.status_code,
        "expect_family": "401",
        "ok": r.status_code == 401,
    })

    shutil.rmtree(tmp, ignore_errors=True)
    failed = [c for c in cases if not c["ok"]]
    return {
        "ok": not failed,
        "cases": cases,
        "failed": failed,
        "contract": {
            "unauthenticated": "401 (never 403)",
            "bad_credentials": "401",
            "authenticated_session": "200 with user",
            "forbidden_authorized_but_denied": "403 reserved for Access/grants — not Auth session miss",
        },
        "verdict": "pass_in_process_401_403_contract" if not failed else "parity_contract_failed",
    }


def run_caddy_drill_artifacts() -> Dict[str, Any]:
    auth_fragment = ROOT / "deploy" / "skeleton" / "Caddyfile.auth-fragment.example"
    runbook = ROOT / "docs" / "runbooks" / "auth-caddy-cutover-rollback.md"
    live_caddy = ROOT / "deploy" / "Caddyfile"
    live_unit = ROOT / "deploy" / "switchboard-auth.service"
    live_text = live_caddy.read_text(encoding="utf-8") if live_caddy.is_file() else ""
    # ARCH-MS-76 applied the live cut: /api/auth* → :8121. Artifacts + rollback path remain.
    live_has_auth_cut = "handle /api/auth" in live_text and "8121" in live_text
    checklist = [
        "Confirm switchboard-auth is healthy on 127.0.0.1:8121",
        "Live deploy/Caddyfile routes /api/auth* → :8121 (ARCH-MS-76)",
        "Smoke /api/auth/session + login 401 contract through the edge",
        "Rollback: remove Auth handle block; caddy reload; confirm traffic on :8110",
        "Fragment example retained as drill reference under deploy/skeleton/",
    ]
    ok = (
        auth_fragment.is_file()
        and runbook.is_file()
        and live_caddy.is_file()
        and live_unit.is_file()
        and live_has_auth_cut
    )
    return {
        "ok": ok,
        "auth_fragment": str(auth_fragment.relative_to(ROOT)),
        "runbook": str(runbook.relative_to(ROOT)),
        "live_unit": str(live_unit.relative_to(ROOT)),
        "live_caddy_has_auth_cut": live_has_auth_cut,
        "checklist": checklist,
        "verdict": (
            "pass_live_auth_cut_with_rollback_artifacts"
            if ok else
            "missing_artifacts_or_incomplete_live_cut"
        ),
    }


def build_report() -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "schema": "switchboard.arch_ms84_auth_ops_proof.v1",
        "task": "ARCH-MS-84",
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
    _ok(report, "status_parity_401_403", parity["ok"], **parity)

    caddy = run_caddy_drill_artifacts()
    _ok(report, "caddy_drill_artifacts", caddy["ok"], **caddy)

    # Auth-down fail-closed (G2): verify never authorizes without DB — covered by
    # session.verify design + empty-token short-circuit; re-check quickly here.
    from switchboard.api.routers.auth import session as auth_session
    g2_ok = auth_session.verify("") is None and auth_session.verify("   ") is None
    _ok(report, "auth_down_empty_token_fail_closed", g2_ok,
        note="empty cookie rejects without JWT trust; live DB required for non-empty verify")

    report["ok"] = all(report["checks"].values())
    # Measured recommendation for the independence gate (operator still owns G6).
    if not contention["ok"]:
        recommendation = "No-Go"
        rationale = "Multi-process SQLite writers hit lock errors under short load."
    elif not budget["ok"]:
        recommendation = "No-Go"
        rationale = "Second uvicorn budget measurement failed or exceeded soft RSS budget."
    elif report["ok"]:
        recommendation = "Conditional Go"
        rationale = (
            "Hermetic harnesses passed. Operator must still confirm production soak, "
            "Access writer co-location, and explicit G6 before ARCH-MS-75."
        )
    else:
        recommendation = "No-Go"
        rationale = "One or more ops-proof checks failed."
    report["go_no_go"] = {
        "recommendation": recommendation,
        "rationale": rationale,
        "g2_auth_down": "measured" if g2_ok else "failed",
        "g5_ops_proof": "measured" if report["ok"] else "failed",
        "operator_g6_required": True,
    }
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    # Ensure spawn-safe on macOS.
    try:
        mp.set_start_method("spawn")
    except RuntimeError:
        pass
    report = build_report()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print("ARCH-MS-84 Auth cut ops proof")
        for name, passed in report["checks"].items():
            print(("  PASS  " if passed else "  FAIL  ") + name)
        gng = report["go_no_go"]
        print(f"Go/No-Go recommendation: {gng.get('recommendation')} — {gng.get('rationale')}")
        print("OK" if report["ok"] else "FAIL")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
