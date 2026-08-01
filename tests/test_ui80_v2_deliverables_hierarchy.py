"""UI-80: the Deliverables page uses the approved V2 hierarchy."""

from path_setup import ROOT


def test_dual_selectors_live_in_one_global_toolbar():
    html = (ROOT / "static" / "index.html").read_text()
    toolbar = html.split('<header class="navbar navbar-expand-md d-print-none tk-toolbar', 1)[1].split(
        "</header>", 1
    )[0]
    assert toolbar.index('id="project-switcher"') < toolbar.index('id="header-deliverable-switcher"')
    assert toolbar.index('id="header-deliverable-switcher"') < toolbar.index('id="f-search"')


def test_legacy_deliverables_toolbar_is_replaced_by_open_v2_canvas():
    html = (ROOT / "static" / "index.html").read_text()
    mission_tab = html.split('<div class="tab-pane" id="tab-mission"', 1)[1].split(
        '<div class="tab-pane" id="tab-settings"', 1
    )[0]
    assert 'class="card mt-3"' not in mission_tab
    assert 'class="card-header d-flex flex-wrap align-items-center gap-2"' not in mission_tab
    assert 'class="tk-mission-shell"' in mission_tab


def test_v2_page_hierarchy_and_secondary_actions_are_rendered():
    mission = (ROOT / "static" / "js" / "mission.js").read_text()
    assert 'class="tk-mission-breadcrumb"' in mission
    assert 'class="tk-mission-title"' in mission
    assert "Verified progress" in mission
    assert "tk-mission-progress" in mission
    assert "${metric('Active'" in mission
    assert "${metric('Ready'" in mission
    assert "${metric('Blocked'" in mission
    assert 'data-mission-header-action="refresh"' in mission
    assert 'data-mission-header-action="brief"' in mission
    assert 'data-mission-header-action="archive"' in mission
    assert "header + kpi +" in mission
    assert "id=\"mission-view-overview\">${blockerHtml}${workLedger}" in mission
    assert "${blockerHtml}${workLedger}${this._missionBreakdownHtml()}" not in mission


def test_original_wordmark_and_responsive_fleet_card_are_preserved():
    html = (ROOT / "static" / "index.html").read_text()
    app = (ROOT / "static" / "app.js").read_text()

    assert "font-size:18px!important" in html
    assert "background:transparent;color:var(--tblr-primary)!important" in html
    assert "#fleet-dock-pill{right:.75rem!important" in html
    assert "_wireFleetDockResponsive()" in app
    assert "query.addEventListener('change', sync)" in app


def test_global_context_controls_have_one_quiet_visual_weight():
    html = (ROOT / "static" / "index.html").read_text()

    assert ".tk-toolbar-project .form-select,.tk-toolbar-deliverable .form-select{" in html
    assert "font-size:.75rem;font-weight:400;box-shadow:none" in html
    assert ".tk-toolbar-deliverable .form-select{font-weight:600}" not in html
    assert ".tk-toolbar-search{width:100%;max-width:280px" in html
    assert "border:1px solid var(--tblr-border-color);border-radius:.5rem" in html


def test_status_surfaces_share_a_stack_and_mobile_fleet_expands_in_place():
    html = (ROOT / "static" / "index.html").read_text()
    app = (ROOT / "static" / "app.js").read_text()

    assert 'class="tk-left-status-stack"' in html
    assert "flex-direction:column-reverse" in html
    assert ".tk-left-status-stack{position:fixed;left:16rem" in html
    assert "body.tk-sidebar-collapsed .tk-left-status-stack{left:5.25rem}" in html
    assert 'narration-ops-panel.js' not in html
    assert 'id="narration-ops-dock"' not in html
    assert "this._dockCollapsed = false;" in app
    assert "body:has(#fleet-dock > .card) { overflow: hidden; }" in (
        ROOT / "static" / "taikun-ui.css"
    ).read_text()
    assert "#toptab-fleet.active" not in app
