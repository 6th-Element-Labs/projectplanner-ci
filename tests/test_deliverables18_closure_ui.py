#!/usr/bin/env python3
"""DELIVERABLES-18: executable Mission Page closure-control proof."""
import json
import subprocess

from path_setup import ROOT


STATIC = ROOT / "static"
CLOSURE = STATIC / "js" / "closure.js"
MISSION = (STATIC / "js" / "mission.js").read_text(encoding="utf-8")
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
APP = (STATIC / "app.js").read_text(encoding="utf-8")

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


ok(CLOSURE.is_file(), "closure UI lives in its own frontend boundary")
ok(INDEX.index('src="js/closure.js?v=') < INDEX.index('src="js/mission.js?v='),
   "closure boundary loads before the Mission Page composes it")
ok("...window.SwitchboardClosure.methods" in APP,
   "SPA composition root installs closure methods")
ok("this.loadClosureReport(this.selectedDeliverableId)" in MISSION,
   "initial Mission Page load fetches the latest closure report")
ok("this.loadClosureReport(id)" in MISSION,
   "live Mission Page polling refreshes closure stamps")
ok("case 'closure-request': return this.requestClosureVerification()" in MISSION,
   "Mission Page action routing reaches the closure dispatcher")

node_proof = r"""
const fs = require('fs');
global.window = global;
const button = { disabled: false };
global.document = { getElementById: (id) => id === 'mission-closure-request' ? button : null };
eval(fs.readFileSync(process.argv[1], 'utf8'));
const methods = global.SwitchboardClosure.methods;
const esc = (value) => String(value == null ? '' : value)
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
const base = Object.assign({}, methods, {
    esc,
    _pmProject: () => 'switchboard',
    selectedDeliverableId: 'deliverable-closure-gate',
    missionClosureRequest: {},
});
const reportCtx = Object.assign({}, base, {
    missionClosure: { report: {
        schema: 'switchboard.deliverable_closure_report.v1',
        report_id: 'closure-proof-7',
        grade: 'hold',
        recommendation: 'repair_failed_checks',
        generated_by: 'verifier/test',
        gates: {
            scope: { pass: false, checks: [
                { id: 'terminal_tasks', pass: false, message: 'One linked task is still active.' },
            ] },
            functional: { pass: true, checks: [
                { id: 'harness:closure', pass: true, summary: 'Harness passed.' },
            ] },
        },
    } },
});
const reportHtml = methods._missionClosureHtml.call(reportCtx);
const emptyHtml = methods._missionClosureHtml.call(Object.assign({}, base, {
    missionClosure: { report: null, missing: true },
}));
const errorHtml = methods._missionClosureHtml.call(Object.assign({}, base, {
    missionClosure: { report: null, error: 'backend unavailable' },
}));
const calls = [];
let rendered = 0;
const requestCtx = Object.assign({}, base, {
    missionClosure: { report: null },
    _dlSend: async (...args) => { calls.push(args); return { dispatched: true, agent_id: 'verifier/closure/proof' }; },
    loadClosureReport: async function (id) { this.loadedClosureId = id; return this.missionClosure; },
    renderMissionPage: () => { rendered += 1; },
});
(async () => {
    await methods.requestClosureVerification.call(requestCtx);
    const failedCtx = Object.assign({}, base, {
        missionClosure: { report: null },
        _dlSend: async () => ({ dispatched: false, error: 'wake not created' }),
        renderMissionPage: () => {},
    });
    await methods.requestClosureVerification.call(failedCtx);
    console.log(JSON.stringify({
        action: methods._missionClosureActionHtml.call(base),
        reportHtml, emptyHtml, errorHtml, calls,
        loadedClosureId: requestCtx.loadedClosureId,
        requestState: requestCtx.missionClosureRequest,
        rendered,
        buttonDisabled: button.disabled,
        failedState: failedCtx.missionClosure,
    }));
})().catch((error) => { console.error(error); process.exit(1); });
"""

run = subprocess.run(
    ["node", "-e", node_proof, str(CLOSURE)],
    cwd=ROOT,
    text=True,
    capture_output=True,
    check=False,
)
ok(run.returncode == 0, "closure UI behavior proof executes in Node")
result = json.loads(run.stdout) if run.returncode == 0 and run.stdout.strip() else {}
action = result.get("action", "")
report_html = result.get("reportHtml", "")
ok("Verify &amp; stamp closure" in action and "data-dl-action=\"closure-request\"" in action,
   "header exposes a closure action distinct from Record outcome")
