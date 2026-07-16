#!/usr/bin/env python3
"""UI-14 — Project Communications settings: domain association + per-project outbound.

Proves the acceptance criteria offline (no live mailbox / SMTP / LLM):
  1. inbound routing  — a domain associated with project X (from the web, no .env edit) routes a
                        matching sender to X's inbox; plus-addressing still routes zero-config;
  2. one-owner rule   — a domain already owned by another project is rejected (no silent misroute);
  3. env + web merge  — inbox_routing merges PM_INBOX_ROUTES with the web-managed map, web wins;
  4. per-project out  — notify resolves a project's recipients, falling back to the global list;
  5. REST contract    — GET/POST /api/projects/{p}/comms + /comms/test back the operator screen;
  6. UI wiring        — the Communications editor is folded into the unified Settings shell
                        (settings.js `_settingsCommsSection`); the modal + rail button retired.

Run directly: `python test_ui14_comms_settings.py`.
"""
import os
import shutil
import sys
import tempfile
from scripts.frontend_test_source import read_frontend_source

_TMP = tempfile.mkdtemp(prefix="ui14-comms-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
# Deterministic, offline: no SMTP host (notify dry-runs), a fixed mailbox for plus-addresses, and
# an empty env routing map so the test drives the WEB-managed map exclusively.
os.environ["PM_IMAP_USER"] = "plan@taikunai.com"
os.environ.pop("PM_SMTP_HOST", None)
os.environ.pop("PM_INBOX_ROUTES", None)
os.environ.pop("PM_NOTIFY_EMAIL_TO", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

try:
    import store          # noqa: E402
    import comms          # noqa: E402
    import notify         # noqa: E402
    from switchboard.integrations import inbox_routing  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  UI-14 comms smoke requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0)

_FAILURES = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        _FAILURES.append(msg)


def setup():
    for p in ("maxwell", "helm", "switchboard"):
        store.init_db(p)


def test_plus_address():
    print("\n[1] plus-address")
    check(comms.plus_address("helm") == "plan+helm@taikunai.com",
          "plus_address derives plan+<project>@<host> from PM_IMAP_USER")


def test_inbound_domains_persist_and_normalize():
    print("\n[2] associated domains persist + normalize")
    res = comms.set_inbound_domains("helm", ["@Acme.com", "acme.com", " boats.io "])
    check(res.get("inbound_domains") == ["acme.com", "boats.io"],
          "domains lowercased, @-stripped, deduped")
    check(comms.inbound_domains("helm") == ["acme.com", "boats.io"],
          "domains round-trip through project meta")
    check(comms.set_inbound_domains("helm", ["not a domain"]).get("error"),
          "malformed domain rejected")


def test_one_owner_rule():
    print("\n[3] a domain maps to exactly one board")
    comms.set_inbound_domains("maxwell", ["totalenergy.com"])
    res = comms.set_inbound_domains("switchboard", ["totalenergy.com"])
    check(bool(res.get("error")) and "maxwell" in res["error"],
          "domain owned by another project is rejected, naming the owner")


def test_routing_acceptance():
    print("\n[4] mapped sender routes to that board; plus-address routes zero-config")
    accept, project = inbox_routing.route("Ops <ops@acme.com>", "plan@taikunai.com")
    check(accept and project == "helm",
          "sender @acme.com routes to helm (web association, no .env edit)")
    accept, project = inbox_routing.route("someone@random.example",
                                          "plan+switchboard@taikunai.com")
    check(accept and project == "switchboard",
          "plan+switchboard@ plus-address routes to switchboard regardless of sender")
    accept, project = inbox_routing.route("sub.acme.com person <p@east.acme.com>", "plan@taikunai.com")
    check(accept and project == "helm", "subdomain of an associated domain routes too")


def test_env_web_merge():
    print("\n[5] inbox_routing merges PM_INBOX_ROUTES with the web map (web wins)")
    os.environ["PM_INBOX_ROUTES"] = "envonly.com=maxwell, acme.com=maxwell"
    try:
        routes = inbox_routing.routes_map()
        check(routes.get("envonly.com") == "maxwell", "env-only route survives the merge")
        check(routes.get("acme.com") == "helm",
              "web association wins over a conflicting PM_INBOX_ROUTES entry")
    finally:
        os.environ.pop("PM_INBOX_ROUTES", None)


def test_persisted_routes():
    print("\n[6] persisted_routes aggregates every project's domains")
    routes = comms.persisted_routes()
    check(routes.get("acme.com") == "helm" and routes.get("totalenergy.com") == "maxwell",
          "persisted_routes returns the merged domain→project map")


def test_outbound_validation():
    print("\n[7] outbound recipients + cadence validation")
    check(comms.set_outbound("helm", notify_recipients=["bad-addr"]).get("error"),
          "invalid recipient rejected")
    check(comms.set_outbound("helm", cadence="hourly").get("error"),
          "unknown cadence rejected")
    res = comms.set_outbound("helm", notify_recipients=["ops@acme.com"],
                             digest_recipients=["lead@acme.com"], cadence="daily")
    check(res.get("notify_recipients") == ["ops@acme.com"] and res.get("cadence") == "daily",
          "valid outbound config persists")


def test_notify_recipient_resolution():
    print("\n[8] notify resolves per-project recipients, else global fallback")
    # helm has notify_recipients set above; no PM_NOTIFY_EMAIL_TO -> per-project list is used.
    out = notify._email("subj", "body", project="helm", kind="notify")
    check(out.get("dry_run") and out.get("to") == ["ops@acme.com"],
          "helm notify uses helm's recipients (dry-run, no SMTP)")
    # switchboard set nothing -> falls back to the global PM_NOTIFY_EMAIL_TO.
    os.environ["PM_NOTIFY_EMAIL_TO"] = "global@taikunai.com"
    try:
        out = notify._email("subj", "body", project="switchboard", kind="notify")
        check(out.get("to") == ["global@taikunai.com"],
              "a project with no recipients falls back to the global list")
    finally:
        os.environ.pop("PM_NOTIFY_EMAIL_TO", None)


def test_rest_contract():
    print("\n[9] REST contract (GET/POST /comms, /comms/test)")
    try:
        from fastapi.testclient import TestClient
        from app import app
    except ModuleNotFoundError as exc:
        print(f"  SKIP  REST checks need optional dependency: {exc.name}")
        return
    client = TestClient(app)

    r = client.get("/api/projects/switchboard/comms")
    check(r.status_code == 200, "GET /comms 200")
    body = r.json()
    check(body.get("inbound", {}).get("plus_address") == "plan+switchboard@taikunai.com",
          "GET returns the project's plus-address")
    check(body.get("can_edit") is True, "dev-open caller can_edit=True")

    r = client.post("/api/projects/switchboard/comms", json={
        "inbound": {"domains": ["client.io"]},
        "outbound": {"notify_recipients": ["sb@client.io"], "cadence": "weekly"},
    })
    check(r.status_code == 200, "POST /comms 200")
    audit = r.json().get("audit", {})
    check(audit.get("actor") and "inbound_domains" in (audit.get("changes") or {}),
          "POST returns an audited change record (actor + changes)")
    cfg = r.json().get("config", {})
    check(cfg.get("inbound", {}).get("domains") == ["client.io"], "POST persisted the domain")
    check(cfg.get("outbound", {}).get("notify_recipients") == ["sb@client.io"],
          "POST persisted notify recipients")

    r = client.post("/api/projects/switchboard/comms", json={"inbound": {"domains": ["nope"]}})
    check(r.status_code == 400, "POST rejects an invalid domain with 400")

    r = client.post("/api/projects/switchboard/comms/test", json={"kind": "notify"})
    check(r.status_code == 200, "POST /comms/test 200")
    tb = r.json()
    check(tb.get("recipients") == ["sb@client.io"] and isinstance(tb.get("results"), list),
          "test send targets the project's recipients (dry-run)")


def test_ui_wiring():
    # UI-20 (3/6): the standalone Communications modal was folded into the unified Settings
    # shell. The inbound-domain + outbound-recipient editor now renders inline in
    # settings.js `_settingsCommsSection`; the modal + rail button are retired.
    print("\n[10] UI wiring (Settings shell — Communications folded in, modal retired)")
    here = os.path.dirname(os.path.abspath(__file__))
    idx = open(os.path.join(here, "static", "index.html"), encoding="utf-8").read()
    js = read_frontend_source(here)
    check('id="settings-panel"' in idx, "index.html hosts the unified Settings shell")
    for gone in ('id="comms-modal"', 'id="btn-project-comms"'):
        check(gone not in idx, f"legacy {gone} retired from index.html")
    # The editor markup + handlers now live in the frontend source (settings.js).
    for needle in ("_settingsCommsSection", "_settingsCommsAddDomain", "_settingsCommsAddRecipient",
                   "_settingsCommsSave", "_settingsCommsTest",
                   "api/projects/${encodeURIComponent(proj)}/comms"):
        check(needle in js, f"settings.js defines {needle}")
    for needle in ('id="comms-plus"', 'id="comms-domains"', 'id="comms-cadence"'):
        check(needle in js, f"the inbound/outbound editor renders {needle} inline")
    # Inventory rule #2: the admin gate must not stay pinned to the retired #comms-modal
    # selector (it would silently no-op once the markup left the modal). It is now applied
    # inline at render time, driven by the server can_edit probe.
    check("#comms-modal .comms-editable" not in js,
          "the buggy #comms-modal-scoped admin-gate selector is gone")
    check("_commsAdmin" in js and "comms-admin-warn" in js,
          "the comms section still gates edits on the server can_edit probe")


def main():
    setup()
    test_plus_address()
    test_inbound_domains_persist_and_normalize()
    test_one_owner_rule()
    test_routing_acceptance()
    test_env_web_merge()
    test_persisted_routes()
    test_outbound_validation()
    test_notify_recipient_resolution()
    test_rest_contract()
    test_ui_wiring()
    print()
    total = "all checks passed" if not _FAILURES else f"{len(_FAILURES)} check(s) FAILED"
    print(f"UI-14 comms settings: {total}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(1 if _FAILURES else 0)


if __name__ == "__main__":
    main()
