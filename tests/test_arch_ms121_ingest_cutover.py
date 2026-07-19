"""ARCH-MS-121 production Ingest cutover contract."""
from __future__ import annotations

import json

from path_setup import ROOT



def test_ingest_unit_is_production_hardened_and_resource_bounded():
    unit = (ROOT / "deploy/switchboard-ingest.service").read_text()
    for required in (
        "User=projectplanner", "EnvironmentFile=/opt/projectplanner/.env",
        "--factory switchboard.services.ingest.app:create_app", "--port 8126",
        "NoNewPrivileges=yes", "ProtectSystem=strict", "ReadWritePaths=/var/lib/projectplanner",
        "MemoryMax=64M",
    ):
        assert required in unit


def test_caddy_owns_only_the_two_method_exact_routes():
    caddy = (ROOT / "deploy/Caddyfile").read_text()
    assert "@ingest_inbox_read {\n        method GET\n        path /api/inbox\n" in caddy
    assert "@ingest_text_intake {\n        method POST\n        path /api/intake\n" in caddy
    assert caddy.count("reverse_proxy 127.0.0.1:8126") == 2
    assert caddy.count("health_uri /ready", caddy.index("@ingest_inbox_read")) >= 2
    assert "path /api/intake/upload" not in caddy


def test_inventory_makes_health_readiness_edge_and_rollback_declarative():
    inventory = json.loads((ROOT / "deploy/service-cut-inventory.json").read_text())
    ingest = next(row for row in inventory["services"] if row["name"] == "switchboard-ingest")
    assert ingest == {
        "name": "switchboard-ingest", "unit": "switchboard-ingest.service", "port": 8126,
        "health": "/health", "ready": "/ready", "restart_order": 60,
        "snapshot": True, "rollback_lifecycle": True,
        "edge_owns": ["@ingest_inbox_read", "@ingest_text_intake"],
        "first_cut_edge_before_monolith_restart": True,
    }


def test_deploy_is_fail_closed_and_restores_ingest_lifecycle():
    deploy = (ROOT / "deploy/redeploy.sh").read_text()
    assert 'INGEST_CUT_WAS_LIVE=0' in deploy
    assert 'sync_caddy_fail_closed.sh' in deploy
    assert deploy.index('sync_caddy_fail_closed.sh') < deploy.index('section "Ingest dual-strip monolith"')
    for required in (
        "INGEST_WAS_ACTIVE", "INGEST_WAS_ENABLED", "INGEST_UNIT_LIVE",
        "switchboard-ingest.service.present", "restart switchboard-ingest",
        "http://127.0.0.1:8126/health", "stop switchboard-ingest",
    ):
        assert required in deploy


def test_monolith_dual_strip_is_explicit():
    unit = (ROOT / "deploy/projectplanner.service").read_text()
    app = (ROOT / "app_impl.py").read_text()
    router = (ROOT / "src/switchboard/api/routers/intake_inbox.py").read_text()
    assert "Environment=PM_INGEST_HTTP_PRIMARY=service" in unit
    assert '_INGEST_HTTP_PRIMARY == "service"' in app
    assert "sibling_bc_only: bool = False" in router
    assert router.count("if not sibling_bc_only:") == 2
