#!/usr/bin/env python3
"""QA-139: SimpleMark readiness-to-Autopilot journey in the real Taikun shell."""
from __future__ import annotations

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import re
import threading

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]
PROJECT = "simplemark"
DELIVERABLE = "sm-local-editor"
HOST_ID = "host/steve-existing-mac"
REPOSITORY = "StevenRidder/simplemark"
PROVIDER = "provider-simplemark"
SCM = "scm-simplemark"

SERVER = ThreadingHTTPServer(
    ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(ROOT / "static"))
)
threading.Thread(target=SERVER.serve_forever, daemon=True).start()

state = {
    "simplemark_granted": False,
    "policy_ready": False,
    "scopes": [],
    "requests": [],
    "responses": [],
}


def grant(target: str, repository: str, concurrency: int, *, status: str = "active") -> dict:
    return {
        "grant_id": f"hostgrant-{target}", "status": status,
        "source_project_id": "switchboard", "target_project_id": target,
        "canonical_repository": repository, "runtime": "codex",
        "provider": "openai-codex", "trust_zone": "org_shared",
        "isolation_mode": "worktree", "max_concurrency": concurrency,
    }


def host() -> dict:
    grants = [
        grant("switchboard", "6th-Element-Labs/projectplanner", 8),
        grant("atlas", "6th-Element-Labs/actionengine", 2),
    ]
    if state["simplemark_granted"]:
        grants.append(grant(PROJECT, REPOSITORY, 2))
    return {
        "host_id": HOST_ID, "hostname": "Steve's existing Mac", "stale": False,
        "heartbeat_at": 2_000_000_000, "runtimes": [{"runtime": "codex"}],
        "limits": {"max_sessions": 16}, "available_sessions": 15,
        "capacity": {
            "active_sessions": 1,
            "owner": {"user_id": "user/steve"},
            "placement": {
                "host_class": "persistent", "trust_zone": "org_shared",
                "owner_user_ids": ["user/steve"], "providers": ["openai-codex"],
                "account_affinity_ids": ["acct-simplemark"],
            },
        },
        "enrollment": {"execution_policy": {"lane_mode": "all_project_lanes",
                                                "max_sessions": 16}},
        "readiness": {"state": "ready", "contract_matches": True},
        "project_grants": grants,
    }


def readiness() -> dict:
    ready = bool(state["simplemark_granted"] and state["policy_ready"])
    blocker = ({
        "code": "persistent_capacity_unavailable", "category": "persistent",
        "message": "No eligible persistent Agent Host currently has capacity.",
        "repair": "Open the named Fleet host and repair its project/repository grant.",
        "details": {"host_id": HOST_ID, "canonical_repository": REPOSITORY},
    } if state["policy_ready"] else {
        "code": "project_execution_policy_incomplete", "category": "configuration",
        "message": "project execution policy is missing required fields",
        "repair": "Select verified provider and SCM connections, then activate the policy.",
    })
    states = {
        "configuration": {"passed": state["policy_ready"],
                          "status": "ready" if state["policy_ready"] else "blocked",
                          "blockers": [] if state["policy_ready"] else [blocker]},
        "provider": {"passed": state["policy_ready"],
                     "status": "ready" if state["policy_ready"] else "blocked",
                     "blockers": []},
        "scm": {"passed": state["policy_ready"],
                "status": "ready" if state["policy_ready"] else "blocked",
                "blockers": []},
        "persistent": {"passed": state["simplemark_granted"],
                       "status": "ready" if state["simplemark_granted"] else "blocked",
                       "candidate_host_ids": [HOST_ID] if state["simplemark_granted"] else [],
                       "eligible_host_ids": [HOST_ID] if state["simplemark_granted"] else [],
                       "blockers": [] if state["simplemark_granted"] else [blocker]},
        "ephemeral": {"passed": True, "status": "not_required", "blockers": []},
        "autopilot": {"passed": True, "status": "ready", "blockers": []},
    }
    blockers = [] if ready else [blocker]
    return {
        "schema": "switchboard.project_execution_readiness.v1", "project": PROJECT,
        "passed": ready, "status": "ready" if ready else "blocked",
        "reason_code": "" if ready else blocker["code"],
        "message": ("Project is ready for Start and Autopilot admission."
                    if ready else "Project execution readiness is blocked."),
        "blockers": blockers, "states": states,
    }


EMPTY = ({"projects": [], "deliverables": [], "workstreams": [], "people": [],
          "items": [], "attention": [], "requests": [], "signals": [], "inbox": [],
          "wake_intents": [], "sessions": [], "prs": [], "deployments": [],
          "hosts": [], "grants": []})


