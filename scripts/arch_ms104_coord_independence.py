#!/usr/bin/env python3
"""Executable ARCH-MS-104 Coordination/board independence gate.

The gate deliberately distinguishes a safe modular-monolith result from permission
to cut traffic to a second process.  A ``go`` verdict is accepted only when every
required gate is true and the artifact explicitly authorizes the process cut.
"""
from __future__ import annotations

import argparse
import ast
import json
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
VERDICT_PATH = ROOT / "docs" / "coord" / "coord_independence_verdict.json"
SCHEMA = "switchboard.coord_independence_verdict.v1"
REQUIRED_ROUTES = {
    ("GET", "/api/board"),
    ("GET", "/api/signals"),
    ("GET", "/ixp/v1/delta"),
    ("GET", "/api/coordination"),
    ("GET", "/api/coordinator_decisions"),
}
REQUIRED_GATES = {
    "G1_ports_independence",
    "G2_route_writer_inventory",
    "G3_auth_project_scope",
    "G4_sqlite_contention",
    "G5_resource_budget",
    "G6_tasks_acceptance",
}
FORBIDDEN_ROOT_IMPORTS = {"auth", "dispatch", "signals", "store"}


def load_verdict(path: Path = VERDICT_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def forbidden_imports(path: Path) -> list[str]:
    """Return direct imports of monolith facades from one Python module."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name.split(".", 1)[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module.split(".", 1)[0]]
        else:
            continue
        found.update(name for name in names if name in FORBIDDEN_ROOT_IMPORTS)
    return sorted(found)


def run_sqlite_probe(*, writes: int = 80, reads_per_worker: int = 80,
                     readers: int = 3) -> dict[str, Any]:
    """Model the day-one read process beside one existing WAL writer.

    Coord owns no day-one writes.  This probe therefore uses one writer (the existing
    monolith/Tasks population) and concurrent read connections (projected Coord load).
    It never touches a repository or production database.
    """
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="arch-ms104-sqlite-") as tmp:
        db = Path(tmp) / "coord.db"
        with sqlite3.connect(db, timeout=5) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE events(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
            conn.commit()
        start = threading.Barrier(readers + 1)

        def writer() -> None:
            try:
                conn = sqlite3.connect(db, timeout=5)
                conn.execute("PRAGMA busy_timeout=5000")
                start.wait()
                for index in range(writes):
                    conn.execute("INSERT INTO events(value) VALUES (?)", (f"v-{index}",))
                    conn.commit()
                conn.close()
            except Exception as exc:  # pragma: no cover - surfaced in report
                errors.append(f"writer:{type(exc).__name__}:{exc}")

        def reader(worker: int) -> None:
            try:
                conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
                conn.execute("PRAGMA busy_timeout=5000")
                start.wait()
                for _ in range(reads_per_worker):
                    conn.execute("SELECT COUNT(*) FROM events").fetchone()
                conn.close()
            except Exception as exc:  # pragma: no cover - surfaced in report
                errors.append(f"reader-{worker}:{type(exc).__name__}:{exc}")

        threads = [threading.Thread(target=reader, args=(idx,)) for idx in range(readers)]
        threads.append(threading.Thread(target=writer))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        stuck = sum(thread.is_alive() for thread in threads)
        with sqlite3.connect(db, timeout=5) as conn:
            row_count = int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
            quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
            journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    lock_errors = sum("locked" in error.lower() or "busy" in error.lower() for error in errors)
    return {
        "schema": "switchboard.coord_sqlite_probe.v1",
        "ok": not errors and not stuck and row_count == writes and quick_check == "ok",
        "journal_mode": journal_mode,
        "writer_transactions": writes,
        "reader_queries": readers * reads_per_worker,
        "reader_workers": readers,
        "row_count": row_count,
        "quick_check": quick_check,
        "lock_errors": lock_errors,
        "errors": errors,
        "stuck_threads": stuck,
    }


def evaluate(root: Path = ROOT, *, run_probe: bool = True) -> dict[str, Any]:
    verdict = load_verdict(root / "docs" / "coord" / "coord_independence_verdict.json")
    routes = {(str(row.get("method")), str(row.get("path")))
              for row in verdict.get("route_inventory") or []}
    gates = verdict.get("gates") or {}
    gate_values = {name: bool((gates.get(name) or {}).get("passed")) for name in REQUIRED_GATES}
    failed_gates = sorted(name for name, passed in gate_values.items() if not passed)
    expected_verdict = "go" if not failed_gates else "nogo"
    expected_authorized = expected_verdict == "go"

    coord_package = root / "src" / "switchboard" / "services" / "coord"
    package_modules = sorted(coord_package.glob("*.py"))
    forbidden = {
        str(path.relative_to(root)): forbidden_imports(path)
        for path in package_modules
        if forbidden_imports(path)
    }
    router_source = (coord_package / "router.py").read_text(encoding="utf-8")
    adapter_source = (
        root / "src" / "switchboard" / "api" / "coord_port_adapters.py"
    ).read_text(encoding="utf-8")
    ports_source = (coord_package / "ports.py").read_text(encoding="utf-8")
    auth_port_bound = (
        "class CoordReadAuthPort(Protocol)" in ports_source
        and "auth_port.authorize(request, project)" in router_source
    )
    query_port_bound = (
        "class CoordQueryPort(Protocol)" in ports_source
        and "class RepositoryCoordQueries" in adapter_source
    )
    repository_adapter_bound = all(name in adapter_source for name in (
        "switchboard.storage.repositories import access",
        "switchboard.storage.repositories import activity",
        "switchboard.storage.repositories import coordination",
        "switchboard.storage.repositories import decisions",
        "switchboard.storage.repositories import tasks",
    ))
    g1_runtime_pass = (
        bool(package_modules) and not forbidden and auth_port_bound
        and query_port_bound and repository_adapter_bound
    )
    coupling = verdict.get("coupling") or {}
    unresolved = coupling.get("unresolved_root_imports") or []
    resolved_paths = coupling.get("resolved_paths") or []
    coupling_evidence_present = not unresolved and bool(resolved_paths) and all(
        (root / str(path)).is_file() for path in resolved_paths
    )
    budget = verdict.get("resource_budget") or {}
    remaining = int(budget.get("memory_available_bytes") or 0) - int(
        budget.get("projected_coord_rss_bytes") or 0)
    budget_math_ok = remaining == int(budget.get("projected_remaining_bytes") or -1)
    budget_pass = remaining >= int(budget.get("minimum_reserve_bytes") or 0)
    sqlite_probe = run_sqlite_probe() if run_probe else None

    checks = {
        "schema": verdict.get("schema") == SCHEMA,
        "task_id": verdict.get("task_id") == "ARCH-MS-104",
        "route_inventory_exact": routes == REQUIRED_ROUTES,
        "route_inventory_uses_thin_router": all(
            row.get("router") == "src/switchboard/services/coord/router.py"
            for row in verdict.get("route_inventory") or []
        ),
        "all_routes_read_only": all(not bool(row.get("writes"))
                                     for row in verdict.get("route_inventory") or []),
        "writer_inventory_empty": verdict.get("writer_inventory") == [],
        "required_gates_present": REQUIRED_GATES.issubset(gates),
        "coord_import_ceiling": not forbidden,
        "coord_auth_port_bound": auth_port_bound,
        "coord_query_port_bound": query_port_bound,
        "repository_adapter_bound": repository_adapter_bound,
        "g1_matches_runtime": bool((gates.get("G1_ports_independence") or {}).get("passed")) == g1_runtime_pass,
        "coupling_evidence_present": coupling_evidence_present,
        "resource_budget_math": budget_math_ok and budget_pass,
        "tasks_acceptance_green": bool((verdict.get("tasks_production_acceptance") or {}).get("green")),
        "verdict_matches_gates": verdict.get("verdict") == expected_verdict,
        "authorization_matches_gates": bool(verdict.get("process_cut_authorized")) == expected_authorized,
        "go_only_task_gated": "ARCH-MS-105" in (verdict.get("go_only_tasks") or []),
    }
    if sqlite_probe is not None:
        checks["sqlite_probe"] = bool(sqlite_probe.get("ok"))
    return {
        "schema": "switchboard.coord_independence_gate_report.v1",
        "ok": all(checks.values()),
        "verdict": verdict.get("verdict"),
        "process_cut_authorized": bool(verdict.get("process_cut_authorized")),
        "failed_gates": failed_gates,
        "forbidden_imports": forbidden,
        "checks": checks,
        "sqlite_probe": sqlite_probe,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-sqlite-probe", action="store_true")
    args = parser.parse_args()
    report = evaluate(run_probe=not args.no_sqlite_probe)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
