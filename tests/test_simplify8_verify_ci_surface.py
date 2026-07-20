"""SIMPLIFY-8 surface ratchet: callers use verify_ci, not mirror plumbing."""
from __future__ import annotations

import re

from path_setup import ROOT


passed = 0
failed = 0


def ok(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok  {msg}")
    else:
        failed += 1
        print(f"FAIL  {msg}")


# Callers listed in the task acceptance must not import mirror/scratchpad internals.
CALLER_PATHS = [
    ROOT / "review_steward.py",
    ROOT / "merge_steward.py",
    ROOT / "merge_coordinator.py",
    ROOT / "coordinator_audit.py",
    ROOT / "static" / "app.js",
]

FORBIDDEN = re.compile(
    r"\b(import\s+ci_scratchpad_dispatch|import\s+external_ci_mirror|"
    r"from\s+ci_scratchpad_dispatch|from\s+external_ci_mirror|"
    r"try_dispatch_scratchpad|request_external_ci_mirror_run|"
    r"mirror_branch\s*=)\b"
)


def test_callers_use_adapter_only():
    for path in CALLER_PATHS:
        text = path.read_text(encoding="utf-8")
        hit = FORBIDDEN.search(text)
        ok(hit is None, f"{path.name} has no direct mirror/scratchpad plumbing")
        if path.suffix == ".py":
            if path.name in {"review_steward.py", "merge_coordinator.py", "coordinator_audit.py"}:
                ok("verify_ci" in text, f"{path.name} references verify_ci")
        if path.name == "app.js":
            ok("/ixp/v1/verify_ci" in text, "UI posts to /ixp/v1/verify_ci")


def test_adapter_and_mcp_rest_exist():
    adapter = ROOT / "src/switchboard/application/commands/verify_ci.py"
    ok(adapter.is_file(), "verify_ci command module exists")
    mcp = (ROOT / "src/switchboard/mcp/tools/external_effects.py").read_text(encoding="utf-8")
    rest = (ROOT / "src/switchboard/api/routers/external_effects.py").read_text(encoding="utf-8")
    jobs = (ROOT / "jobs.py").read_text(encoding="utf-8")
    ok("def verify_ci(" in mcp and '"verify_ci"' in mcp, "MCP exposes verify_ci")
    ok('/ixp/v1/verify_ci' in rest, "REST exposes POST /ixp/v1/verify_ci")
    ok("VerifyCiBody" in rest, "REST verify_ci uses a typed body model")
    ok('def verify(' in jobs and '"verify": verify' in jobs,
       "jobs.py verify is the SHA-only re-verify command")


def test_github_sync_routes_through_verify_ci():
    text = (ROOT / "github_sync.py").read_text(encoding="utf-8")
    ok("verify_ci_command.verify" in text, "github_sync ensure path uses verify_ci")
    ok("try_dispatch_scratchpad(" not in text,
       "github_sync no longer calls try_dispatch_scratchpad directly")
    ok("try_dispatch_merge_group(" not in text,
       "github_sync no longer calls try_dispatch_merge_group directly")


if __name__ == "__main__":
    test_callers_use_adapter_only()
    test_adapter_and_mcp_rest_exist()
    test_github_sync_routes_through_verify_ci()
    print(f"\nsimplify8_verify_ci_surface: {passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
