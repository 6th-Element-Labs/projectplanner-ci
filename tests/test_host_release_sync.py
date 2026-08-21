#!/usr/bin/env python3
"""Host release sync: an incompatible Agent Host must not be given work.

The 2026-07-31 drain canary lost all three Wave A missions this way. BUG-249
added `session_policy_profile` to the execution-assignment contract server-side.
The Agent Host bundles its own copy of that module, re-derives the contract, and
admission compares whole dicts — so every launch was refused with
`execution_assignment_contract_mismatch`, but only AFTER the wake was claimed
and the 90s hold burned. The host heartbeated green throughout.

Two things are pinned here:
  1. Readiness is not liveness. A live host running an incompatible bundle is
     `blocked`, and `blocked` withholds work.
  2. Version strings are not identity. The incident was recovered by copying one
     file into the deployed 0.4.15 tree, so the version never changed while the
     bundle did. Only the digest catches that.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401

TMP = tempfile.mkdtemp(prefix="host-release-sync-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
store.init_db("switchboard")   # create + migrate this board's tables
from switchboard.connect import execution_assignment as ea  # noqa: E402
from switchboard.domain import host_readiness as hr  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


SERVER_FP = ea.contract_fingerprint()
# enforce=True: withholding is what this file is about. Enforcement is staged
# separately (tests/test_host_release_staged_enforcement.py) precisely so a
# fresh promotion cannot strand a fleet that predates attestation.
REQUIRED = {"version": "0.4.16", "bundle_digest": "sha256:bbbb",
            "contract_fingerprint": SERVER_FP, "enforce": True}


def host(**over):
    base = {"stale": False, "agent_host_version": "0.4.16",
            "bundle_digest": "sha256:bbbb", "contract_fingerprint": SERVER_FP}
    base.update(over)
    return base


try:
    # ── the fingerprint is a property of the shape, not of one contract ──
    ok(SERVER_FP.startswith("eac1:"), f"fingerprint is namespaced: {SERVER_FP}")
    ok(ea.contract_fingerprint() == SERVER_FP, "fingerprint is stable across calls")
    ok("session_policy_profile" in ea.CONTRACT_FIELDS,
       "the field that broke the fleet is declared in CONTRACT_FIELDS")
    prefixes = ea.MISSION_KEY_PREFIXES
    ea.MISSION_KEY_PREFIXES = ("v4:",)
    legacy_v4_only = ea.contract_fingerprint()
    ea.MISSION_KEY_PREFIXES = prefixes
    ok(legacy_v4_only != SERVER_FP,
       "adding v5 mission semantics changes the Host compatibility fingerprint")

    # Every key the builder can emit must be declared, or a future wire change
    # ships with a fingerprint that fails to move — the exact silent drift.
    built = ea.build_execution_assignment(
        task_id="QA-46",
        assignment={"assignment_id": "asg-1"},
        lifecycle={"role": "implementation", "execution_id": "exec-1",
                   "generation": 1, "session_policy_profile": "code_strict",
                   "reason_code": "start", "pr_number": 0, "pr_url": ""},
    )
    undeclared = sorted(set(built) - set(ea.CONTRACT_FIELDS))
    ok(not undeclared, f"builder emits no undeclared field: {undeclared}")
    v5 = ea.build_execution_assignment(
        task_id="QA-175",
        assignment={"assignment_id": "asg-v5"},
        lifecycle={"role": "implementation", "execution_id": "exec-v5",
                   "generation": 1,
                   "mission_key": "v5:1:QA-175:1:implementation"},
    )
    ok(v5["typed_tools"] == ea.MISSION_TYPED_TOOLS,
       "the Host contract gives v5 the mission context and yield tools")

    # ── readiness ──────────────────────────────────────────────────────────
    good = hr.evaluate(host(), REQUIRED)
    ok(good["state"] == hr.READY, f"a promoted-release host is ready: {good['state']}")
    ok(good["withholds_work"] is False, "a ready host receives work")

    # The incident, exactly: live, green, older contract.
    incident = hr.evaluate(
        host(agent_host_version="0.4.15", bundle_digest="sha256:aaaa",
             contract_fingerprint="eac1:0000000000000000"), REQUIRED)
    ok(incident["state"] == hr.BLOCKED,
       f"an incompatible contract blocks: {incident['state']}")
    ok(incident["reason"] == "host_release_incompatible",
       f"the refusal is named: {incident['reason']}")
    ok(incident["withholds_work"] is True, "a blocked host is denied work")
    ok(incident["actionable"] is True, "the operator is offered a fix")
    ok("refused" in incident["detail"], f"the detail says why: {incident['detail'][:60]}")

    # A bundle from before attestation cannot prove it is safe.
    silent = hr.evaluate(host(contract_fingerprint=""), REQUIRED)
    ok(silent["state"] == hr.BLOCKED,
       "a host that reports no fingerprint is blocked, not assumed good")

    # The hand-patched tree: same version, different bytes.
    patched = hr.evaluate(host(bundle_digest="sha256:hand-patched"), REQUIRED)
    ok(patched["state"] == hr.UPDATE_AVAILABLE,
       f"a digest mismatch is visible even at the right version: {patched['state']}")
    ok(patched["withholds_work"] is False,
       "a digest drift warns but does not stop the fleet: the contract still agrees")

    behind = hr.evaluate(host(agent_host_version="0.4.15",
                              bundle_digest="sha256:bbbb"), REQUIRED)
    ok(behind["state"] == hr.UPDATE_AVAILABLE, "a behind-but-compatible host warns")
    failed_update = hr.evaluate(
        host(agent_host_version="0.4.15", bundle_digest="sha256:failed",
             update_error="download URL must stay on trusted origin"), REQUIRED)
    ok(failed_update["state"] == hr.UPDATE_FAILED,
       "a failed install stays distinct from an available update")
    ok("trusted origin" in failed_update["detail"],
       "the exact Host failure remains visible to the operator")

    managed = hr.evaluate(
        host(agent_host_version="0.2.0", bundle_digest="sha256:source-tree",
             release_management="deployment_managed"), REQUIRED)
    ok(managed["state"] == hr.READY,
       "a deployment-managed Host is not compared to the desktop bundle")
    ok(managed["actionable"] is False,
       "a deployment-managed Host is never offered an impossible self-update")
    ok(managed["required_version"] == "" and managed["required_digest"] == "",
       "desktop release metadata does not masquerade as its deployment target")
    ok("deployment" in managed["detail"].lower(),
       "the operator is told who manages the Host")

    # ── liveness is a separate axis ────────────────────────────────────────
    ok(hr.evaluate(host(stale=True), REQUIRED)["state"] == hr.OFFLINE,
       "an expired heartbeat is offline, not blocked")
    ok(hr.evaluate(host(update_state="draining"), REQUIRED)["state"] == hr.UPDATING,
       "a self-updating host reports updating, not blocked")

    # ── fail-open: this module must never be why a fleet cannot work ───────
    none_promoted = hr.evaluate(
        host(contract_fingerprint="eac1:anything", bundle_digest="x"), None)
    ok(none_promoted["state"] == hr.READY,
       "with no promoted release the control plane has no opinion")
    ok(none_promoted["withholds_work"] is False, "fail-open keeps hosts eligible")

    # ── the placement gate reads the verdict ───────────────────────────────
    from switchboard.storage.repositories import coordination  # noqa: E402
    selector = {"runtime": "codex"}
    blocked_host = host(agent_host_version="0.4.15",
                        contract_fingerprint="eac1:0000000000000000")
    blocked_host["readiness"] = hr.evaluate(blocked_host, REQUIRED)
    blocked_host["runtimes"] = [{"runtime": "codex", "local_auth": {"available": True}}]
    ok(coordination._host_can_handle(blocked_host, selector) is False,
       "placement refuses a contract-incompatible host")

    ready_host = host()
    ready_host["readiness"] = hr.evaluate(ready_host, REQUIRED)
    ready_host["runtimes"] = [{"runtime": "codex", "local_auth": {"available": True}}]
    ok(coordination._host_can_handle(ready_host, selector) is True,
       "placement still admits a compatible host")

    # ── the promoted release is storage, and singular ──────────────────────
    from switchboard.storage.repositories import host_releases as rel  # noqa: E402
    P = "switchboard"
    a = rel.record_release({"version": "0.4.15", "bundle_digest": "sha256:aaaa",
                            "contract_fingerprint": "eac1:old"},
                           actor="test", promote=True, project=P)
    ok(rel.get_promoted_release(project=P)["version"] == "0.4.15",
       "the promoted release is readable")
    b = rel.record_release({"version": "0.4.16", "bundle_digest": "sha256:bbbb",
                            "contract_fingerprint": SERVER_FP},
                           actor="test", promote=True, project=P)
    promoted = rel.get_promoted_release(project=P)
    ok(promoted["version"] == "0.4.16", "promoting swaps the single promoted row")
    ok(len([r for r in rel.list_releases(project=P) if r["promoted"]]) == 1,
       "exactly one release is ever promoted")
    ok(a["release_id"] != b["release_id"], "releases are identified by version+digest")

    managed_registration = coordination.register_host({
        "host_id": "host/managed-vm", "hostname": "managed-vm",
        "agent_host_version": "0.2.0", "heartbeat_ttl_s": 60,
        "runtimes": [{"runtime": "claude-code"}], "limits": {"max_sessions": 1},
        "capacity": {"active_sessions": 0,
                     "release_management": "deployment_managed",
                     "host_attestation": {"contract_fingerprint": SERVER_FP,
                                          "bundle_digest": "sha256:source"}},
    }, project=P)
    ok(not managed_registration.get("error"), "a deployment-managed Host registers")
    refused = coordination.request_host_update("host/managed-vm", project=P)
    ok(refused.get("error") == "host_update_not_supported",
       "Capacity refuses a desktop-package update for a deployment-managed Host")
    ok("deployment" in refused.get("detail", "").lower(),
       "the refusal names the correct update owner")

    try:
        rel.record_release({"version": "0.4.17"}, project=P)
        ok(False, "a release without a digest must be refused")
    except rel.HostReleaseError as exc:
        ok(exc.code == "host_release_bundle_digest_required",
           f"a release without a digest is refused: {exc.code}")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nHost release sync: {passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
