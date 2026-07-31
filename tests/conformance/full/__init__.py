"""Tier-3 completion conformance: bounded real-run orchestration."""

from tests.conformance.full.runner import (
    FullLoopConfig,
    FullLoopPorts,
    SpendEnvelopeExceeded,
    run_full_loop,
)

__all__ = [
    "FullLoopConfig",
    "FullLoopPorts",
    "SpendEnvelopeExceeded",
    "run_full_loop",
]
