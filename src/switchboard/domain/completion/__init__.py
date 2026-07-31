"""Live completion-domain contracts that outlived the Mission Bot v1 controller.

SIMPLIFY-30 deleted the retired v1 reducer, normalization law/state machine,
and effect executor. What remains here is the small set of contracts
production still runs on:

- ``human_closeout`` — the frozen PROTO-7 closeout payload for route=human
  attention requests (agent_requires_human / record_human_blocker, and the
  attention repository's follow-up request).
- ``routing`` — route-aware dispatch selection shared by the coordinator
  daemon, mission coordinator, and deliverables repository.
- ``repair_proof`` — cross-task review-repair classification used by the
  review-remediation repository.
"""

from .human_closeout import build_human_closeout_request
from .routing import task_ready_for_dispatch

__all__ = [
    "build_human_closeout_request",
    "task_ready_for_dispatch",
]
