"""Curated, hard-budgeted Tier-3 completion conformance orchestration.

The ports are deliberately external.  Production callers bind them to the
``conformance`` project and sandbox repository; unit tests bind deterministic
doubles.  This keeps the merge gate hermetic without inventing a second
completion driver.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from threading import Event, Lock
from time import monotonic
from typing import Any, Callable, Mapping, Sequence


LIVE_PROJECT = "conformance"
SANDBOX_REPOSITORY = "6th-Element-Labs/switchboard-conformance"
FORBIDDEN_PROJECTS = frozenset({"switchboard", "atlas"})
TERMINALS = frozenset({"merged", "blocked", "human", "reconcile_done"})
ROW_SCHEMA = "switchboard.completion_full_scoreboard_row.v1"
RUN_SCHEMA = "switchboard.completion_full_run.v1"


class SpendEnvelopeExceeded(RuntimeError):
    """Raised when another boot or poll would cross the hard run envelope."""


@dataclass(frozen=True)
class FullLoopConfig:
    run_id: str
    project: str = LIVE_PROJECT
    repository: str = SANDBOX_REPOSITORY
    max_scenarios: int = 12
    max_concurrent_boots: int = 2
    max_spend_usd: float = 25.0
    wall_clock_s: float = 3600.0
    poll_interval_s: float = 5.0

    def validate(self) -> None:
        if not self.run_id.strip():
            raise ValueError("run_id is required")
        if self.project in FORBIDDEN_PROJECTS or self.project != LIVE_PROJECT:
            raise ValueError("T3 may run only on project='conformance'")
        if self.repository != SANDBOX_REPOSITORY:
            raise ValueError(f"T3 may target only {SANDBOX_REPOSITORY}")
        if not 1 <= self.max_scenarios <= 12:
            raise ValueError("max_scenarios must be between 1 and 12")
        if not 1 <= self.max_concurrent_boots <= 2:
            raise ValueError("max_concurrent_boots must be between 1 and 2")
        if self.max_spend_usd <= 0:
            raise ValueError("max_spend_usd must be positive")
        if self.wall_clock_s <= 0 or self.poll_interval_s < 0:
            raise ValueError("wall-clock and poll settings must be non-negative")


@dataclass(frozen=True)
class FullLoopPorts:
    """Bindings to the existing capacity/coordination surfaces.

    ``start`` must enter capacity through ``start_task`` and return a task id.
    ``observe`` returns terminal/roles/generation/spend truth for that task.
    ``stop_run`` fences/stops every live runner tagged with the run id.
    """

    start: Callable[[Mapping[str, Any], str, str, str], Mapping[str, Any]]
    observe: Callable[[str, str], Mapping[str, Any]]
    stop_run: Callable[[str, str], Any]
    sleep: Callable[[float], None]


class _Spend:
    def __init__(self, cap: float) -> None:
        self.cap = cap
        self.total = 0.0
        self._lock = Lock()

    def record(self, amount: float) -> float:
        if amount < 0:
            raise ValueError("observed spend cannot be negative")
        with self._lock:
            if self.total + amount > self.cap:
                raise SpendEnvelopeExceeded(
                    f"hard spend cap exceeded: {self.total + amount:.4f} > "
                    f"{self.cap:.4f} USD"
                )
            self.total += amount
            return self.total


def _expected_roles(scenario: Mapping[str, Any]) -> list[str]:
    return list((scenario.get("expect") or {}).get("role_sequence") or [])


def _row(
    scenario: Mapping[str, Any],
    observation: Mapping[str, Any],
    *,
    duration_s: float,
) -> dict[str, Any]:
    expect = dict(scenario.get("expect") or {})
    terminal = str(observation.get("terminal") or "")
    roles = list(observation.get("role_sequence") or [])
    expected_terminal = str(expect.get("terminal") or "")
    expected_roles = _expected_roles(scenario)
    human = None
    if terminal == "human":
        human = (
            "expected_human"
            if expected_terminal == "human"
            else "unexpected_human"
        )
    status = "pass"
    if terminal == "timeout":
        status = "timeout"
    elif terminal != expected_terminal or roles != expected_roles:
        status = "fail"
    return {
        "schema": ROW_SCHEMA,
        "scenario_id": scenario.get("id"),
        "expected_terminal": expected_terminal,
        "actual_terminal": terminal,
        "human_classification": human,
        "expected_role_sequence": expected_roles,
        "role_sequence": roles,
        "reason_codes": list(observation.get("reason_codes") or []),
        "generation_count": int(observation.get("generation_count") or 0),
        "spend_usd": float(observation.get("spend_usd") or 0.0),
        "duration_s": duration_s,
        "status": status,
    }


def run_full_loop(
    scenarios: Sequence[Mapping[str, Any]],
    *,
    config: FullLoopConfig,
    ports: FullLoopPorts,
) -> dict[str, Any]:
    """Run the curated subset and kill the run on timeout or budget breach."""
    config.validate()
    selected = list(scenarios)
    if not selected:
        raise ValueError("at least one curated scenario is required")
    if len(selected) > config.max_scenarios:
        raise ValueError("scenario count exceeds the curated run limit")
    ids = [str(row.get("id") or "") for row in selected]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("scenario ids must be present and unique")

    budget = _Spend(config.max_spend_usd)
    abort = Event()
    started = monotonic()

    def execute(scenario: Mapping[str, Any]) -> dict[str, Any]:
        scenario_started = monotonic()
        try:
            if abort.is_set():
                return _row(scenario, {"terminal": "timeout"}, duration_s=0.0)
            receipt = dict(ports.start(
                scenario, config.run_id, config.project, config.repository,
            ))
            task_id = str(receipt.get("task_id") or "")
            if not task_id:
                raise RuntimeError("start_task receipt omitted task_id")
            last_spend = 0.0
            while not abort.is_set():
                elapsed = monotonic() - started
                if elapsed >= config.wall_clock_s:
                    abort.set()
                    return _row(
                        scenario,
                        {"terminal": "timeout", "spend_usd": last_spend},
                        duration_s=monotonic() - scenario_started,
                    )
                observation = dict(ports.observe(task_id, config.project))
                observed_spend = float(observation.get("spend_usd") or 0.0)
                budget.record(observed_spend - last_spend)
                last_spend = observed_spend
                terminal = str(observation.get("terminal") or "")
                if terminal in TERMINALS:
                    return _row(
                        scenario, observation,
                        duration_s=monotonic() - scenario_started,
                    )
                ports.sleep(config.poll_interval_s)
            return _row(
                scenario, {"terminal": "timeout", "spend_usd": last_spend},
                duration_s=monotonic() - scenario_started,
            )
        except Exception:
            abort.set()
            raise

    rows: list[dict[str, Any]] = []
    error = ""
    try:
        with ThreadPoolExecutor(
            max_workers=config.max_concurrent_boots,
            thread_name_prefix="conformance-full",
        ) as pool:
            futures = {pool.submit(execute, scenario): scenario for scenario in selected}
            for future in as_completed(futures):
                rows.append(future.result())
    except SpendEnvelopeExceeded as exc:
        abort.set()
        error = str(exc)
    except Exception as exc:
        abort.set()
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if abort.is_set() or error:
            ports.stop_run(config.run_id, config.project)

    rows.sort(key=lambda row: str(row["scenario_id"]))
    summary = {
        "pass": sum(row["status"] == "pass" for row in rows),
        "fail": sum(row["status"] == "fail" for row in rows),
        "timeout": sum(row["status"] == "timeout" for row in rows),
        "unexpected_human": sum(
            row["human_classification"] == "unexpected_human" for row in rows
        ),
        "over_generation": sum(
            row["generation_count"] > len(row["expected_role_sequence"])
            for row in rows
        ),
    }
    return {
        "schema": RUN_SCHEMA,
        "run_id": config.run_id,
        "project": config.project,
        "repository": config.repository,
        "spend_cap_usd": config.max_spend_usd,
        "spend_usd": budget.total,
        "aborted": bool(error),
        "error": error or None,
        "rows": rows,
        "summary": summary,
    }


__all__ = [
    "FullLoopConfig", "FullLoopPorts", "SpendEnvelopeExceeded", "run_full_loop",
]
