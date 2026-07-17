#!/usr/bin/env python3
"""Executable ARCH-MS-114 Tally independence Go/No-Go gate."""
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
VERDICT_PATH = ROOT / "docs" / "tally" / "tally_independence_verdict.json"
SCHEMA = "switchboard.tally_independence_verdict.v1"
DAY_ONE = {
    ("GET", "/tally/v1/kpis"),
    ("GET", "/tally/v1/outcomes"),
    ("GET", "/tally/v1/task/{task_id}"),
    ("GET", "/tally/v1/kpi/{kpi_id}"),
    ("GET", "/tally/v1/project"),
    ("GET", "/tally/v1/deliverable/{deliverable_id}"),
}
REQUIRED_GATES = {
    "G1_route_repository_writer_inventory", "G2_attribution_integrity",
    "G3_project_scope", "G4_writer_transaction_boundaries",
    "G5_sqlite_contention", "G6_resource_budget",
}


def load_verdict(path: Path = VERDICT_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def router_inventory(path: Path) -> list[dict[str, Any]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rows: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        route: tuple[str, str] | None = None
        for decorator in node.decorator_list:
            if (isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and isinstance(decorator.func.value, ast.Name)
                    and decorator.func.value.id == "router"
                    and decorator.args and isinstance(decorator.args[0], ast.Constant)):
                route = (decorator.func.attr.upper(), str(decorator.args[0].value))
        if route is None:
            continue
        calls: set[str] = set()
        for call in ast.walk(node):
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id in {"store", "auth"}):
                calls.add(f"{call.func.value.id}.{call.func.attr}")
        rows.append({"method": route[0], "path": route[1], "handler": node.name,
                     "calls": sorted(calls)})
    return rows


def project_scope_proof(path: Path) -> dict[str, Any]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    reads = writes = read_bound = write_bound = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        methods: list[str] = []
        for decorator in node.decorator_list:
            if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                    and isinstance(decorator.func.value, ast.Name)
                    and decorator.func.value.id == "router"):
                methods.append(decorator.func.attr.upper())
        if not methods:
            continue
        called = {
            call.func.id for call in ast.walk(node)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        }
        if methods[0] == "GET":
            reads += 1
            # Defaults are stored separately from args; the resolver call is the
            # durable structural requirement and Query(...) is enforced by FastAPI.
            if "resolve_project" in called:
                read_bound += 1
        else:
            writes += 1
            if "resolve_body_project" in called:
                write_bound += 1
    return {"ok": reads == read_bound == 6 and writes == write_bound == 7,
            "reads": reads, "read_bound": read_bound,
            "writes": writes, "write_bound": write_bound}


def _function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    node = next((item for item in tree.body
                 if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and item.name == name), None)
    return ast.unparse(node) if node is not None else ""


def attribution_safety_proof(source: str) -> dict[str, Any]:
    usage = _function_source(source, "report_usage")
    outcome = _function_source(source, "record_outcome")
    conflict_rejected = "conflicting_usage_attribution" in usage
    outcome_parent_validated = "outcome_parent_not_found" in outcome
    return {
        "safe": conflict_rejected and outcome_parent_validated,
        "conflicting_usage_attribution_rejected": conflict_rejected,
        "outcome_parent_validated": outcome_parent_validated,
        "findings": [code for code, passed in (
            ("conflicting_usage_attribution_not_rejected", conflict_rejected),
            ("outcome_parent_not_validated", outcome_parent_validated),
        ) if not passed],
    }


def writer_transaction_inventory_complete(writers: list[dict[str, Any]]) -> bool:
    return len(writers) == 7 and all(
        row.get("ownership") == "monolith"
        and bool(str(row.get("boundary_ref") or "").strip())
        and bool(str(row.get("transaction") or "").strip())
        for row in writers
    )


