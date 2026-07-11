#!/usr/bin/env bash
# HARDEN-40: pin cgroup resource guards so the batch/timer jobs can't starve the
# web app on the small (2-vCPU / ~900 MB) box.
#
# Background (2026-07-08 outage): narrate (~90% of a core) + the per-PR ci-gate
# (fresh venv + pytest) can saturate both cores AND thrash swap, which jams the
# single uvicorn worker's event loop so even the cheap /health times out. These
# limits give the web app a reserved memory floor + the lion's share of CPU, and
# soft-cap the batch jobs so a spike throttles (reclaim) instead of taking the
# site down.
#
# Applied via `systemctl set-property`, which persists under
# /etc/systemd/system.control/ and survives reboot. Idempotent — re-running just
# re-asserts the values. Run once on a fresh box AFTER the units are installed and
# enabled (see PROVISION.md). These are the exact values running in prod today.
set -euo pipefail

# Web app: top CPU share + a memory floor it can always reclaim under pressure.
systemctl set-property projectplanner.service CPUWeight=900 MemoryLow=250M

# MCP tool surface: second priority.
systemctl set-property projectplanner-mcp.service CPUWeight=400

# Batch/timer jobs: low CPU share + a per-job soft memory cap, so a spike gets
# throttled instead of swapping the box into the ground.
for unit in narrate monitors inbox summarize digest; do
  systemctl set-property "projectplanner-${unit}.service" CPUWeight=20 MemoryHigh=180M
done

# Reconcile scans every project DB. Production profiling (HARDEN-51..61) proved
# its legitimate file-cache working set exceeds the generic 180M batch ceiling;
# this envelope completed all 12 projects in 30s while the web service retained
# MemoryLow=250M. Keep a hard cap so drift scans cannot consume the whole VM.
systemctl set-property projectplanner-reconcile.service \
  CPUWeight=20 MemoryHigh=384M MemoryMax=512M

# CI gate is the heaviest batch job (fresh venv + pytest per PR): stricter still,
# with a HARD ceiling so it can never OOM the box.
systemctl set-property projectplanner-ci-gate.service CPUWeight=10 MemoryHigh=220M MemoryMax=320M

echo "resource guards applied. verify e.g.:"
echo "  systemctl show projectplanner.service -p CPUWeight -p MemoryLow"
echo "  systemctl show projectplanner-ci-gate.service -p MemoryHigh -p MemoryMax"
echo "  systemctl show projectplanner-reconcile.service -p MemoryHigh -p MemoryMax"
