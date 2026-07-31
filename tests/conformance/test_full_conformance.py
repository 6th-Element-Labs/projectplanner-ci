#!/usr/bin/env python3
"""Tier-3 orchestration contract: isolation, convergence scoreboard, hard cap."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SRC = ROOT / "src"
for _entry in (str(ROOT), str(SRC)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from tests.conformance.full.runner import (  # noqa: E402
    FullLoopConfig,
    FullLoopPorts,
    run_full_loop,
)


def scenario(name: str = "remediation") -> dict:
    return {
        "id": name,
        "expect": {
            "terminal": "merged",
            "role_sequence": ["remediation", "review_merge"],
        },
    }


class FullLoopContract(unittest.TestCase):
    def test_terminal_roles_generations_and_spend_are_reported(self):
        starts = []

        def start(value, run_id, project, repository):
            starts.append((value["id"], run_id, project, repository))
            return {"task_id": "CONF-1"}

        ports = FullLoopPorts(
            start=start,
            observe=lambda *_: {
                "terminal": "merged",
                "role_sequence": ["remediation", "review_merge"],
                "reason_codes": ["ci_failed", "ready"],
                "generation_count": 2,
                "spend_usd": 1.25,
            },
            stop_run=lambda *_: None,
            sleep=lambda _seconds: None,
        )
        result = run_full_loop(
            [scenario()],
            config=FullLoopConfig(run_id="nightly-1"),
            ports=ports,
        )
        self.assertEqual(result["summary"]["pass"], 1)
        self.assertEqual(result["spend_usd"], 1.25)
        self.assertEqual(result["rows"][0]["generation_count"], 2)
        self.assertEqual(starts[0][2], "conformance")
        self.assertEqual(
            starts[0][3], "6th-Element-Labs/switchboard-conformance",
        )

    def test_spend_cap_is_hard_and_stops_live_run(self):
        stopped = []
        ports = FullLoopPorts(
            start=lambda *_: {"task_id": "CONF-1"},
            observe=lambda *_: {"terminal": "", "spend_usd": 2.01},
            stop_run=lambda run_id, project: stopped.append((run_id, project)),
            sleep=lambda _seconds: None,
        )
        result = run_full_loop(
            [scenario()],
            config=FullLoopConfig(run_id="nightly-cap", max_spend_usd=2.0),
            ports=ports,
        )
        self.assertTrue(result["aborted"])
        self.assertIn("hard spend cap exceeded", result["error"])
        self.assertEqual(stopped, [("nightly-cap", "conformance")])

    def test_live_product_projects_and_large_runs_fail_closed(self):
        ports = FullLoopPorts(
            start=lambda *_: {},
            observe=lambda *_: {},
            stop_run=lambda *_: None,
            sleep=lambda *_: None,
        )
        with self.assertRaises(ValueError):
            run_full_loop(
                [scenario()],
                config=FullLoopConfig(run_id="bad", project="switchboard"),
                ports=ports,
            )
        with self.assertRaises(ValueError):
            run_full_loop(
                [scenario(str(i)) for i in range(13)],
                config=FullLoopConfig(run_id="too-many"),
                ports=ports,
            )

    def test_reaper_stops_live_runners_independently_of_pr_cleanup(self):
        from tests.conformance.observe.reaper import reap

        stopped = []
        result = reap(
            "nightly-1",
            repo="6th-Element-Labs/switchboard-conformance",
            gh_runner=lambda _args: {
                "returncode": 0, "stdout": "[]", "stderr": "",
            },
            list_run_runners=lambda _run_id: [
                {"runner_session_id": "run-live", "status": "running"},
                {"runner_session_id": "run-done", "status": "completed"},
            ],
            stop_runner=lambda runner_id: stopped.append(runner_id) or {"ok": True},
        )
        self.assertEqual(result["runner_session_ids"], ["run-live"])
        self.assertEqual(stopped, ["run-live"])
        self.assertTrue(result["cleanup_complete"])


if __name__ == "__main__":
    unittest.main()
