#!/usr/bin/env python3
"""ARCH-MS-43: ixp/v1 version negotiation + field-alias compatibility suite."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT

TMP = tempfile.mkdtemp(prefix="arch-ms43-ixp-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from adapters.switchboard_core import SUPPORTED_PROTOCOL, ensure_compatible  # noqa: E402
from switchboard.domain.ixp.protocol import (  # noqa: E402
    PROTOCOL_ENVELOPE,
    apply_field_aliases,
    check_protocol_compatibility,
    field_aliases_for,
    negotiate_protocol,
    normalize_send_ack_deadline,
    protocol_envelope,
    render_protocol_envelope_json,
)

FIXTURES = ROOT / "fixtures" / "ixp"
GOLDEN_ENVELOPE = FIXTURES / "protocol_envelope.v1.json"
ALIAS_VECTORS = FIXTURES / "send_message_ack_aliases.v1.json"

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


# --- package + golden envelope ----------------------------------------------
ok((ROOT / "src/switchboard/domain/ixp/protocol.py").is_file(),
   "src/switchboard/domain/ixp/protocol.py exists")
ok(store.PROTOCOL_ENVELOPE == PROTOCOL_ENVELOPE,
   "store.PROTOCOL_ENVELOPE re-exports the domain envelope")
ok(GOLDEN_ENVELOPE.is_file(), "fixtures/ixp/protocol_envelope.v1.json exists")

rendered = render_protocol_envelope_json()
ok(rendered == GOLDEN_ENVELOPE.read_text(encoding="utf-8"),
   "golden protocol envelope matches render_protocol_envelope_json()")
ok(rendered == render_protocol_envelope_json(protocol_envelope()),
   "render_protocol_envelope_json is deterministic")
ok('"field_aliases"' in rendered and '"compatible_versions"' in rendered,
   "envelope documents field_aliases + compatible_versions")

# --- check_protocol_compatibility -------------------------------------------
exact = check_protocol_compatibility({"version": "ixp.v1", "profile": "p0-dogfood"})
ok(exact["compatible"] is True and exact["mode"] == "exact",
   "ixp.v1 advertisement is exact-compatible")
ok(exact.get("version") == "ixp.v1", "exact result carries version")

ixp_key = check_protocol_compatibility({"ixp_version": "ixp.v1"})
ok(ixp_key["compatible"] is True and ixp_key["mode"] == "exact",
   "ixp_version alias key is accepted")

legacy = check_protocol_compatibility(None)
ok(legacy["compatible"] is True and legacy["mode"] == "legacy_assumed",
   "missing advertisement is legacy_assumed")

rejected = check_protocol_compatibility({"version": "ixp.v9"})
ok(rejected["compatible"] is False and rejected["mode"] == "reject",
   "unsupported version is rejected")

# --- negotiate_protocol (intersection) --------------------------------------
neg_exact = negotiate_protocol(
    {"version": "ixp.v1", "compatible_versions": ["ixp.v1"]},
    PROTOCOL_ENVELOPE,
)
ok(neg_exact["compatible"] is True and neg_exact["mode"] == "exact",
   "negotiate prefers client's advertised version when in intersection")
ok(neg_exact["negotiated_version"] == "ixp.v1", "negotiated_version is ixp.v1")

# Server that still speaks v1 + a hypothetical v2; client only speaks v1.
server_multi = {
    **PROTOCOL_ENVELOPE,
    "compatible_versions": ["ixp.v1", "ixp.v2"],
    "version": "ixp.v2",
}
neg_pick = negotiate_protocol(
    {"compatible_versions": ["ixp.v1"], "version": "ixp.v1"},
    server_multi,
)
ok(neg_pick["compatible"] is True and neg_pick["negotiated_version"] == "ixp.v1",
   "negotiate intersects multi-version server down to shared ixp.v1")

neg_reject = negotiate_protocol(
    {"version": "ixp.v9", "compatible_versions": ["ixp.v9"]},
    PROTOCOL_ENVELOPE,
)
ok(neg_reject["compatible"] is False and neg_reject["mode"] == "reject",
   "negotiate rejects empty intersection")

neg_legacy = negotiate_protocol(None, PROTOCOL_ENVELOPE)
ok(neg_legacy["compatible"] is True and neg_legacy["mode"] == "legacy_assumed",
   "negotiate treats missing client as legacy_assumed")

# --- field aliases ----------------------------------------------------------
aliases = field_aliases_for("send_agent_message")
ok(aliases.get("ack_timeout_s") == "ack_deadline_minutes",
   "ack_timeout_s aliases to ack_deadline_minutes")
ok(aliases.get("ack_timeout_seconds") == "ack_deadline_minutes",
   "ack_timeout_seconds aliases to ack_deadline_minutes")

ok(ALIAS_VECTORS.is_file(), "fixtures/ixp/send_message_ack_aliases.v1.json exists")
vectors = json.loads(ALIAS_VECTORS.read_text(encoding="utf-8"))
expected_minutes = float(vectors["canonical_minutes"])
resolved = []
for vector in vectors["vectors"]:
    minutes = normalize_send_ack_deadline(**vector["input"])
    resolved.append(minutes)
    ok(minutes == expected_minutes,
       f"alias vector {vector['label']} → {expected_minutes} minutes")
ok(len(set(resolved)) == 1, "all ack alias vectors resolve to the same deadline")

body = apply_field_aliases(
    "send_agent_message",
    {"ack_timeout_s": 120, "message": "hi"},
)
ok(body.get("ack_deadline_minutes") == 2.0 and "ack_timeout_s" not in body,
   "apply_field_aliases converts ack_timeout_s and drops the alias key")

# Canonical minutes wins over seconds alias.
prefer = apply_field_aliases(
    "send_agent_message",
    {"ack_deadline_minutes": 3, "ack_timeout_seconds": 90},
)
ok(prefer.get("ack_deadline_minutes") == 3,
   "explicit ack_deadline_minutes wins over seconds aliases")

# --- working agreement + register_agent surfaces ----------------------------
store.init_db("switchboard")
agreement = store.get_working_agreement(project="switchboard")
proto = agreement.get("protocol") or {}
ok(proto.get("version") == "ixp.v1", "get_working_agreement advertises ixp.v1")
ok("field_aliases" in proto and "compatible_versions" in proto,
   "working agreement protocol includes aliases + compatible_versions")

reg = store.register_agent(
    "cursor/ARCH-MS-43-proof",
    runtime="cursor",
    project="switchboard",
    protocol={"version": "ixp.v1", "profile": "p0-dogfood"},
    ttl_s=60,
)
compat = reg.get("protocol_compatibility") or {}
ok(compat.get("compatible") is True and compat.get("mode") == "exact",
   "register_agent returns top-level protocol_compatibility")
presence = {
    a["agent_id"]: a
    for a in store.list_active_agents(project="switchboard")
}
stored = (presence.get("cursor/ARCH-MS-43-proof") or {}).get("control") or {}
ok((stored.get("protocol_compatibility") or {}).get("compatible") is True,
   "protocol_compatibility is persisted under agent_presence.control")
ok((stored.get("protocol") or {}).get("version") == "ixp.v1",
   "advertised protocol is persisted under agent_presence.control")

bad = store.register_agent(
    "cursor/ARCH-MS-43-bad",
    runtime="cursor",
    project="switchboard",
    protocol={"version": "ixp.v9"},
    ttl_s=60,
)
ok((bad.get("protocol_compatibility") or {}).get("compatible") is False,
   "register_agent rejects ixp.v9 at protocol_compatibility")

# --- send_agent_message alias parity (store) --------------------------------
store.register_agent("cursor/ARCH-MS-43-peer", runtime="cursor",
                     project="switchboard", ttl_s=60)
msg_minutes = store.send_agent_message(
    "cursor/ARCH-MS-43-proof", "cursor/ARCH-MS-43-peer", "minutes",
    requires_ack=True, ack_deadline_minutes=1.5, project="switchboard",
)
msg_seconds = store.send_agent_message(
    "cursor/ARCH-MS-43-proof", "cursor/ARCH-MS-43-peer", "seconds",
    requires_ack=True, ack_timeout_seconds=90, project="switchboard",
)
msg_s = store.send_agent_message(
    "cursor/ARCH-MS-43-proof", "cursor/ARCH-MS-43-peer", "short",
    requires_ack=True, ack_timeout_s=90, project="switchboard",
)
ok(msg_minutes.get("ack_deadline") and msg_seconds.get("ack_deadline")
   and msg_s.get("ack_deadline"),
   "ack alias variants create ack deadlines on send_agent_message")
# Deadlines are absolute timestamps; compare deltas from now is flaky — assert
# the three records all carry a numeric deadline within a tight band.
deadlines = [float(m["ack_deadline"]) for m in (msg_minutes, msg_seconds, msg_s)]
ok(max(deadlines) - min(deadlines) < 2.0,
   "ack_deadline_minutes / ack_timeout_seconds / ack_timeout_s agree within 2s")

# --- adapter ensure_compatible ----------------------------------------------
ensure_compatible({"protocol": PROTOCOL_ENVELOPE})
ok(True, "ensure_compatible accepts current PROTOCOL_ENVELOPE")
try:
    ensure_compatible({"protocol": {"version": "ixp.v9", "compatible_versions": ["ixp.v9"]}})
    ok(False, "ensure_compatible should reject ixp.v9")
except RuntimeError:
    ok(True, "ensure_compatible rejects unsupported server versions")
ok(SUPPORTED_PROTOCOL["version"] == "ixp.v1",
   "adapter SUPPORTED_PROTOCOL stays on ixp.v1")

# --- docs ------------------------------------------------------------------
conformance = (ROOT / "docs" / "IXP-CONFORMANCE.md").read_text(encoding="utf-8")
ok("ARCH-MS-43" in conformance or "field_aliases" in conformance,
   "docs/IXP-CONFORMANCE.md mentions protocol compatibility suite")

print()
print(f"{passed} passed, {failed} failed")
shutil.rmtree(TMP, ignore_errors=True)
raise SystemExit(1 if failed else 0)
