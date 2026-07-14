#!/usr/bin/env python3
"""CONSOL-9: H2 census — per-tool call counts exist; seeded zero-callers are gone."""
from pathlib import Path

from mcp_observability import MCPObservability


ROOT = Path(__file__).resolve().parent

DELETED_MCP_TOOLS = {
    "replay_verify",
    "simulate_dispatch",
    "get_coordination_receipt",
    "list_coordination_receipts",
    "project_task_receipts",
    "evaluate_dbos_runtime",
}

DELETED_ROUTE_FRAGMENTS = (
    "/ixp/v1/replay/verify",
    "/ixp/v1/replay/simulate_dispatch",
    "/ixp/v1/receipts",
    "/ixp/v1/tasks/{task_id}/receipts",
    "/ixp/v1/background_jobs/evaluate_dbos",
)

DOC_ONLY_PM_FLAGS = {
    "PM_OPERATOR_TOKEN",
    "PM_SYSTEM_TOKEN",
    "PM_WAKE_ID",
    "PM_WEBHOOK_SECRET",
}


def ok(condition, message):
    if not condition:
        raise AssertionError(message)


obs = MCPObservability(sample_limit=8, slow_log_limit=4, slow_call_ms=10_000)
obs.record("claim_task", 12.5)
snap = obs.snapshot()
ok("calls" in snap["tools"]["claim_task"], "mcp_observability exposes per-tool call counters")
ok(snap["tools"]["claim_task"]["calls"] == 1, "call counter increments on record()")

mcp_src = (ROOT / "mcp_server.py").read_text(encoding="utf-8") + (
    ("\n" + (ROOT / "mcp_server_impl.py").read_text(encoding="utf-8"))
    if (ROOT / "mcp_server_impl.py").is_file() else "")
for tool in DELETED_MCP_TOOLS:
    ok(f"def {tool}(" not in mcp_src, f"MCP tool {tool} removed from mcp_server.py")

app_src = (ROOT / "app.py").read_text(encoding="utf-8") + (
    ("\n" + (ROOT / "app_impl.py").read_text(encoding="utf-8"))
    if (ROOT / "app_impl.py").is_file() else "")
for fragment in DELETED_ROUTE_FRAGMENTS:
    ok(fragment not in app_src, f"REST route fragment {fragment} removed from app.py")

store_src = (ROOT / "store.py").read_text(encoding="utf-8")
for name in ("replay_verify", "simulate_dispatch", "project_task_receipts"):
    ok(f"def {name}(" not in store_src, f"store facade {name} removed")

ok(not (ROOT / "receipts_store.py").exists(), "receipts_store leaf removed with receipt MCP tools")

jobs_src = (ROOT / "jobs_store.py").read_text(encoding="utf-8")
ok("def evaluate_dbos_runtime(" not in jobs_src,
   "evaluate_dbos_runtime facade removed from jobs_store")

for rel in ("docs/SWITCHBOARD-RUNBOOK.md", "docs/AGENT-HOST-SPEC.md", "docs/design/operator-ui-wireframes.html"):
    text = (ROOT / rel).read_text(encoding="utf-8")
    for flag in DOC_ONLY_PM_FLAGS:
        ok(flag not in text, f"doc-only flag {flag} removed from {rel}")

print("CONSOL-9 H2 census checks passed")
