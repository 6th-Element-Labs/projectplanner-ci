"""Open N scenario branches/PRs on the sandbox conformance repo, tagged with run_id.

Never targets the product canonical repo — isolation rule #2 in the harness
design doc ("Conformance never opens PRs against the product canonical
repo."). ``repo`` must always be passed explicitly by the caller; nothing in
this module defaults to a product repo constant.

Each opened PR:

- lives on branch ``conformance/{run_id}/{scenario_id}`` — the convention
  ``tests.conformance.observe.reaper`` searches for during cleanup;
- carries ``scenario.json`` at the repo root of that branch;
- has a body embedding the same JSON plus a ``run_id:`` tag line, grep-able
  by ``gh pr list --search`` for anyone auditing a run.

Required-status-context bound (see ``docs/COMPLETION-CONFORMANCE-OPERATOR.md``):
the sandbox repo's branch protection is expected to require exactly ONE
context, ``"Switchboard Conformance / scenario"``. The obedient workflow reads
the checked-out ``scenario.json`` (``world`` / ``timing``) and reports that one
context accordingly (pass/fail/hang/never-report). This module only opens the
PR; it does not configure branch protection or write the workflow file.

This module shells out to `git`/`gh` and is CLI-only — it is not imported by
any test and is never invoked in CI.
"""
from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional


GhRunner = Callable[[list[str], Optional[str]], dict[str, Any]]


def _default_runner(args: list[str], cwd: Optional[str] = None) -> dict[str, Any]:
    proc = subprocess.run(
        args, text=True, capture_output=True, check=False, cwd=cwd, timeout=60,
    )
    return {
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
    }


@dataclass
class OpenedScenarioPR:
    scenario_id: str
    branch: str
    number: int = 0
    url: str = ""


def branch_name(run_id: str, scenario_id: str) -> str:
    return f"conformance/{run_id}/{scenario_id}"


def open_scenario_pr(
    scenario: dict[str, Any],
    *,
    repo: str,
    run_id: str,
    checkout_dir: str,
    base: str = "main",
    runner: Optional[GhRunner] = None,
) -> OpenedScenarioPR:
    """Commit ``scenario.json`` to a fresh branch and open a sandbox PR.

    ``checkout_dir`` must already be a clean clone of ``repo`` — this function
    does not clone; callers own repo/clone lifecycle so a matrix run reuses
    one checkout across many scenarios.
    """
    run = runner or _default_runner
    branch = branch_name(run_id, scenario["id"])

    def git(*args: str) -> dict[str, Any]:
        return run(["git", *args], checkout_dir)

    git("checkout", base)
    git("pull", "--ff-only")
    git("checkout", "-b", branch)
    scenario_path = Path(checkout_dir) / "scenario.json"
    scenario_path.write_text(
        json.dumps(scenario, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    git("add", "scenario.json")
    git("commit", "-m", f"conformance: {scenario['id']} (run_id:{run_id})")
    git("push", "-u", "origin", branch)
    body = (
        f"run_id:{run_id}\n\n"
        "Automated Completion Conformance T2 scenario PR. Do not merge by "
        "hand — the reaper closes this once the run finishes.\n\n"
        "```json\n" + json.dumps(scenario, indent=2, sort_keys=True) + "\n```\n"
    )
    created = run([
        "gh", "pr", "create", "--repo", repo, "--base", base, "--head", branch,
        "--title", f"[conformance] {scenario['id']} ({run_id})",
        "--body", body,
    ], checkout_dir)
    number = 0
    url = ""
    stdout = created.get("stdout") or ""
    tail = stdout.rsplit("/", 1)[-1]
    if tail.isdigit():
        number = int(tail)
        url = stdout
    return OpenedScenarioPR(
        scenario_id=scenario["id"], branch=branch, number=number, url=url,
    )


def open_scenario_prs(
    scenarios: list[dict[str, Any]],
    *,
    repo: str,
    run_id: str,
    checkout_dir: str,
    base: str = "main",
    runner: Optional[GhRunner] = None,
) -> list[OpenedScenarioPR]:
    return [
        open_scenario_pr(
            scenario, repo=repo, run_id=run_id, checkout_dir=checkout_dir,
            base=base, runner=runner,
        )
        for scenario in scenarios
    ]


def open_scenario_prs_concurrently(
    scenarios: list[dict[str, Any]],
    *,
    repo: str,
    run_id: str,
    checkout_dirs: list[str],
    base: str = "main",
    concurrency: int = 40,
    runner: Optional[GhRunner] = None,
) -> list[OpenedScenarioPR]:
    """Open a burst using one clean checkout per scenario.

    Sharing a checkout across concurrent branch creation races git's index and
    HEAD.  Requiring distinct checkout paths makes that unsafe state
    unrepresentable and lets the caller reuse the T2 run-id/reaper convention.
    """
    if len(checkout_dirs) != len(scenarios):
        raise ValueError("checkout_dirs must contain one path per scenario")
    if len(set(checkout_dirs)) != len(checkout_dirs):
        raise ValueError("concurrent scenario PRs require distinct checkout_dirs")
    if concurrency < 1:
        raise ValueError("concurrency must be positive")
    opened: list[OpenedScenarioPR] = []
    with ThreadPoolExecutor(
        max_workers=min(concurrency, len(scenarios)) if scenarios else 1,
        thread_name_prefix="conformance-pr",
    ) as pool:
        futures = [
            pool.submit(
                open_scenario_pr,
                scenario,
                repo=repo,
                run_id=run_id,
                checkout_dir=checkout_dir,
                base=base,
                runner=runner,
            )
            for scenario, checkout_dir in zip(scenarios, checkout_dirs)
        ]
        for future in as_completed(futures):
            opened.append(future.result())
    return sorted(opened, key=lambda item: item.scenario_id)


__all__ = [
    "OpenedScenarioPR",
    "branch_name",
    "open_scenario_pr",
    "open_scenario_prs",
    "open_scenario_prs_concurrently",
]
