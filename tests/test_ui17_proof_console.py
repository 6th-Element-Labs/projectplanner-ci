#!/usr/bin/env python3
"""UI-17: Mission Proof Console — reuse Tabler Mission/Fleet/Watch; fail-closed evidence."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from path_setup import ROOT  # noqa: F401
from scripts.frontend_test_source import read_frontend_source

STATIC = Path(ROOT) / "static"
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
PROOF = (STATIC / "js" / "proof-console.js").read_text(encoding="utf-8")
MISSION = (STATIC / "js" / "mission.js").read_text(encoding="utf-8")
CSS = (STATIC / "taikun-tabler.css").read_text(encoding="utf-8")
DESIGN = (Path(ROOT) / "docs" / "OPERATOR-UI-DESIGN.md").read_text(encoding="utf-8")
APP = (STATIC / "app.js").read_text(encoding="utf-8")
COMPOSED = read_frontend_source(str(ROOT))

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


# ---- Module wiring -----------------------------------------------------------
ok('src="js/proof-console.js?v=' in INDEX, "index.html loads proof-console.js")
proof_pos = INDEX.find('src="js/proof-console.js?v=')
runner_pos = INDEX.find('src="js/runner-session.js?v=')
app_pos = INDEX.find('src="app.js?v=')
ok(0 <= runner_pos < proof_pos < app_pos,
   "proof-console.js loads after runner-session and before app.js")
ok("SwitchboardProofConsole.methods" in APP,
   "app.js composes SwitchboardProofConsole")
ok("SwitchboardProofConsole" in PROOF, "proof-console publishes SwitchboardProofConsole")

# ---- Design tokens / reuse (no second frontend) ------------------------------
ok("taikun-tabler.css" in INDEX, "index still loads taikun-tabler.css")
ok("--tblr-primary" in CSS or "#c0392b" in CSS.lower() or "c0392b" in CSS.lower(),
   "Tabler primary brand token remains in taikun-tabler.css")
ok("## UI-17" in DESIGN and "proof-console.js" in DESIGN,
   "OPERATOR-UI-DESIGN.md documents UI-17 Proof Console")
ok("Vue" not in PROOF and "react" not in PROOF.lower() and "createRoot" not in PROOF,
   "proof console does not introduce a second UI framework")

# ---- Deep link + Mission integration -----------------------------------------
ok("_proofModeFromUrl" in PROOF and "mode" in PROOF and "proof" in PROOF,
   "proof mode reads ?proof=1 / mode=proof")
ok("proofMode" in INDEX or "modeParam" in INDEX,
   "index.html deep-links Mission for proof query")
ok("mission-proof-toggle" in MISSION and "proofConsoleHtml" in MISSION,
   "Mission page toggles and embeds Proof Console")
ok("Proof console" in MISSION or "proof console" in MISSION.lower(),
   "Mission header exposes Proof console control")

# ---- Surface: identity, providers, Arm, Watch reuse --------------------------
needles = (
    "proof-console",
    "proofConsoleHtml",
    "armProofConsole",
    "coordinator_tick",
    "proof-identity-grid",
    "proof-provider-table",
    "task_id",
    "claim_id",
    "Work Session",
    "runner_session_id",
    "provider identity ref",
    "source SHA",
    "Codex",
    "Claude Code",
    "Cursor",
    "configured",
    "initialize",
    "tools_list",
    "bound_read",
    "allowed_scoped_action",
    "cross_scope_denial",
    "expiry_revocation",
    "cleanup",
    "_proofRedact",
    "[redacted]",
    "bg-red-lt",
    "proof blocked",
    "runnerControlHtml",
    "openRunnerWatch",
    "COORD-34",
    "CO-14",
)
for needle in needles:
    ok(needle in COMPOSED, f"composed frontend exposes UI-17 needle: {needle}")

ok("Start / Arm" in PROOF or "Start/Arm" in PROOF or "proof-arm" in PROOF,
   "Start/Arm control is present")
ok("proof-cleanup-grid" in PROOF and "AWS fleet-zero" in PROOF,
   "cleanup / provenance evidence grid is present")
ok("fail closed" in PROOF.lower() or "fail-closed" in PROOF.lower()
   or "blocks a green" in PROOF.lower() or "proof blocked" in PROOF,
   "fail-closed green-proof gate is documented in UI")

# ---- Redaction + fail-closed behavioral proof (Node) -------------------------
node_proof = r"""
const fs = require('fs');
const path = process.argv[1];
const src = fs.readFileSync(path, 'utf8');
const window = {
  PM_PROJECT: 'switchboard',
  location: { href: 'http://localhost/?proof=1&project=switchboard' },
  history: { replaceState() {} },
  document: { getElementById() { return null; } },
};
global.window = window;
global.document = window.document;
eval(src);
const m = window.SwitchboardProofConsole.methods;
const ctx = {
  esc(s) { return String(s == null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;'); },
  canWriteProjects: true,
  isAdmin: true,
  principal: { effective_scopes: ['write:tasks'] },
  runnerControlHtml() { return '<div id="runner-control-panel" data-task-id="T1"></div>'; },
  ...m,
};
const secret = ctx._proofRedact('Bearer sk-abc123SECRETTOKEN');
if (secret !== '[redacted]') {
  console.error('redact_failed', secret);
  process.exit(2);
}
const long = ctx._proofRedact('A'.repeat(64));
if (!String(long).includes('[redacted]')) {
  console.error('long_blob_not_redacted', long);
  process.exit(3);
}
const emptyBind = { selectedTaskId: '', watch: null, runner: null, workSession: null, providerConnections: [] };
const verdict = ctx._proofVerdict(emptyBind, { deliverable: { id: 'd1' }, done_with_proof: [] });
if (verdict.green) {
  console.error('empty_bind_should_block_green', verdict);
  process.exit(4);
}
if (!String(verdict.badge).includes('bg-red-lt')) {
  console.error('blocked_badge_not_red', verdict.badge);
  process.exit(5);
}
const html = ctx.proofConsoleHtml({ deliverable: { id: 'd1' }, deliverable_id: 'd1', next_actions: [], active_work: [] }, emptyBind);
if (!html.includes('id="proof-console"') || !html.includes('proof-provider-table')) {
  console.error('missing_console_markup');
  process.exit(6);
}
if (!html.includes('Codex') || !html.includes('Claude Code') || !html.includes('Cursor')) {
  console.error('missing_provider_rows');
  process.exit(7);
}
if (!html.includes('bg-red-lt') || !html.includes('proof blocked')) {
  console.error('missing_fail_closed_visuals');
  process.exit(8);
}
if (!html.includes('No linked task available for Watch/Chat')) {
  console.error('empty_bind_should_fail_closed_watch');
  process.exit(9);
}
// With full CO-14 probe + cleanup + bind, green is allowed.
const probes = {};
for (const k of window.SwitchboardProofConsole.MCP_PROBE_KEYS) probes[k] = { ok: true, status: 'ok' };
const providers = {};
for (const p of window.SwitchboardProofConsole.PROVIDER_ROWS) providers[p.id] = probes;
const good = {
  selectedTaskId: 'T1',
  watch: { watchable: true, runner_session_id: 'rs1' },
  workSession: { work_session_id: 'ws1', claim_id: 'cl1' },
  runner: {
    runner_session_id: 'rs1', claim_id: 'cl1', host_id: 'host1', runtime: 'codex',
    metadata: {
      provider_identity_ref: 'acct-ref-1',
      source_sha: 'abc1234',
      provider_cli: 'codex',
      placement: 'dedicated_host',
      mcp_probe: providers,
      credential_cleanup: 'purged',
      host_drain: 'drained',
      aws_fleet_zero: 'zero',
    },
  },
  providerConnections: [{ provider: 'codex', id: 'conn1' }],
};
const ready = ctx._proofVerdict(good, { deliverable: { id: 'd1' }, done_with_proof: [{ provenance: { label: 'merged' } }] });
if (!ready.green) {
  console.error('full_evidence_should_be_green', ready);
  process.exit(10);
}
const enrollOnly = {
  selectedTaskId: 'T1',
  watch: { watchable: true, runner_session_id: 'rs1' },
  workSession: { work_session_id: 'ws1', claim_id: 'cl1' },
  runner: {
    runner_session_id: 'rs1', claim_id: 'cl1', host_id: 'host1', runtime: 'codex',
    metadata: {
      mcp_probe: providers,
      credential_cleanup: 'purged',
      host_drain: 'drained',
      aws_fleet_zero: 'zero',
    },
  },
  providerConnections: [{ provider: 'codex', id: 'conn1' }],
};
const enrollVerdict = ctx._proofVerdict(enrollOnly, { deliverable: { id: 'd1' }, done_with_proof: [{ provenance: { label: 'merged' } }] });
if (enrollVerdict.green || !enrollVerdict.missing.includes('provider identity reference')) {
  console.error('enrolled_connection_without_bound_identity_must_block', enrollVerdict);
  process.exit(12);
}
const failingCleanup = JSON.parse(JSON.stringify(good));
failingCleanup.runner.metadata.credential_cleanup = 'failed';
failingCleanup.runner.metadata.host_drain = 'pending';
failingCleanup.runner.metadata.aws_fleet_zero = 'missing';
const badCleanup = ctx._proofVerdict(failingCleanup, { deliverable: { id: 'd1' }, done_with_proof: [{ provenance: { label: 'merged' } }] });
if (badCleanup.green || !badCleanup.missing.includes('cleanup evidence')) {
  console.error('failed_cleanup_strings_must_block', badCleanup);
  process.exit(13);
}
const readyHtml = ctx.proofConsoleHtml({ deliverable: { id: 'd1' }, deliverable_id: 'd1', next_actions: [], active_work: [], done_with_proof: [{ provenance: { label: 'merged' } }] }, good);
if (!readyHtml.includes('runner-control-panel') || !readyHtml.includes('proof ready')) {
  console.error('watch_chat_not_reused_or_not_green', readyHtml.slice(0, 400));
  process.exit(11);
}
console.log('ui17_proof_ok');
"""
proof_path = STATIC / "js" / "proof-console.js"
run = subprocess.run(
    ["node", "-e", node_proof, str(proof_path)],
    capture_output=True, text=True, cwd=str(ROOT),
)
ok(run.returncode == 0 and "ui17_proof_ok" in (run.stdout or ""),
   f"Node redaction/fail-closed/green-path proof runs (rc={run.returncode})")
if run.returncode != 0:
    print((run.stderr or run.stdout or "")[:800])

# ---- Guard: no hardcoded secrets in source -----------------------------------
ok(not re.search(r"sk-[A-Za-z0-9]{20,}", PROOF),
   "proof-console.js does not embed live-looking API secret material")

print(f"\nUI-17 proof console: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
