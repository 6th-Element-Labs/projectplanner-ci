"""State-machine rules for provider-originated attention requests."""
from __future__ import annotations

ATTENTION_STATES = frozenset({
    "pending",
    "decision_recorded",
    "delivering",
    "resolved",
    "failed",
    "expired",
    "cancelled",
    "orphaned",
})
TERMINAL_ATTENTION_STATES = frozenset({
    "resolved", "failed", "expired", "cancelled", "orphaned",
})
ATTENTION_TRANSITIONS = {
    "pending": frozenset({
        "decision_recorded", "failed", "expired", "cancelled", "orphaned",
    }),
    "decision_recorded": frozenset({
        "delivering", "failed", "expired", "cancelled", "orphaned",
    }),
    "delivering": frozenset({"resolved", "failed", "orphaned"}),
}


class AttentionLifecycleError(ValueError):
    """Raised when a request attempts an undefined lifecycle transition."""

    def __init__(self, current: str, target: str) -> None:
        self.current = current
        self.target = target
        super().__init__(f"attention request cannot transition from {current!r} to {target!r}")


def assert_attention_transition(current: str, target: str) -> None:
    """Fail closed unless ``current -> target`` is an explicit transition."""
    if current not in ATTENTION_STATES or target not in ATTENTION_STATES:
        raise AttentionLifecycleError(current, target)
    if target not in ATTENTION_TRANSITIONS.get(current, frozenset()):
        raise AttentionLifecycleError(current, target)
