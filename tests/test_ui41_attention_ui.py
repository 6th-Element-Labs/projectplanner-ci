"""UI-41 focused static-client contract tests."""
from pathlib import Path
import subprocess

from path_setup import ROOT


ATTENTION = ROOT / "static/js/attention.js"
APP = ROOT / "static/app.js"
INDEX = ROOT / "static/index.html"


def test_attention_javascript_parses():
    subprocess.run(["node", "--check", str(ATTENTION)], check=True)


def test_bell_and_queue_share_the_authoritative_projection():
    attention = ATTENTION.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    index = INDEX.read_text(encoding="utf-8")
    assert "setCount(data.count)" in attention
    assert "window.PMAttention = { load }" in attention
    assert "await window.PMAttention.load" in app
    assert 'aria-label="Open Needs-you queue"' in index


def test_completion_handoff_and_receipt_gated_states_are_explicit():
    source = ATTENTION.read_text(encoding="utf-8")
    for label in (
        "Implementation complete, human action required",
        "Completed work",
        "Why automation stopped",
        "What you need to do",
        "Resume condition",
        "Next automatic step",
        "Only the frozen choices above are authorized",
        "Open session",
    ):
        assert label in source
    assert "request.status === 'resolved' && request.delivery_receipt" in source
    assert "failed', 'expired', 'cancelled', 'orphaned" in source
    assert "if (delivering) return" in source
    assert "needs-custom" not in source