def run_sqlite_probe(*, writes: int = 80, reads_per_worker: int = 80,
                     readers: int = 3) -> dict[str, Any]:
    errors: list[str] = []
    mismatches: list[str] = []
    with tempfile.TemporaryDirectory(prefix="arch-ms114-tally-") as tmp:
        db = Path(tmp) / "tally.db"
        with sqlite3.connect(db, timeout=5) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE tasks(id TEXT PRIMARY KEY, project TEXT NOT NULL);
                CREATE TABLE kpis(id TEXT PRIMARY KEY, project TEXT NOT NULL);
                CREATE TABLE outcomes(id TEXT PRIMARY KEY, project TEXT NOT NULL, task_id TEXT NOT NULL, revision INTEGER NOT NULL);
                CREATE TABLE spend(id INTEGER PRIMARY KEY, project TEXT NOT NULL, task_id TEXT NOT NULL, outcome_id TEXT NOT NULL, revision INTEGER NOT NULL);
                CREATE TABLE links(id TEXT PRIMARY KEY, project TEXT NOT NULL, outcome_id TEXT NOT NULL, kpi_id TEXT NOT NULL, revision INTEGER NOT NULL);
                INSERT INTO tasks VALUES ('t1', 'switchboard');
                INSERT INTO kpis VALUES ('k1', 'switchboard');
            """)
            conn.commit()
        start = threading.Barrier(readers + 1)

        def writer() -> None:
            try:
                conn = sqlite3.connect(db, timeout=5)
                conn.execute("PRAGMA busy_timeout=5000")
                start.wait()
                for revision in range(1, writes + 1):
                    oid = f"o{revision}"
                    with conn:
                        conn.execute("INSERT INTO outcomes VALUES (?, 'switchboard', 't1', ?)", (oid, revision))
                        conn.execute("INSERT INTO spend(project,task_id,outcome_id,revision) VALUES ('switchboard','t1',?,?)", (oid, revision))
                        conn.execute("INSERT INTO links VALUES (?, 'switchboard', ?, 'k1', ?)", (f"l{revision}", oid, revision))
                conn.close()
            except Exception as exc:  # pragma: no cover
                errors.append(f"writer:{type(exc).__name__}:{exc}")

        def reader(worker: int) -> None:
            try:
                conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
                conn.execute("PRAGMA busy_timeout=5000")
                start.wait()
                for _ in range(reads_per_worker):
                    conn.execute("BEGIN")
                    try:
                        bad_spend = conn.execute("""
                            SELECT COUNT(*) FROM spend s LEFT JOIN outcomes o ON o.id=s.outcome_id
                            WHERE o.id IS NULL OR s.project!=o.project OR s.task_id!=o.task_id OR s.revision!=o.revision
                        """).fetchone()[0]
                        bad_links = conn.execute("""
                            SELECT COUNT(*) FROM links l LEFT JOIN outcomes o ON o.id=l.outcome_id
                            LEFT JOIN kpis k ON k.id=l.kpi_id
                            WHERE o.id IS NULL OR k.id IS NULL OR l.project!=o.project OR l.project!=k.project OR l.revision!=o.revision
                        """).fetchone()[0]
                    finally:
                        conn.commit()
                    if bad_spend or bad_links:
                        mismatches.append(f"reader-{worker}:{bad_spend}:{bad_links}")
                conn.close()
            except Exception as exc:  # pragma: no cover
                errors.append(f"reader-{worker}:{type(exc).__name__}:{exc}")

        threads = [threading.Thread(target=reader, args=(index,)) for index in range(readers)]
        threads.append(threading.Thread(target=writer))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        stuck = sum(thread.is_alive() for thread in threads)
        with sqlite3.connect(db, timeout=5) as conn:
            final_revision = int(conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0])
            quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
            journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    lock_errors = sum("locked" in error.lower() or "busy" in error.lower() for error in errors)
    return {
        "schema": "switchboard.tally_sqlite_probe.v1",
        "ok": not errors and not mismatches and not stuck and final_revision == writes
              and quick_check == "ok",
        "journal_mode": journal_mode, "writer_transactions": writes,
        "reader_transactions": readers * reads_per_worker, "reader_workers": readers,
        "final_revision": final_revision, "quick_check": quick_check,
        "lock_errors": lock_errors, "snapshot_mismatches": len(mismatches),
        "errors": errors, "stuck_threads": stuck,
    }


def go_only_task_authorized(verdict: dict[str, Any], task_id: str) -> bool:
    return (task_id in (verdict.get("go_only_tasks") or [])
            and verdict.get("verdict") == "go"
            and verdict.get("process_build_authorized") is True)


def evaluate(root: Path = ROOT, *, run_probe: bool = True) -> dict[str, Any]:
    verdict = load_verdict(root / "docs" / "tally" / "tally_independence_verdict.json")
    router_path = root / "src" / "switchboard" / "api" / "routers" / "tally.py"
    repository_path = root / "src" / "switchboard" / "storage" / "repositories" / "kpis_economics.py"
    actual = router_inventory(router_path)
    actual_by_route = {(row["method"], row["path"]): row for row in actual}
    reads = verdict.get("route_inventory") or []
    writers = verdict.get("writer_inventory") or []
    declared = reads + writers
    declared_by_route = {(row.get("method"), row.get("path")): row for row in declared}
    call_match = all(sorted(row.get("calls") or []) == actual_by_route[key]["calls"]
                     for key, row in declared_by_route.items() if key in actual_by_route)
    scope = project_scope_proof(router_path)
    attribution = attribution_safety_proof(repository_path.read_text(encoding="utf-8"))
    budget = verdict.get("resource_budget") or {}
    remaining = int(budget.get("memory_available_bytes") or 0) - int(
        budget.get("projected_tally_rss_bytes") or 0)
    headroom = remaining - int(budget.get("minimum_reserve_bytes") or 0)
    gates = verdict.get("gates") or {}
    failed_gates = sorted(name for name in REQUIRED_GATES
                          if not bool((gates.get(name) or {}).get("passed")))
    expected_verdict = "go" if not failed_gates else "nogo"
    probe = run_sqlite_probe() if run_probe else None
    checks = {
        "schema": verdict.get("schema") == SCHEMA,
        "task_id": verdict.get("task_id") == "ARCH-MS-114",
        "router_inventory_complete": set(declared_by_route) == set(actual_by_route),
        "day_one_surface_exact": {(row.get("method"), row.get("path")) for row in reads} == DAY_ONE,
        "repository_calls_exact": call_match,
        "all_day_one_routes_read_only": all(row.get("writes") is False for row in reads),
        "writer_transactions_complete": writer_transaction_inventory_complete(writers),
        "all_writers_remain_monolith": bool((verdict.get("writer_policy") or {}).get("all_inventory_entries_stay_on_monolith")),
        "project_scope_structural": bool(scope.get("ok")),
        "attribution_gate_matches_source": bool((gates.get("G2_attribution_integrity") or {}).get("passed")) == bool(attribution.get("safe")),
        "attribution_findings_visible": set(attribution.get("findings") or []) == {
            row.get("code") for row in (verdict.get("attribution_contract") or {}).get("blocking_findings") or []},
        "resource_budget_math": remaining == budget.get("projected_remaining_bytes")
                                and headroom == budget.get("headroom_after_projection_bytes"),
        "resource_gate_matches_math": bool((gates.get("G6_resource_budget") or {}).get("passed")) == (headroom >= 0),
        "required_gates_present": REQUIRED_GATES.issubset(gates),
        "verdict_matches_gates": verdict.get("verdict") == expected_verdict,
        "build_authorization_matches": bool(verdict.get("process_build_authorized")) == (expected_verdict == "go"),
        "production_cut_not_preauthorized": verdict.get("production_cutover_authorized") is False,
        "go_only_task_fail_closed": "ARCH-MS-115" in (verdict.get("go_only_tasks") or [])
                                    and not go_only_task_authorized(verdict, "ARCH-MS-115"),
        "nogo_exit_visible": bool((verdict.get("nogo_exit") or {}).get("keep_in_process"))
                             and (verdict.get("nogo_exit") or {}).get("ship_uvicorn") is False,
    }
    if probe is not None:
        checks["sqlite_probe"] = bool(probe.get("ok"))
    return {
        "schema": "switchboard.tally_independence_gate_report.v1",
        "ok": all(checks.values()), "verdict": verdict.get("verdict"),
        "process_build_authorized": bool(verdict.get("process_build_authorized")),
        "production_cutover_authorized": bool(verdict.get("production_cutover_authorized")),
        "failed_gates": failed_gates, "checks": checks,
        "project_scope_proof": scope, "attribution_safety_proof": attribution,
        "resource_headroom_bytes": headroom, "sqlite_probe": probe,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-sqlite-probe", action="store_true")
    parser.add_argument("--require-go-task", default="")
    args = parser.parse_args()
    report = evaluate(run_probe=not args.no_sqlite_probe)
    if args.require_go_task:
        verdict = load_verdict()
        report["requested_go_only_task"] = args.require_go_task
        report["requested_go_only_task_authorized"] = go_only_task_authorized(
            verdict, args.require_go_task)
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["ok"]:
        return 1
    if args.require_go_task and not report["requested_go_only_task_authorized"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
