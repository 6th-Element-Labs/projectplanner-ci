"""BUG-179: remediation executions use the managed completion handoff."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_remediation_claims_use_the_fenced_completion_handoff():
    source = (
        ROOT / "src" / "switchboard" / "storage" / "repositories" / "claims.py"
    ).read_text(encoding="utf-8")

    assert 'role in {"implementation", "remediation"}' in source
