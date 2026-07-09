#!/usr/bin/env python3
"""Restore SQLite databases from an off-box backup snapshot (HARDEN-43).

Reads a snapshot set produced by scripts/backup_databases.py — either the
latest (or a named) snapshot under s3://<bucket>/<prefix>/, or a local
snapshot directory — verifies every file's sha256 against the manifest,
gunzips into --target-dir, and runs PRAGMA integrity_check on each restored
database. Refuses to overwrite existing *.db files unless --force is given.

Run this from an operator machine or the replacement box with credentials
that can read the bucket (the box's own backup creds are put-only and cannot
restore — that is intentional). See docs/BACKUP-RESTORE-RUNBOOK.md.

Exit status: 0 if every database restored and verified, 1 otherwise.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            sha.update(chunk)
    return sha.hexdigest()


def gunzip_file(src: Path, dest: Path) -> None:
    with gzip.open(src, "rb") as fin, open(dest, "wb") as fout:
        while True:
            chunk = fin.read(CHUNK)
            if not chunk:
                break
            fout.write(chunk)


def integrity_check(path: Path) -> None:
    import sqlite3

    # Read-write on purpose: a WAL-mode snapshot cannot be opened read-only
    # until its -shm exists, and an rw open also checkpoints the fresh file.
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    finally:
        conn.close()
    if rows != [("ok",)]:
        raise RuntimeError(f"integrity_check failed for {path.name}: {rows[:5]}")


class LocalDirSource:
    def __init__(self, snapshot_dir: Path):
        self.dir = snapshot_dir

    def describe(self) -> str:
        return str(self.dir)

    def fetch(self, name: str, dest: Path) -> None:
        data = (self.dir / name).read_bytes()
        dest.write_bytes(data)


class S3Source:
    def __init__(self, bucket: str, prefix: str, snapshot: str):
        import boto3  # lazy: only needed for S3 mode

        self.client = boto3.client("s3")
        self.bucket = bucket
        prefix = prefix.strip("/")
        if not snapshot:
            snapshot = self._latest_snapshot(prefix)
        self.key_prefix = f"{prefix}/{snapshot}"

    def _latest_snapshot(self, prefix: str) -> str:
        paginator = self.client.get_paginator("list_objects_v2")
        stamps = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix + "/", Delimiter="/"):
            for cp in page.get("CommonPrefixes", []):
                stamps.append(cp["Prefix"].rstrip("/").rsplit("/", 1)[-1])
        if not stamps:
            raise RuntimeError(f"no snapshots found under s3://{self.bucket}/{prefix}/")
        return max(stamps)  # stamps are UTC ISO-ish, so lexicographic max == newest

    def describe(self) -> str:
        return f"s3://{self.bucket}/{self.key_prefix}"

    def fetch(self, name: str, dest: Path) -> None:
        self.client.download_file(self.bucket, f"{self.key_prefix}/{name}", str(dest))


def run_restore(source, target_dir: Path, force: bool) -> int:
    target_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pprestore-") as tmp:
        tmpdir = Path(tmp)
        mpath = tmpdir / "manifest.json"
        source.fetch("manifest.json", mpath)
        manifest = json.loads(mpath.read_text())
        print(f"snapshot {manifest.get('created_utc')} from {source.describe()}")
        if manifest.get("errors"):
            print(f"WARNING: snapshot recorded backup errors: {manifest['errors']}", file=sys.stderr)
        failures = []
        for entry in manifest.get("databases", []):
            name = entry["name"]
            target = target_dir / name
            if target.exists() and not force:
                failures.append(f"{name}: {target} exists (use --force to overwrite)")
                print(f"SKIPPED {name}: target exists", file=sys.stderr)
                continue
            try:
                gz = tmpdir / (name + ".gz")
                source.fetch(name + ".gz", gz)
                actual = sha256_file(gz)
                if actual != entry["sha256_gz"]:
                    raise RuntimeError(f"sha256 mismatch: manifest {entry['sha256_gz']} != downloaded {actual}")
                gunzip_file(gz, target)
                gz.unlink()
                integrity_check(target)
                print(f"restored {name}: {target.stat().st_size} bytes, integrity ok")
            except Exception as exc:
                failures.append(f"{name}: {exc}")
                print(f"FAILED {name}: {exc}", file=sys.stderr)
    if failures:
        print(f"restore INCOMPLETE: {len(failures)} failure(s)", file=sys.stderr)
        return 1
    print(f"restore complete: {len(manifest.get('databases', []))} databases in {target_dir}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", default=os.environ.get("PM_BACKUP_S3_BUCKET", ""))
    ap.add_argument("--prefix", default=os.environ.get("PM_BACKUP_S3_PREFIX", "prod"))
    ap.add_argument("--snapshot", default="", help="snapshot stamp, e.g. 2026-07-09T071900Z (default: latest)")
    ap.add_argument("--from-dir", default="", help="restore from a local snapshot directory instead of S3")
    ap.add_argument("--target-dir", required=True, help="directory to restore *.db files into")
    ap.add_argument("--force", action="store_true", help="overwrite existing *.db files in target dir")
    args = ap.parse_args(argv)

    if args.from_dir:
        source = LocalDirSource(Path(args.from_dir))
    elif args.bucket:
        source = S3Source(args.bucket, args.prefix, args.snapshot)
    else:
        print("set PM_BACKUP_S3_BUCKET/--bucket or pass --from-dir", file=sys.stderr)
        return 1
    return run_restore(source, Path(args.target_dir), args.force)


if __name__ == "__main__":
    sys.exit(main())
