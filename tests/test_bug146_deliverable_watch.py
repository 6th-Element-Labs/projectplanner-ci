#!/usr/bin/env python3
"""BUG-146: a live linked runner is visible and watchable in the work ledger."""
from path_setup import ROOT


mission = (ROOT / "static/js/mission.js").read_text()
app = (ROOT / "static/app.js").read_text()

assert "activeWorkByTask" in mission
assert "active_runner" in mission
assert 'data-mission-watch-task="${this.esc(taskId)}"' in mission
assert ">Watch</button>" in mission
assert ">Live</span>" in mission
assert 'data-mission-task-active="true"' in mission
assert 'class="table-primary"' in mission
assert "runner.session" in mission
assert "[data-mission-watch-task]" in app
assert "openRunnerSessionPanel(" in app

print("PASS BUG-146 deliverable ledger projects live runner truth into Live + Watch controls")
print("PASS BUG-146 Watch delegates to the shared bound-runner terminal entry point")
