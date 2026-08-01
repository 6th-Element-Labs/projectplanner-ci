from path_setup import ROOT


def test_primary_navigation_is_grouped_without_changing_tab_targets():
    html = (ROOT / "static" / "index.html").read_text()

    assert '>Shape</li>' in html
    assert '>Deliver</li>' in html
    assert '>Collaborate</li>' in html
    assert '>Utilities</div>' in html

    expected = {
        "toptab-overview": "#tab-exec",
        "toptab-scope": "#tab-scope",
        "toptab-plan": "#tab-plan-hub",
        "toptab-mission": "#tab-mission",
        "toptab-fleet": "#tab-fleet",
        "toptab-inbox": "#tab-inbox-hub",
        "toptab-ask": "#tab-ask",
    }
    for controller, target in expected.items():
        assert f'id="{controller}"' in html
        assert f'href="{target}"' in html

    assert '<span class="nav-link-title">Deliverables</span>' in html
    assert '<span class="nav-link-title">Fleet</span>' in html


def test_mobile_navigation_delegates_to_canonical_controllers():
    html = (ROOT / "static" / "index.html").read_text()
    js = (ROOT / "static" / "taikun-ui.js").read_text()

    assert 'aria-label="Mobile navigation"' in html
    for controller in (
        "#toptab-overview",
        "#toptab-plan",
        "#toptab-mission",
        "#toptab-fleet",
        "#toptab-scope",
        "#toptab-inbox",
        "#toptab-ask",
    ):
        assert f'data-tk-mobile-tab="{controller}"' in html

    assert "controller.click()" in js
    assert "document.getElementById('menu-settings')" in js
    assert "document.getElementById('btn-new-project')" in js
