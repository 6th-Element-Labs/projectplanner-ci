#!/usr/bin/env python3
"""Executable ARCH-MS-109 Deliverables independence decision gate."""
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
VERDICT_PATH = ROOT / "docs" / "deliverables" / "deliverables_independence_verdict.json"
SCHEMA = "switchboard.deliverables_independence_verdict.v1"
DAY_ONE = {
    ("GET", "/api/deliverables"),
    ("GET", "/api/deliverables/{deliverable_id}"),
    ("GET", "/api/mission_status"),
    ("GET", "/api/deliverables/{deliverable_id}/mission_status"),
    ("GET", "/api/deliverables/{deliverable_id}/closure_report"),
    ("GET", "/api/deliverables/{deliverable_id}/dependency_graph"),
    ("GET", "/api/deliverables/breakdown_proposals"),
    ("GET", "/api/deliverables/breakdown_proposals/{proposal_id}"),
}
REQUIRED_GATES = {
    "G1_route_repository_transaction_inventory", "G2_closure_transaction_boundary",
    "G3_auth_project_scope", "G4_revision_drift_binding",
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
                    and call.func.value.id in {"store", "deliverable_closure",
                                               "create_deliverable_command",
                                               "update_deliverable_command"}):
                calls.add(f"{call.func.value.id}.{call.func.attr}")
        rows.append({"method": route[0], "path": route[1], "handler": node.name,
                     "calls": sorted(calls)})
    return rows


def _function(tree: ast.AST, name: str) -> ast.FunctionDef | None:
    return next((node for node in getattr(tree, "body", [])
                 if isinstance(node, ast.FunctionDef) and node.name == name), None)


def _attribute_call(node: ast.AST, name: str) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == name)


