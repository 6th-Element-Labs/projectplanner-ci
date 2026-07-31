"""``run_observe_tick``: drive ``run_completion_tick`` with observe-cut adapters.

Fixture mode (default, hermetic, merge-gate safe): builds a snapshot from a
``scenario.json`` world using the same builder T1 uses
(``tests.conformance._shared.build_snapshot``) and patches the same storage
seams T1 patches (see ``hermetic_completion_patches``). No socket, no `gh`.

Live mode (CLI only, gated behind ``CONFORMANCE_LIVE=1``): points the *real*
``completion_driver.hydrate_completion_snapshot`` at a task on the sandbox
Switchboard project (``conformance`` by default) — real GitHub PR/checks/
review/queue state — while still using the observe-cut adapters, so no
``start_task`` / Connect wake / merge mutation is ever issued. Everything else
about a live tick (decision-record corpus, completion_run projection) persists
normally against that project's own database; only the *mutating external*
effect ports are cut.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Mapping, Optional

from switchboard.application import completion_driver

from tests.conformance import _shared
from tests.conformance.observe.adapters import ObserveEffectAdapters


LIVE_ENV_VAR = "CONFORMANCE_LIVE"
LIVE_PROJECT = "conformance"
#: Never observe-tick these — they are live product/dogfood boards, not the
#: sandbox. Isolation rule #1 in the harness design doc.
FORBIDDEN_LIVE_PROJECTS = frozenset({"switchboard", "atlas"})


def run_observe_tick_fixture(
    scenario: Mapping[str, Any],
    *,
    task_id: str = "COORD-65",
    project: str = "switchboard",
    actor: str = "conformance-observer",
    agent_id: str = "conformance-observer",
    ticks: int = 2,
) -> dict[str, Any]:
    """Hermetic observe tick: T1's snapshot builder, observe-cut adapters.

    Runs ``ticks`` (default 2) so callers can assert the same idempotent
    second-tick behavior T1 proves, but through the observe-cut adapters.
    """
    scenario = dict(scenario)
    run_id = f"fixture-observe-{scenario['id']}"
    observe = ObserveEffectAdapters(run_id=run_id)
    ledger = _shared.EffectLedger()
    run = {"run_id": f"{run_id}-run", "state_version": 1, "attempt": 0}
    snapshot = _shared.build_snapshot(scenario, task_id=task_id)
    results = []
    with _shared.hermetic_completion_patches(run, ledger):
        for _ in range(max(1, ticks)):
            results.append(completion_driver.run_completion_tick(
                task_id,
                project=project,
                actor=actor,
                agent_id=agent_id,
                store_mod=object(),
                hydrator=lambda *_a, **_k: snapshot,
                adapters=observe.as_completion_adapters(),
            ))
    return {
        "mode": "fixture",
        "run_id": run_id,
        "scenario_id": scenario["id"],
        "ticks": results,
        "receipts": observe.receipts,
    }


def run_observe_tick_live(
    task_id: str,
    *,
    project: str = LIVE_PROJECT,
    actor: str = "conformance-observer",
    agent_id: str = "conformance-observer",
    hydrator: Optional[Callable[..., dict[str, Any]]] = None,
    run_id: str = "",
) -> dict[str, Any]:
    """Real-sandbox observe tick against a live conformance PR.

    See module docstring for what stays real (hydration, DB writes) vs. what
    is cut (mutating external effects). Refuses to run unless
    ``CONFORMANCE_LIVE=1`` is set, and refuses ``project`` values that name a
    live product board even with the flag set.
    """
    if os.environ.get(LIVE_ENV_VAR) != "1":
        raise RuntimeError(
            f"run_observe_tick_live requires {LIVE_ENV_VAR}=1 -- refusing to "
            "hydrate real GitHub/task state outside an explicit live run"
        )
    if project in FORBIDDEN_LIVE_PROJECTS:
        raise RuntimeError(
            f"conformance must never observe project={project!r} (live "
            "product/dogfood board) -- see harness design isolation rules"
        )
    observe = ObserveEffectAdapters(run_id=run_id)
    result = completion_driver.run_completion_tick(
        task_id,
        project=project,
        actor=actor,
        agent_id=agent_id,
        hydrator=hydrator or completion_driver.hydrate_completion_snapshot,
        adapters=observe.as_completion_adapters(),
    )
    return {
        "mode": "live",
        "task_id": task_id,
        "project": project,
        "tick": result,
        "receipts": observe.receipts,
    }


def run_observe_tick(
    *,
    scenario: Optional[Mapping[str, Any]] = None,
    task_id: Optional[str] = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Dispatch to fixture mode (``scenario=``) or live mode (``task_id=``)."""
    if bool(scenario) == bool(task_id):
        raise ValueError(
            "run_observe_tick requires exactly one of scenario= or task_id="
        )
    if scenario is not None:
        return run_observe_tick_fixture(scenario, **kwargs)
    return run_observe_tick_live(str(task_id), **kwargs)


__all__ = [
    "LIVE_ENV_VAR",
    "LIVE_PROJECT",
    "FORBIDDEN_LIVE_PROJECTS",
    "run_observe_tick",
    "run_observe_tick_fixture",
    "run_observe_tick_live",
]
