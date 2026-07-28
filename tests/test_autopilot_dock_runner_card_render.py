"""UI-68: runner cards lead with tasks and honest output activity."""

from path_setup import ROOT


def main() -> None:
    source = (ROOT / "static/app.js").read_text()
    start = source.index("    _dockRunnerHtml(s) {")
    end = source.index("\n    // Fleet-dock presentation", start)
    card = source[start:end]

    assert "runnerConditions(this, s, attention)" in card
    assert "border-left:2px solid ${accent}" in card
    assert "${this.esc(taskId)} · ${this.esc(title)}" in card
    assert 'title="${this.esc(s.runner_session_id || \'\')}"' in card
    assert "env.log_tail" in card
    assert "quiet ${window.SwitchboardFleetDock.shortAge(age)}" in card
    assert "up ${window.SwitchboardFleetDock.shortAge(env.uptime_seconds)}" in card
    assert "ms-auto text-secondary small" not in card
    assert "attn.join" not in card
    assert "const tone =" not in card
    assert "const dead =" not in card
    assert "${this.esc(s.runner_session_id)}</span>" not in card
    print("PASS: runner card leads with task and output activity")


if __name__ == "__main__":
    main()
