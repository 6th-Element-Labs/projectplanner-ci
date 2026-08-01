"""UI-72/UI-77: complete desktop dock and navigation-native mobile status."""
from __future__ import annotations

import subprocess

from path_setup import ROOT


def main() -> None:
    app = (ROOT / "static/app.js").read_text()
    dock = (ROOT / "static/js/fleet-dock.js").read_text()
    css = (ROOT / "static/taikun-ui.css").read_text()
    board = (ROOT / "src/switchboard/api/routers/board.py").read_text()
    deployment = (ROOT / "deployment_status.py").read_text()

    # Operator order 2026-07-28: healthy runners are a labelled bucket, not a
    # disclosure. Nothing in the dock collapses — it is the whole operator surface.
    for label in ("Needs you", "Broken", "Stalled", "Working normally"):
        assert label in app, label
    assert "dock-healthy" not in app, "healthy runners are collapsed again"
    for text in ("Merge queue", "data-pr-merge", "data-pr-regate",
                 "Autodeploy failing", "Not yet deployed"):
        assert text in app, text
    assert "@media (max-width: 991.98px)" in css
    assert "#fleet-dock > .card, #fleet-dock-pill { display: none !important; }" in css
    assert "min-height: 44px" in css
    assert "fleet-mobile-activity" in app and "mobile-fleet-badge" in app
    assert "_dockMobileAnswer" in app and "dock-answer-sheet" in app
    assert "/api/pull-requests/{pr_number}/merge" in board
    assert "/api/pull-requests/{pr_number}/regate" in board
    assert '"--auto"' in board and '"--squash"' not in board
    assert '"tasks": board["tasks"]' in deployment
    assert "((s.environment || {}).progress_fault || {})" in dock
    assert "age >= 600" not in dock
    subprocess.run(["node", "--check", "static/app.js"], cwd=ROOT, check=True)
    subprocess.run(["node", "--check", "static/js/fleet-dock.js"], cwd=ROOT, check=True)
    print("PASS test_autopilot_dock_complete_wireframe")


if __name__ == "__main__":
    main()
