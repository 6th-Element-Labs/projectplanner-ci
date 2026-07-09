#!/usr/bin/env python3
"""Consistent off-box SQLite backups (HARDEN-43).

Snapshots every *.db in the data dir with the sqlite3 online-backup API —
safe against live writers, unlike a raw file copy — verifies each snapshot
with PRAGMA quick_check, gzips it, and ships the set to S3 (or a local
--dest-dir for tests and ad-hoc runs) together with a manifest.json holding
sha256 sums. The manifest is shipped last, so its presence marks a complete
snapshot set.

One DB is staged at a time so peak scratch usage stays under the largest
single DB. This is deliberately a standalone script that does NOT import the
app (store/jobs): a backup run must never be able to wedge the 1 GB box the
way heavy batch jobs can (HARDEN-32). boto3 is imported lazily so local-dir
mode works with a bare stdlib.

Config via flags or env:
  PM_BACKUP_DATA_DIR   dir holding *.db files   (default /var/lib/projectplanner)
  PM_BACKUP_S3_BUCKET  S3 bucket for snapshots  (required unless --dest-dir)
  PM_BACKUP_S3_PREFIX  key prefix inside bucket (default "prod")

Exit status: 0 if every DB shipped, 1 if any failed (remaining DBs are still
attempted; failures are listed in the manifest and on stderr).
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

CHUNK = 1024 * 1024


def snapshot_db(src: Path, dest: Path) -> None:
    """Copy a live SQLite DB to dest via the online-backup API and verify it."""
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=60)
    dst_conn = sqlite3.connect(dest)
    try:
        # Small page batches with a sleep let live writers proceed between
        # chunks instead of blocking them for the whole copy.
        src_conn.backup(dst_conn, pages=512, sleep=0.05)
        row = dst_conn.execute("PRAGMA quick_check").fetchone()
        if not row or row[0] != "ok":
            raise RuntimeError(f"quick_check failed on snapshot of {src.name}: {row}")
    finally:
        src_conn.close()
        dst_conn.close()


def gzip_file(src: Path, dest: Path) -> str:
    """Gzip src into dest, returning the sha256 hex digest of the gz bytes."""
    sha = hashlib.sha256()
    with open(src, "rb") as fin, open(dest, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as fout:
            while True:
                chunk = fin.read(CHUNK)
                if not chunk:
                    break
                fout.write(chunk)
    with open(dest, "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            sha.update(chunk)
    return sha.hexdigest()


class LocalDirShipper:
    def __init__(self, dest_dir: Path, stamp: str):
        self.dir = dest_dir / stamp
        self.dir.mkdir(parents=True, exist_ok=True)

    def ship(self, local: Path, name: str) -> str:
        target = self.dir / name
        shutil.copyfile(local, target)
        return str(target)


class S3Shipper:
    def __init__(self, bucket: str, prefix: str, stamp: str):
        import boto3  # lazy: only needed for S3 mode

        self.client = boto3.client("s3")
        self.bucket = bucket
        self.key_prefix = f"{prefix.strip('/')}/{stamp}"

    def ship(self, local: Path, name: str) -> str:
        key = f"{self.key_prefix}/{name}"
        self.client.upload_file(str(local), self.bucket, key)
        return f"s3://{self.bucket}/{key}"


def run_backup(data_dir: Path, shipper, stamp: str) -> dict:
    manifest: dict = {
        "schema": "projectplanner.backup_manifest.v1",
        "created_utc": stamp,
        "host": os.uname().nodename,
        "data_dir": str(data_dir),
        "databases": [],
        "errors": [],
    }
    db_files = sorted(p for p in data_dir.glob("*.db") if p.is_file())
    if not db_files:
        manifest["errors"].append(f"no *.db files found in {data_dir}")
    for db in db_files:
        t0 = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="ppbackup-") as tmp:
                snap = Path(tmp) / db.name
                gz = Path(tmp) / (db.name + ".gz")
                snapshot_db(db, snap)
                sha = gzip_file(snap, gz)
                snap_bytes = snap.stat().st_size
                gz_bytes = gz.stat().st_size
                location = shipper.ship(gz, db.name + ".gz")
            manifest["databases"].append(
                {
                    "name": db.name,
                    "bytes": snap_bytes,
                    "gz_bytes": gz_bytes,
                    "sha256_gz": sha,
                    "quick_check": "ok",
                    "location": location,
                    "seconds": round(time.monotonic() - t0, 2),
                }
            )
            print(f"backed up {db.name}: {snap_bytes} -> {gz_bytes} bytes ({location})")
        except Exception as exc:  # keep going: one bad DB must not block the rest
            manifest["errors"].append(f"{db.name}: {exc}")
            print(f"FAILED {db.name}: {exc}", file=sys.stderr)
    with tempfile.TemporaryDirectory(prefix="ppbackup-") as tmp:
        mpath = Path(tmp) / "manifest.json"
        mpath.write_text(json.dumps(manifest, indent=2))
        location = shipper.ship(mpath, "manifest.json")
    print(f"manifest: {location} ({len(manifest['databases'])} ok, {len(manifest['errors'])} failed)")
    return manifest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=os.environ.get("PM_BACKUP_DATA_DIR", "/var/lib/projectplanner"))
    ap.add_argument("--bucket", default=os.environ.get("PM_BACKUP_S3_BUCKET", ""))
    ap.add_argument("--prefix", default=os.environ.get("PM_BACKUP_S3_PREFIX", "prod"))
    ap.add_argument("--dest-dir", default="", help="ship to a local directory instead of S3")
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        print(f"data dir not found: {data_dir}", file=sys.stderr)
        return 1
    stamp = time.strftime("%Y-%m-%dT%H%M%SZ", time.gmtime())
    if args.dest_dir:
        shipper = LocalDirShipper(Path(args.dest_dir), stamp)
    elif args.bucket:
        shipper = S3Shipper(args.bucket, args.prefix, stamp)
    else:
        print("set PM_BACKUP_S3_BUCKET/--bucket or pass --dest-dir", file=sys.stderr)
        return 1
    manifest = run_backup(data_dir, shipper, stamp)
    return 1 if manifest["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
