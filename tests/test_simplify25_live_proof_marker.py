from datetime import datetime
import re

from path_setup import ROOT

MARKER = ROOT / "docs" / "evidence" / "live" / "simplify24-clean-a.md"


def test_live_proof_marker_has_structured_switchboard_evidence() -> None:
    text = MARKER.read_text(encoding="utf-8")
    fields = dict(
        re.findall(r"^- ([^:]+): `([^`\n]+)`$", text, flags=re.MULTILINE)
    )

    assert set(fields) == {
        "Task ID",
        "Work Session ID",
        "Execution ID",
        "Execution generation",
        "Execution role",
        "Branch",
        "Switchboard observed at",
    }
    assert fields["Task ID"] == "SIMPLIFY-25"
    assert re.fullmatch(r"worksession-[0-9a-f]{16}", fields["Work Session ID"])
    assert re.fullmatch(r"execlease-[0-9a-f]{20}", fields["Execution ID"])
    assert int(fields["Execution generation"]) > 0
    assert fields["Execution role"] == "implementation"
    assert fields["Branch"].startswith("codex/SIMPLIFY-25-")

    observed_at = datetime.fromisoformat(
        fields["Switchboard observed at"].replace("Z", "+00:00")
    )
    assert observed_at.tzinfo is not None
