#!/usr/bin/env python3
"""Executable ARCH-MS-119 Ingest independence Go/No-Go gate."""
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
VERDICT_PATH = ROOT / "docs" / "ingest" / "ingest_independence_verdict.json"
SCHEMA = "switchboard.ingest_independence_verdict.v1"
DAY_ONE = {("GET", "/api/inbox"), ("POST", "/api/intake")}
REQUIRED_GATES = {
    "G1_route_writer_inventory", "G2_idempotency_and_retry",
    "G3_project_routing_isolation", "G4_auth_boundary",
    "G5_sqlite_contention", "G6_resource_budget",
}


def load_verdict(path: Path = VERDICT_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    node = next((item for item in tree.body
                 if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and item.name == name), None)
    return ast.unparse(node) if node is not None else ""


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
        # Delegated functions passed to ``asyncio.to_thread`` are Attribute
        # references, not Call nodes. Inventory both direct and delegated calls.
        for ref in ast.walk(node):
            if isinstance(ref, ast.Attribute) and isinstance(ref.value, ast.Name):
                owner = ref.value
                if owner.id in {
                        "store", "inbox_mod", "intake", "attachments", "transcribe"}:
                    calls.add(f"{owner.id}.{ref.attr}")
        rows.append({"method": route[0], "path": route[1], "handler": node.name,
                     "calls": sorted(calls)})
    return rows


def project_scope_proof(router_path: Path) -> dict[str, Any]:
    tree = ast.parse(router_path.read_text(encoding="utf-8"))
    routes = bound = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        is_route = any(isinstance(dec, ast.Call)
                       and isinstance(dec.func, ast.Attribute)
                       and isinstance(dec.func.value, ast.Name)
                       and dec.func.value.id == "router" for dec in node.decorator_list)
        if not is_route:
            continue
        routes += 1
        called = {call.func.id for call in ast.walk(node)
                  if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)}
        if "resolve_project" in called:
            bound += 1
    return {"ok": routes == bound == 8, "routes": routes, "project_bound": bound}


def failure_semantics_proof(inbox_source: str, store_source: str,
                            router_source: str,
                            migration_source: str = "") -> dict[str, Any]:
    process = _function_source(inbox_source, "process")
    queue = _function_source(router_source, "_queue_triage")
    schema_contract = store_source + "\n" + migration_source
    schema_has_unique_dedupe = ("UNIQUE(source, external_id)" in schema_contract
                                or "UNIQUE (source, external_id)" in schema_contract
                                or "ux_inbox" in schema_contract)
    adapter = (ROOT / "src/switchboard/api/ingest_port_adapters.py").read_text(encoding="utf-8")
    standalone = (ROOT / "src/switchboard/services/ingest/router.py").read_text(encoding="utf-8")
    ledger_present = "ingest_operations" in store_source and "BEGIN IMMEDIATE" in adapter
    queue_failure_visible = "except Exception as exc" in adapter and "status='failed'" in adapter
    findings = {
        "dedupe_check_then_insert_without_unique_constraint":
            "inbox_exists" in process and "add_inbox_item" in process
            and not schema_has_unique_dedupe,
        "intake_multi_effect_has_no_retry_ledger": not ledger_present and
            "rag.add_document" in (ROOT / "intake.py").read_text(encoding="utf-8")
            and "agent.triage" in (ROOT / "intake.py").read_text(encoding="utf-8"),
        "triage_queue_write_failure_swallowed": not queue_failure_visible and
            "except Exception" in queue and "pass" in queue,
        "standalone_idempotency_key_optional": "Header(..., alias=\"Idempotency-Key\")" not in standalone,
    }
    return {"safe": not any(findings.values()),
            "findings": sorted(code for code, present in findings.items() if present)}


