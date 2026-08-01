"""UI-80: pin the approved dual-selector V2 navigation information architecture."""

from path_setup import ROOT


def test_v2_global_toolbar_has_only_global_controls_in_order():
    html = (ROOT / "static" / "index.html").read_text()
    toolbar = html.split('<header class="navbar navbar-expand-md d-print-none tk-toolbar', 1)[1].split(
        "</header>", 1
    )[0]

    controls = [
        'aria-label="Taikun Switchboard — home"',
        'id="project-switcher"',
        'id="header-deliverable-switcher"',
        'id="f-search"',
        'id="btn-ack-inbox"',
        'id="btn-new-task"',
        'id="user-menu"',
    ]
    positions = [toolbar.index(control) for control in controls]
    assert positions == sorted(positions)

    assert 'placeholder="Search this project…"' in toolbar
    assert '<span class="tk-action-label">New</span>' in toolbar
    assert 'id="btn-autopilot"' not in toolbar
    assert "tk-toolbar-filter" not in toolbar
    assert "tk-toolbar-export" not in toolbar


def test_project_and_autopilot_keep_their_approved_owners():
    html = (ROOT / "static" / "index.html").read_text()
    mission = (ROOT / "static" / "js" / "mission.js").read_text()

    sidebar = html.split('<aside class="navbar navbar-vertical', 1)[1].split("</aside>", 1)[0]
    assert 'id="project-switcher"' not in sidebar
    assert 'id="header-deliverable-switcher"' not in sidebar
    assert 'data-autopilot-action="start" data-autopilot-scope="deliverable"' in mission

    # UI-79 changes the global top bar, not the five-destination mobile navigation.
    assert 'aria-label="Mobile navigation"' in html
    assert html.count('data-tk-mobile-tab="#toptab-') >= 7
