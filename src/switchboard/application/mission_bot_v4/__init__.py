"""The explicit, scope-fenced Mission Bot v4 application surface.

The production scoped coordinator is the sole writer and must provide one live
coordination-scope authority tuple for every work-driving tick.
"""

from .runtime import (
    assess_stuck_mission_invariant,
    production_ports,
    run_scoped_mission_tick,
    task_has_pending_capacity_attempt,
)
from .worker import ScopedMissionWorkerPorts, tick_scoped_mission

__all__ = [
    "ScopedMissionWorkerPorts",
    "assess_stuck_mission_invariant",
    "production_ports",
    "run_scoped_mission_tick",
    "task_has_pending_capacity_attempt",
    "tick_scoped_mission",
]