def run_sqlite_probe(*, writes: int = 80, readers: int = 3) -> dict[str, Any]:
    """Prove routed DB isolation and ordinary WAL read/write safety.

    This deliberately does not bless inbox idempotency; that is a schema/source gate.
    """
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="arch-ms119-ingest-") as tmp:
        dbs = {name: Path(tmp) / f"{name}.db" for name in ("alpha", "beta")}
        for db in dbs.values():
            with sqlite3.connect(db) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("CREATE TABLE inbox(id INTEGER PRIMARY KEY, external_id TEXT, status TEXT)")
        barrier = threading.Barrier(readers + 1)

        def writer() -> None:
            try:
                conn = sqlite3.connect(dbs["alpha"], timeout=5)
                conn.execute("PRAGMA busy_timeout=5000")
                barrier.wait()
                for index in range(writes):
                    with conn:
                        conn.execute("INSERT INTO inbox(external_id,status) VALUES (?, 'pending')",
                                     (f"message-{index}",))
                conn.close()
            except Exception as exc:  # pragma: no cover
                errors.append(f"writer:{type(exc).__name__}:{exc}")

        def reader() -> None:
            try:
                conn = sqlite3.connect(f"file:{dbs['alpha']}?mode=ro", uri=True, timeout=5)
                barrier.wait()
                for _ in range(writes):
                    conn.execute("SELECT COUNT(*) FROM inbox").fetchone()
                conn.close()
            except Exception as exc:  # pragma: no cover
                errors.append(f"reader:{type(exc).__name__}:{exc}")

        threads = [threading.Thread(target=reader) for _ in range(readers)]
        threads.append(threading.Thread(target=writer))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        stuck = sum(thread.is_alive() for thread in threads)
        counts = {}
        checks = {}
        for project, db in dbs.items():
            with sqlite3.connect(db) as conn:
                counts[project] = conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]
                checks[project] = conn.execute("PRAGMA quick_check").fetchone()[0]
    lock_errors = sum("locked" in error.lower() for error in errors)
    return {"schema": "switchboard.ingest_sqlite_probe.v1",
            "ok": not errors and not stuck and counts == {"alpha": writes, "beta": 0}
                  and set(checks.values()) == {"ok"},
            "project_counts": counts, "quick_check": checks,
            "writer_transactions": writes, "reader_transactions": readers * writes,
            "lock_errors": lock_errors, "errors": errors, "stuck_threads": stuck}


def go_only_task_authorized(verdict: dict[str, Any], task_id: str) -> bool:
    return (task_id in (verdict.get("go_only_tasks") or [])
            and verdict.get("verdict") == "go"
            and verdict.get("process_build_authorized") is True)


