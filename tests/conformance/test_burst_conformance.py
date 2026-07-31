#!/usr/bin/env python3
"""COORD-68: completion-conformance concurrency invariants."""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SRC = ROOT / "src"
for _entry in (str(ROOT), str(SRC)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from scripts.completion_conformance.github_sandbox import (  # noqa: E402
    open_scenario_prs_concurrently,
)
from tests.conformance.observe.burst import (  # noqa: E402
    BurstCase,
    burst_findings,
    run_burst,
)


def cases(count: int = 40) -> list[BurstCase]:
    return [
        BurstCase(
            case_id=f"CONF-{index:03}",
            scenario={"id": f"burst-{index:03}"},
        )
        for index in range(count)
    ]


class ObserveBurst(unittest.TestCase):
    def test_forty_start_together_without_spending_capacity(self):
        lock = threading.Lock()
        inside = threading.Barrier(40)
        active = 0
        peak = 0

        def worker(case):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            inside.wait()
            with lock:
                active -= 1
            return {
                "task_id": case["case_id"],
                "boots_requested": 1,
                "boots_admitted": 0,
                "boots_completed": 0,
                "coordinator_tick_ids": [f"tick-{case['case_id']}"],
            }

        scoreboard = run_burst(
            cases(),
            run_id="burst-observe",
            worker=worker,
            canary_probe=lambda: True,
        )
        self.assertTrue(scoreboard["passed"])
        self.assertEqual(scoreboard["case_count"], 40)
        self.assertEqual(scoreboard["boots_requested"], 40)
        self.assertEqual(scoreboard["boots_admitted"], 0)
        self.assertEqual(scoreboard["duplicate_boot_count"], 0)
        self.assertEqual(scoreboard["orphaned_runner_count"], 0)
        self.assertTrue(scoreboard["post_burst_canary_booted"])
        self.assertEqual(peak, 40)

    def test_full_mode_requires_budget_and_watching_operator(self):
        with self.assertRaises(RuntimeError):
            run_burst(
                cases(1), run_id="unsafe", worker=lambda _case: {},
                canary_probe=lambda: True, mode="full",
            )
        with self.assertRaises(RuntimeError):
            run_burst(
                cases(1), run_id="unwatched", worker=lambda _case: {},
                canary_probe=lambda: True, mode="full", capacity_budget=1,
            )


class BurstFailuresStayRed(unittest.TestCase):
    def test_duplicate_race_orphan_budget_and_wedge_are_reported(self):
        scoreboard = run_burst(
            cases(1),
            run_id="bad-burst",
            mode="full",
            capacity_budget=1,
            operator_watching=True,
            worker=lambda case: {
                "task_id": case["case_id"],
                "boots_requested": 2,
                "boots_admitted": 2,
                "boots_completed": 0,
                "runner_ids": ["run-a", "run-b"],
                "coordinator_tick_ids": ["tick-a", "tick-b"],
                "live_runner_ids": ["run-b"],
                "failed_dispatch": True,
            },
            canary_probe=lambda: False,
            assert_invariants=False,
        )
        self.assertEqual(
            burst_findings(scoreboard),
            [
                "capacity_budget_exceeded",
                "duplicate_boot",
                "raced_coordinator_tick",
                "orphaned_runner",
                "failed_dispatch_left_live_runner",
                "post_burst_canary_failed",
            ],
        )
        self.assertFalse(scoreboard["passed"])

    def test_worker_exception_is_preserved(self):
        scoreboard = run_burst(
            cases(1),
            run_id="dispatch-failed",
            worker=lambda _case: (_ for _ in ()).throw(RuntimeError("spawn failed")),
            canary_probe=lambda: True,
            assert_invariants=False,
        )
        self.assertEqual(burst_findings(scoreboard), ["worker_error"])
        self.assertIn("spawn failed", scoreboard["errors"][0]["error"])


class ConcurrentSandboxOpening(unittest.TestCase):
    def test_requires_distinct_checkouts(self):
        scenarios = [{"id": "one"}, {"id": "two"}]
        with self.assertRaises(ValueError):
            open_scenario_prs_concurrently(
                scenarios,
                repo="example/conformance",
                run_id="run-1",
                checkout_dirs=["/tmp/same", "/tmp/same"],
            )

    def test_distinct_checkouts_open_in_parallel(self):
        scenarios = [{"id": "one"}, {"id": "two"}]
        first_command = threading.Barrier(2)
        lock = threading.Lock()
        first_calls = 0

        def runner(args, cwd=None):
            nonlocal first_calls
            if args[:2] == ["git", "checkout"] and args[-1] == "main":
                with lock:
                    first_calls += 1
                first_command.wait()
            if args[:3] == ["gh", "pr", "create"]:
                scenario_id = "one" if cwd.endswith("one") else "two"
                number = 1 if scenario_id == "one" else 2
                return {
                    "returncode": 0,
                    "stdout": f"https://github.test/example/conformance/pull/{number}",
                    "stderr": "",
                }
            return {"returncode": 0, "stdout": "", "stderr": ""}

        with tempfile.TemporaryDirectory() as root:
            checkout_dirs = [str(Path(root) / "one"), str(Path(root) / "two")]
            for checkout_dir in checkout_dirs:
                Path(checkout_dir).mkdir()
            opened = open_scenario_prs_concurrently(
                scenarios,
                repo="example/conformance",
                run_id="run-1",
                checkout_dirs=checkout_dirs,
                concurrency=2,
                runner=runner,
            )
        self.assertEqual(first_calls, 2)
        self.assertEqual([item.scenario_id for item in opened], ["one", "two"])


if __name__ == "__main__":
    unittest.main()
