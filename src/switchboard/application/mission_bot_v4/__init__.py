"""Explicit, scope-fenced Mission Bot v4 application surface.

Nothing imports this package from the production v1 loop.  A caller must opt
in and provide one live coordination-scope authority tuple.
"""

from .runtime import production_ports, run_scoped_mission_tick
from .worker import ScopedMissionWorkerPorts, tick_scoped_mission

__all__ = [
    "ScopedMissionWorkerPorts",
    "production_ports",
    "run_scoped_mission_tick",
    "tick_scoped_mission",
]
