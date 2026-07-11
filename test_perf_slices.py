#!/usr/bin/env python3
"""PERF-4 — static checks for interactive vs batch systemd slice hierarchy."""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
passed = failed = 0

INTERACTIVE = (
    "deploy/projectplanner.service",
    "deploy/projectplanner-mcp.service",
    "deploy/projectplanner-gateway.service",
    "deploy/projectplanner-agent-host.service",
)
BATCH = (
    "deploy/projectplanner-narrate.service",
    "deploy/projectplanner-reconcile.service",
    "deploy/projectplanner-ci-gate.service",
    "deploy/projectplanner-monitors.service",
    "deploy/projectplanner-inbox.service",
    "deploy/projectplanner-summarize.service",
    "deploy/projectplanner-digest.service",
    "deploy/projectplanner-backup.service",
)


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def read(rel_path):
    with open(os.path.join(ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


interactive_slice = read("deploy/projectplanner-interactive.slice")
batch_slice = read("deploy/projectplanner-batch.slice")
guards = read("deploy/apply-resource-guards.sh")
verify = read("scripts/verify_cgroup_slices.sh")
provision = read("deploy/PROVISION.md")

ok("CPUWeight=900" in interactive_slice, "interactive slice reserves top CPU share")
ok("MemoryLow=250M" in interactive_slice, "interactive slice sets memory floor")
ok("MemorySwapMax=0" in interactive_slice, "interactive slice forbids swap")
ok("CPUQuota=80%" in batch_slice, "batch slice caps CPU at ~40% of 2-vCPU box")
ok("IOWeight=10" in batch_slice, "batch slice deprioritizes I/O")
ok("CPUWeight=50" in batch_slice, "batch slice has low CPU weight")

for path in INTERACTIVE:
    text = read(path)
    ok("Slice=projectplanner-interactive.slice" in text,
       f"{path} joins interactive slice")

for path in BATCH:
    text = read(path)
    ok("Slice=projectplanner-batch.slice" in text,
       f"{path} joins batch slice")
    ok("Nice=10" in text, f"{path} runs at Nice=10")

ci_gate = read("deploy/projectplanner-ci-gate.service")
reconcile = read("deploy/projectplanner-reconcile.service")
ok("MemoryMax=320M" in ci_gate, "ci-gate keeps strict 320M MemoryMax")
ok("MemoryMax=512M" in reconcile, "reconcile keeps production-validated 512M MemoryMax")

ok("projectplanner-interactive.slice" in guards and "projectplanner-batch.slice" in guards,
   "apply-resource-guards installs both slice units")
ok("reset-property" in guards, "apply-resource-guards clears legacy set-property overrides")

ok("projectplanner-interactive.slice" in verify, "verify script checks interactive slice")
ok("projectplanner-batch.slice" in verify, "verify script checks batch slice")
ok("Slice=projectplanner-interactive.slice" in verify, "verify script checks interactive Slice=")

ok("projectplanner-interactive.slice" in provision and "projectplanner-batch.slice" in provision,
   "PROVISION.md installs slice units")
ok("verify_cgroup_slices.sh" in provision, "PROVISION.md documents slice verification")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
