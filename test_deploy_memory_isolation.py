#!/usr/bin/env python3
"""PERF-3/4 — static checks for zram swap + cgroup slice isolation."""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def read(rel_path):
    with open(os.path.join(ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


zram = read("deploy/setup-zram-swap.sh")
guards = read("deploy/apply-resource-guards.sh")
verify_mem = read("scripts/verify_memory_isolation.sh")
verify_slices = read("scripts/verify_cgroup_slices.sh")
interactive_slice = read("deploy/projectplanner-interactive.slice")
reconcile = read("deploy/projectplanner-reconcile.service")
provision = read("deploy/PROVISION.md")

ok(os.path.isfile(os.path.join(ROOT, "deploy/setup-zram-swap.sh")), "setup-zram-swap.sh exists")
ok("compression-algorithm = zstd" in zram, "zram config selects zstd compression")
ok("swap = on" in zram, "zram config enables swap")
ok("disable_disk_swap" in zram, "zram setup disables disk swap")

ok("MemorySwapMax=0" in interactive_slice, "interactive slice forbids swap")
ok("MemoryLow=250M" in interactive_slice, "interactive slice sets memory floor")
ok("projectplanner-interactive.slice" in guards and "projectplanner-batch.slice" in guards,
   "apply-resource-guards installs slice units")
ok("reset-property" in guards, "apply-resource-guards clears legacy set-property overrides")
ok("MemoryMax=512M" in reconcile, "reconcile keeps production-validated 512M MemoryMax")
ok("PM_SQLITE_MMAP_BYTES=0" in reconcile, "reconcile disables sqlite mmap under batch cap")

ok("grep -q zram" in verify_mem, "verify_memory_isolation checks zram swap")
ok("projectplanner-reconcile.service MemoryMax <= 512M" in verify_mem,
   "verify_memory_isolation accepts reconcile hard cap")
ok("projectplanner-interactive.slice" in verify_slices, "verify_cgroup_slices checks interactive slice")
ok("Slice=projectplanner-batch.slice" in verify_slices, "verify_cgroup_slices checks batch Slice=")

ok("setup-zram-swap.sh" in provision, "PROVISION.md documents zram setup")
ok("verify_memory_isolation.sh" in provision, "PROVISION.md documents memory verification")
ok("verify_cgroup_slices.sh" in provision, "PROVISION.md documents slice verification")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
