#!/usr/bin/env python3
"""UI-84: Settings truthfully routes capacity repair to the exact Fleet Host."""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from path_setup import ROOT  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402


PROJECT = "simplemark"
HOST_ID = "host/steve-existing-mac"
PROVIDER = "provider-simplemark"
SCM = "scm-simplemark"
REPOSITORY = "StevenRidder/simplemark"

STATES = {
    "configuration": {"passed": True, "status": "ready", "blockers": []},
    "provider": {"passed": True, "status": "ready", "blockers": []},
    "scm": {"passed": True, "status": "ready", "blockers": []},
    "persistent": {
        "passed": False,
        "status": "blocked",
        "candidate_host_ids": [HOST_ID],
        "blockers": [{
            "code": "persistent_capacity_unavailable",
            "category": "persistent",
            "message": "No eligible persistent Agent Host currently has capacity.",
            "repair": "Open the named Fleet host and repair its project/repository grant, capacity, or revocation state.",
            "details": {"host_id": HOST_ID, "canonical_repository": REPOSITORY},
        }],
    },
    "ephemeral": {"passed": True, "status": "not_required", "blockers": []},
    "autopilot": {"passed": True, "status": "ready", "blockers": []},
}
BLOCKER = STATES["persistent"]["blockers"][0]
READINESS = {
    "schema": "switchboard.project_execution_readiness.v1",
    "project": PROJECT,
    "passed": False,
    "status": "blocked",
    "reason_code": BLOCKER["code"],
    "message": "Project execution readiness is blocked.",
    "blockers": [BLOCKER],
    "states": STATES,
}


with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1180, "height": 820})
    page.route("http://settings.test/**", lambda route: route.fulfill(
        status=200, content_type="text/html", body='<main id="mount"></main>'))
    page.goto(f"http://settings.test/?project={PROJECT}")
    page.add_script_tag(path=str(ROOT / "static/js/settings.js"))
    page.evaluate(
        """async ({project, hostId, providerRef, scmRef, repository, readiness}) => {
            window.PM_PROJECT = project;
            const methods = window.SwitchboardSettings.methods;
            const ctx = {
                esc: (value) => String(value ?? '').replaceAll('&', '&amp;')
                    .replaceAll('<', '&lt;').replaceAll('>', '&gt;')
                    .replaceAll('"', '&quot;'),
                _settingsCard: methods._settingsCard,
                _settingsRows: methods._settingsRows,
                _settingsExecutionFleetHref: methods._settingsExecutionFleetHref,
                _settingsExecutionRepairAction: methods._settingsExecutionRepairAction,
                _settingsExecutionConnectionSummary: methods._settingsExecutionConnectionSummary,
                _settingsExecutionFocus: methods._settingsExecutionFocus,
                _sfetch: async (url) => {
                    if (url.endsWith('/execution_policy')) return {
                        runtimes: {allowed: ['codex'], default: 'codex'},
                        placement: {host_classes: ['shared'], trust_zones: ['org_shared']},
                        providers: {selectors: [{provider: 'openai-codex', connection_reference: providerRef}]},
                        scm: {provider: 'github_app', connection_reference: scmRef},
                    };
                    if (url.endsWith('/provider-connections')) return {connections: [{
                        credential_reference: providerRef, provider: 'openai-codex',
                        provider_account_id: 'Steve ChatGPT', execution_ready: true,
                        connection_kind: 'personal_subscription', user_id: 'user/steve',
                        lifecycle_state: 'active', revocation_state: 'not_revoked',
                    }]};
                    if (url.endsWith('/scm-connections')) return {connections: [{
                        connection_id: scmRef, provider: 'github_app', lifecycle_state: 'active',
                        project_allowlist: [project], repository_allowlist: [repository],
                        operation_scopes: ['clone', 'fetch', 'push', 'create_pr'],
                    }]};
                    if (url.endsWith('/repo_topology')) return {roles: {canonical: {repo: repository}}};
                    return readiness;
                },
            };
            const mount = document.getElementById('mount');
            mount.innerHTML = await methods._settingsExecutionSection.call(ctx);
            mount.addEventListener('click', (event) => {
                const button = event.target.closest('[data-set-action]');
                if (!button) return;
                const action = button.dataset.setAction || '';
                if (action.startsWith('execution-focus:')) {
                    methods._settingsExecutionFocus.call(ctx, action.slice('execution-focus:'.length));
                }
            });
        }""",
        {"project": PROJECT, "hostId": HOST_ID, "providerRef": PROVIDER,
         "scmRef": SCM, "repository": REPOSITORY, "readiness": READINESS},
    )

    summary = page.locator("#execution-readiness-summary")
    assert summary.inner_text().startswith("Blocked")
    remaining = page.locator("#execution-remaining-blocker")
    assert "Start remains blocked." in remaining.inner_text()
    assert BLOCKER["message"] in remaining.inner_text()
    assert BLOCKER["repair"] in remaining.inner_text()
    assert BLOCKER["code"] in remaining.inner_text()

    host_link = remaining.locator("[data-readiness-host-link]")
    assert host_link.get_attribute("data-readiness-host-link") == HOST_ID
    href = host_link.get_attribute("href") or ""
    assert f"project={PROJECT}" in href
    assert "fleet_host=host%2Fsteve-existing-mac" in href
    assert "readiness_from=execution" in href
    assert href.endswith("#tab-fleet")

    integration = page.locator("#execution-integration-summary").inner_text()
    assert "Connection owner\nuser/steve" in integration
    assert "Personal subscription · no metered API billing" in integration
    assert "Provider revocation\nnot_revoked" in integration
    assert f"Repository scope\n{REPOSITORY}" in integration
    assert "Host placement\nshared capacity" in integration

    # Even if every individual card claims success, the top-level authoritative
    # bit owns green. A client-side aggregation must never override it.
    page.evaluate("""() => {
        document.querySelectorAll('[data-readiness-state] .badge').forEach((node) => {
            node.textContent = 'ready'; node.className = 'badge bg-green-lt text-green ms-auto';
        });
    }""")
    assert summary.inner_text().startswith("Blocked")

    # The server's policy error is shown byte-for-byte instead of being replaced
    # by a generic authorization message.
    verbatim = page.evaluate(
        """async () => {
            window.fetch = async () => ({ok:false, status:403,
                json: async () => ({detail:'SimpleMark owner approval is required.'})});
            try { await window.SwitchboardSettings.methods._sSend('/policy', 'POST', {}); }
            catch (error) { return error.message; }
            return '';
        }""")
    assert verbatim == "SimpleMark owner approval is required."
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    browser.close()

print("PASS UI-84 Chromium: truthful Settings-to-Fleet readiness repair")
