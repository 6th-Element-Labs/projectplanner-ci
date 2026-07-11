#!/usr/bin/env python3
"""PERF-3 — static checks for deploy memory-isolation scripts."""
import os
import re
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
verify = read("scripts/verify_memory_isolation.sh")
provision = read("deploy/PROVISION.md")

ok(os.path.isfile(os.path.join(ROOT, "deploy/setup-zram-swap.sh")), "setup-zram-swap.sh exists")
ok("compression-algorithm = zstd" in zram, "zram config selects zstd compression")
ok("swap = on" in zram, "zram config enables swap")
ok("disable_disk_swap" in zram, "zram setup disables disk swap")

for unit in ("projectplanner.service", "projectplanner-mcp.service", "projectplanner-gateway.service"):
    ok(re.search(rf"apply_interactive\s+{re.escape(unit)}\s+\d+\s+\d+M\s+\d+M", guards) is not None,
       f"interactive guard covers {unit}")
ok("MemorySwapMax=0" in guards, "apply-resource-guards sets MemorySwapMax=0")
ok("MemoryMin=${mem_min}" in guards, "apply-resource-guards sets MemoryMin")
ok("MemoryMax=${mem_max}" in guards, "apply-resource-guards sets MemoryMax on batch jobs")
ok("320M" in guards, "ci-gate keeps 320M MemoryMax")

ok("MemorySwapMax=0" in verify, "verify script checks MemorySwapMax=0")
ok("grep -q zram" in verify, "verify script checks zram swap")
ok("MemoryMax" in verify, "verify script checks batch MemoryMax")
ok("setup-zram-swap.sh" in provision, "PROVISION.md documents zram setup")
ok("verify_memory_isolation.sh" in provision, "PROVISION.md documents verification")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
