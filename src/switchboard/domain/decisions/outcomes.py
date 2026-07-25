"""``switchboard.decision_outcome.v1`` — the outcome vocabulary (outcomes spec §3.1).

``decision_records`` shipped six outcome columns under the comment "backfilled by
reconcile / webhook". The backfill was never built, so every episode ever written
reads ``outcome='open'``: the ledger can prove a loop occurred and never that an
attempt accomplished anything.

An outcome without a registry is the same defect the reason-code registry was built
to fix. This module is the sibling position the spec names: every outcome the corpus
may record is declared once, with whether it is **terminal** (the episode is closed
and will not be revisited) and what kind of evidence closed it. An outcome not
declared here is *surfaced* by the counts as unregistered rather than silently
counted as free text.

Additive only. Outcomes are API and are never renamed in place — ``converged`` and
``escalated`` predate the spec vocabulary and stay registered as ``legacy`` for
exactly that reason; ``compact_decision_snapshots`` reads ``escalated`` to decide
which snapshot bodies survive their TTL.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


DECISION_OUTCOME_SCHEMA = "switchboard.decision_outcome.v1"

# What closed the episode. Not the actor — the *class of evidence*, so a count can ask
# "which reason codes only ever close because a human stepped in".
CLOSURES = frozenset({"none", "provenance", "head", "policy", "human"})


@dataclass(frozen=True)
class DecisionOutcome:
    """One declared entry in ``switchboard.decision_outcome.v1``."""

    outcome: str
    terminal: bool
    closed_by: str
    legacy: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": DECISION_OUTCOME_SCHEMA,
            "outcome": self.outcome,
            "terminal": self.terminal,
            "closed_by": self.closed_by,
            "legacy": self.legacy,
        }


def _outcome(outcome: str, terminal: bool, closed_by: str,
             legacy: bool = False) -> DecisionOutcome:
    if closed_by not in CLOSURES:
        raise ValueError(f"unknown decision-outcome closure: {closed_by}")
    return DecisionOutcome(outcome, terminal, closed_by, legacy)


_ENTRIES: tuple[DecisionOutcome, ...] = (
    # The only non-terminal outcome, and the default every row is written with.
    _outcome("open", False, "none"),
    # Canonical merge provenance reached the target branch.
    _outcome("merged", True, "provenance"),
    # The task reached a terminal Done with provenance recorded.
    _outcome("done", True, "provenance"),
    # The head moved on. Whatever this episode decided, it decided about a head that
    # no longer exists — the single most common real closure, and the one that turns
    # "remediation ran seven times" into "and none of them moved the work".
    _outcome("superseded", True, "head"),
    # The retry budget was spent without resolving the reason code.
    _outcome("abandoned", True, "policy"),
    # An operator closed it out rather than the loop resolving it.
    _outcome("human_resolved", True, "human"),
    # -- legacy (COORD-50, pre-spec) -------------------------------------------
    _outcome("converged", True, "provenance", legacy=True),
    _outcome("escalated", True, "human", legacy=True),
)

DECISION_OUTCOMES: dict[str, DecisionOutcome] = {
    entry.outcome: entry for entry in _ENTRIES
}

OUTCOMES = frozenset(DECISION_OUTCOMES)
TERMINAL_OUTCOMES = frozenset(
    entry.outcome for entry in _ENTRIES if entry.terminal
)
OPEN_OUTCOME = "open"

# Snapshot bodies for these survive the TTL: the scarce demonstrations (parent spec §5).
RETAINED_OUTCOMES = frozenset({"escalated", "abandoned"})


def normalize_outcome(outcome: str) -> str:
    return str(outcome or "").strip().lower()


def get_outcome(outcome: str) -> Optional[DecisionOutcome]:
    return DECISION_OUTCOMES.get(normalize_outcome(outcome))


def is_registered_outcome(outcome: str) -> bool:
    return normalize_outcome(outcome) in DECISION_OUTCOMES


def is_terminal_outcome(outcome: str) -> bool:
    """Report whether ``outcome`` closes the episode.

    An *unregistered* outcome is never treated as terminal. Guessing terminality for
    a value nobody declared is how a stalled loop reads as resolved.
    """
    entry = get_outcome(outcome)
    return bool(entry and entry.terminal)


__all__ = [
    "CLOSURES",
    "DECISION_OUTCOMES",
    "DECISION_OUTCOME_SCHEMA",
    "DecisionOutcome",
    "OPEN_OUTCOME",
    "OUTCOMES",
    "RETAINED_OUTCOMES",
    "TERMINAL_OUTCOMES",
    "get_outcome",
    "is_registered_outcome",
    "is_terminal_outcome",
    "normalize_outcome",
]
