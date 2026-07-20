#!/usr/bin/env python3
"""SIMPLIFY-5: effective runtime identity is visible and enforced at placement."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import sys
import tempfile


_TMP = tempfile.mkdtemp(prefix="simplify5-runtime-profile-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
import store  # noqa: E402
from adapters import agent_host  # noqa: E402
from switchboard.domain.coordination.placement import (  # noqa: E402
    evaluate_host,
    plan_hybrid_placement,
)
from switchboard.domain.coordination.runtime_profile import (  # noqa: E402
    RUNTIME_PROFILE_SCHEMA,
    build_runtime_profile,
    evaluate_runtime_profile,
    runtime_profile_requirement,
)


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


P = "switchboard"
RUNTIME = "codex"
GOOD_MODULE = "adapters.codex_local_worker:run"
BAD_MODULE = "claude_personal_worker:run"


def profile(*, module=GOOD_MODULE, auto=True, version="0.2.25",
            gh=True, watch=True):
    return build_runtime_profile(
        runtimes=[RUNTIME],
        work_modules={RUNTIME: module},
        auto_work_session=auto,
        agent_host_version=version,
        binaries={"git": True, "gh": gh, "codex": True},
        runner_watch=watch,
    )


requirement = runtime_profile_requirement(
    RUNTIME, session_policy="code_strict", require_runner_watch=True,
    agent_host_version="0.2.25",
)
good = profile()
same = profile()
ok(good["schema"] == RUNTIME_PROFILE_SCHEMA
   and good["hash"] == same["hash"]
   and good["components"]["work_modules"][RUNTIME] == GOOD_MODULE,
   "the effective profile has a stable versioned hash and visible components")

accepted = evaluate_runtime_profile(good, requirement)
ok(accepted["eligible"] is True and accepted["mismatches"] == [],
   "an exact effective profile satisfies coordinator admission")

wrong_module = evaluate_runtime_profile(profile(module=BAD_MODULE), requirement)
wrong_reason = (wrong_module.get("mismatches") or [{}])[0].get("reason", "")
ok(wrong_module["eligible"] is False
   and wrong_module["reason_code"] == "runtime_profile_drift"
   and "PM_AGENT_WORK_MODULE_CODEX=claude_personal_worker:run" in wrong_reason
   and GOOD_MODULE in wrong_reason,
   "a wrong work module is refused with the drifted key and values named")

missing_gh = evaluate_runtime_profile(profile(gh=False), requirement)
ok(any(item.get("label") == "binary:gh" and item.get("actual") is False
       for item in missing_gh["mismatches"]),
   "a launchd PATH that loses gh is visible before work is placed")

no_relay = evaluate_runtime_profile(profile(watch=False), requirement)
ok(any(item.get("label") == "runner_watch" for item in no_relay["mismatches"]),
   "runner_watch remains a host-proven relay fact, never a policy grant")

tampered = copy.deepcopy(good)
tampered["components"]["binaries"]["gh"] = False
tampered_result = evaluate_runtime_profile(tampered, requirement)
ok(tampered_result["reason_code"] == "runtime_profile_invalid"
   and any(item.get("key") == "profile_hash" for item in tampered_result["mismatches"]),
   "a profile whose components do not match its hash fails closed")

malformed = copy.deepcopy(good)
malformed["profile_version"] = "not-an-integer"
malformed_result = evaluate_runtime_profile(malformed, requirement)
ok(malformed_result["eligible"] is False
   and malformed_result["reason_code"] == "runtime_profile_invalid",
   "a malformed profile version is refused without crashing placement")


# Prove the real Agent Host producer and list_agent_hosts read model, not only the
# pure profile contract.  Binary/relay probes are controlled so this is hermetic.
saved_env = {key: os.environ.get(key) for key in (
    "PM_RUNTIME", "PM_AGENT_WORK_MODULE_CODEX", "PM_AUTO_WORK_SESSION",
    "PM_AGENT_HOST_ALLOW_WORK", "PM_HOST_PROJECTS", "PM_HOST_SESSION_POLICIES",
)}
saved_watch = agent_host.host_serves_runner_watch
saved_which = agent_host.shutil.which
saved_version = agent_host.AGENT_HOST_VERSION
try:
    os.environ.update({
        "PM_RUNTIME": RUNTIME,
        "PM_AGENT_WORK_MODULE_CODEX": GOOD_MODULE,
        "PM_AUTO_WORK_SESSION": "1",
        "PM_AGENT_HOST_ALLOW_WORK": "1",
        "PM_HOST_PROJECTS": P,
        "PM_HOST_SESSION_POLICIES": "code_strict",
    })
    agent_host.host_serves_runner_watch = lambda: True
    agent_host.shutil.which = lambda name: f"/test/bin/{name}"
    agent_host.AGENT_HOST_VERSION = "0.2.25"
    inventory = agent_host.default_inventory()
    agent_host.shutil.which = lambda name: None if name == "gh" else f"/test/bin/{name}"
    refreshed_capacity = agent_host.heartbeat_capacity(inventory)
finally:
    agent_host.host_serves_runner_watch = saved_watch
    agent_host.shutil.which = saved_which
    agent_host.AGENT_HOST_VERSION = saved_version
    for key, value in saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

advertised = (inventory.get("capacity") or {}).get("runtime_profile") or {}
ok(advertised.get("hash") and advertised.get("components") == good.get("components"),
   "the Agent Host advertises its actual effective config, binaries, build, and relay")
ok(refreshed_capacity.get("runtime_profile", {}).get("components", {})
   .get("binaries", {}).get("gh") is False
   and refreshed_capacity.get("runtime_profile", {}).get("hash") != advertised.get("hash"),
   "heartbeat re-probes effective binaries instead of replaying a startup snapshot")

store.init_db(P)
registered = store.register_host(inventory, actor="simplify5-test", project=P)
listed = store.list_agent_hosts(include_stale=True, project=P)
ok(registered.get("host_id") == inventory.get("host_id")
   and listed and listed[0].get("runtime_profile", {}).get("hash") == advertised.get("hash")
   and listed[0].get("runtime_profile", {}).get("components") == advertised.get("components"),
   "list_agent_hosts exposes profile hash and components without a schema migration")


drifted_host = copy.deepcopy(listed[0])
drifted_profile = profile(module=BAD_MODULE)
drifted_host["runtime_profile"] = drifted_profile
drifted_host.setdefault("capacity", {})["runtime_profile"] = drifted_profile
drifted_host["stale"] = False
drifted_host["status"] = "online"
drifted_host["available_sessions"] = 1
policy = {
    "mode": "co_fleet",
    "scheduler": {
        "mode": "hybrid", "allow_persistent": True,
        "allow_ephemeral": False, "burst_enabled": False,
    },
    "placement": {
        "canonical_repo": "6th-Element-Labs/projectplanner",
        "session_policy": "code_strict",
        "isolation": "task_worktree",
        "runtime_profile": requirement,
    },
}
selector = {"runtime": RUNTIME, "lane": "SIMPLIFY"}
candidate = evaluate_host(drifted_host, selector, policy, project=P)
plan = plan_hybrid_placement([drifted_host], selector, policy, project=P)
named_refusal = ((plan.get("refusal_details") or [{}])[0].get("reasons") or [""])[0]
ok(candidate["eligible"] is False
   and "runtime_profile_drift" in candidate["reason_codes"]
   and candidate["profile_drift"],
   "hybrid placement excludes a host whose effective profile drifts")
ok(plan["selected_host_id"] is None
   and plan["reason_code"] == "runtime_profile_drift"
   and "PM_AGENT_WORK_MODULE_CODEX" in named_refusal,
   "the coordinator's placement refusal preserves the exact drifted key")

print(f"\nSIMPLIFY-5 runtime profile: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
