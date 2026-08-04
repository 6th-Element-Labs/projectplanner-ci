#!/usr/bin/env python3
"""Run one CI test while holding a host-wide capacity slot.

Canonical gates execute from isolated workspaces, so their local ``xargs -P``
pools cannot see one another.  Advisory file locks provide one small shared
capacity boundary across those workspaces without introducing test ordering or
shared test data.  Locks are released by the kernel if a worker exits or dies.
"""
from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import IO, Sequence


POLL_SECONDS = 0.05


def default_slot_dir() -> Path:
    """Return a stable per-user directory shared by all local workspaces."""
    configured = os.environ.get("SWITCHBOARD_CI_SLOT_DIR")
    if configured:
        return Path(configured)
    return Path("/tmp") / f"switchboard-ci-{os.getuid()}-slots"


def acquire_slots(slot_dir: Path, slots: int, weight: int) -> list[IO[str]]:
    """Atomically wait for and return ``weight`` exclusively locked slots."""
    slot_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    start = os.getpid() % slots
    while True:
        acquired: list[IO[str]] = []
        with (slot_dir / "allocator.lock").open("a+", encoding="utf-8") as allocator:
            fcntl.flock(allocator.fileno(), fcntl.LOCK_EX)
            for offset in range(slots):
                slot = (start + offset) % slots
                handle = (slot_dir / f"slot-{slot}.lock").open("a+", encoding="utf-8")
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    handle.close()
                    continue
                acquired.append(handle)
                if len(acquired) == weight:
                    return acquired
        for handle in acquired:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        time.sleep(POLL_SECONDS)


def run_with_slots(
    command: Sequence[str], *, slots: int, weight: int, slot_dir: Path,
) -> int:
    """Run ``command`` under weighted capacity and return its exact exit status."""
    handles = acquire_slots(slot_dir, slots, weight)
    try:
        return subprocess.run(list(command), check=False).returncode
    finally:
        for handle in handles:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="run a command under the shared Switchboard CI host limit",
    )
    parser.add_argument("--slots", type=int, required=True)
    parser.add_argument("--weight", type=int, default=1)
    parser.add_argument("--slot-dir", type=Path, default=default_slot_dir())
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.slots < 1:
        parser.error("--slots must be a positive integer")
    if args.weight < 1 or args.weight > args.slots:
        parser.error("--weight must be between 1 and --slots")
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command is required after --")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return run_with_slots(
        args.command, slots=args.slots, weight=args.weight, slot_dir=args.slot_dir,
    )


if __name__ == "__main__":
    raise SystemExit(main())