def evaluate(root: Path = ROOT, *, run_probe: bool = True) -> dict[str, Any]:
    verdict = load_verdict(root / "docs" / "ingest" / "ingest_independence_verdict.json")
    router_path = root / "src" / "switchboard" / "api" / "routers" / "intake_inbox.py"
    actual = router_inventory(router_path)
    actual_by_route = {(row["method"], row["path"]): row for row in actual}
    declared = verdict.get("route_inventory") or []
    declared_by_route = {(row.get("method"), row.get("path")): row for row in declared}
    calls_match = all(sorted(row.get("calls") or []) == actual_by_route[key]["calls"]
                      for key, row in declared_by_route.items() if key in actual_by_route)
    scope = project_scope_proof(router_path)
    failure = failure_semantics_proof(
        (root / "inbox.py").read_text(encoding="utf-8"),
        (root / "db" / "schema.py").read_text(encoding="utf-8"),
        router_path.read_text(encoding="utf-8"),
        (root / "src" / "switchboard" / "storage" / "migrations" / "runner.py")
        .read_text(encoding="utf-8"))
    gates = verdict.get("gates") or {}
    failed_gates = sorted(name for name in REQUIRED_GATES
                          if not bool((gates.get(name) or {}).get("passed")))
    expected_verdict = "go" if not failed_gates else "nogo"
    budget = verdict.get("resource_budget") or {}
    remaining = int(budget.get("memory_available_bytes") or 0) - int(
        budget.get("projected_ingest_rss_bytes") or 0)
    headroom = remaining - int(budget.get("minimum_reserve_bytes") or 0)
    expected_findings = {row.get("code") for row in
                         (verdict.get("failure_semantics") or {}).get("blocking_findings") or []}
    checks = {
        "schema": verdict.get("schema") == SCHEMA,
        "task_id": verdict.get("task_id") == "ARCH-MS-119",
        "router_inventory_complete": set(declared_by_route) == set(actual_by_route),
        "day_one_surface_exact": {(row.get("method"), row.get("path")) for row in declared
                                  if row.get("day_one")} == DAY_ONE,
        "repository_calls_exact": calls_match,
        "writer_inventory_complete": len(verdict.get("writer_inventory") or []) == 7,
        "project_scope_structural": bool(scope.get("ok")),
        "project_storage_isolated": (verdict.get("project_scope") or {}).get("storage_isolation")
                                    == "one SQLite database per Switchboard project",
        "failure_gate_matches_source": bool((gates.get("G2_idempotency_and_retry") or {}).get("passed"))
                                       == bool(failure.get("safe")),
        "failure_findings_visible": set(failure.get("findings") or []) == expected_findings,
        "auth_boundary_visible": bool((verdict.get("auth_contract") or {}).get("standalone_port_present"))
                                 == bool((gates.get("G4_auth_boundary") or {}).get("passed")),
        "resource_budget_math": remaining == budget.get("projected_remaining_bytes")
                                and headroom == budget.get("headroom_after_projection_bytes"),
        "resource_gate_matches_math": bool((gates.get("G6_resource_budget") or {}).get("passed"))
                                      == (headroom >= 0),
        "required_gates_present": REQUIRED_GATES.issubset(gates),
        "verdict_matches_gates": verdict.get("verdict") == expected_verdict,
        "build_authorization_matches": bool(verdict.get("process_build_authorized"))
                                       == (expected_verdict == "go"),
        "production_cut_not_preauthorized": verdict.get("production_cutover_authorized") is False,
        "go_only_task_authorization_matches": "ARCH-MS-120" in (verdict.get("go_only_tasks") or [])
                                    and go_only_task_authorized(verdict, "ARCH-MS-120")
                                    == (expected_verdict == "go"),
        "exit_matches_verdict": (
            expected_verdict == "go"
            and (verdict.get("go_exit") or {}).get("ship_uvicorn") is True
        ) or (
            expected_verdict == "nogo"
            and bool((verdict.get("nogo_exit") or {}).get("keep_in_process"))
            and (verdict.get("nogo_exit") or {}).get("ship_uvicorn") is False
        ),
    }
    probe = run_sqlite_probe() if run_probe else None
    if probe is not None:
        checks["sqlite_probe"] = bool(probe.get("ok"))
    return {"schema": "switchboard.ingest_independence_gate_report.v1",
            "ok": all(checks.values()), "verdict": verdict.get("verdict"),
            "process_build_authorized": bool(verdict.get("process_build_authorized")),
            "production_cutover_authorized": bool(verdict.get("production_cutover_authorized")),
            "failed_gates": failed_gates, "checks": checks,
            "project_scope_proof": scope, "failure_semantics_proof": failure,
            "resource_headroom_bytes": headroom, "sqlite_probe": probe}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-sqlite-probe", action="store_true")
    parser.add_argument("--require-go-task", default="")
    args = parser.parse_args()
    report = evaluate(run_probe=not args.no_sqlite_probe)
    if args.require_go_task:
        report["requested_go_only_task"] = args.require_go_task
        report["requested_go_only_task_authorized"] = go_only_task_authorized(
            load_verdict(), args.require_go_task)
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["ok"]:
        return 1
    if args.require_go_task and not report["requested_go_only_task_authorized"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
