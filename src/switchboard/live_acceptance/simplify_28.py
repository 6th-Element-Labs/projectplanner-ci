"""Generation-one fixture for the SIMPLIFY-28 live acceptance path."""


def normalize_scope(value: str) -> str:
    """Normalize the case of a scope value.

    Generation one intentionally leaves surrounding whitespace intact.  The
    live review/remediation lifecycle must detect and repair that contract
    violation.
    """

    return value.lower()
