#!/usr/bin/env python3
"""CO-4: interruption drain, checkpoint reconstruction, BYOA fencing, and purge proof."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from path_setup import ROOT  # noqa: F401  (adds the repository root to sys.path)

from adapters import agent_host, co_drain  # noqa: E402


TMP = Path(tempfile.mkdtemp(prefix="co4-graceful-drain-"))
REQUEST_PATH = TMP / "run" / "drain-request.json"
RECEIPT_PATH = TMP / "run" / "drain-receipt.json"
RUNTIME_ROOT = TMP / "provider-runtimes"
os.environ["PM_CO_DRAIN_REQUEST_PATH"] = str(REQUEST_PATH)
os.environ["PM_CO_DRAIN_RECEIPT_PATH"] = str(RECEIPT_PATH)
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), text=True, capture_output=True, check=True)
    return result.stdout.strip()


try:
    os.environ["PM_CO_DRAIN_IMDS"] = "1"
    imds_calls = []

    def imds(method, path, headers):
        imds_calls.append((method, path, dict(headers)))
        if path == "/latest/api/token":
            return 200, "imds-token"
        if path.endswith("/spot/instance-action"):
            return 200, json.dumps({"action": "terminate", "time": "2030-01-01T00:00:00Z"})
        return 404, ""

    interruption = co_drain.detect_ec2_interruption(imds, now=1000)
    ok(interruption.get("reason") == "spot_interruption"
       and interruption.get("deadline") > interruption.get("requested_at")
       and imds_calls[1][2].get("X-aws-ec2-metadata-token") == "imds-token",
       "IMDSv2 Spot notice creates a bounded interruption drain request")

    request = co_drain.write_request({
        "request_id": "drain-no-new-claims",
        "reason": "spot_interruption",
        "termination_kind": "ephemeral_instance",
        "requested_at": time.time(),
        "deadline": time.time() + 100,
    })
    inventory = {
        "host_id": "host/i-co4", "repo_root": str(TMP),
        "policy": {"allow_work": True, "allow_global_claim": False},
        "runtimes": [{
            "runtime": "codex", "lanes": ["CO"],
            "policy": {"allow_work": True, "allow_global_claim": False},
        }],
        "limits": {"max_sessions": 1},
    }
    calls = []

    def fake_try(method, path, body=None):
        calls.append((method, path, body))
        if path.startswith(agent_host.P_LIST_RUNNERS):
            return {"sessions": []}
        if path.startswith(agent_host.P_LIST_WORK_SESSIONS):
            return {"work_sessions": []}
        if path == agent_host.P_HEARTBEAT_HOST:
            return {"host_id": "host/i-co4", "status": (body or {}).get("status")}
        return {}

    RUNTIME_ROOT.mkdir()
    (RUNTIME_ROOT / "provider-token-cache").write_text("runtime-secret")
    os.environ["PM_PROVIDER_RUNTIME_ROOT"] = str(RUNTIME_ROOT)
    original_try = agent_host._try
    agent_host._try = fake_try
    try:
        summary = agent_host.run_once(inventory)
    finally:
        agent_host._try = original_try
    claimed = any(path.startswith(agent_host.P_LIST_WAKES) or path == agent_host.P_CLAIM_WAKE
                  for _method, path, _body in calls)
    ok(summary.get("draining") is True and not claimed
       and summary.get("drain_receipt", {}).get("no_new_claims") is True,
       "drain marker is checked before wake polling so the host claims no new work")
    ok(not list(RUNTIME_ROOT.iterdir())
       and summary.get("drain_receipt", {}).get("durable_acknowledged") is True,
       "host purges provider runtime residue and durably acknowledges the drain")
    advertised = co_drain.inventory_for_drain(inventory)
    ok(advertised["policy"]["allow_work"] is False
       and advertised["runtimes"][0]["policy"]["allow_work"] is False,
       "draining inventory remains ineligible even across host re-registration")

    registered = []

    def register_try(method, path, body=None):
        registered.append((method, path, body))
        return {"runner_session_id": "runner-affinity"}

    agent_host._try = register_try
    try:
        agent_host.register_runner_session(
            {"runner_session_id": "runner-affinity", "status": "running"},
            {
                "task_id": "CO-4", "selector": {"runtime": "codex"},
                "policy": {"account_binding": {
                    "provider": "openai-codex",
                    "provider_account_id": "raw-personal-account",
                    "credential_reference": "vault:must-not-copy",
                    "credential_lease_id": "lease-for-drain",
                    "work_session_id": "worksession-for-drain",
                    "account_affinity_id": "affinity-safe-hash",
                }},
            },
            inventory,
        )
    finally:
        agent_host._try = original_try
    registration = registered[0][2]
    registration_text = json.dumps(registration, sort_keys=True)
    ok(registration["metadata"].get("credential_lease_id") == "lease-for-drain"
       and registration["metadata"].get("work_session_id") == "worksession-for-drain"
       and "raw-personal-account" not in registration_text
       and "vault:must-not-copy" not in registration_text,
       "runner retains the personal-login lease/work binding without copying account secrets")

    remote = TMP / "remote.git"
    workspace_root = TMP / "workspaces"
    worktree = workspace_root / "co4"
    remote.mkdir()
    git(remote, "init", "--bare")
    worktree.mkdir(parents=True)
    git(worktree, "init", "-b", "master")
    git(worktree, "config", "user.name", "CO-4 Test")
    git(worktree, "config", "user.email", "co4@example.invalid")
    (worktree / "handoff.txt").write_text("before\n")
    git(worktree, "add", "handoff.txt")
    git(worktree, "commit", "-m", "base")
    git(worktree, "remote", "add", "origin", str(remote))
    git(worktree, "switch", "-c", "codex/CO-4-interruption-drain")
    (worktree / "handoff.txt").write_text("checkpointed after interruption\n")
    work_session = {
        "work_session_id": "worksession-co4",
        "task_id": "CO-4",
        "agent_id": "codex/CO-4-worker",
        "worktree_path": str(worktree),
        "status": "active",
        "hygiene": {"executed_test_run": {
            "schema": "switchboard.executed_test_run.v1",
            "executed": True, "status": "passed", "output_hash": "test-output-sha256",
        }},
    }
    checkpoint = co_drain.checkpoint_work_session(
        work_session, task_id="CO-4", request_id="drain-checkpoint-proof",
        workspace_root=workspace_root)
    replacement = TMP / "replacement"
    subprocess.run(
        ["git", "clone", "--branch", checkpoint.get("branch", ""), str(remote),
         str(replacement)], text=True, capture_output=True, check=True)
    ok(checkpoint.get("pushed") is True
       and checkpoint.get("head_sha") == checkpoint.get("remote_head_sha")
       and checkpoint.get("test_evidence_present") is True
       and (replacement / "handoff.txt").read_text() == "checkpointed after interruption\n",
       "replacement reconstructs the exact pushed branch, head, and test-evidence handoff")

    (worktree / ".env").write_text("OPENAI_API_KEY=must-not-checkpoint\n")
    refused = co_drain.checkpoint_work_session(
        work_session, task_id="CO-4", request_id="drain-secret-proof",
        workspace_root=workspace_root)
    ok(refused.get("error_code") == "checkpoint_secret_scan_failed"
       and refused.get("pushed") is False,
       "checkpoint fails closed when a credential-bearing path would be committed")
    (worktree / ".env").unlink()
    (worktree / "handoff.txt").write_text("final drain checkpoint\n")

    RUNTIME_ROOT.mkdir(exist_ok=True)
    (RUNTIME_ROOT / "codex-auth.json").write_text("opaque-personal-login")
    sequence = []
    host_updates = []
    runner_updates = []

    def supervisor(action, runner_id, options):
        sequence.append(action)
        if action == "snapshot":
            return {"last_snapshot": {
                "runner_session_id": runner_id, "task_id": "CO-4",
                "branch": "codex/CO-4-interruption-drain",
                "head_sha": git(worktree, "rev-parse", "HEAD"),
                "log_tail": "opaque-personal-login must stay local",
            }}
        return {"runner_session_id": runner_id, "status": "killed", "alive": False}

    def release(lease_id, reason):
        sequence.append("release")
        return {"state": "released", "lease_id": lease_id, "reason": reason}

    def publish(status, capacity):
        host_updates.append((status, capacity))
        return {"host_id": "host/i-co4", "status": status}

    drain_receipt = co_drain.drain_host(
        {
            "request_id": "drain-active-runner",
            "reason": "spot_interruption",
            "termination_kind": "ephemeral_instance",
            "requested_at": time.time(), "deadline": time.time() + 100,
        },
        inventory,
        runners=[{
            "runner_session_id": "runner-co4", "status": "running", "stale": False,
            "task_id": "CO-4", "claim_id": "claim-co4",
            "agent_id": "codex/CO-4-worker",
            "metadata": {
                "work_session_id": "worksession-co4",
                "credential_lease_id": "lease-secret-identifier",
                "provider": "openai-codex",
                "provider_account_id": "raw-account-must-not-report",
                "account_affinity_id": "affinity-safe-hash",
            },
        }],
        work_sessions=[work_session],
        supervisor=supervisor,
        release_lease=release,
        publish_host=publish,
        update_runner=runner_updates.append,
        workspace_root=workspace_root,
        runtime_root=RUNTIME_ROOT,
    )
    receipt_text = json.dumps(drain_receipt, sort_keys=True)
    runner_checkpoint = drain_receipt["runners"][0]["checkpoint"]
    ok(drain_receipt.get("status") == "drained"
       and sequence == ["snapshot", "lease_stop", "release"]
       and runner_checkpoint.get("pushed") is True
       and host_updates[-1][0] == "drained" and runner_updates,
       "active runner is snapshotted, interrupted, checkpointed, fenced, and reported")
    ok("lease-secret-identifier" not in receipt_text
       and "raw-account-must-not-report" not in receipt_text
       and "opaque-personal-login" not in receipt_text
       and not list(RUNTIME_ROOT.iterdir())
       and drain_receipt["runners"][0]["credential"]["account_affinity_id"]
       == "affinity-safe-hash",
       "drain report preserves redacted provider affinity with no credential or runtime residue")

    REQUEST_PATH.unlink(missing_ok=True)
    RECEIPT_PATH.unlink(missing_ok=True)
    persistent_request = co_drain.write_request({
        "request_id": "drain-persistent-host",
        "reason": "persistent_host_removal",
        "termination_kind": "persistent_host",
        "requested_at": time.time(), "deadline": time.time() + 100,
    })
    ok(persistent_request.get("termination_kind") == "persistent_host"
       and co_drain.inventory_for_drain(inventory)["policy"]["allow_work"] is False,
       "persistent Agent Host removal uses the same no-new-claims drain contract")
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print(f"\nCO-4 graceful drain: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
