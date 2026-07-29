"""UI-68: condition-led actions keep destructive Kill in overflow."""

from path_setup import ROOT


def main() -> None:
    source = (ROOT / "static/app.js").read_text()
    start = source.index("    _dockRunnerActions(s, condition, attention) {")
    end = source.index("\n    // Fleet-dock presentation", start)
    actions = source[start:end]

    assert "condition.key === 'waiting_on_you'" in actions
    assert "data-runner-answer=" in actions
    assert ">Answer</button>" in actions
    assert "condition.key === 'silent'" in actions  # Nudge = inject on a silent runner
    assert "actions.includes('inject')" in actions  # Nudge injects; Watch is unconditional
    assert ">Nudge</button>" in actions
    # Restart is offered for a runner that broke, never for a clean finish.
    # 'exited' is gone: the dock now tells completed/stopped/failed apart.
    assert "'failed', 'lost_host', 'ended_unknown'" in actions
    assert "actions.includes('restart')" in actions
    assert ">Restart</button>" in actions
    assert "condition.key === 'exited'" not in actions
    assert "'finished'" not in actions, "a finished runner has nothing to restart"
    assert "actions.includes('kill')" in actions
    assert 'class="dropdown-item text-danger"' in actions
    assert "btn-outline-danger" not in actions
    assert "expected_version: attention.version" in actions
    assert "/decide?${p}" in actions
    assert "querySelectorAll('[data-runner-answer]')" in source
    print("PASS: runner actions follow condition and hide Kill in overflow")


if __name__ == "__main__":
    main()
