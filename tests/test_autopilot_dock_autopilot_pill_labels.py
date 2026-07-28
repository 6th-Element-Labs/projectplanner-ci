"""UI-68: Autopilot pills use concise state verbs."""

from path_setup import ROOT


def main() -> None:
    source = (ROOT / "static/js/fleet-dock.js").read_text()
    assert "live: ['Driving', 'green', 'route'," in source
    assert "armed: ['Armed', 'azure', 'clock'," in source
    assert "paused: ['Paused', 'yellow', 'player-pause'," in source
    assert "stale: ['Stale', 'orange', 'alert-triangle'," in source
    assert "none: ['Arm', 'secondary', 'player-play'," in source
    assert "Autopilot armed" not in source
    assert "Autopilot paused" not in source
    assert "Autopilot stale" not in source
    assert "Arm autopilot" not in source
    assert "loadCoverage(app, prs)" in source
    assert "runner task ids" not in source
    print("PASS: Autopilot pill labels use state verbs")


if __name__ == "__main__":
    main()
