"""Mission Bot v4 domain decisions.

This package has no storage, Capacity, provider, or adapter dependencies.
"""

from .controller import decide_mission_transition

__all__ = ["decide_mission_transition"]
