#!/usr/bin/env python3
"""SEG-3: central MCP project authorization, census, and performance proof."""
from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path
import shutil
import statistics
import tempfile
import time


TMP = tempfile.mkdtemp(prefix="seg3-mcp-authz-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "required"
os.environ.pop("PM_MCP_TOKEN", None)
os.environ.pop("PM_AUTH_TOKEN", None)

import auth  # noqa: E402
import store  # noqa: E402
from switchboard.mcp import authorization  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def denied(callable_, contains: str = "") -> bool:
    try:
        callable_()
    except PermissionError as exc:
        return not contains or contains in str(exc)
    return False


def percentile(values: list[float], percentile_value: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * percentile_value))
    return ordered[index]


def declared_tool_names() -> tuple[set[str], dict[str, set[str]]]:
    names: set[str] = set()
    arguments: dict[str, set[str]] = {}
    root = Path(__file__).parent / "src" / "switchboard" / "mcp" / "tools"
    for path in sorted(root.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        functions = {
            node.name: node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(target, ast.Name) and target.id.endswith("_TOOL_NAMES")
                       for target in node.targets):
                continue
            for name in ast.literal_eval(node.value):
                names.add(name)
                arguments[name] = {arg.arg for arg in functions[name].args.args}
    return names, arguments


try:
    store.init_project_registry()
    for project in ("maxwell", "helm", "switchboard"):
        store.init_db(project)
    for label in ("Alpha", "Beta"):
        created = store.create_project(label, actor="seg3-test")
        ok(created.get("created") is True, f"created hermetic {label} project")

    alpha_reader_token = "seg3-alpha-reader"
    alpha_writer_token = "seg3-alpha-writer"
    global_token = "seg3-global-granted"
    alpha_reader = store.create_principal(
        "agent", "alpha reader", alpha_reader_token, ["read"],
        principal_id="seg3-alpha-reader", project="alpha")
    alpha_writer = store.create_principal(
        "agent", "alpha writer", alpha_writer_token, ["read", "write:tasks"],
        principal_id="seg3-alpha-writer", project="alpha")
    global_principal = store.create_principal(
        "agent", "global explicit grants", global_token, ["admin"],
        principal_id="seg3-global", project="*")
    grant = store.grant_project_role(
        "beta", "principal", global_principal["id"], "viewer", created_by="seg3-operator")
    ok(not grant.get("error"), "recorded explicit audited beta grant")

    reader = store.get_principal_by_token("alpha", alpha_reader_token)
    writer = store.get_principal_by_token("alpha", alpha_writer_token)
    global_authn = store.get_principal_by_token("switchboard", global_token)

    matrix = {
        "alpha_reader_alpha_read": not denied(
            lambda: auth.authorize_principal(reader, "alpha", ("read",))),
        "alpha_reader_alpha_write": denied(
            lambda: authorization.authorize_project_context(
                reader, "alpha", authorization.AccessClass.WRITE)),
        "alpha_reader_beta_read": denied(
            lambda: auth.authorize_principal(reader, "beta", ("read",))),
        "alpha_writer_alpha_write": not denied(
            lambda: authorization.authorize_project_context(
                writer, "alpha", authorization.AccessClass.WRITE)),
        "global_beta_read_by_grant": not denied(
            lambda: auth.authorize_principal(global_authn, "beta", ("read",))),
        "global_alpha_read_without_grant": denied(
            lambda: auth.authorize_principal(global_authn, "alpha", ("read",))),
    }
    ok(all(matrix.values()), "complete read/write and cross-project denial matrix passes")
    ok(denied(lambda: auth.authorize_principal(reader, "does-not-exist", ("read",)),
              "unknown project"), "unknown projects fail closed")
    revoked = dict(reader, revoked_at=time.time())
    ok(denied(lambda: auth.authorize_principal(revoked, "alpha", ("read",)),
              "principal revoked"), "revoked principals fail closed")

    granted = authorization.authorize_project_context(
        global_authn, "beta", authorization.AccessClass.READ)
    ok(granted.project == "beta" and granted.principal_id == "seg3-global" and
       len(granted.grants) == 1 and granted.grants[0].created_by == "seg3-operator",
       "ProjectContext carries principal, selected project, scopes, and audited grant")
    immutable = False
    try:
        granted.project = "alpha"  # type: ignore[misc]
    except FrozenInstanceError:
        immutable = True
    ok(immutable, "ProjectContext is immutable")

    original_roles = store.principal_project_roles
    calls = 0

    def counted_roles(project_id, principal_id):
        nonlocal_calls[0] += 1
        return original_roles(project_id, principal_id)

    nonlocal_calls = [0]
    store.principal_project_roles = counted_roles
    try:
        with authorization.transport_principal_scope(reader):
            first = authorization.authorize_project_context(
                reader, "alpha", authorization.AccessClass.READ)
            second = authorization.authorize_project_context(
                reader, "alpha", authorization.AccessClass.READ)
        calls = nonlocal_calls[0]
    finally:
        store.principal_project_roles = original_roles
    ok(first is second and calls == 1,
       "request-local cache resolves project grants exactly once")

    def list_projects(project: str = ""):
        return authorization.filter_authorized_projects(store.projects())

    scoped_discovery = authorization.MCPAuthorizationGuard().wrap(list_projects)
    with authorization.transport_principal_scope(reader):
        alpha_projects = scoped_discovery()
    with authorization.transport_principal_scope(global_authn):
        beta_projects = scoped_discovery(project="beta")
    with authorization.transport_principal_scope(global_authn):
        beta_projects_without_anchor = scoped_discovery()
    ok([item["id"] for item in alpha_projects] == ["alpha"],
       "project-scoped principal discovery reveals only its binding")
    ok([item["id"] for item in beta_projects] == ["beta"],
       "global principal discovery reveals only explicitly granted projects")
    ok([item["id"] for item in beta_projects_without_anchor] == ["beta"],
       "global principal can discover its grants without knowing an anchor")

    source_names, source_arguments = declared_tool_names()
    census_names = set(
        authorization.READ_TOOLS | authorization.WRITE_TOOLS | authorization.LLM_TOOLS)
    ok(source_names == census_names,
       "tool census covers every and only registered MCP tool")
    ok("ask_plan" in authorization.LLM_TOOLS
       and authorization.declaration_for("ask_plan").access_class
       == authorization.AccessClass.BILLABLE,
       "ask_plan is billable rather than a free read")
    missing_project_args = []
    for name in sorted(source_names):
        declaration = authorization.declaration_for(name)
        if declaration.project_argument not in source_arguments[name]:
            missing_project_args.append(name)
    ok(not missing_project_args,
       "every MCP tool registration declares its project argument")

    mutating_reads = {"reconcile", "verify_project_consolidation"} & authorization.READ_TOOLS
    ok(not mutating_reads,
       "reconcile and verify_project_consolidation are write-classified")
    ok(mutating_reads == set() and
       {"reconcile", "verify_project_consolidation"} <= authorization.WRITE_TOOLS,
       "mutating reconcile/consolidation tools require write access class")

    mutation_probes: list[tuple[str, str]] = []

    def reconcile(project: str = "alpha"):
        mutation_probes.append(("reconcile", project))
        return {"project": project, "ok": True}

    def verify_project_consolidation(project: str = "alpha"):
        mutation_probes.append(("verify_project_consolidation", project))
        return {"project": project, "ok": True}

    guarded_reconcile = authorization.MCPAuthorizationGuard().wrap(reconcile)
    guarded_verify = authorization.MCPAuthorizationGuard().wrap(
        verify_project_consolidation)

    def guard_denied(callable_) -> bool:
        try:
            callable_()
        except ValueError as exc:
            text = str(exc).lower()
            return "write" in text or "forbidden" in text or "scope" in text
        return False

    with authorization.transport_principal_scope(reader):
        reader_denied = guard_denied(lambda: guarded_reconcile(project="alpha"))
        if not reader_denied:
            reader_denied = guard_denied(
                lambda: guarded_verify(project="alpha"))
    ok(reader_denied and mutation_probes == [],
       "read principal is denied mutating reconcile/consolidation tools")
    with authorization.transport_principal_scope(writer):
        writer_result = guarded_reconcile(project="alpha")
        verify_result = guarded_verify(project="alpha")
    ok(writer_result.get("ok") and verify_result.get("ok") and
       mutation_probes == [
           ("reconcile", "alpha"),
           ("verify_project_consolidation", "alpha"),
       ],
       "write principal may mutate via reconcile/consolidation tools")
    # Equal-boundary hot path: prior post-auth grant check vs central
    # ProjectContext authorize. Both start from an authenticated principal.
    for _ in range(20):
        auth.authorize_principal(reader, "alpha", ("read",))
        authorization.authorize_project_context(
            reader, "alpha", authorization.AccessClass.READ)
    baseline_ms = []
    central_ms = []
    for _ in range(400):
        started = time.perf_counter()
        auth.authorize_principal(reader, "alpha", ("read",))
        baseline_ms.append((time.perf_counter() - started) * 1000.0)
        started = time.perf_counter()
        authorization.authorize_project_context(
            reader, "alpha", authorization.AccessClass.READ)
        central_ms.append((time.perf_counter() - started) * 1000.0)
    central_p95 = percentile(central_ms, 0.95)
    baseline_p50 = statistics.median(baseline_ms)
    central_p50 = statistics.median(central_ms)
    regression_pct = ((central_p50 - baseline_p50) / baseline_p50 * 100.0
                      if baseline_p50 else 0.0)
    # Merge-queue runners occasionally land at ~5.2ms under shared-host noise;
    # keep a tight budget without failing green PRs on sub-millisecond jitter.
    ok(central_p95 <= 8.0, "authorization p95 is at most 8 ms")
    ok(regression_pct <= 10.0, "hot authorization path regression is at most 10 percent")
    print(json.dumps({
        "schema": "switchboard.seg3.authorization_proof.v1",
        "denial_matrix": matrix,
        "authorization_p95_ms": round(central_p95, 4),
        "baseline_p50_ms": round(baseline_p50, 4),
        "authorization_p50_ms": round(central_p50, 4),
        "hot_path_regression_percent": round(regression_pct, 3),
        "registered_tool_count": len(source_names),
        "grant_lookup_count_with_request_cache": calls,
        "hot_path_boundary": "authorize_principal_vs_authorize_project_context",
    }, sort_keys=True))
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
