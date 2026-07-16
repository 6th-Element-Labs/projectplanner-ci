#!/usr/bin/env python3
"""UI-18: the unified Settings shell — information architecture, role visibility,
deep links, responsive collapse, state transitions, and accessibility basics.

Same "API + served-HTML + frontend-source needle" style as test_ui9_admin.py: the
shell is plain Tabler markup driven by static/js/settings.js, so the contract we can
assert without a browser is (a) the served shell, (b) the module's registry and
router, and (c) the REST surfaces each section reads.
"""
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scripts.frontend_test_source import read_frontend_source  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="ui18-")
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
    settings_js = open("static/js/settings.js", encoding="utf-8").read()

    # ---- the shell is served, and is the ActionEngine two-card structure --------
    print("\n[1] Settings shell markup")
    for needle in ('id="settings-page"', 'id="settings-nav"', 'id="settings-panel"',
                   'id="settings-nav-collapse"', 'id="settings-nav-toggle"',
                   'id="settings-nav-current"'):
        ok(needle in html, f"index.html exposes {needle}")
    # left category card beside a focused content card (Tabler grid, as ported)
    ok('col-12 col-md-3' in html and 'col-12 col-md-9' in html,
       "shell uses the ported col-md-3 / col-md-9 category+content split")
    ok('list-group list-group-flush" id="settings-nav"' in html,
       "the category card is a flush list-group, as in the ActionEngine pattern")

    # ---- the Settings entry is open to every signed-in user --------------------
    print("\n[2] Role visibility")
    # UI-23 moved the visible entry to the top-right user menu; the #main-nav entry
    # remains as the (hidden) tab controller. Either way the point stands: the entry is
    # never gated on the caller's scopes.
    ok('id="menu-settings"' in html, "a Settings entry exists for every signed-in user")
    ok("nav.style.display = this.canWriteProjects" not in js,
       "loadPrincipal no longer hides the whole Settings tab from non-editors")
    ok('style="display:none"' not in html.split('id="user-menu"')[1][:400],
       "the Settings entry does not wait on a session probe to become visible")
    # personal sections carry no scope; project/system sections do
    for section, scope in (("'profile'", "null"), ("'ai-accounts'", "null"), ("'appearance'", "null")):
        ok(re.search(rf"id: {section}.*scope: {scope}", settings_js),
           f"personal section {section} is always available (scope: {scope})")
    # UI-20: members is write:system — every backing route (access.py members /
    # project_role / revoke / invite) requires it, so write:projects promised access the
    # server refuses. comms stays write:projects: it is readable by anyone who can read the
    # project and the section disables its own edit path from the server's can_edit probe.
    for section, scope in (("'members'", "'write:system'"), ("'comms'", "'write:projects'"),
                           ("'github'", "'write:system'"), ("'tokens'", "'write:system'"),
                           ("'fleet'", "'write:system'"), ("'capacity'", "'write:system'"),
                           ("'narration'", "'write:system'"), ("'provenance'", "'write:system'"),
                           ("'advanced'", "'write:system'")):
        ok(re.search(rf"id: {section}.*scope: {scope}", settings_js),
           f"section {section} is gated on {scope}")
    ok("_settingsLockedCard" in settings_js and "is required to view or change this section" in settings_js,
       "a gated section renders a named lock instead of disappearing")
    # UI-20: the nav's coarse gate must not promise access the routes refuse. Read the
    # scope the server actually enforces and compare, so this can't drift back.
    access_py = open("src/switchboard/api/routers/access.py", encoding="utf-8").read()
    members_route = access_py.split('"/api/access/members"')[1][:400]
    server_scope = "write:system" if '("write:system",)' in members_route else "write:projects"
    nav_scope = re.search(r"id: 'members'.*?scope: '([a-z:]+)'", settings_js).group(1)
    ok(nav_scope == server_scope,
       f"the members nav gate ({nav_scope}) matches what the route enforces ({server_scope})")
    ok('data-settings-locked="${allowed ? \'0\' : \'1\'}"' in settings_js,
       "the nav marks locked sections rather than omitting them")
    ok("_settingsCan(section)" in settings_js and "if (!need) return true;" in settings_js,
       "gating is per-section and personal sections short-circuit to allowed")

    # ---- the information architecture the task specifies -----------------------
    print("\n[3] Information architecture")
    for group in ("My settings", "Project settings", "Operations"):
        ok(f"group: '{group}'" in settings_js, f"category group '{group}' exists")
    for label in ("Profile & security", "Personal AI accounts", "Appearance",
                  "Members & access", "Communications", "GitHub & repositories",
                  "Access tokens", "Fleet & runners", "Capacity & box pressure",
                  "Narration", "Reconcile & provenance", "Advanced"):
        ok(f"label: '{label}'" in settings_js, f"section '{label}' is in the IA")
    ok("window.SwitchboardSettings" in js and "Object.freeze({ methods, SECTIONS })" in settings_js,
       "the shell is an extracted, reusable module with an explicit namespace")

    # ---- deep links restore section + project context --------------------------
    print("\n[4] Deep links")
    ok(r"/^#tab-settings\/([a-z0-9-]+)$/i" in settings_js,
       "the module parses a #tab-settings/<section> deep link")
    ok("_settingsWriteHash" in settings_js and "window.history.replaceState" in settings_js,
       "selecting a section writes the section into the URL")
    ok("window.location.pathname + window.location.search + '#tab-settings/'" in settings_js,
       "the deep link preserves ?project=, so section AND project context restore together")
    ok(re.search(r"/\^#\(tab-\[a-z0-9-\]\+\)\(\?:\\/\[a-z0-9-\]\+\)\?\$/i", html),
       "index.html's on-load router accepts a #tab-<name>/<section> suffix")
    ok("_settingsOnHashChange" in settings_js and "hashchange" in js,
       "an externally edited hash re-routes the shell")
    ok("_settingsCurrentId" in settings_js and "DEFAULT_SECTION" in settings_js,
       "an absent or unknown section falls back to a default rather than a blank pane")

    # ---- responsive collapse ---------------------------------------------------
    print("\n[5] Responsive collapse")
    ok('class="collapse d-md-block" id="settings-nav-collapse"' in html,
       "the category nav collapses below md and is always shown from md up")
    ok('d-md-none' in html and 'data-bs-target="#settings-nav-collapse"' in html,
       "a mobile-only toggle drives the collapse")
    ok("Collapse.getOrCreateInstance(collapse).hide()" in settings_js,
       "choosing a section on mobile closes the nav so the panel is what you land on")

    # ---- accessibility basics --------------------------------------------------
    print("\n[6] Accessibility basics")
    ok('role="tablist"' in html and 'aria-orientation="vertical"' in html,
       "the category list is a vertical tablist")
    ok('aria-label="Settings categories"' in html, "the tablist is labelled")
    ok('aria-expanded="false"' in html and 'aria-controls="settings-nav-collapse"' in html,
       "the mobile toggle declares expanded state and what it controls")
    ok('id="settings-panel" role="tabpanel" tabindex="-1"' in html,
       "the content pane is a focusable tabpanel")
    ok('aria-selected="${active ? \'true\' : \'false\'}"' in settings_js,
       "the selected category is announced via aria-selected")
    ok("aria-controls=\"settings-panel\"" in settings_js,
       "each category points at the panel it controls")
    ok("setAttribute('aria-labelledby', `settings-tab-${id}`)" in settings_js,
       "the panel is labelled by its active category")
    ok("panel.focus({ preventScroll: true })" in settings_js,
       "focus moves to the panel on selection so keyboard users follow the swap")
    ok('aria-hidden="true"' in settings_js, "decorative icons are hidden from assistive tech")

    # ---- state transitions -----------------------------------------------------
    print("\n[7] State transitions")
    ok("if (this._settingsSectionId === id) host.innerHTML = html;" in settings_js,
       "a slow section cannot overwrite a newer selection (stale-render guard)")
    ok("Loading ${this.esc(section.label)}…" in settings_js,
       "each section shows a loading state while it fetches")
    ok("html = this._settingsErrCard(section.label" in settings_js,
       "a section that throws renders a visible error, not a blank pane")
    ok("this._principalReady" in settings_js and "this._principalReady = this.loadPrincipal()" in js,
       "the shell awaits the principal so sections never render spuriously locked")
    ok("_settingsSelect(nav.getAttribute('data-settings-section'))" in js,
       "app.js routes category clicks into the shell")

    # ---- every section's REST surface actually exists ---------------------------
    print("\n[8] Section data sources respond")
    proj = "maxwell"
    for label, url in (
        ("members", f"/api/projects/{proj}"),
        ("comms", f"/api/projects/{proj}/comms"),
        ("github", f"/api/projects/{proj}/repo_topology"),
        ("tokens", "/api/access/tokens?project=maxwell"),
        ("fleet", f"/ixp/v1/agent_hosts?project={proj}&include_stale=1"),
        ("capacity", f"/ixp/v1/saturation_signals?project={proj}"),
        ("narration", f"/api/narration/health?project={proj}"),
        ("provenance", f"/ixp/v1/external_ci_runs?project={proj}"),
        ("advanced", "/api/projects?include_archived=1"),
    ):
        r = client.get(url)
        ok(r.status_code == 200, f"{label} section source {url} returns 200")

    # A project created through the API has the project_access row the credential vault
    # joins on, so this is the shape the AI-accounts section actually reads.
    made = client.post("/api/projects", json={"id": "ui18probe", "label": "UI-18 probe"})
    ok(made.status_code in (200, 201), "a project can be created for the credential-vault probe")
    conns = client.get("/api/projects/ui18probe/provider-connections")
    ok(conns.status_code == 200, "ai-accounts source /provider-connections returns 200")
    ok("connections" in (conns.json() or {}), "provider-connections exposes a connections list")
    policy = client.get("/api/projects/ui18probe/provider-auth-capabilities")
    ok(policy.status_code == 200,
       "ai-accounts source /provider-auth-capabilities returns 200")
    ok(policy.json().get("fail_closed") is True and policy.json().get("capabilities"),
       "AI accounts renders the authoritative fail-closed provider matrix")
    ok("Promise.all([" in settings_js and "provider-auth-capabilities" in settings_js,
       "AI accounts loads connections and CO-15 policy into the same section")
    ok("LiteLLM personal-subscription broker" in settings_js,
       "AI accounts labels LiteLLM as API/paygo rather than a personal-login broker")

    # /api/auth/session is 401 with no global session — the normal dev-open state. The
    # Profile section must report that honestly instead of reading {} and claiming the
    # caller is not a superadmin.
    sess = client.get("/api/auth/session")
    ok(sess.status_code in (200, 401), "profile source /api/auth/session answers")
    ok("const signedIn = !session.error && !!session.authenticated;" in settings_js,
       "the profile section distinguishes 'no session' from 'session says no'")
    ok("if (signedIn) {" in settings_js and "rows.push(['Superadmin'" in settings_js,
       "the superadmin row is only claimed when a session actually answered")

    # the exact field paths each section reads, so a shape drift fails here
    print("\n[9] Section field contracts")
    comms = client.get(f"/api/projects/{proj}/comms").json()
    ok("inbound" in comms and "plus_address" in comms["inbound"] and "domains" in comms["inbound"],
       "comms exposes inbound.plus_address + inbound.domains")
    ok("outbound" in comms and "cadence" in comms["outbound"]
       and "digest_recipients" in comms["outbound"] and "notify_recipients" in comms["outbound"],
       "comms exposes outbound.cadence + digest/notify recipients")
    ok("can_edit" in comms, "comms reports whether this caller may edit")
    sat = client.get(f"/ixp/v1/saturation_signals?project={proj}").json()
    ok("status" in sat and "alerts" in sat, "saturation exposes status + alerts")
    nar = client.get(f"/api/narration/health?project={proj}").json()
    ok("queue" in nar and "receipts" in nar and "freshness" in nar and "alerts" in nar,
       "narration health exposes queue + receipts + freshness + alerts")
    hosts = client.get(f"/ixp/v1/agent_hosts?project={proj}&include_stale=1").json()
    ok("hosts" in hosts and isinstance(hosts["hosts"], list), "agent_hosts exposes a hosts list")
    toks = client.get("/api/access/tokens", params={"project": "maxwell"}).json()
    ok("tokens" in toks and isinstance(toks["tokens"], list), "access tokens exposes a tokens list")

    # ---- the legacy surfaces still work; UI-20 retires their entry points -------
    print("\n[10] Launchers + folded-in surfaces")
    # goto-fleet is a cross-tab jump, not a modal launcher, so it stays.
    ok("case 'goto-fleet':" in settings_js, "the shell still dispatches goto-fleet")
    # UI-20 (2/6): Access tokens is folded into the shell — its launcher is retired and the
    # create/revoke flow runs inline, so no open-tokens launcher / openApiKeys call remains.
    ok("case 'open-tokens':" not in settings_js and "openApiKeys" not in settings_js,
       "the Access-tokens launcher is retired; the surface is inline in Settings")
    ok("_settingsCreateToken" in settings_js and "_settingsRevokeToken" in settings_js,
       "tokens create/revoke run inline in the Settings shell")
    # UI-20 (3/6): Communications is folded into the shell — its launcher is retired and the
    # inbound-domain + outbound-recipient editor runs inline, so no open-comms launcher
    # (or the modal-era openComms) remains.
    ok("case 'open-comms':" not in settings_js and "openComms" not in settings_js,
       "the Communications launcher is retired; the surface is inline in Settings")
    ok("_settingsCommsSection" in settings_js and "_settingsCommsSave" in settings_js,
       "the comms inbound/outbound editor runs inline in the Settings shell")
    # UI-20 (4/6): Members & access is folded into the shell — its launcher is retired and
    # the member table + add/role/revoke flow runs inline, so no open-members launcher
    # (or the modal-era openMembers) remains.
    ok("case 'open-members':" not in settings_js and "openMembers" not in settings_js,
       "the Members launcher is retired; the surface is inline in Settings")
    ok("_settingsMembersSection" in settings_js and "_settingsMembersChangeRole" in settings_js
       and "_settingsMembersAdd" in settings_js,
       "the members table + add/role/revoke flow runs inline in the Settings shell")
    ok('id="members-modal"' not in html and 'id="btn-project-members"' not in html,
       "the legacy members modal + rail button are retired from index.html")
    # UI-20 (5/6): Connect-a-repo is folded into the shell — its launcher is retired and the
    # repo association + webhook wiring runs inline, so no open-github launcher (or the
    # modal-era openGithubAssoc) remains.
    ok("case 'open-github':" not in settings_js and "openGithubAssoc" not in settings_js,
       "the Connect-repo launcher is retired; the surface is inline in Settings")
    ok("_settingsGithubSection" in settings_js and "_settingsSaveGithubRepo" in settings_js
       and "_settingsVerifyGithub" in settings_js,
       "the repo association + webhook wiring runs inline in the Settings shell")
    # Rule #3: the section open path must never probe GitHub — only Verify passes ?check=1.
    ok("github_association?check=1" not in settings_js
       and "check ? '?check=1' : ''" in settings_js,
       "the GitHub section reads on open but only Verify probes reachability (?check=1)")
    ok('id="github-assoc-modal"' not in html and 'id="btn-project-github"' not in html,
       "the legacy Connect-repo modal + rail button are retired from index.html")
    # The New Project → repo handoff replaced the modal's ga-goto with a settings deep link.
    ok("'#tab-settings/github'" in js,
       "the New Project repo handoff deep-links into Settings -> GitHub, not the old modal")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nUI-18 settings shell: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
