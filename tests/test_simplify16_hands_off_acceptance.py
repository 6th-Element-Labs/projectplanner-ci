#!/usr/bin/env python3
"""SIMPLIFY-16: machine-check the recorded four-task hands-off proof."""
from __future__ import annotations

import inspect
import json

from path_setup import ROOT

import coordinator_daemon  # noqa: E402

EVIDENCE = ROOT / "docs/evidence/SIMPLIFY-16-hands-off-run.json"

passed = failed = 0


def check(condition: bool, message: str) -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {message}")
    else:
        failed += 1
        print(f"  FAIL  {message}")


proof = json.loads(EVIDENCE.read_text())
check(proof["evidence_mode"] == "hermetic_acceptance_fixture",
      "evidence is the hermetic acceptance fixture")
tasks = proof["tasks"]
check(len(tasks) == 4, "four-task restart proof is recorded")
check(sorted(task["path"] for task in tasks) == [
    "clean", "clean", "red_ci", "review_correction",
], "paths cover two clean, red-CI, and review-correction")

execution_ids: set[str] = set()
for task in tasks:
    implementation = task["implementation"]
    check(implementation["role"] == "implementation",
          f"{task['task_id']} starts with an implementation generation")
    check(implementation["terminal_ack_sequence"] < implementation["in_review_sequence"],
          f"{task['task_id']} hard-handoffs before In Review")
    check(all(implementation[signal] is True for signal in (
        "pid_terminal", "pty_terminal", "token_rejected", "lease_terminal",
        "fleet_live_absent", "direct_session_authority_terminal",
    )), f"{task['task_id']} implementation is fully terminal before handoff")

    generations = [implementation, *task["role_generations"]]
    check([row["generation"] for row in generations] == list(
        range(1, len(generations) + 1)
    ), f"{task['task_id']} role generations are fresh and ordered")
    check(any(row["role"] == "review_merge" for row in task["role_generations"]),
          f"{task['task_id']} includes a review/merge generation")
    check(all(row["execution_id"] not in execution_ids for row in generations),
          f"{task['task_id']} execution ids are unique across the run")
    execution_ids.update(row["execution_id"] for row in generations)

    heads = task["heads"]
    check(heads["pr"] == heads["ci"] == heads["verdict"],
          f"{task['task_id']} exact-head gates stay aligned")
    check(len(heads["merge_group"]) == 40 and len(heads["canonical_merged"]) == 40,
          f"{task['task_id']} merge/canonical heads are recorded")
    check(task["done_provenance"] == "github_pr_merged"
          and task["merge_queue_entries"] == 1,
          f"{task['task_id']} Done provenance is one merge-queue merge")

    if task["path"] in {"red_ci", "review_correction"}:
        check(task["remediation_rounds"] == 1
              and task["prior_head"] != heads["pr"]
              and any(row["role"] == "remediation"
                      for row in task["role_generations"]),
              f"{task['task_id']} remediation recovers on a new exact head")

check(set(row["component"] for row in proof["restarts"]) == {
    "coordinator", "agent_host",
}, "coordinator and Agent Host restart recovery are recorded")
check(all(row.get("duplicate_dispatches", 0) == 0 for row in proof["restarts"])
      and all(row.get("duplicate_merge_queue_entries", 0) == 0
              for row in proof["restarts"]),
      "restarts do not duplicate dispatch or merge-queue work")
check(all(value == 0 for value in proof["manual_actions"].values()),
      "zero manual actions across the four-task proof")

scope = proof["scope_authority"]
check(scope["max_concurrent_leases"] >= 2
      and scope["canonical_registry"] == "autopilot_scopes"
      and scope["exact_holder_fence_generation_enforced"] is True
      and scope["out_of_scope_refusals"] >= 2
      and scope["out_of_scope_side_effects"] == 0
      and scope["race_safe_takeovers"] >= 1
      and scope["closed_scope_authority_released"] is True
      and scope["restart_resumed_only_durable_scopes"] is True,
      "scope fencing and restart resume stay authority-safe")

messaging = proof["messaging"]
check(set(messaging["cases"]) == {
    "ack_timeout", "duplicate_timeout", "late_ack",
    "stale_recipient_generation", "unavailable_target", "monitor_restart",
}, "timeout isolation matrix is complete")
check(messaging["audit_facts"] == 1
      and messaging["sender_notifications"] == 1
      and messaging["operator_findings"] == 1
      and all(messaging[field] == 0 for field in (
          "wake_effects", "restart_effects", "supersession_effects",
          "retry_effects", "lease_mutations", "process_stops",
          "nonterminal_monitors",
      )), "messaging records facts without execution side effects")

census = coordinator_daemon.CoordinatorDaemon._drain_lifecycle(None, "switchboard")[
    "action_census"
]
required_zero = {
    "start_task", "review", "remediation", "merge", "retry", "message",
    "send_agent_message", "work_instruction", "acknowledgement_monitor",
    "agent_directed_message",
}
check(required_zero <= census.keys()
      and all(census[action] == 0 for action in required_zero),
      "janitor publishes explicit zero messaging census")
recorded = proof["janitor_action_census"]
check(all(recorded[action] == 0 for action in required_zero),
      "recorded evidence matches the zero messaging census")
source = inspect.getsource(coordinator_daemon.CoordinatorDaemon)
check("send_agent_message(" not in source,
      "janitor source has no send_agent_message call site")

print(f"\nSIMPLIFY-16 hands-off acceptance: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
