#!/usr/bin/env python3
"""ADAPTER-59: cache-stable MCP worker pack + handshake result cache.

CLI runners (work_session / direct_session) see one fixed tool list every turn.
Operators keep the full census. Handshake reads can return a not-modified stub.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="adapter59-mcp-worker-pack-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ.pop("PM_REDIS_URL", None)

from path_setup import ROOT, entrypoint_source  # noqa: E402
from switchboard.mcp import authorization as mcp_authorization  # noqa: E402
from switchboard.mcp import handshake_cache  # noqa: E402
from switchboard.mcp import worker_pack  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


REQUIRED = frozenset({
    "prepare_agent_session", "get_working_agreement", "get_project_contract",
    "register_agent", "list_unacked_messages", "ack_message",
    "list_unblock_requests", "search_tasks", "get_task", "claim_task",
    "claim_next", "add_comment", "complete_claim", "record_executed_test_run",
    "record_review_verdict", "merge_gate", "pre_tool_check", "heartbeat",
    "submit_bug", "control_plane_probe", "explain_task_block", "finish_turn",
    "get_work_session_health", "create_work_session", "get_lane_delta",
    "get_mission_status",
})
FORBIDDEN = frozenset({
    "create_scoped_token", "execute_project_purge",
    "enroll_provider_connection", "create_project_purge_intent",
    "begin_agent_host_enrollment",
})
CENSUS = (
    mcp_authorization.READ_TOOLS
    | mcp_authorization.WRITE_TOOLS
    | mcp_authorization.LLM_TOOLS
)


class _Tool:
    def __init__(self, name: str):
        self.name = name


def test_pack_is_cache_stable_and_complete() -> None:
    names = worker_pack.WORKER_PACK
    ok(tuple(names) == tuple(sorted(names)), "worker pack names are sorted")
    ok(set(names) <= CENSUS, "worker pack is a subset of the MCP census")
    missing = sorted(REQUIRED - set(names))
    extra_forbidden = sorted(set(names) & FORBIDDEN)
    ok(not missing, f"worker pack includes handshake/claim/complete tools {missing}")
    ok(not extra_forbidden, f"worker pack excludes admin tools {extra_forbidden}")
    ok(12 <= len(names) <= 48, f"worker pack stays thin ({len(names)} tools)")


def test_principal_selection() -> None:
    ok(worker_pack.uses_worker_pack({"kind": "work_session"}) is True,
       "work_session tokens get the worker pack")
    ok(worker_pack.uses_worker_pack({"kind": "direct_session"}) is True,
       "direct_session tokens get the worker pack")
    ok(worker_pack.uses_worker_pack({"kind": "user"}) is False,
       "human users keep the full catalog")
    ok(worker_pack.uses_worker_pack({"kind": "env", "id": "env-mcp-token"}) is False,
       "operator env token keeps the full catalog")
    ok(worker_pack.uses_worker_pack(None) is False,
       "missing principal does not shrink the catalog")


def test_list_and_call_filter() -> None:
    tools = [_Tool("complete_claim"), _Tool("execute_project_purge"),
             _Tool("search_tasks")]
    worker = {"kind": "work_session", "id": "work-session:ws-1"}
    listed = worker_pack.filter_tools(tools, worker)
    ok([t.name for t in listed] == ["complete_claim", "search_tasks"],
       "tools/list drops admin tools for workers")
    operator_listed = worker_pack.filter_tools(tools, {"kind": "user"})
    ok([t.name for t in operator_listed] == [t.name for t in tools],
       "tools/list stays full for operators")
    ok(worker_pack.blocks_tool("execute_project_purge", worker) is True,
       "tools/call denies admin tools for workers")
    ok(worker_pack.blocks_tool("complete_claim", worker) is False,
       "tools/call still allows complete_claim")
    ok(worker_pack.blocks_tool("execute_project_purge", {"kind": "user"}) is False,
       "operators may still call admin tools")


def test_handshake_digest_and_not_modified() -> None:
    cache = handshake_cache.HandshakeCache(backend=handshake_cache.MemoryBackend())
    body = {"project": "switchboard", "session_start_sequence": ["register_agent"]}
    first = cache.wrap("working_agreement", "switchboard", body,
                       principal_id="work-session:ws-1")
    parsed = json.loads(first)
    ok(parsed["project"] == "switchboard", "first handshake returns the full body")
    ok(parsed["session_start_sequence"] == ["register_agent"],
       "first handshake keeps existing fields")
    digest = parsed["cache"]["digest"]
    ok(digest.startswith("sha256:") and len(digest) == 71,
       "first handshake advertises a sha256 digest")
    second = cache.wrap("working_agreement", "switchboard", body,
                        principal_id="work-session:ws-1")
    parsed_second = json.loads(second)
    ok(parsed_second.get("unchanged") is True, "repeat call is not-modified")
    ok(parsed_second.get("digest") == digest, "not-modified repeats the digest")
    ok("session_start_sequence" not in parsed_second,
       "not-modified omits the fat handshake body")
    third = cache.wrap("working_agreement", "switchboard", body,
                       principal_id="work-session:ws-2",
                       if_none_match=digest)
    parsed_third = json.loads(third)
    ok(parsed_third.get("unchanged") is True,
       "explicit if_none_match hits across principals")
    changed = cache.wrap("working_agreement", "switchboard",
                         {**body, "branch_convention": "codex/<TASK-ID>-<slug>"},
                         principal_id="work-session:ws-2",
                         if_none_match=digest)
    parsed_changed = json.loads(changed)
    ok(parsed_changed.get("unchanged") is not True,
       "digest mismatch returns the new full body")
    ok(parsed_changed["branch_convention"].startswith("codex/"),
       "changed handshake includes the new fields")


def test_memory_backend_when_redis_url_missing() -> None:
    opened = handshake_cache.open_backend()
    ok(isinstance(opened, handshake_cache.MemoryBackend),
       "unset PM_REDIS_URL uses in-process memory")
    fake = handshake_cache.MemoryBackend()
    fake.setex("k", 30, "v")
    ok(fake.get("k") == "v", "memory backend round-trips values")


def test_install_tolerates_stub_fastmcp() -> None:
    class _FastMCP:
        def __init__(self, *a, **k):
            pass

        def tool(self, *a, **k):
            return lambda f: f

        def __getattr__(self, n):
            return lambda *a, **k: None

    try:
        worker_pack.install_worker_catalog(_FastMCP())
    except Exception as exc:  # noqa: BLE001 — assert the stub import path
        ok(False, f"install_worker_catalog crashed stub FastMCP: {exc}")
        return
    ok(True, "install_worker_catalog does not crash FastMCP stubs used by ACCESS tests")


def test_install_wraps_real_tool_manager() -> None:
    class _Mgr:
        def list_tools(self):
            return [_Tool("complete_claim"), _Tool("execute_project_purge")]

    class _Mcp:
        def __init__(self):
            self._tool_manager = _Mgr()

    mcp = _Mcp()
    worker_pack.install_worker_catalog(mcp)
    worker = {"kind": "work_session", "id": "work-session:ws-1"}
    with mcp_authorization.transport_principal_scope(worker):
        listed = list(mcp._tool_manager.list_tools())
    ok([t.name for t in listed] == ["complete_claim"],
       "installed catalog filters tools/list for workers")


def test_composition_root_installs_the_pack() -> None:
    source = entrypoint_source("mcp_server")
    impl_lines = sum(1 for _ in (ROOT / "mcp_server_impl.py").open(encoding="utf-8"))
    ok(impl_lines <= 499,
       f"mcp_server_impl stays under Phase 1 residual ceiling ({impl_lines})")
    ok("install_worker_runtime" in source,
       "MCP composition root installs the worker catalog filter")
    deps_src = (ROOT / "src/switchboard/mcp/deps.py").read_text(encoding="utf-8")
    ok("install_worker_catalog" in deps_src,
       "deps.install_worker_runtime wraps install_worker_catalog")
    ok("handshake_cache" in deps_src,
       "MCP composition root wires handshake caching")
    auth_src = (ROOT / "src/switchboard/mcp/authorization.py").read_text(
        encoding="utf-8")
    ok("blocks_tool" in auth_src,
       "authorization wrap denies off-pack tool calls for workers")
    plan_src = (ROOT / "src/switchboard/mcp/tools/plan.py").read_text(
        encoding="utf-8")
    boot_src = (ROOT / "src/switchboard/mcp/tools/boot.py").read_text(
        encoding="utf-8")
    ok("if_none_match" in plan_src, "get_working_agreement accepts if_none_match")
    ok("if_none_match" in boot_src, "get_project_contract accepts if_none_match")


def main() -> int:
    test_pack_is_cache_stable_and_complete()
    test_principal_selection()
    test_list_and_call_filter()
    test_handshake_digest_and_not_modified()
    test_memory_backend_when_redis_url_missing()
    test_install_tolerates_stub_fastmcp()
    test_install_wraps_real_tool_manager()
    test_composition_root_installs_the_pack()
    print(f"{passed}/{passed + failed} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
