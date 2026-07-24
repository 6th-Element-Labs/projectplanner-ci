"""Remediation generation for the SIMPLIFY-28 live acceptance path."""


def normalize_scope(value: str) -> str:
    """Normalize scope: trim whitespace and lowercase."""

    return value.strip().lower()