ok("GRADE HOLD" in report_html and "repair_failed_checks" in report_html,
   "latest closure grade and report summary are visible")
ok("terminal_tasks" in report_html and "FAIL" in report_html
   and "harness:closure" in report_html and "PASS" in report_html,
   "per-check pass and fail results render with evidence summaries")
ok("report_id=closure-proof-7" in report_html and "target=\"_blank\"" in report_html,
   "full-report link addresses the exact stamped report")
ok("No closure report yet" in result.get("emptyHtml", "")
   and "NOT STAMPED" in result.get("emptyHtml", ""),
   "missing report is explicit rather than optimistically green")
ok("Closure report unavailable" in result.get("errorHtml", "")
   and "backend unavailable" in result.get("errorHtml", ""),
   "report transport failures stay visibly red")
calls = result.get("calls") or []
ok(bool(calls) and calls[0][0].endswith("/closure_request")
   and calls[0][1:] == ["POST", {}],
   "header action POSTs the existing closure verifier-dispatch REST route")
ok(result.get("loadedClosureId") == "deliverable-closure-gate"
   and result.get("requestState", {}).get("dispatched") is True
   and result.get("rendered") == 1 and result.get("buttonDisabled") is False,
   "successful dispatch refreshes report state, rerenders, and restores the control")
ok(result.get("failedState", {}).get("error") == "wake not created",
   "a rejected verifier wake stays visibly failed even when REST returned HTTP 200")

# Dismiss behaviour: an X hides the closure card on a re-opened deliverable, the hide
# survives the live-refresh re-render, and the card returns only when a NEW report is
# stamped (or the operator re-clicks the header dispatch, which clears the flag).
dismiss_proof = r"""
const fs = require('fs');
const store = {};
global.window = global;
global.window.localStorage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
};
eval(fs.readFileSync(process.argv[1], 'utf8'));
const methods = global.SwitchboardClosure.methods;
const esc = (v) => String(v == null ? '' : v).replaceAll('&', '&amp;').replaceAll('<', '&lt;');
const reportState = (rid) => ({ report: { report_id: rid, grade: 'pass', gates: {} } });
const ctx = Object.assign({}, methods, {
    esc,
    _pmProject: () => 'switchboard',
    selectedDeliverableId: 'd-1',
    missionClosureRequest: {},
    missionClosure: reportState('closure-A'),
    renderMissionPage: () => {},
});
const before = methods._missionClosureHtml.call(ctx);
methods.dismissClosure.call(ctx);
const afterDismiss = methods._missionClosureHtml.call(ctx);
// A live re-render with the SAME report is still hidden:
const stillHidden = methods._missionClosureHtml.call(ctx);
// A brand-new stamped report re-appears on its own:
ctx.missionClosure = reportState('closure-B');
const afterNewReport = methods._missionClosureHtml.call(ctx);
// Dismiss again, then the header dispatch button clears it immediately (before any new report):
ctx.missionClosure = reportState('closure-B');
methods.dismissClosure.call(ctx);
const hiddenAgain = methods._missionClosureHtml.call(ctx);
ctx._dlSend = async () => ({ dispatched: true });
ctx.loadClosureReport = async function () { return this.missionClosure; };
methods._undismissClosure.call(ctx, 'd-1');
const afterUndismiss = methods._missionClosureHtml.call(ctx);
console.log(JSON.stringify({
    hasDismissButton: before.includes('data-dl-action="closure-dismiss"'),
    afterDismiss, stillHidden,
    reappearsOnNewReport: afterNewReport.includes('data-dl-action="closure-dismiss"'),
    hiddenAgain,
    afterUndismiss,
}));
"""
drun = subprocess.run(["node", "-e", dismiss_proof, str(CLOSURE)],
                      cwd=ROOT, text=True, capture_output=True, check=False)
ok(drun.returncode == 0, "closure dismiss proof executes in Node")
dres = json.loads(drun.stdout) if drun.returncode == 0 and drun.stdout.strip() else {}
ok(dres.get("hasDismissButton") is True,
   "the closure card renders a dismiss (X) control")
ok(dres.get("afterDismiss") == "" and dres.get("stillHidden") == "",
   "dismissing hides the card and it stays hidden across live-refresh re-renders")
ok(dres.get("reappearsOnNewReport") is True,
   "a newly stamped report reappears on its own after a prior dismiss")
ok(dres.get("hiddenAgain") == "" and dres.get("afterUndismiss") != "",
   "re-dispatching (clearing the flag) brings the card back before any new report")

print(f"\nDELIVERABLES-18 closure UI proof: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
