#!/usr/bin/env python3
"""BUG-318: simultaneous canonical gates share one host-capacity ceiling."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parent
WRAPPER = ROOT / "scripts" / "ci_host_slot.py"


def update_state(state_path: Path, delta: int, weight: int) -> None:
    lock_path = state_path.with_suffix(".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["active"] += delta
        state["peak"] = max(state["peak"], state["active"])
        state["active_weight"] += delta * weight
        state["peak_weight"] = max(state["peak_weight"], state["active_weight"])
        state_path.write_text(json.dumps(state), encoding="utf-8")


def worker(state_path: Path, weight: int) -> int:
    update_state(state_path, 1, weight)
    time.sleep(0.25)
    update_state(state_path, -1, weight)
    return 0


if len(sys.argv) == 4 and sys.argv[1] == "--worker":
    raise SystemExit(worker(Path(sys.argv[2]), int(sys.argv[3])))


passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


with tempfile.TemporaryDirectory(prefix="bug318-host-slots-") as raw_tmp:
    tmp = Path(raw_tmp)
    state_path = tmp / "state.json"
    empty_state = {"active": 0, "peak": 0, "active_weight": 0, "peak_weight": 0}
    state_path.write_text(json.dumps(empty_state), encoding="utf-8")
    slot_dir = tmp / "shared-slots"
    workspace_a = tmp / "workspace-a"
    workspace_b = tmp / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    environment = {**os.environ, "SWITCHBOARD_CI_SLOT_DIR": str(slot_dir)}
    workers = [
        subprocess.Popen([
            sys.executable, str(WRAPPER),
            "--slots", "2", "--",
            sys.executable, str(Path(__file__).resolve()), "--worker", str(state_path), "1",
        ], cwd=workspace_a if index % 2 == 0 else workspace_b, env=environment)
        for index in range(6)
    ]
    returncodes = [process.wait(timeout=20) for process in workers]
    state = json.loads(state_path.read_text(encoding="utf-8"))

    ok(returncodes == [0] * 6, "all workers complete through the shared slot wrapper")
    ok(state == {"active": 0, "peak": 2, "active_weight": 0, "peak_weight": 2},
       "six cross-process workers never exceed the two-slot host ceiling")

    state_path.write_text(json.dumps(empty_state), encoding="utf-8")
    weighted = [3, 3, 1, 1]
    weighted_workers = [
        subprocess.Popen([
            sys.executable, str(WRAPPER),
            "--slots", "4", "--weight", str(weight), "--",
            sys.executable, str(Path(__file__).resolve()),
            "--worker", str(state_path), str(weight),
        ], cwd=workspace_a if index % 2 == 0 else workspace_b, env=environment)
        for index, weight in enumerate(weighted)
    ]
    weighted_returncodes = [process.wait(timeout=20) for process in weighted_workers]
    weighted_state = json.loads(state_path.read_text(encoding="utf-8"))
    ok(weighted_returncodes == [0] * len(weighted),
       "weighted workers complete without partial-slot deadlock")
    ok(weighted_state["active_weight"] == 0
       and 3 <= weighted_state["peak_weight"] <= 4,
       "weighted workers reserve capacity without exceeding the host ceiling")

    forwarded = subprocess.run([
        sys.executable, str(WRAPPER),
        "--slots", "1", "--",
        sys.executable, "-c", "raise SystemExit(7)",
    ], check=False, env=environment)
    ok(forwarded.returncode == 7, "the wrapper preserves a failing test exit status")

ci_source = (ROOT / "scripts" / "switchboard_ci.sh").read_text(encoding="utf-8")
ok("SWITCHBOARD_CI_HOST_JOBS" in ci_source and "scripts/ci_host_slot.py" in ci_source,
   "the canonical per-file runner uses the cross-workspace host slot pool")
ok("tests/browser/*" in ci_source and '--weight "$weight"' in ci_source,
   "browser tests remain enabled and reserve extra startup capacity")

print(f"\nBUG-318 host CI slots: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
