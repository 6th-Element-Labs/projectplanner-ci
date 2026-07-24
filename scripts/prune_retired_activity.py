#!/usr/bin/env python3
"""Delete activity rows belonging to RETIRED subsystems. Dry-run by default.

Scope is deliberately narrow. This is not a general retention sweep: it removes
only `activity` rows whose `kind` was emitted by code that no longer exists, so
there is no judgement call about how much history is worth keeping.

Why it exists: the per-deliverable review/merge stewards were collapsed into one
coordinator loop (SIMPLIFY-2), and they stopped ticking. They left ~323 MB behind
on prod — 265 MB of `coordinator.review_steward.tick` plus 58 MB of
`coordinator.merge_steward.tick` — because each tick logged the FULL result of
every action it executed. Individual rows reach 415 KB; one stuck
`rerun_scratchpad_ci` retry fired ~2,190 times against a single task, writing a
~400 KB row every attempt.

Safety: a kind is only eligible if it has been silent for --min-idle-hours. If
anything is still writing that kind, the subsystem is not retired and this
refuses to touch it. That check is the whole point — it is what makes "retired"
a fact about the data rather than an assumption in this script.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path


# Kinds emitted only by the steward fan-out that SIMPLIFY-2 replaced.
RETIRED_KINDS = (
    "coordinator.review_steward.tick",
    "coordinator.merge_steward.tick",
)

MB = 1048576.0


def _fmt(n: int) -> str:
    return f"{n:,}"


def survey(conn: sqlite3.Connection, kinds, min_idle_hours: float) -> list:
    now = time.time()
    report = []
    for kind in kinds:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(payload)),0), MIN(created_at), "
            "MAX(created_at), COALESCE(MAX(LENGTH(payload)),0) "
            "FROM activity WHERE kind=?", (kind,)).fetchone()
        count, total_bytes, first, last, biggest = row
        idle_h = (now - last) / 3600.0 if last else float("inf")
        report.append({
            "kind": kind,
            "rows": count,
            "bytes": total_bytes,
            "biggest_row": biggest,
            "idle_hours": idle_h,
            "span_days": ((last - first) / 86400.0) if (first and last) else 0.0,
            "eligible": bool(count) and idle_h >= min_idle_hours,
        })
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, help="path to the SQLite database")
    parser.add_argument("--kind", action="append", dest="kinds",
                        help="activity kind to prune (repeatable). "
                             "Defaults to the retired steward kinds.")
    parser.add_argument("--min-idle-hours", type=float, default=24.0,
                        help="refuse to prune a kind written more recently than this "
                             "(default 24). A live kind is not a retired one.")
    parser.add_argument("--apply", action="store_true",
                        help="actually delete. Without this nothing is written.")
    parser.add_argument("--vacuum", action="store_true",
                        help="VACUUM after deleting to return freed pages to the OS. "
                             "Needs free disk roughly equal to the database size.")
    parser.add_argument("--batch", type=int, default=5000,
                        help="rows per DELETE batch (default 5000)")
    args = parser.parse_args(argv)

    kinds = args.kinds or list(RETIRED_KINDS)
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: no such database: {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(db_path)
    try:
        report = survey(conn, kinds, args.min_idle_hours)

        print(f"database: {db_path}")
        print(f"size now: {db_path.stat().st_size / MB:,.1f} MB")
        print(f"mode:     {'APPLY (will delete)' if args.apply else 'DRY RUN (no writes)'}")
        print()
        print("%-38s %10s %12s %11s %10s %s" % (
            "KIND", "ROWS", "MB", "BIGGEST_KB", "IDLE_H", "ELIGIBLE"))
        for item in report:
            print("%-38s %10s %12.1f %11.1f %10.1f %s" % (
                item["kind"], _fmt(item["rows"]), item["bytes"] / MB,
                item["biggest_row"] / 1024.0, item["idle_hours"],
                "yes" if item["eligible"] else "NO"))

        skipped = [i for i in report if i["rows"] and not i["eligible"]]
        for item in skipped:
            print()
            print(f"REFUSING {item['kind']}: last written {item['idle_hours']:.1f}h ago "
                  f"(< --min-idle-hours={args.min_idle_hours}). Still live, not retired.")

        eligible = [i for i in report if i["eligible"]]
        total_rows = sum(i["rows"] for i in eligible)
        total_bytes = sum(i["bytes"] for i in eligible)
        print()
        print(f"would delete: {_fmt(total_rows)} rows, {total_bytes / MB:,.1f} MB of payload")

        if not args.apply:
            print()
            print("DRY RUN — nothing was written. Re-run with --apply to delete.")
            return 0
        if not eligible:
            print("nothing eligible; no changes made.")
            return 0

        deleted = 0
        for item in eligible:
            while True:
                cur = conn.execute(
                    "DELETE FROM activity WHERE id IN ("
                    "  SELECT id FROM activity WHERE kind=? LIMIT ?)",
                    (item["kind"], args.batch))
                conn.commit()
                if not cur.rowcount:
                    break
                deleted += cur.rowcount
                print(f"  deleted {_fmt(deleted)}/{_fmt(total_rows)}...", end="\r", flush=True)
        print(f"  deleted {_fmt(deleted)} rows{' ' * 20}")

        if args.vacuum:
            print("VACUUM (returning freed pages to the OS)...")
            conn.execute("VACUUM")
            conn.commit()
    finally:
        conn.close()

    print(f"size after: {db_path.stat().st_size / MB:,.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
