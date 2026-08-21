#!/usr/bin/env python3
"""Contract incompatibility cannot be weakened by rollout observation.

Found by looking at the real prod fleet after deploy: both live hosts report an
empty contract fingerprint, because they predate attestation. Under a plain
promotion the readiness model judges exactly that state incompatible — correctly
— and withholds work from every one of them at once. There is no way back,
because the self-update that would rescue a host ships inside the release it
does not have.

Release rollout metadata may start in OBSERVE, but an incompatible Host cannot
successfully launch. It must be withheld in both modes. The updater and Host
heartbeat remain available; only new work is denied until the contract agrees.

This is the same rule autopilot fixes live under: the loop must converge on its
own, and "the operator notices and repairs it" is not convergence.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401

TMP = tempfile.mkdtemp(prefix="staged-enforcement-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
store.init_db("switchboard")
from switchboard.domain import host_readiness as hr  # noqa: E402
from switchboard.storage.repositories import coordination  # noqa: E402
from switchboard.storage.repositories import host_releases as rel  # noqa: E402

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


# The two hosts actually on prod on 2026-07-31: both attest nothing.
LEGACY = {"stale": False, "agent_host_version": "0.4.15",
          "bundle_digest": "", "contract_fingerprint": ""}
SKEWED = {"stale": False, "agent_host_version": "0.4.15",
          "bundle_digest": "sha256:old", "contract_fingerprint": "eac1:old"}

try:
    published = rel.record_release(
        {"version": "0.5.0", "bundle_digest": "sha256:new",
         "contract_fingerprint": "eac1:new",
         "download_url": "/ixp/v1/host_releases/x/bundle"},
        actor="test", promote=True, project=P)
    promoted = rel.get_promoted_release(project=P)

    ok(promoted["enforce"] is False,
       "a freshly promoted release does NOT enforce — publishing is not a fleet-wide stop")

    # Observe never means "claim wakes that cannot launch".
    observed = hr.evaluate(LEGACY, promoted)
    ok(observed["state"] == hr.BLOCKED,
       f"a pre-attestation host is still judged incompatible: {observed['state']}")
    ok(observed["actionable"] is True, "and the operator is still offered the fix")
    ok(observed["withholds_work"] is True,
       "contract-incompatible work is withheld even during rollout observation")
    ok("cannot be trusted" in observed["detail"],
       "the reason names the missing compatibility proof")

    host = dict(LEGACY)
    host["readiness"] = observed
    host["runtimes"] = [{"runtime": "codex", "local_auth": {"available": True}}]
    ok(coordination._host_can_handle(host, {"runtime": "codex"}) is False,
       "placement refuses it before it can claim and fail a wake")

    # The rollout flag does not weaken or strengthen contract safety.
    enforced_release = rel.set_enforcement(enforce=True, project=P)
    ok(enforced_release["enforce"] is True, "enforcement is a separate deliberate flip")

    enforced = hr.evaluate(LEGACY, rel.get_promoted_release(project=P))
    ok(enforced["state"] == hr.BLOCKED, "the verdict is unchanged")
    ok(enforced["withholds_work"] is True,
       "contract safety remains enforced")
    ok("Observe mode" not in enforced["detail"],
       "and stops claiming otherwise")

    host2 = dict(LEGACY)
    host2["readiness"] = enforced
    host2["runtimes"] = [{"runtime": "codex", "local_auth": {"available": True}}]
    ok(coordination._host_can_handle(host2, {"runtime": "codex"}) is False,
       "placement now refuses it")

    # A skewed (not merely old) host behaves the same way under both modes.
    ok(hr.evaluate(SKEWED, promoted)["withholds_work"] is True,
       "observe also refuses a contract-skewed host")
    ok(hr.evaluate(SKEWED, rel.get_promoted_release(project=P))["withholds_work"] is True,
       "and enforcement catches it")

    # Disabling rollout enforcement cannot make an impossible launch eligible.
    rel.set_enforcement(enforce=False, project=P)
    ok(hr.evaluate(LEGACY, rel.get_promoted_release(project=P))["withholds_work"] is True,
       "the rollout flag cannot bypass contract compatibility")

    # Offline is liveness, not policy: it withholds regardless of enforcement.
    ok(hr.evaluate({**LEGACY, "stale": True},
                   rel.get_promoted_release(project=P))["withholds_work"] is True,
       "a dead host is still withheld in observe — enforcement is not a bypass")

    # Promoting a NEWER release must not silently inherit enforcement, nor
    # silently drop it. A second promotion is a new decision about a new artifact.
    rel.set_enforcement(enforce=True, project=P)
    rel.record_release(
        {"version": "0.6.0", "bundle_digest": "sha256:newer",
         "contract_fingerprint": "eac1:newer", "download_url": "/x"},
        actor="test", promote=True, project=P)
    after = rel.get_promoted_release(project=P)
    ok(after["version"] == "0.6.0", "the newer release is promoted")
    ok(after["enforce"] is False,
       "and starts in observe again — every new artifact earns enforcement on its own")

    try:
        store.init_db("maxwell")
        rel.set_enforcement(enforce=True, project="maxwell")
        ok(False, "enforcing with nothing promoted must be refused")
    except rel.HostReleaseError as exc:
        ok(exc.code == "host_release_none_promoted",
           f"enforcing with nothing promoted is refused: {exc.code}")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nStaged enforcement: {passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
