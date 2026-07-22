#!/usr/bin/env python3
"""UI-42: the Fleet dock PR card resolves to ONE authoritative status chip.

The card used to carry two independent status layers: a fixed top-left pill that
only ever said Open/Draft, and a variable badge strip further down that collected
CI / conflicts / merge-queue state. When a PR went bad the pill still said "Open"
and the real news moved around inside the strip, so no two cards put the same
information in the same place.

`_prConditions` now ranks every condition that holds, worst-first, and the card
renders the winner in the fixed slot plus at most ONE muted runner-up. These are
behaviour tests: the real JS is sliced out of static/app.js and executed on node,
so they fail if the ladder is reordered or the second chip starts multiplying.

Run:
    python3 test_ui42_pr_status_chip.py
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

APP = Path(__file__).resolve().parent / "static" / "app.js"
START, END = "    _dockBadge(text, tone, icon) {", "    _renderFleetDock("

FAILED = []


def ok(cond, label):
    print(("   OK   " if cond else "  FAIL  ") + label)
    if not cond:
        FAILED.append(label)


def render(prs):
    """Execute the real _prConditions/_dockPrHtml against `prs` on node."""
    src = APP.read_text(encoding="utf-8")
    body = src[src.index(START):src.index(END)]
    harness = """
const T = {
    esc: (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
        (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
    _fleetAge: () => '3d',
%s
};
const out = INPUT.map((x) => ({
    conditions: T._prConditions(x).map((c) => c.key),
    html: T._dockPrHtml(x),
}));
console.log(JSON.stringify(out));
""" % body
    tmp = Path(tempfile.mkdtemp(prefix="ui42-"))
    try:
        script = tmp / "run.js"
        script.write_text("const INPUT = %s;\n%s" % (json.dumps(prs), harness), encoding="utf-8")
        proc = subprocess.run([shutil.which("node") or "node", str(script)],
                              capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise AssertionError(proc.stderr.strip())
        return json.loads(proc.stdout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if not shutil.which("node"):
    print("  SKIP  UI-42 chip proof requires node")
    sys.exit(0)

BASE = {"number": 1, "url": "https://example.test/1", "title": "t", "updated_at": 0}


def pr(**kw):
    row = dict(BASE)
    row.update(kw)
    return row


CASES = [
    # (label, pr, expected condition ladder)
    ("red CI outranks conflicts and draft",
     pr(ci_state="failure", ci_failing=["VM gate"], mergeable_state="dirty", draft=True),
     ["ci_failed", "conflicts", "draft"]),
    ("conflicts outrank a stall",
     pr(ci_state="success", mergeable_state="dirty", stalled=True),
     ["conflicts", "stalled"]),
    ("a stalled green PR reports stalled, not ready",
     pr(ci_state="success", mergeable_state="clean", stalled=True),
     ["stalled", "ready"]),
    ("queue position outranks ready",
     pr(ci_state="success", mergeable_state="clean", queue_position=2),
     ["queued", "ready"]),
    ("a clean green PR is ready to merge",
     pr(ci_state="success", mergeable_state="clean"),
     ["ready"]),
    ("draft outranks no-checks",
     pr(draft=True), ["draft", "no_checks"]),
    ("a bare PR still yields a chip",
     pr(ci_state="success", mergeable_state="blocked"), ["merge_blocked"]),
]

rows = render([c[1] for c in CASES])
for (label, _, expected), got in zip(CASES, rows):
    ok(got["conditions"] == expected,
       f"{label} — {got['conditions']} == {expected}")

# The card face: winner in the fixed slot, at most one muted runner-up, and the
# left edge tinted with the winner's tone so a column of cards is scannable.
worst = rows[0]["html"]
ok(worst.count('-lt"') == 1, "exactly one toned chip on the card")
ok(worst.count("+ Conflicts") == 1, "the runner-up renders once, muted and prefixed")
ok("Draft" not in worst, "the third condition is dropped, not stacked")
ok("--tblr-red" in worst, "the left edge carries the winner's tone")
ok("bg-transparent border text-secondary" in worst, "the runner-up chip is untoned")

ready = rows[4]["html"]
ok(ready.count('-lt"') == 1 and "+ " not in ready,
   "a single-condition PR renders no runner-up chip")
ok("--tblr-green" in ready, "a ready PR tints its edge green")

# The escaping contract survives: check names come from GitHub, so they are data.
hostile = render([pr(ci_state="failure", ci_failing=['<img src=x onerror="a">'])])[0]["html"]
ok("<img" not in hostile and "&lt;img" in hostile, "check names from GitHub are escaped")

print(("FAIL: %d check(s)" % len(FAILED)) if FAILED else "PASS: UI-42 PR status chip")
sys.exit(1 if FAILED else 0)
