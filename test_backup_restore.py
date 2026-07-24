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

# --------------------------------------------------------------------------------
# Regression: snapshot_db must TERMINATE on a database that is being written the whole
# time. The old implementation used the incremental online-backup API, which restarts
# the copy from page 0 on every concurrent write; against the live 1.2 GB
# switchboard.db it never converged and systemd killed the job at its 30-minute
# timeout, silently stopping off-box backups for 13+ hours. A sustained writer is the
# condition that reproduced it, so pin it.
# --------------------------------------------------------------------------------
with tempfile.TemporaryDirectory(prefix="pptest-hot-") as tmp:
    tmp_path = Path(tmp)
    hot = tmp_path / "hot.db"
    conn = sqlite3.connect(hot)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE rows(id INTEGER PRIMARY KEY, blob TEXT)")
    # Enough pages that a restart-on-write copy has a real window to be interrupted.
    conn.executemany("INSERT INTO rows(blob) VALUES(?)",
                     [("x" * 512,) for _ in range(20000)])
    conn.commit()
    conn.close()

    stop = threading.Event()
    writer_errors = []

    def hammer():
        try:
            wconn = sqlite3.connect(hot, timeout=30)
            wconn.execute("PRAGMA journal_mode=WAL")
            while not stop.is_set():
                wconn.execute("INSERT INTO rows(blob) VALUES(?)", ("y" * 512,))
                wconn.commit()
                time.sleep(0.001)
            wconn.close()
        except Exception as exc:  # surfaced below rather than silently ignored
            writer_errors.append(repr(exc))

    writer = threading.Thread(target=hammer, daemon=True)
    writer.start()
    time.sleep(0.2)  # let the writer actually get going before we snapshot
    snap_dest = tmp_path / "hot-snapshot.db"
    t0 = time.monotonic()
    try:
        backup_mod.snapshot_db(hot, snap_dest)
        snap_error = None
    except Exception as exc:
        snap_error = repr(exc)
    elapsed = time.monotonic() - t0
    stop.set()
    writer.join(timeout=10)

    check("snapshot of a continuously-written DB completes", snap_error is None, snap_error or "")
    check("snapshot under sustained writes finishes promptly (no restart livelock)",
          elapsed < 30, f"took {elapsed:.1f}s")
    check("concurrent writer was not blocked into an error", not writer_errors,
          "; ".join(writer_errors))
    if snap_error is None:
        vconn = sqlite3.connect(snap_dest)
        integrity = vconn.execute("PRAGMA integrity_check").fetchone()[0]
        count = vconn.execute("SELECT COUNT(*) FROM rows").fetchone()[0]
        vconn.close()
        check("hot snapshot is internally consistent", integrity == "ok", str(integrity))
        # The snapshot is a point-in-time read: it must contain at least the pre-existing
        # rows. Anything less means we captured a torn prefix.
        check("hot snapshot captured a complete point-in-time view", count >= 20000,
              f"rows={count}")

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL BACKUP/RESTORE TESTS PASSED")
sys.exit(0)
