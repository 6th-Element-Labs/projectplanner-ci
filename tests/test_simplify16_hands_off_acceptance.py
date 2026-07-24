#!/usr/bin/env python3
"""SIMPLIFY-16: machine-check the recorded four-task hands-off proof."""
from __future__ import annotations

import inspect
import json

from path_setup import ROOT

import coordinator_daemon

EVIDENCE = ROOT / "docs/evidence/SIMPLIFY-16-hands-off-run.json"


def ok(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


proof = json.loads(EVIDENCE.read_text())
ok(proof["evidence_mode"] == "hermetic_acceptance_fixture", "evidence mode")
tasks = proof["tasks"]
ok(len(tasks) == 4, "four-task acceptance corpus")
ok(
    sorted(task["path"] for task in tasks)
    == ["clean", "clean", "red_ci", "review_correction"],
    "task path mix",
)

execution_ids: set[str] = set()
for task in tasks:
    implementation = task["implementation"]
    ok(implementation["role"] == "implementation", f"{task['path']} impl role")
    ok(
        implementation["terminal_ack_sequence"] < implementation["in_review_sequence"],
        f"{task['path']} hard handoff before In Review",
    )
    for signal in (
        "pid_terminal",
        "pty_terminal",
        "token_rejected",
        "lease_terminal",
        "fleet_live_absent",
        "direct_session_authority_terminal",
    ):
        ok(implementation[signal] is True, f"{task['path']} {signal}")

    generations = [implementation, *task["role_generations"]]
    ok(
        [row["generation"] for row in generations]
        == list(range(1, len(generations) + 1)),
        f"{task['path']} contiguous generations",
    )
    ok(
        any(row["role"] == "review_merge" for row in task["role_generations"]),
        f"{task['path']} review_merge generation",
    )
    ok(
        all(row["execution_id"] not in execution_ids for row in generations),
        f"{task['path']} unique execution ids",
    )
    execution_ids.update(row["execution_id"] for row in generations)

    heads = task["heads"]
    ok(heads["pr"] == heads["ci"] == heads["verdict"], f"{task['path']} exact head")
    ok(len(heads["merge_group"]) == 40, f"{task['path']} merge-group sha")
    ok(len(heads["canonical_merged"]) == 40, f"{task['path']} canonical sha")
    ok(task["done_provenance"] == "github_pr_merged", f"{task['path']} done provenance")
    ok(task["merge_queue_entries"] == 1, f"{task['path']} single merge-queue entry")

    if task["path"] in {"red_ci", "review_correction"}:
        ok(task["remediation_rounds"] == 1, f"{task['path']} one remediation round")
        ok(task["prior_head"] != heads["pr"], f"{task['path']} head changed")
        ok(
            any(row["role"] == "remediation" for row in task["role_generations"]),
            f"{task['path']} remediation generation",
        )

ok(
    set(row["component"] for row in proof["restarts"])
    == {"coordinator", "agent_host"},
    "restart components",
)
ok(
    all(row.get("duplicate_dispatches", 0) == 0 for row in proof["restarts"]),
    "no duplicate dispatches after restart",
)
ok(
    all(row.get("duplicate_merge_queue_entries", 0) == 0 for row in proof["restarts"]),
    "no duplicate merge-queue entries after restart",
)
ok(all(value == 0 for value in proof["manual_actions"].values()), "zero manual actions")

scope = proof["scope_authority"]
ok(scope["max_concurrent_leases"] >= 2, "concurrent leases")
ok(scope["canonical_registry"] == "autopilot_scopes", "canonical registry")
ok(scope["exact_holder_fence_generation_enforced"] is True, "holder fence")
ok(scope["out_of_scope_refusals"] >= 2, "out-of-scope refusals")
ok(scope["out_of_scope_side_effects"] == 0, "no out-of-scope side effects")
ok(scope["race_safe_takeovers"] >= 1, "race-safe takeovers")
ok(scope["closed_scope_authority_released"] is True, "closed scope released")
ok(scope["restart_resumed_only_durable_scopes"] is True, "durable scopes only")

messaging = proof["messaging"]
ok(
    set(messaging["cases"])
    == {
        "ack_timeout",
        "duplicate_timeout",
        "late_ack",
        "stale_recipient_generation",
        "unavailable_target",
        "monitor_restart",
    },
    "messaging cases",
)
ok(messaging["audit_facts"] == 1, "audit facts")
ok(messaging["sender_notifications"] == 1, "sender notifications")
ok(messaging["operator_findings"] == 1, "operator findings")
for field in (
    "wake_effects",
    "restart_effects",
    "supersession_effects",
    "retry_effects",
    "lease_mutations",
    "process_stops",
    "nonterminal_monitors",
):
    ok(messaging[field] == 0, f"messaging {field} is zero")

census = coordinator_daemon.CoordinatorDaemon._drain_lifecycle(None, "switchboard")[
    "action_census"
]
required_zero = {
    "start_task",
    "review",
    "remediation",
    "merge",
    "retry",
    "message",
    "send_agent_message",
    "work_instruction",
    "acknowledgement_monitor",
    "agent_directed_message",
}
ok(required_zero <= census.keys(), "janitor census publishes messaging zeros")
ok(all(census[action] == 0 for action in required_zero), "live janitor census zeros")

recorded = proof["janitor_action_census"]
ok(all(recorded[action] == 0 for action in required_zero), "recorded janitor census zeros")
source = inspect.getsource(coordinator_daemon.CoordinatorDaemon)
ok("send_agent_message(" not in source, "janitor source has no send_agent_message calls")

print("SIMPLIFY-16 hands-off acceptance proof passed")
