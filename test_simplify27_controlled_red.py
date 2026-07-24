"""Product-CI acceptance check intentionally red in implementation generation 1."""

from simplify27_live_acceptance import CONTROLLED_RED_READY


assert CONTROLLED_RED_READY, (
    "SIMPLIFY-27 controlled-red contract: implementation generation 1 must fail "
    "exact-head product CI until a fresh remediation generation flips readiness"
)
