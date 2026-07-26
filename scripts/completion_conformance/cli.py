#!/usr/bin/env python3
"""Completion Conformance operator CLI.

    python -m scripts.completion_conformance <subcommand> ...

Subcommands:

  validate         Validate every scenario.json under tests/conformance/scenarios
                   (or --dir) against the scenario.v1 schema subset.
  observe-fixture  Run the T2 observe tick (hermetic, fixture mode) for every
                   scenario and print a scoreboard. No network, no capacity.
  matrix-seed      Report undefined axis cells; with --emit DIR, generate draft
                   scenario files for them by asking the real classifier what
                   it decides on that world (never invents an outcome).
  reaper           Close PRs / delete branches / archive tasks tagged with a
                   run_id from a live T2 sweep. Prints a dry report if `gh`
                   is unavailable.

``observe-fixture`` and ``matrix-seed`` never touch the network. Live T2
sweeps (opening real sandbox PRs, running ``run_observe_tick_live``) are a
separate, explicit CLI-only path gated by ``CONFORMANCE_LIVE=1`` — see
``docs/COMPLETION-CONFORMANCE-OPERATOR.md``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
for _entry in (str(ROOT), str(SRC)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

DEFAULT_SCENARIO_DIR = ROOT / "tests" / "conformance" / "scenarios"


def _cmd_validate(args: argparse.Namespace) -> int:
    from tests.conformance._shared import load_scenarios

    scenario_dir = Path(args.dir)
    try:
        scenarios = load_scenarios(scenario_dir)
    except AssertionError as exc:
        print(f"INVALID  {exc}", file=sys.stderr)
        return 1
    for scenario in scenarios:
        print(f"OK  {scenario['id']}")
    print(f"\n{len(scenarios)} scenario(s) valid under {scenario_dir}")
    return 0


def _cmd_observe_fixture(args: argparse.Namespace) -> int:
    from tests.conformance._shared import load_scenarios
    from tests.conformance.observe.runner import run_observe_tick_fixture
    from tests.conformance.observe.scoreboard import build_row, summarize

    scenarios = load_scenarios(Path(args.dir))
    if args.scenario:
        scenarios = [s for s in scenarios if s["id"] == args.scenario]
        if not scenarios:
            print(f"no scenario named {args.scenario!r} under {args.dir}",
                  file=sys.stderr)
            return 1
    rows = []
    for scenario in scenarios:
        outcome = run_observe_tick_fixture(scenario)
        row = build_row(scenario, outcome["ticks"][0])
        row["receipts"] = outcome["receipts"][0]["effect"] if outcome["receipts"] else None
        rows.append(row)
        print(json.dumps(row, sort_keys=True))
    summary = summarize(rows)
    print("\nSCOREBOARD SUMMARY " + json.dumps(summary, sort_keys=True))
    return 0 if summary["fail"] == 0 and summary["timeout"] == 0 else 1


def _draft_world(cell: dict[str, Any]) -> dict[str, Any]:
    runner_role = cell["runner"]
    ci = cell["ci"]
    return {
        "draft": cell["draft"],
        "ci": ci,
        "ci_context": "Switchboard CI / VM gate",
        "ci_attribution": "infrastructure" if ci == "error" else "product",
        "review": cell["review"],
        "mergeable": cell["mergeability"],
        "merge_state_status": (
            "CLEAN"
            if ci == "pass" and cell["mergeability"] and cell["review"] == "passed"
            else "BLOCKED"
        ),
        "queue": cell["queue"],
        "runner": {
            "live": runner_role != "none",
            "role": runner_role,
            "head": "same" if runner_role != "none" else "none",
        },
    }


def _cmd_matrix_seed(args: argparse.Namespace) -> int:
    from switchboard.domain.completion.effects import plan_effect
    from switchboard.domain.completion.state_machine import classify_completion

    from tests.conformance._shared import build_snapshot, load_scenarios, terminal_for
    from tests.conformance.observe.coverage import coverage_report

    scenarios = load_scenarios(Path(args.dir))
    report = coverage_report(scenarios)
    print(f"coverage: defined={report['defined']} undefined={report['undefined']} "
          f"total={report['total']}")
    if not args.emit:
        for row in report["rows"]:
            if row["scenario_id"] is None:
                print("UNDEFINED " + json.dumps(row["cell"], sort_keys=True))
        return 0

    out_dir = Path(args.emit)
    out_dir.mkdir(parents=True, exist_ok=True)
    emitted = 0
    for row in report["rows"]:
        if row["scenario_id"] is not None:
            continue
        cell = row["cell"]
        world = _draft_world(cell)
        scenario_id = "gen_" + "_".join(
            str(cell[axis]).lower().replace(" ", "") for axis in cell
        )
        draft_scenario = {"id": scenario_id, "world": world}
        snapshot = build_snapshot(draft_scenario, task_id="COORD-65")
        decision = classify_completion({}, snapshot)
        plan = plan_effect(decision, snapshot, {})
        scenario_doc = {
            "schema": "switchboard.completion_conformance.scenario.v1",
            "id": scenario_id,
            "expect": {
                "terminal": terminal_for({"decision": decision, "plan": plan}),
                "reason_code": decision.get("reason_code") or "unknown",
                "role_sequence": (
                    [decision["desired_role"]]
                    if decision.get("desired_role") else []
                ),
                "route": decision.get("route") or "none",
                "effect": plan.get("effect") or "none",
            },
            "world": world,
            "timing": {
                "ci_delay_seconds": 0,
                "never_report": False,
                "hang_seconds": 0,
                "close_pr_while_runner_live": False,
                "move_head_during_assessment": False,
            },
        }
        path = out_dir / f"{scenario_id}.json"
        path.write_text(
            json.dumps(scenario_doc, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        emitted += 1
        print(f"EMIT  {path}")
    print(f"\n{emitted} draft scenario(s) written to {out_dir} -- "
          "review before adding to the merge-gated catalog.")
    return 0


def _cmd_reaper(args: argparse.Namespace) -> int:
    from tests.conformance.observe.reaper import reap

    result = reap(
        args.run_id, repo=args.repo, dry_run=args.dry_run,
        task_ids=list(args.task_id or []),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result.get("gh_available", True):
        return 0
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.completion_conformance",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="Validate scenario.json files.")
    validate.add_argument("--dir", default=str(DEFAULT_SCENARIO_DIR))
    validate.set_defaults(func=_cmd_validate)

    observe = sub.add_parser(
        "observe-fixture", help="Hermetic T2 observe tick + scoreboard.",
    )
    observe.add_argument("--dir", default=str(DEFAULT_SCENARIO_DIR))
    observe.add_argument("--scenario", default="", help="Run one scenario id only.")
    observe.set_defaults(func=_cmd_observe_fixture)

    matrix = sub.add_parser(
        "matrix-seed", help="Report/emit draft scenarios for undefined axis cells.",
    )
    matrix.add_argument("--dir", default=str(DEFAULT_SCENARIO_DIR))
    matrix.add_argument(
        "--emit", default="",
        help="Directory to write generated draft scenario files into.",
    )
    matrix.set_defaults(func=_cmd_matrix_seed)

    reaper = sub.add_parser(
        "reaper", help="Clean up gh artifacts + report task archival for a run_id.",
    )
    reaper.add_argument("--run-id", dest="run_id", required=True)
    reaper.add_argument("--repo", required=True,
                        help="owner/repo of the sandbox conformance repo.")
    reaper.add_argument("--task-id", dest="task_id", action="append", default=[])
    reaper.add_argument("--dry-run", action="store_true")
    reaper.set_defaults(func=_cmd_reaper)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
