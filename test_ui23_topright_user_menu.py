#!/usr/bin/env python3
"""UI-23: Settings is reached from the top-right user dropdown (demo.taikunai.com parity).

demo.taikunai.com is the ActionEngine HMI app, so "the same dropdown method" is a real
component with a source of truth: ActionEngine scada/hmi-enhanced-clean/js/navbar.js:752-816.
These checks pin the ported structure, the entry point move, and the two things that would
silently break: the tab controller Bootstrap needs, and the session probe that used to gate
the menu's visibility.
"""
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scripts.frontend_test_source import read_frontend_source  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="ui23-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "plan.db")
os.environ["PM_AUTH_DB_PATH"] = os.path.join(_TMP, "auth.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


try:
    client = TestClient(app_module.app)
    index = client.get("/")
    ok(index.status_code == 200, "index.html serves")
    html = index.text
    js = read_frontend_source(os.path.dirname(os.path.abspath(__file__)))

    # ---- the ported ActionEngine/demo dropdown ---------------------------------
    print("\n[1] The demo dropdown component")
    for needle in ('id="user-menu"',
                   'class="nav-item dropdown"',
                   'data-bs-toggle="dropdown"',
                   'aria-label="Open user menu"',
                   'class="avatar avatar-sm bg-primary-lt"',
                   'dropdown-menu dropdown-menu-end dropdown-menu-arrow'):
        ok(needle in html, f"index.html carries the navbar.js structure: {needle}")
    ok('id="menu-settings"' in html and "ti ti-settings me-2" in html,
       "Settings is an item in the dropdown, with the demo's icon convention")

    # ---- the entry point actually moved ---------------------------------------
    print("\n[2] Entry point moved out of the rail")
    head, _, rail = html.partition('<aside class="navbar navbar-vertical')
    ok('id="user-menu"' in head,
       "the user menu lives in the top header, not the rail")
    ok('id="user-menu"' not in rail, "the rail no longer carries a user menu")
    ok('id="user-menu-signout"' in head and 'id="menu-coordination"' in head,
       "sign out + agent coordination travelled with it (ids preserved for the handlers)")
    ok(re.search(r'id="nav-settings"[^>]*class="[^"]*d-none|class="[^"]*d-none[^"]*"[^>]*id="nav-settings"', html)
       or 'class="nav-item d-none" id="nav-settings"' in html,
       "the rail's Settings entry is hidden")

    # ---- the controller Bootstrap needs (trap 2) -------------------------------
    print("\n[3] The tab controller survives (or the dropdown does nothing)")
    ok('id="toptab-settings"' in html and 'data-bs-toggle="tab"' in html,
       "a #tab-settings tab controller still exists")
    main_nav = html.split('id="main-nav"')[1].split("</ul>")[0]
    ok('href="#tab-settings"' in main_nav,
       "the controller stays inside #main-nav — Bootstrap's Tab needs a "
       "[role=tablist] parent, and ctrlFor/activeTop/_renderActiveTop resolve through it")
    ok('role="tablist"' in html.split('id="main-nav"')[0][-120:]
       or 'id="main-nav" role="tablist"' in html,
       "#main-nav is the tablist that makes the controller work")
    ok("TAIKUN_showTab('#tab-settings')" in html,
       "the dropdown item drives the tab through the shared router, not a special case")

    # ---- the session-probe trap (trap 1) --------------------------------------
    print("\n[4] The menu does not wait on a session probe")
    menu_block = html.split('id="user-menu"')[1][:500]
    ok('style="display:none"' not in menu_block,
       "the user menu is not display:none — Settings must not require a taikun_session cookie")
    ok("menu.style.display = ''" not in html,
       "the old reveal-on-session line is gone (it would have gated Settings)")
    ok("if (email && data.user.email) email.textContent = data.user.email;" in html,
       "the session still fills in the identity label when present")
    ok('id="user-menu-email"' in html and ">Account<" in html,
       "a placeholder label shows before/without a session")

    # ---- deep links still work (exit criterion 2) ------------------------------
    print("\n[5] The UI-18 shell is untouched")
    for needle in ('id="settings-page"', 'id="settings-nav"', 'id="settings-panel"',
                   'id="tab-settings"'):
        ok(needle in html, f"shell intact: {needle}")
    ok(re.search(r"/\^#\(tab-\[a-z0-9-\]\+\)\(\?:\\/\[a-z0-9-\]\+\)\?\$/i", html),
       "#tab-settings/<section> deep links still resolve on load")
    ok("_settingsSelect" in js and "_settingsOnHashChange" in js,
       "the settings router is still composed")

    # ---- the pages the menu points at still exist ------------------------------
    print("\n[6] Menu destinations respond")
    for label, path in (("account", "/account"), ("coordination", "/coordination")):
        r = client.get(path)
        ok(r.status_code == 200, f"{label} page still serves ({path})")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nUI-23 top-right user menu: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