try:
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        console_errors: list[str] = []
        failed_requests: list[str] = []
        page.on("console", lambda message: console_errors.append(message.text)
                if message.type == "error" else None)
        page.on("requestfailed", lambda request: failed_requests.append(request.url))

        def api(route):
            request = route.request
            url = request.url
            body = json.loads(request.post_data or "{}") if request.post_data else {}
            if request.method != "GET":
                state["requests"].append({"url": url, "method": request.method, "body": body})
            status = 200
            payload: dict | list = EMPTY
            if "/api/auth/session" in url:
                payload = {"authenticated": True, "user": {"id": "user/steve",
                    "name": "QA-139", "is_superadmin": True,
                    "projects": ["switchboard", PROJECT, "atlas"]}}
            elif "/api/auth/me" in url:
                payload = {"mode": "session", "principal": {
                    "principal_id": "user/steve",
                    "effective_scopes": ["admin", "write:system", "write:projects"],
                }}
            elif f"/api/projects/{PROJECT}/repo_topology" in url:
                payload = {"roles": {"canonical": {"repo": REPOSITORY}}}
            elif "/ixp/v1/agent-host-grants/revoke" in url:
                state["simplemark_granted"] = False
                payload = {"revoked": True, "grant": grant(PROJECT, REPOSITORY, 2,
                                                             status="revoked")}
            elif "/ixp/v1/agent-host-grants" in url and request.method == "POST":
                if body.get("canonical_repository") != REPOSITORY:
                    status = 409
                    payload = {"error": "canonical_repository_mismatch",
                               "message": "repository must exactly match the target project"}
                else:
                    state["simplemark_granted"] = True
                    payload = {"granted": True, "grant": grant(PROJECT, REPOSITORY, 2)}
            elif "/ixp/v1/agent-host-grants" in url:
                payload = {"grants": host()["project_grants"]}
            elif "/ixp/v1/agent_hosts" in url:
                payload = {"hosts": [host()]}
            elif f"/api/deliverables/{DELIVERABLE}/autopilot" in url and request.method == "POST":
                scope = {
                    "schema": "switchboard.autopilot_scope.v1",
                    "scope_id": "autopilot-qa139", "scope_type": "deliverable",
                    "deliverable_id": DELIVERABLE, "status": "active",
                    "scope_authority": {"generation": 1, "fence_epoch": 1,
                                         "holder_agent_id": "coordinator/qa139"},
                    "last_result": {
                        "start_authority": "start_task", "wake_id": "wake-qa139",
                        "runner_session_id": "run-qa139",
                        "execution_assignment": {"repository": REPOSITORY,
                            "checkout_sha": "4109db132918c2eeda07ee67ff23cbbc25b11365"},
                    },
                }
                state["scopes"] = [scope]
                payload = {**scope, "started": True}
            elif f"/api/deliverables/{DELIVERABLE}/autopilot" in url:
                payload = {"scopes": state["scopes"]}
            elif "/api/projects" in url:
                payload = {"projects": [{"id": "switchboard"}, {"id": PROJECT},
                                         {"id": "atlas"}]}
            elif "/api/board" in url:
                payload = {"workstreams": []}
            elif "/api/people" in url:
                payload = {"people": []}
            elif "/health/saturation" in url:
                payload = {}
            state["responses"].append(payload)
            route.fulfill(status=status, content_type="application/json",
                          body=json.dumps(payload))

        page.route("**/api/**", api)
        page.route("**/ixp/v1/**", api)
        page.route("**/health/saturation**", api)
        page.route("**/tally/**", api)
        page.goto(f"http://127.0.0.1:{SERVER.server_port}/index.html?project=switchboard",
                  wait_until="domcontentloaded")
        page.wait_for_function("() => typeof TeepPlan !== 'undefined' && window.SwitchboardSettings && window.SwitchboardMission")

        # Fleet: existing Switchboard and Atlas grants survive while the operator
        # adds the repo-scoped SimpleMark grant with concurrency two.
        page.wait_for_function("() => !!TeepPlan._principalReady")
        page.evaluate("() => TeepPlan._principalReady")
        page.evaluate("""() => {
            TeepPlan.project = 'switchboard';
            TeepPlan.isAdmin = true;
        }""")
        page.locator("#toptab-fleet").click()
        page.locator('[data-fleet-filter="all"]').click()
        page.locator(f'[data-host-grant="{HOST_ID}"]').wait_for(state="visible")
        answers = iter([PROJECT, "codex", "openai-codex", "org_shared", "worktree", "2"])
        page.on("dialog", lambda dialog: dialog.accept(next(answers))
                if dialog.type == "prompt" else dialog.accept())
        page.locator(f'[data-host-grant="{HOST_ID}"]').click()
        page.wait_for_timeout(300)
        assert state["simplemark_granted"] is True
        grants = host()["project_grants"]
        assert {item["target_project_id"] for item in grants} == {
            "switchboard", "atlas", PROJECT}
        simplemark = next(item for item in grants if item["target_project_id"] == PROJECT)
        assert simplemark["canonical_repository"] == REPOSITORY
        assert simplemark["max_concurrency"] == 2

        # Settings: the authoritative server bit owns red/green. Select only
        # verified references, activate shared placement, and rerun the same gate.
        page.evaluate("""async ({project, providerRef, scmRef, repo, initial}) => {
            window.PM_PROJECT = project;
            window.__qaPolicy = {runtimes:{allowed:['codex'],default:'codex'},
                placement:{host_classes:['shared'],trust_zones:['org_shared']},
                providers:{selectors:[]},scm:{provider:'',connection_reference:''},
                autopilot:{enabled:true,profile_id:'autopilot-default'},lifecycle:{status:'draft'}};
            window.__qaReady = initial;
            const methods = window.SwitchboardSettings.methods;
            const ctx = {
                esc: (value) => String(value ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),
                _settingsCard: methods._settingsCard,
                _settingsRows: methods._settingsRows,
                _settingsExecutionFleetHref: methods._settingsExecutionFleetHref,
                _settingsExecutionRepairAction: methods._settingsExecutionRepairAction,
                _settingsExecutionConnectionSummary: methods._settingsExecutionConnectionSummary,
                _settingsExecutionFocus: methods._settingsExecutionFocus,
                _sfetch: async (url) => {
                    if (url.endsWith('/execution_policy')) return window.__qaPolicy;
                    if (url.endsWith('/provider-connections')) return {connections:[{
                        credential_reference:providerRef,provider:'openai-codex',
                        provider_account_id:'SimpleMark host-native',execution_ready:true,
                        lifecycle_state:'active',revocation_state:'not_revoked'}]};
                    if (url.endsWith('/scm-connections')) return {connections:[{
                        connection_id:scmRef,provider:'github_app',lifecycle_state:'active',
                        project_allowlist:[project],repository_allowlist:[repo],
                        operation_scopes:['clone','fetch','push','create_pr']}]};
                    if (url.endsWith('/repo_topology')) return {roles:{canonical:{repo}}};
                    return window.__qaReady;
                },
                _sSend: async (_url, _method, payload) => {
                    window.__qaPolicy = {...window.__qaPolicy, ...payload,
                        autopilot:{enabled:true,profile_id:'autopilot-default'}};
                    window.__qaReady = {schema:'switchboard.project_execution_readiness.v1',
                        project,passed:true,status:'ready',reason_code:'',
                        message:'Project is ready for Start and Autopilot admission.',blockers:[],
                        states:Object.fromEntries(['configuration','provider','scm','persistent','ephemeral','autopilot'].map(
                            (key) => [key,{passed:true,status:'ready',blockers:[]}]))};
                    window.__policyWrite = payload;
                    return {execution_policy:window.__qaPolicy};
                },
            };
            ctx.renderSettings = async () => {
                document.getElementById('qa139-mount').innerHTML =
                    await methods._settingsExecutionSection.call(ctx);
            };
            window.__qaSettingsContext = ctx;
            document.body.insertAdjacentHTML('beforeend','<main id="qa139-mount"></main>');
            await ctx.renderSettings();
            document.getElementById('qa139-mount').addEventListener('click',(event) => {
                const button=event.target.closest('[data-set-action]');
                if(button) void methods._settingsAction.call(ctx,button.dataset.setAction);
            });
        }""", {"project": PROJECT, "providerRef": PROVIDER, "scmRef": SCM,
                  "repo": REPOSITORY, "initial": readiness()})
        assert page.locator("#execution-readiness-summary").inner_text().startswith("Blocked")
        page.locator("#execution-provider").select_option(PROVIDER)
        page.locator("#execution-scm").select_option(SCM)
        page.locator("#execution-host-class").select_option("shared")
        page.get_by_role("button", name="Save & activate").click()
        page.wait_for_function("() => window.__policyWrite !== undefined")
        page.wait_for_function("() => document.querySelector('#execution-readiness-summary')?.innerText.startsWith('Ready')")
        policy_write = page.evaluate("window.__policyWrite")
        assert policy_write["providers"]["selectors"][0]["connection_reference"] == PROVIDER
        assert policy_write["scm"]["connection_reference"] == SCM
        assert policy_write["placement"] == {
            "host_classes": ["shared"], "trust_zones": ["org_shared"],
            "burst": {"enabled": False, "max_concurrent_ephemeral": 0},
        }
        state["policy_ready"] = True

        # Mission: a UI Autopilot click records a fenced scope and a start_task
        # receipt bound to the exact SimpleMark repository/checkout and Capacity runner.
        page.evaluate("""async ({deliverable}) => {
            const methods = window.SwitchboardMission.methods;
            const ctx = {selectedDeliverableId:deliverable,autopilotScopes:[],
                esc:(value)=>String(value??''),
                _autopilotScope:methods._autopilotScope,
                loadAutopilotScopes:methods.loadAutopilotScopes,
                refreshMissionPage:async()=>{},
            };
            document.body.insertAdjacentHTML('beforeend',
                '<div id="qa139-autopilot">'+methods._missionAutopilotControlsHtml.call(ctx)+'</div>');
            window.__qaMissionContext=ctx;
            document.querySelector('#qa139-autopilot [data-autopilot-action="start"]').addEventListener(
                'click',()=>methods.controlAutopilot.call(ctx,'start','deliverable','',''));
        }""", {"deliverable": DELIVERABLE})
        page.locator('#qa139-autopilot [data-autopilot-action="start"]').click()
        page.wait_for_function("() => window.__qaMissionContext.autopilotScopes.length === 1")
        scope = page.evaluate("window.__qaMissionContext.autopilotScopes[0]")
        assert scope["scope_authority"] == {
            "generation": 1, "fence_epoch": 1, "holder_agent_id": "coordinator/qa139"}
        assert scope["last_result"]["start_authority"] == "start_task"
        assert scope["last_result"]["wake_id"] == "wake-qa139"
        assert scope["last_result"]["runner_session_id"] == "run-qa139"
        assert scope["last_result"]["execution_assignment"] == {
            "repository": REPOSITORY,
            "checkout_sha": "4109db132918c2eeda07ee67ff23cbbc25b11365",
        }

        # Revocation removes only SimpleMark, makes the authoritative gate red,
        # and wrong-repository regrant fails visibly without touching Atlas/Switchboard.
        page.evaluate("""(host) => {
            TeepPlan._fleetHosts = [host]; TeepPlan._renderFleetHosts();
        }""", host())
        page.locator('[data-host-grant-revoke="hostgrant-simplemark"]').click()
        page.wait_for_timeout(200)
        assert state["simplemark_granted"] is False
        assert readiness()["reason_code"] == "persistent_capacity_unavailable"
        assert {item["target_project_id"] for item in host()["project_grants"]} == {
            "switchboard", "atlas"}
        rejected = page.evaluate("""async () => {
            const response = await fetch('/ixp/v1/agent-host-grants', {method:'POST',
                headers:{'Content-Type':'application/json'},body:JSON.stringify({
                    project:'switchboard',host_id:'host/steve-existing-mac',
                    target_project:'simplemark',canonical_repository:'attacker/wrong',
                    runtime:'codex',provider:'openai-codex',trust_zone:'org_shared',
                    isolation_mode:'worktree',max_concurrency:2})});
            return {status:response.status,body:await response.json()};
        }""")
        assert rejected["status"] == 409
        assert rejected["body"]["error"] == "canonical_repository_mismatch"

        transcript = json.dumps({"requests": state["requests"], "responses": state["responses"]})
        assert not re.search(r"(?i)(api[_-]?key|password|secret|bearer\s+|sk-[a-z0-9]|gh[pousr]_)",
                             transcript), transcript
        expected_conflicts = [message for message in console_errors
                              if "409 (Conflict)" in message]
        unexpected_console_errors = [message for message in console_errors
                                     if message not in expected_conflicts]
        assert len(expected_conflicts) == 1, console_errors
        assert unexpected_console_errors == [], unexpected_console_errors
        assert failed_requests == [], failed_requests
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        browser.close()

    print("PASS QA-139 Chromium: SimpleMark Fleet grant, Settings activation, and Autopilot Start")
    print("PASS QA-139 negatives: revocation and wrong repository remain visibly blocked")
    print("PASS QA-139 evidence: fenced scope, exact checkout, Capacity runner, and credential-free logs")
finally:
    SERVER.shutdown()
