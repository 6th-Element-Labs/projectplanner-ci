#!/usr/bin/env python3
"""Operator regressions from the UI-72 rework: Watch/Kill and deploy history.

Reported live 2026-07-28: "the deploy tab doesn't show our history" and
"i can't watch a runner anymore, and kill". The rework condition-gated the
action row so a healthy runner offered nothing, mislabeled the watch-panel
button as Nudge, and dropped the shipped-deployments list. Also: the operator
pressed Re-gate without knowing what it meant — the label is now Re-run CI.
"""
from path_setup import ROOT

app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def section(src, start, end):
    i = src.index(start)
    return src[i:src.index(end, i)]


actions = section(app, "_dockRunnerActions(s, condition, attention) {", "_dockAnswer(")

# Watch is unconditional for any bound runner — not gated on a condition key.
assert "data-runner-watch-task" in actions, "Watch button missing from the action row"
watch_line = next(l for l in actions.splitlines() if "data-runner-watch-task" in l)
assert "Watch" in watch_line, "the watch button must be labeled Watch, not Nudge"
assert "condition.key" not in watch_line, "Watch must not be condition-gated"

# Nudge is an inject action, not a mislabeled watch-panel opener.
assert '"inject"' in actions or "data-runner-action=\"inject\"" in actions, \
    "Nudge must map to the inject action"

# Kill lives in the overflow dropdown.
assert 'data-runner-action="kill"' in actions and "dropdown-menu" in actions

# Deploy tab renders shipped history behind a native disclosure.
deploy = section(app, "const manifest = deployments.filter((x) => !x.deployed);",
                 "const attnBadge")
assert "x.deployed)" in deploy and "Recently shipped" in deploy, \
    "shipped-deployments history missing from the deploy tab"
assert "_dockDeploymentHtml" in deploy, "history must reuse the full deployment card"

# The CI re-run control says what it does.
assert ">Re-run CI</button>" in app and ">Re-gate</button>" not in app

# Cache buster bumped so a normal reload picks this up.
assert "app.js?v=64" not in index

print("PASS test_autopilot_dock_watch_kill_history")
