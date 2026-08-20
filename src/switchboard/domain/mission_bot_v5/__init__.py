"""Mission Bot v5 domain policy.

The package is deliberately small.  It accepts facts from the three ADR-0008
planes and returns one pager decision.  It has no storage, provider, adapter,
authentication, or process-control dependencies.
"""

from .controller import ROLES, STATES, decide_mission_transition

__all__ = ["ROLES", "STATES", "decide_mission_transition"]
