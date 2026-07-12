"""Backward-compatible shim — prefer ``switchboard.domain.coordination``."""
from switchboard.domain.coordination.delivery import (
    build_message_delivery_receipt,
    classify_agent_delivery,
    infer_runtime_for_agent,
    runtime_matches_selector,
)

__all__ = [
    "build_message_delivery_receipt",
    "classify_agent_delivery",
    "infer_runtime_for_agent",
    "runtime_matches_selector",
]
