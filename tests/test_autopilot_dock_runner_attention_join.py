"""UI-71: the browser joins pending attention requests to live runners."""

from path_setup import ROOT


def main() -> None:
    dock = (ROOT / "static/js/fleet-dock.js").read_text()
    app = (ROOT / "static/app.js").read_text()

    start = dock.index("        async loadRunnerAttention(app) {")
    end = dock.index("\n        // One compact autopilot control", start)
    loader = dock[start:end]

    assert "/api/attention/requests?${p}&limit=200" in loader
    assert "(await res.json()).items" in loader
    assert "item.runner_session_id" in loader
    assert "request_id: item.request_id" in loader
    assert "version: item.version" in loader
    assert "prompt: item.prompt" in loader
    assert "choices: item.choices" in loader
    assert "recommended_default: item.recommended_default" in loader
    assert "return {};" in loader

    assert "window.SwitchboardFleetDock.loadRunnerAttention(this)" in app
    assert "this._dockAttention = attention" in app
    assert "_fleetSignature(runners, prs, deployments, prUnavailable, autopilotCoverage, attention)" in app
    assert "(request || {}).request_id" in app
    assert "(request || {}).version" in app
    assert "this._dockAutopilot, this._dockAttention" in app

    # The join remains at the browser edge; runner liveness stays independent.
    runner_source = (ROOT / "src/switchboard/storage/repositories/runner.py").read_text()
    assert "awaiting_input" not in runner_source

    print("PASS: pending attention joins runners at the browser edge")


if __name__ == "__main__":
    main()
