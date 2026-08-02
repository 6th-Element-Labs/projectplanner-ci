"""UI-68/UI-75: Fleet navigation keeps Autopilot dock labels and identifiers."""

from path_setup import ROOT


def main() -> None:
    html = (ROOT / "static/index.html").read_text()
    app = (ROOT / "static/app.js").read_text()
    dock = (ROOT / "static/js/fleet-dock.js").read_text()

    assert 'id="toptab-fleet"' in html
    assert 'href="#tab-fleet"' in html
    assert 'id="tab-fleet"' in html
    assert 'id="fleet-dock"' in html
    assert "SwitchboardFleetDock" in dock
    assert "_renderFleetDock" in app
    assert "_loadFleetDock" in app
    assert "/ixp/v1/" in app

    assert '<span class="nav-link-title">Fleet</span>' in html
    assert '<h2 class="page-title mb-1">Fleet</h2>' in html
    assert 'Hosts, runners, and installed adapter releases.' in html
    assert '<span class="fw-medium">Autopilot</span>' in app
    assert "'All clear'" in app
    print("PASS: Fleet navigation preserves Autopilot dock labels and internals")


if __name__ == "__main__":
    main()
