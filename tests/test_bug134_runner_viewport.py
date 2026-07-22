#!/usr/bin/env python3
"""BUG-134: Watch replay must not move the operator's terminal viewport."""
from pathlib import Path

from path_setup import ROOT


runner = (Path(ROOT) / "static/js/runner-session.js").read_text(encoding="utf-8")
index = (Path(ROOT) / "static/index.html").read_text(encoding="utf-8")

assert "_runnerPtyWritePreservingViewport" in runner
assert "active.viewportY" in runner and "active.baseY" in runner
assert "rp.term.scrollToBottom()" in runner
assert "rp.term.scrollToLine(viewportY)" in runner
assert "this._runnerPtyWritePreservingViewport(rp, frame.data)" in runner
assert "runner-pty-new-output" in index
assert "New output below" in index

print("PASS: Watch preserves followed and scrolled-up viewports across replay")
