#!/usr/bin/env python3
"""Backup/restore roundtrip test for HARDEN-43 (script-style, stdlib only).

Covers: online-backup snapshot consistency under a concurrent writer,
manifest sha256 integrity, restore verification, overwrite refusal without
--force, and sha256-mismatch detection. Run as: python test_backup_restore.py
"""

import importlib.util
import json
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


backup_mod = load("backup_databases")
restore_mod = load("restore_databases")

failures = []


def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}: {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


with tempfile.TemporaryDirectory(prefix="pptest-") as tmp:
    tmp = Path(tmp)
    data_dir = tmp / "data"
    data_dir.mkdir()

    # DB 1: plain rollback-journal DB with known rows.
    static_db = data_dir / "static.db"
    conn = sqlite3.connect(static_db)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"row-{i}",) for i in range(500)])
    conn.commit()
    conn.close()

    # DB 2: WAL-mode DB with a writer running DURING the backup — the core
    # claim of the online-backup API is a consistent snapshot despite this.
    live_db = data_dir / "live.db"
    conn = sqlite3.connect(live_db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"seed-{i}",) for i in range(200)])
    conn.commit()
    conn.close()

    stop = threading.Event()

    def writer():
        wconn = sqlite3.connect(live_db, timeout=30)
        i = 0
        while not stop.is_set():
            wconn.execute("INSERT INTO t (v) VALUES (?)", (f"live-{i}",))
            wconn.commit()
            i += 1
            time.sleep(0.002)
        wconn.close()

    wt = threading.Thread(target=writer)
    wt.start()
    try:
        time.sleep(0.1)
        rc = backup_mod.main(["--data-dir", str(data_dir), "--dest-dir", str(tmp / "backups")])
    finally:
        stop.set()
        wt.join()
    check("backup exits 0", rc == 0, f"rc={rc}")

    snapshots = sorted((tmp / "backups").iterdir())
    check("one snapshot dir created", len(snapshots) == 1)
    snap_dir = snapshots[0]
    names = sorted(p.name for p in snap_dir.iterdir())
    check("snapshot contains both dbs + manifest",
          names == ["live.db.gz", "manifest.json", "static.db.gz"], str(names))

    manifest = json.loads((snap_dir / "manifest.json").read_text())
    check("manifest has no errors", manifest["errors"] == [], str(manifest["errors"]))
    check("manifest quick_check ok",
          all(e["quick_check"] == "ok" for e in manifest["databases"]))

    # Restore into a fresh dir and verify contents.
    restore_dir = tmp / "restored"
    rc = restore_mod.main(["--from-dir", str(snap_dir), "--target-dir", str(restore_dir)])
    check("restore exits 0", rc == 0, f"rc={rc}")

    conn = sqlite3.connect(restore_dir / "static.db")
    rows = conn.execute("SELECT count(*), min(v), max(v) FROM t").fetchone()
    conn.close()
    check("static.db rows survive roundtrip", rows[0] == 500, str(rows))

    conn = sqlite3.connect(restore_dir / "live.db")
    n_seed = conn.execute("SELECT count(*) FROM t WHERE v LIKE 'seed-%'").fetchone()[0]
    ic = conn.execute("PRAGMA integrity_check").fetchall()
    conn.close()
    check("live.db snapshot is consistent (integrity ok)", ic == [("ok",)], str(ic[:3]))
    check("live.db keeps all pre-backup rows", n_seed == 200, f"seed rows={n_seed}")

    # Overwrite refusal without --force.
    rc = restore_mod.main(["--from-dir", str(snap_dir), "--target-dir", str(restore_dir)])
    check("restore refuses to overwrite without --force", rc == 1, f"rc={rc}")
    rc = restore_mod.main(["--from-dir", str(snap_dir), "--target-dir", str(restore_dir), "--force"])
    check("restore --force succeeds", rc == 0, f"rc={rc}")

    # Corruption detection: flip bytes in one gz, expect sha256 mismatch.
    gz = snap_dir / "static.db.gz"
    corrupted = bytearray(gz.read_bytes())
    corrupted[len(corrupted) // 2] ^= 0xFF
    gz.write_bytes(bytes(corrupted))
    rc = restore_mod.main(["--from-dir", str(snap_dir), "--target-dir", str(tmp / "restored2")])
    check("restore detects corrupted snapshot (sha256 mismatch)", rc == 1, f"rc={rc}")

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL BACKUP/RESTORE TESTS PASSED")
sys.exit(0)
