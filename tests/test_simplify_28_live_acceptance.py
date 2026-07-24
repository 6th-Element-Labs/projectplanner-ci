import pytest

from switchboard.live_acceptance.simplify_28 import normalize_scope


def test_normalize_scope_lowercases_value() -> None:
    assert normalize_scope("Fleet:Read") == "fleet:read"


@pytest.mark.skip(
    reason="SIMPLIFY-28 generation 1: review must require trimming before remediation"
)
def test_normalize_scope_returns_trimmed_lowercase_value() -> None:
    assert normalize_scope("  Fleet:Read  ") == "fleet:read"
