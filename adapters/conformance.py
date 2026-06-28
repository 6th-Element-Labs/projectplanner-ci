#!/usr/bin/env python3
"""Reusable P0 conformance fixture for Switchboard runtime adapters.

The first transport is a local StoreClient that runs against throwaway SQLite files. Runtime
packs can reuse ``run_p0_conformance`` with a REST/MCP client later; the checks stay the same.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parents[1]
P0_PROTOCOL = {
    "name": "switchboard-adapter",
    "version": "ixp.v1",
    "profile": "p0-dogfood",
    "profiles": {
        "ixp_core": "1.0",
        "txp_dispatch": "0.1",
        "oxp_tally": "0.1",
        "reconcile": "0.1",
    },
    "compatible_versions": ["ixp.v1"],
}


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ConformanceResult:
    adapter: str
    runtime: str
    project: str
    control_mode: str
    checks: List[CheckResult]
    capability_statement: Dict[str, Any]

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


def _git_head() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                           text=True, timeout=3)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _record(checks: List[CheckResult], name: str, condition: bool, detail: str,
            data: Optional[Dict[str, Any]] = None) -> None:
    checks.append(CheckResult(name=name, ok=bool(condition), detail=detail, data=data or {}))


class LocalStoreClient:
    """Reference conformance client backed by isolated Switchboard SQLite databases."""

    def __init__(self, project: str = "switchboard", adapter: str = "local-store",
                 runtime: str = "reference", keep_tmp: bool = False):
        self.project = project
        self.adapter = adapter
        self.runtime = runtime
        self.keep_tmp = keep_tmp
        self.tmpdir = tempfile.mkdtemp(prefix="switchboard-conformance-")
        self.agent_id = f"{runtime}/conformance"
        self.actor = "conformance"
        self.principal: Dict[str, Any] = {}
        self.store = None
        self.auth = None

    def __enter__(self) -> "LocalStoreClient":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def start(self) -> None:
        os.environ["PM_DB_PATH"] = os.path.join(self.tmpdir, "maxwell.db")
        os.environ["PM_HELM_DB_PATH"] = os.path.join(self.tmpdir, "helm.db")
        os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(self.tmpdir, "switchboard.db")
        os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(self.tmpdir, "project_registry.db")
        os.environ["PM_DYNAMIC_PROJECTS_DIR"] = self.tmpdir
        os.environ["PM_AUTH_MODE"] = "required"
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))

        import store  # noqa: WPS433
        import auth  # noqa: WPS433

        self.store = importlib.reload(store)
        self.auth = importlib.reload(auth)
        self.store.init_project_registry()
        self.store.init_db(self.project)
        head = _git_head()
        if head:
            self.store.update_canonical_main_sha(head, actor="conformance", project=self.project)
        self.principal = self.store.create_principal(
            kind="agent",
            display_name=self.agent_id,
            token="conformance-token",
            scopes=["read", "write:tasks", "write:ixp", "write:system"],
            principal_id="agent-conformance",
            project=self.project,
        )
        self.actor = self.auth.actor(self.principal)

    def close(self) -> None:
        if not self.keep_tmp:
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def invalid_token_rejected(self) -> bool:
        try:
            self.auth.authenticate(self.project, "", ("write:ixp",))
            return False
        except PermissionError:
            return True

    def authenticate(self) -> Dict[str, Any]:
        return self.auth.authenticate(self.project, "conformance-token", ("write:ixp",))

    def working_agreement(self) -> Dict[str, Any]:
        return self.store.get_working_agreement(project=self.project)

    def register_agent(self, control: Dict[str, Any], protocol: Dict[str, Any]) -> Dict[str, Any]:
        return self.store.register_agent(
            agent_id=self.agent_id,
            runtime=self.runtime,
            model="conformance-model",
            lane="CONF",
            control=control,
            protocol=protocol,
            principal_id=self.principal["id"],
            actor=self.actor,
            ttl_s=300,
            project=self.project,
        )

    def send_message(self, signal: str, message: str) -> Dict[str, Any]:
        return self.store.send_agent_message(
            from_agent="switchboard/operator",
            to_agent=self.agent_id,
            message=message,
            signal=signal,
            requires_ack=True,
            priority=10 if signal == "stop" else 1,
            principal_id=self.principal["id"],
            project=self.project,
        )

    def inbox(self) -> List[Dict[str, Any]]:
        return self.store.list_unacked_messages(self.agent_id, project=self.project)

    def ack(self, message_id: int, response: str) -> Dict[str, Any]:
        return self.store.ack_message(message_id, response=response, actor=self.agent_id,
                                      project=self.project)

    def create_task(self, title: str, depends_on: Optional[List[str]] = None) -> Dict[str, Any]:
        return self.store.create_task(
            {
                "workstream_id": "CONF",
                "workstream_name": "Conformance",
                "title": title,
                "description": "requires capabilities: python",
                "depends_on": depends_on or [],
                "risk_level": "Low",
            },
            actor=self.actor,
            project=self.project,
        )

    def delta(self, cursor: int = 0) -> Dict[str, Any]:
        return self.store.get_activity_delta(cursor, lane="CONF", project=self.project)

    def claim_resource(self, task_id: str) -> Dict[str, Any]:
        return self.store.claim_resources(
            agent_id=self.agent_id,
            resource_type="file",
            names=["adapters/conformance.py"],
            task_id=task_id,
            principal_id=self.principal["id"],
            actor=self.actor,
            idem_key="conformance-file-claim",
            project=self.project,
        )

    def release_resource(self, lease_id: str) -> Dict[str, Any]:
        return self.store.release_resource_lease(lease_id, actor=self.actor, project=self.project)

    def active_leases(self) -> List[Dict[str, Any]]:
        return self.store.list_active_resource_leases(project=self.project)

    def claim_next(self) -> Dict[str, Any]:
        return self.store.claim_next(
            agent_id=self.agent_id,
            lanes=["CONF"],
            capabilities=["python"],
            max_risk="medium",
            principal_id=self.principal["id"],
            actor=self.actor,
            ttl_seconds=300,
            idem_key="conformance-claim-next",
            project=self.project,
        )

    def report_usage(self, task_id: str, claim_id: str) -> Dict[str, Any]:
        return self.store.report_usage(
            source="agent_report",
            confidence="reported",
            task_id=task_id,
            claim_id=claim_id,
            agent_id=self.agent_id,
            runtime=self.runtime,
            model="conformance-model",
            prompt_tokens=100,
            completion_tokens=40,
            cost_usd=0.14,
            principal_id=self.principal["id"],
            request_id="conformance-usage",
            project=self.project,
        )

    def record_outcome(self, task_id: str, claim_id: str) -> Dict[str, Any]:
        return self.store.record_outcome(
            outcome_type="conformance",
            title="P0 adapter smoke completed",
            task_id=task_id,
            claim_id=claim_id,
            evidence={"fixture": "adapters/conformance.py"},
            actor=self.agent_id,
            project=self.project,
        )

    def verify_outcome(self, outcome_id: str) -> Dict[str, Any]:
        return self.store.verify_outcome(
            outcome_id,
            verifier="conformance",
            verification="fixture",
            evidence={"verified_by": "p0-conformance"},
            actor=self.agent_id,
            project=self.project,
        )

    def create_kpi(self) -> Dict[str, Any]:
        return self.store.create_kpi(
            name="adapter conformance",
            unit="pass",
            direction="increase",
            baseline_value=0,
            target_value=1,
            actor=self.agent_id,
            project=self.project,
        )

    def link_outcome_to_kpi(self, outcome_id: str, kpi_id: str) -> Dict[str, Any]:
        return self.store.link_outcome_to_kpi(
            outcome_id=outcome_id,
            kpi_id=kpi_id,
            contribution=1,
            contribution_unit="pass",
            confidence="measured",
            rationale="P0 conformance fixture verified the outcome",
            actor=self.agent_id,
            project=self.project,
        )

    def task_tally(self, task_id: str) -> Dict[str, Any]:
        return self.store.task_tally(task_id, project=self.project)

    def complete_claim(self, claim_id: str) -> Dict[str, Any]:
        return self.store.complete_claim(
            claim_id,
            evidence={
                "branch": "conformance/local",
                "head_sha": _git_head() or "conformance-head",
                "verification": "P0 adapter conformance fixture passed",
            },
            actor=self.agent_id,
            project=self.project,
        )

    def reconcile(self) -> Dict[str, Any]:
        return self.store.reconcile(project=self.project)


def run_p0_conformance(client: LocalStoreClient, control_mode: str = "advisory_poll") -> ConformanceResult:
    checks: List[CheckResult] = []
    control = {
        "mode": control_mode,
        "poll": True,
        "state_save": "fixture",
        "verified_by": "adapters/conformance.py:p0",
    }

    _record(checks, "auth.invalid_token_rejected", client.invalid_token_rejected(),
            "required auth rejects missing/invalid credentials")
    principal = client.authenticate()
    _record(checks, "auth.valid_token", principal.get("id") == client.principal["id"],
            "valid adapter token authenticates", {"principal_id": principal.get("id")})

    agreement = client.working_agreement()
    protocol = agreement.get("protocol") or {}
    _record(checks, "handshake.protocol_version", protocol.get("version") == "ixp.v1",
            "working agreement advertises ixp.v1", {"protocol": protocol})
    _record(checks, "handshake.profile", protocol.get("profile") == "p0-dogfood",
            "working agreement advertises the P0 dogfood profile")

    reg = client.register_agent(control=control, protocol=P0_PROTOCOL)
    compatibility = reg.get("protocol_compatibility") or {}
    _record(checks, "presence.register_agent", reg.get("agent_id") == client.agent_id,
            "adapter registers presence with stable agent id", {"agent_id": reg.get("agent_id")})
    _record(checks, "presence.control_fidelity", reg.get("control", {}).get("mode") == control_mode,
            "adapter advertises truthful control fidelity", {"control": reg.get("control")})
    _record(checks, "presence.protocol_compatibility", compatibility.get("compatible") is True,
            "server accepts adapter protocol envelope", compatibility)

    heads_up = client.send_message("heads_up", "startup inbox proof")
    stop = client.send_message("stop", "stop before doing more work")
    inbox = client.inbox()
    inbox_ids = {m.get("id") for m in inbox}
    _record(checks, "inbox.drain", {heads_up["id"], stop["id"]}.issubset(inbox_ids),
            "startup drain sees directed messages", {"message_ids": sorted(inbox_ids)})
    acked = [client.ack(m["id"], f"handled under {control_mode}") for m in inbox]
    _record(checks, "inbox.ack", all(a.get("acked_at") for a in acked),
            "adapter acks handled inbox messages")
    _record(checks, "signal.stop_handled", any(m.get("signal") == "stop" for m in inbox),
            "stop signal is surfaced for the adapter's advertised fidelity")

    cursor0 = client.delta(0).get("cursor", 0)
    first = client.create_task("ready adapter conformance task")
    second = client.create_task("dependent adapter conformance task", depends_on=[first["task_id"]])
    delta = client.delta(cursor0)
    _record(checks, "delta.cursor_advances", any(u.get("task_id") == first["task_id"]
                                                 for u in delta.get("updates", [])),
            "delta reports activity after the saved cursor", {"cursor": delta.get("cursor")})
    delta_empty = client.delta(delta.get("cursor", cursor0))
    _record(checks, "delta.cursor_reuse", delta_empty.get("updates") == [],
            "reusing the newest cursor returns no duplicate updates")

    lease = client.claim_resource(first["task_id"])
    _record(checks, "lease.claim", "lease_id" in lease,
            "adapter can claim a resource before work", lease)
    released = client.release_resource(lease.get("lease_id", ""))
    _record(checks, "lease.release", released.get("released") is True,
            "adapter can release a claimed resource", released)

    claim = client.claim_next()
    task = claim.get("task") or {}
    _record(checks, "txp.claim_next", claim.get("claimed") is True and
            task.get("task_id") == first["task_id"],
            "adapter can claim the next unblocked task", {"claim_id": claim.get("claim_id"),
                                                          "task_id": task.get("task_id")})
    _record(checks, "txp.dependency_guard", second["task_id"] != task.get("task_id"),
            "claim_next leaves dependent work unclaimed until dependencies are complete")

    usage = client.report_usage(first["task_id"], claim["claim_id"])
    _record(checks, "tally.usage_report", usage.get("total_tokens") == 140,
            "adapter reports usage into Tally", usage)
    outcome = client.record_outcome(first["task_id"], claim["claim_id"])
    verified = client.verify_outcome(outcome["id"])
    kpi = client.create_kpi()
    client.link_outcome_to_kpi(verified["id"], kpi["id"])
    tally = client.task_tally(first["task_id"])
    _record(checks, "tally.verified_outcome", tally.get("outcomes", {}).get("verified") == 1,
            "verified outcomes enter the Tally denominator", tally.get("outcomes", {}))
    _record(checks, "tally.kpi_link", bool(tally.get("kpis")) and
            tally["kpis"][0].get("verified_contribution") == 1.0,
            "verified outcomes can be linked to KPI movement", {"kpis": tally.get("kpis", [])})

    completed = client.complete_claim(claim["claim_id"])
    _record(checks, "txp.complete_claim", completed.get("status") == "In Review" and
            (completed.get("git_state") or {}).get("head_sha"),
            "adapter completes claims with evidence", completed)
    _record(checks, "exit.releases_task_lease",
            not any(l.get("task_id") == first["task_id"] for l in client.active_leases()),
            "claim completion releases the task lease")

    report = client.reconcile()
    _record(checks, "reconcile.runs", "findings" in report,
            "adapter smoke can run reconcile against the throwaway board")
    _record(checks, "reconcile.no_claim_drift",
            not any(f.get("task_id") == first["task_id"] for f in report.get("findings", [])),
            "completed conformance task has no reconcile drift", {"findings": report.get("findings", [])})

    core_prefixes = ("auth.", "handshake.", "presence.", "inbox.", "signal.", "delta.",
                     "lease.", "exit.")
    ixp_core_ok = all(c.ok for c in checks if c.name.startswith(core_prefixes))
    ok = all(c.ok for c in checks)
    capability = {
        "adapter": client.adapter,
        "runtime": client.runtime,
        "version": "0.1.0",
        "profile": "p0-dogfood",
        "ixp_core": ixp_core_ok,
        "control_mode": control_mode,
        "txp_claim_next": any(c.name == "txp.claim_next" and c.ok for c in checks),
        "tally_usage_report": any(c.name == "tally.usage_report" and c.ok for c in checks),
        "reconcile": any(c.name == "reconcile.runs" and c.ok for c in checks),
        "checks_passed": sum(1 for c in checks if c.ok),
        "checks_failed": sum(1 for c in checks if not c.ok),
        "verified_at": _iso_now(),
    }
    return ConformanceResult(client.adapter, client.runtime, client.project, control_mode,
                             checks, capability)


def print_result(result: ConformanceResult, json_only: bool = False) -> None:
    if json_only:
        print(json.dumps(result.capability_statement, indent=2, sort_keys=True))
        return
    for check in result.checks:
        label = "PASS" if check.ok else "FAIL"
        print(f"  {label:<4}  {check.name}: {check.detail}")
    print("\nCapability statement:")
    print(json.dumps(result.capability_statement, indent=2, sort_keys=True))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run Switchboard P0 adapter conformance")
    parser.add_argument("--adapter", default="local-store")
    parser.add_argument("--runtime", default="reference")
    parser.add_argument("--project", default="switchboard")
    parser.add_argument("--control-mode", default="advisory_poll",
                        choices=["observe_only", "advisory_poll", "hook_deny",
                                 "runner_kill", "managed"])
    parser.add_argument("--keep-tmp", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    with LocalStoreClient(project=args.project, adapter=args.adapter, runtime=args.runtime,
                          keep_tmp=args.keep_tmp) as client:
        result = run_p0_conformance(client, control_mode=args.control_mode)
        print_result(result, json_only=args.json)
        if args.keep_tmp and not args.json:
            print(f"\nTemporary DB directory kept at: {client.tmpdir}")
        return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