def closure_transaction_proof(source: str) -> dict[str, Any]:
    """Structurally prove closure metadata and audit use the same connection scope.

    Repository-wide string matching is deliberately insufficient: the UPDATE and
    activity INSERT must both be ``c.execute`` calls under one ``with ..._conn``
    node in ``_record_deliverable_closure_impl``.  The public entrypoint must also
    return ``_write_through(... _record_deliverable_closure_impl(...))``.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return {"ok": False, "reason": "syntax_error", "message": str(exc)}
    implementation = _function(tree, "_record_deliverable_closure_impl")
    entrypoint = _function(tree, "record_deliverable_closure")
    if implementation is None or entrypoint is None:
        return {"ok": False, "reason": "closure_functions_missing"}

    atomic_scope = False
    for scope in (node for node in ast.walk(implementation) if isinstance(node, ast.With)):
        bound_names = {
            item.optional_vars.id for item in scope.items
            if isinstance(item.optional_vars, ast.Name)
            and _attribute_call(item.context_expr, "_conn")
        }
        if "c" not in bound_names:
            continue
        sql_calls = [
            call for call in ast.walk(scope)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name) and call.func.value.id == "c"
            and call.func.attr == "execute"
        ]
        sql_text = [
            " ".join(str(value.value) for value in ast.walk(call)
                     if isinstance(value, ast.Constant) and isinstance(value.value, str))
            for call in sql_calls
        ]
        metadata_update = any("UPDATE deliverables SET metadata_json" in text
                              for text in sql_text)
        audited_insert = any("INSERT INTO activity" in text
                             and "deliverable.closure_verified" in text
                             for text in sql_text)
        if metadata_update and audited_insert:
            atomic_scope = True
            break

    write_through_bound = False
    for statement in entrypoint.body:
        if not isinstance(statement, ast.Return) or not _attribute_call(statement.value, "_write_through"):
            continue
        write_through_bound = any(
            _attribute_call(node, "_record_deliverable_closure_impl")
            for node in ast.walk(statement.value)
        )
    return {
        "ok": atomic_scope and write_through_bound,
        "atomic_connection_scope": atomic_scope,
        "write_through_entrypoint": write_through_bound,
    }


def writer_transaction_inventory_complete(writers: list[dict[str, Any]]) -> bool:
    """Require every monolith writer to name its concrete boundary and transaction shape."""
    return len(writers) == 20 and all(
        str(row.get("ownership") or "").strip() == "monolith"
        and bool(str(row.get("boundary_ref") or "").strip())
        and bool(str(row.get("transaction") or "").strip())
        for row in writers
    )


def run_sqlite_probe(*, writes: int = 80, reads_per_worker: int = 80,
                     readers: int = 3) -> dict[str, Any]:
    """Prove readers see the closure row and audit stamp from one committed revision."""
    errors: list[str] = []
    mismatches: list[str] = []
    with tempfile.TemporaryDirectory(prefix="arch-ms109-sqlite-") as tmp:
        db = Path(tmp) / "deliverables.db"
        with sqlite3.connect(db, timeout=5) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE deliverables(id TEXT PRIMARY KEY, revision INTEGER, report_revision INTEGER)")
            conn.execute("CREATE TABLE activity(revision INTEGER PRIMARY KEY, kind TEXT)")
            conn.execute("INSERT INTO deliverables VALUES ('d1', 0, 0)")
            conn.execute("INSERT INTO activity VALUES (0, 'deliverable.closure_verified')")
            conn.commit()
        start = threading.Barrier(readers + 1)

        def writer() -> None:
            try:
                conn = sqlite3.connect(db, timeout=5)
                conn.execute("PRAGMA busy_timeout=5000")
                start.wait()
                for revision in range(1, writes + 1):
                    with conn:
                        conn.execute("UPDATE deliverables SET revision=?, report_revision=? WHERE id='d1'",
                                     (revision, revision))
                        conn.execute("INSERT INTO activity VALUES (?, 'deliverable.closure_verified')",
                                     (revision,))
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
                        row = conn.execute("SELECT revision, report_revision FROM deliverables WHERE id='d1'").fetchone()
                        audit = conn.execute("SELECT MAX(revision) FROM activity").fetchone()
                    finally:
                        conn.commit()
                    if row is None or audit is None or row[0] != row[1] or row[0] != audit[0]:
                        mismatches.append(f"reader-{worker}:{row!r}:{audit!r}")
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
            final_revision = int(conn.execute("SELECT revision FROM deliverables").fetchone()[0])
            quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
            journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    lock_errors = sum("locked" in error.lower() or "busy" in error.lower() for error in errors)
    return {
        "schema": "switchboard.deliverables_sqlite_probe.v1",
        "ok": not errors and not mismatches and not stuck and final_revision == writes
              and quick_check == "ok",
        "journal_mode": journal_mode, "writer_transactions": writes,
        "reader_transactions": readers * reads_per_worker, "reader_workers": readers,
        "final_revision": final_revision, "quick_check": quick_check,
        "lock_errors": lock_errors, "snapshot_mismatches": len(mismatches),
        "errors": errors, "stuck_threads": stuck,
    }


def evaluate(root: Path = ROOT, *, run_probe: bool = True) -> dict[str, Any]:
    verdict = load_verdict(root / "docs" / "deliverables" / "deliverables_independence_verdict.json")
    actual = router_inventory(root / "src" / "switchboard" / "api" / "routers" / "deliverables.py")
    actual_by_route = {(row["method"], row["path"]): row for row in actual}
    reads = verdict.get("route_inventory") or []
    writers = verdict.get("writer_inventory") or []
    declared = reads + writers
    declared_by_route = {(row.get("method"), row.get("path")): row for row in declared}
    call_match = all(sorted(row.get("calls") or []) == actual_by_route[key]["calls"]
                     for key, row in declared_by_route.items() if key in actual_by_route)
    repository_source = (root / "src" / "switchboard" / "storage" / "repositories" / "deliverables.py").read_text(encoding="utf-8")
    closure_proof = closure_transaction_proof(repository_source)
    writer_transactions_complete = writer_transaction_inventory_complete(writers)
    auth_scope = verdict.get("auth_project_scope") or {}
    revision = verdict.get("revision_drift_binding") or {}
    budget = verdict.get("resource_budget") or {}
    remaining = int(budget.get("memory_available_bytes") or 0) - int(
        budget.get("projected_deliverables_rss_bytes") or 0)
    headroom = remaining - int(budget.get("minimum_reserve_bytes") or 0)
    gates = verdict.get("gates") or {}
    failed_gates = sorted(name for name in REQUIRED_GATES
                          if not bool((gates.get(name) or {}).get("passed")))
    expected_verdict = "go" if not failed_gates else "nogo"
    probe = run_sqlite_probe() if run_probe else None
    checks = {
        "schema": verdict.get("schema") == SCHEMA,
        "task_id": verdict.get("task_id") == "ARCH-MS-109",
        "router_inventory_complete": set(declared_by_route) == set(actual_by_route),
        "day_one_surface_exact": {(row.get("method"), row.get("path")) for row in reads} == DAY_ONE,
        "repository_calls_exact": call_match,
        "all_day_one_routes_read_only": all(row.get("writes") is False for row in reads),
        "all_writers_remain_monolith": len(writers) == 20 and bool(
            (verdict.get("writer_policy") or {}).get("all_inventory_entries_stay_on_monolith")),
        "writer_transactions_complete": writer_transactions_complete,
        "closure_transaction_atomic": bool(closure_proof.get("ok")) and bool(
            (verdict.get("closure_consistency") or {}).get("unsafe_split_forbidden")),
        "auth_project_scope_bound": auth_scope.get("required_port") == "DeliverablesReadAuthPort"
                                     and auth_scope.get("explicit_project_required") is True,
        "revision_drift_contract": bool(revision.get("mission_cache_stamp"))
                                   and revision.get("closure_identity") == ["report_id", "evidence_hash"],
        "resource_budget_math": remaining == budget.get("projected_remaining_bytes")
                                and headroom == budget.get("headroom_after_projection_bytes")
                                and headroom >= 0,
        "required_gates_present": REQUIRED_GATES.issubset(gates),
        "verdict_matches_gates": verdict.get("verdict") == expected_verdict,
        "build_authorization_matches": bool(verdict.get("process_build_authorized"))
                                       == (expected_verdict == "go"),
        "production_cut_not_preauthorized": verdict.get("production_cutover_authorized") is False,
        "go_only_task_gated": "ARCH-MS-110" in (verdict.get("go_only_tasks") or []),
    }
    if probe is not None:
        checks["sqlite_probe"] = bool(probe.get("ok"))
    return {
        "schema": "switchboard.deliverables_independence_gate_report.v1",
        "ok": all(checks.values()), "verdict": verdict.get("verdict"),
        "process_build_authorized": bool(verdict.get("process_build_authorized")),
        "production_cutover_authorized": bool(verdict.get("production_cutover_authorized")),
        "failed_gates": failed_gates, "checks": checks,
        "closure_transaction_proof": closure_proof, "sqlite_probe": probe,
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
