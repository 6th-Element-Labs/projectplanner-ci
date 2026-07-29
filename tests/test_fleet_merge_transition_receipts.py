#!/usr/bin/env python3
"""Fleet dock preserves the visible handoff from PRs to Deploy."""
from __future__ import annotations

import json
import subprocess

from path_setup import ROOT


app_source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
assert "moved to Deploy" in app_source
assert "data-dock-go-deploy" in app_source

script = ROOT / "static" / "js" / "fleet-dock.js"
node = f"""
const fs = require('fs');
const vm = require('vm');
const context = {{window: {{}}}};
vm.createContext(context);
vm.runInContext(fs.readFileSync({json.dumps(str(script))}, 'utf8'), context);
const dock = context.window.SwitchboardFleetDock;
const app = {{}};
const pr1058 = {{number: 1058, title: 'First', url: '/pull/1058'}};
const pr1059 = {{number: 1059, title: 'Second', url: '/pull/1059'}};
const pr1063 = {{number: 1063, title: 'Still open', url: '/pull/1063'}};
dock.updateMergeReceipts(app, [pr1058, pr1059, pr1063], [], 1000);
let receipts = dock.updateMergeReceipts(app, [pr1059, pr1063], [], 2000);
if (receipts.length) throw new Error('a disappeared PR was treated as merged');
app._dockPreviousPrs = [pr1058, pr1059, pr1063];
receipts = dock.updateMergeReceipts(app, [pr1063], [
  {{number: 1058, title: 'First', merged_at: '2026-07-29T03:26:07Z'}},
  {{number: 1059, title: 'Second', merged_at: '2026-07-29T03:27:07Z'}}
], 3000);
if (receipts.length !== 2 || !receipts.some((x) => x.number === 1058)
    || !receipts.some((x) => x.number === 1059)) {{
  throw new Error('quick confirmed merges did not stack');
}}
receipts = dock.updateMergeReceipts(app, [pr1063], [], 63001);
if (receipts.length) throw new Error('merge receipts did not expire');
console.log('fleet_merge_receipts_ok');
"""
result = subprocess.run(
    ["node", "-e", node], capture_output=True, text=True, check=False)
assert result.returncode == 0, result.stderr or result.stdout
assert "fleet_merge_receipts_ok" in result.stdout

print("PASS fleet merge transition receipts stack, require merge proof, and expire")
