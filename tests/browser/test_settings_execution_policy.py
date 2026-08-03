#!/usr/bin/env python3
"""SETTINGS-1 Chromium proof for accepted execution-policy placement values."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from path_setup import ROOT


TMP = tempfile.mkdtemp(prefix="settings-execution-policy-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_TOP_LEVEL_PROJECTS"] = "maxwell,helm,switchboard"

import db.connection as db_connection  # noqa: E402
import store  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402
from switchboard.storage.repositories import project_execution_policy as policy_repository  # noqa: E402


PROJECT = "settings-placement-browser"
PROVIDER_REFERENCE = "provider-settings-placement"
SCM_REFERENCE = "scm-settings-placement"
CANONICAL_REPO = "acme/settings-placement"
EXPECTED_PAIRS = {
    "personal": "personal",
    "shared": "org_shared",
    "ephemeral": "cloud_ephemeral",
}


class ProviderConnections:
    def get_metadata(self, reference, *, project, admin):
        assert (reference, project, admin) == (PROVIDER_REFERENCE, PROJECT, True)
        return {
            "provider": "openai-codex", "lifecycle_state": "active",
            "refresh_state": "ready", "revocation_state": "not_revoked",
            "materialization_mode": "vault_envelope", "credential_present": True,
        }


class SCMConnections:
    def get(self, reference):
        assert reference == SCM_REFERENCE
        return {
            "provider": "github_app", "lifecycle_state": "active",
            "project_allowlist": [PROJECT], "repository_allowlist": [CANONICAL_REPO],
            "operation_scopes": ["clone", "fetch", "push", "create_pr"],
        }


try:
    store.init_db("switchboard")
    created = store.create_project(
        PROJECT, project_id=PROJECT, actor="settings-browser-test",
        purpose="Settings placement interaction proof",
        boundary="isolated SETTINGS-1 browser fixture")
    assert created.get("created") is True, created
    store.init_db(PROJECT)
    store.set_project_repo_topology(
        project=PROJECT, canonical_repo=CANONICAL_REPO,
        canonical_default_branch="master")
    policy_repository.default_provider_credential_repository = ProviderConnections()
    policy_repository.default_scm_connection_repository = SCMConnections()

    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        console_errors = []
        page.on("console", lambda message: console_errors.append(message.text)
                if message.type == "error" else None)
        page.set_content('<main id="mount"></main>')
        page.add_script_tag(path=str(ROOT / "static/js/settings.js"))
        page.evaluate(
            """async ({project, providerReference, scmReference}) => {
                window.PM_PROJECT = project;
                window.__executionPolicyWrites = [];
                const methods = window.SwitchboardSettings.methods;
                const ctx = {
                    esc: (value) => String(value ?? '').replaceAll('&', '&amp;')
                        .replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
                    _settingsCard: methods._settingsCard,
                    _sfetch: async (url) => {
                        if (url.endsWith('/execution_policy')) return {
                            runtimes: {allowed: ['codex'], default: 'codex'},
                            placement: {host_classes: ['shared'], trust_zones: ['org_shared']},
                            providers: {selectors: [{
                                provider: 'openai-codex', connection_reference: providerReference,
                                account_affinity_id: 'billing-affinity', priority: 7,
                            }]},
                            scm: {provider: 'github', connection_reference: scmReference},
                        };
                        if (url.endsWith('/provider-connections')) return {connections: [{
                            credential_reference: providerReference, provider: 'openai-codex',
                            provider_account_id: 'Settings browser fixture', execution_ready: true,
                        }]};
                        if (url.endsWith('/scm-connections')) return {connections: [{
                            connection_id: scmReference, provider: 'github_app', lifecycle_state: 'active',
                            project_allowlist: [project],
                            repository_allowlist: ['acme/settings-placement'],
                            operation_scopes: ['clone', 'fetch', 'push', 'create_pr'],
                        }]};
                        if (url.endsWith('/repo_topology')) return {
                            roles: {canonical: {repo: 'acme/settings-placement'}},
                        };
                        return {passed: true, states: {}, message: 'Ready'};
                    },
                    _sSend: async (url, method, payload) => {
                        window.__executionPolicyWrites.push({url, method, payload});
                        return {execution_policy: payload};
                    },
                    renderSettings: async () => {},
                };
                const mount = document.getElementById('mount');
                mount.innerHTML = await methods._settingsExecutionSection.call(ctx);
                mount.addEventListener('click', (event) => {
                    const button = event.target.closest('[data-set-action]');
                    if (button) void methods._settingsAction.call(ctx, button.dataset.setAction);
                });
            }""",
            {"project": PROJECT, "providerReference": PROVIDER_REFERENCE,
             "scmReference": SCM_REFERENCE},
        )

        placement = page.locator("#execution-host-class")
        assert placement.input_value() == "shared"
        assert placement.locator('option[value="persistent"]').count() == 0
        for index, host_class in enumerate(EXPECTED_PAIRS, start=1):
            placement.select_option(host_class)
            page.get_by_role("button", name="Save & activate").click()
            page.wait_for_function(
                "count => window.__executionPolicyWrites.length === count", arg=index)
        writes = page.evaluate("window.__executionPolicyWrites")
        assert console_errors == [], console_errors
        browser.close()

    for write, (host_class, trust_zone) in zip(writes, EXPECTED_PAIRS.items()):
        payload = write["payload"]
        assert write["method"] == "POST"
        assert write["url"].endswith(f"/{PROJECT}/execution_policy")
        assert payload["placement"] == {
            "host_classes": [host_class], "trust_zones": [trust_zone],
            "burst": {"enabled": False, "max_concurrent_ephemeral": 0},
        }
        assert payload["providers"]["selectors"][0]["account_affinity_id"] == "billing-affinity"
        assert payload["providers"]["selectors"][0]["priority"] == 7
        assert "persistent" not in str(payload) and "local_trusted" not in str(payload)
        accepted = store.set_project_execution_policy(
            project=PROJECT, updates=payload, actor="settings-browser-test")
        assert not accepted.get("error"), accepted
        stored = store.get_project_execution_policy(PROJECT)
        assert stored["placement"]["host_classes"] == [host_class]
        assert stored["placement"]["trust_zones"] == [trust_zone]
        assert stored["providers"]["selectors"][0]["connection_reference"] == PROVIDER_REFERENCE
        assert stored["providers"]["selectors"][0]["account_affinity_id"] == "billing-affinity"
        assert stored["providers"]["selectors"][0]["priority"] == 7
        assert stored["scm"]["connection_reference"] == SCM_REFERENCE

    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('<main id="mount"></main>')
        page.add_script_tag(path=str(ROOT / "static/js/settings.js"))
        page.evaluate(
            """async ({project, providerReference, scmReference}) => {
                window.PM_PROJECT = project;
                window.__blockedWrites = [];
                const methods = window.SwitchboardSettings.methods;
                const ctx = {
                    esc: (value) => String(value ?? '').replaceAll('&', '&amp;')
                        .replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
                    _settingsCard: methods._settingsCard,
                    _sfetch: async (url) => {
                        if (url.endsWith('/execution_policy')) return {
                            runtimes: {allowed: ['codex'], default: 'codex'},
                            placement: {host_classes: ['personal'], trust_zones: ['personal']},
                            providers: {selectors: [{
                                provider: 'openai-codex', connection_reference: providerReference,
                                account_affinity_id: 'billing-affinity', priority: 7,
                            }]},
                            scm: {provider: 'github_app', connection_reference: scmReference},
                        };
                        if (url.endsWith('/provider-connections')) return {connections: [{
                            credential_reference: providerReference, provider: 'openai-codex',
                            provider_account_id: 'Stale stored provider', execution_ready: false,
                            lifecycle_state: 'active', refresh_state: 'stale',
                        }]};
                        if (url.endsWith('/scm-connections')) return {connections: [{
                            connection_id: scmReference, provider: 'github_app', lifecycle_state: 'active',
                            project_allowlist: [project], repository_allowlist: ['acme/other'],
                            operation_scopes: ['clone', 'fetch', 'push', 'create_pr'],
                        }]};
                        if (url.endsWith('/repo_topology')) return {
                            roles: {canonical: {repo: 'acme/settings-placement'}},
                        };
                        return {passed: false, states: {}, message: 'Blocked'};
                    },
                    _sSend: async (...args) => { window.__blockedWrites.push(args); },
                    renderSettings: async () => {},
                };
                const mount = document.getElementById('mount');
                mount.innerHTML = await methods._settingsExecutionSection.call(ctx);
                mount.addEventListener('click', (event) => {
                    const button = event.target.closest('[data-set-action]');
                    if (button) void methods._settingsAction.call(ctx, button.dataset.setAction);
                });
            }""",
            {"project": PROJECT, "providerReference": PROVIDER_REFERENCE,
             "scmReference": SCM_REFERENCE},
        )
        provider = page.locator("#execution-provider")
        scm = page.locator("#execution-scm")
        assert provider.input_value() == PROVIDER_REFERENCE
        assert scm.input_value() == SCM_REFERENCE
        assert "unavailable" in provider.locator("option:checked").inner_text().lower()
        assert "unavailable" in scm.locator("option:checked").inner_text().lower()
        assert provider.locator("option:checked").is_disabled()
        assert scm.locator("option:checked").is_disabled()
        page.get_by_role("button", name="Save & activate").click()
        assert page.evaluate("window.__blockedWrites") == []
        assert "unavailable" in page.locator("#execution-policy-flash").inner_text().lower()
        browser.close()
finally:
    db_connection._close_pooled_conns()
    shutil.rmtree(TMP, ignore_errors=True)

print("PASS SETTINGS-1 Chromium: policy authority survives save and unavailable readback")
