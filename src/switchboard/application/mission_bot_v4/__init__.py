"""Explicit, scope-fenced Mission Bot v4 application surface.

Nothing imports this package from the production v1 loop.  A caller must opt
in and provide one live coordination-scope authority tuple.
"""

from .runtime import (
    assess_stuck_mission_invariant,
    production_ports,
    run_scoped_mission_tick,
)
from .shadow import (
    compare_shadow_decisions,
    run_shadow_batch,
    run_shadow_comparison,
)
from .worker import ScopedMissionWorkerPorts, tick_scoped_mission

__all__ = [
    "ScopedMissionWorkerPorts",
    "assess_stuck_mission_invariant",
    "compare_shadow_decisions",
    "production_ports",
    "run_shadow_batch",
    "run_shadow_comparison",
    "run_scoped_mission_tick",
    "tick_scoped_mission",
]
