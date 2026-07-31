"""The promoted Agent Host release — one explicitly chosen artifact.

Hosts are compared against a release an operator (or the release pipeline)
promoted, never against whatever is on master. Comparing to the tip would block
the entire fleet on every merge, which is the opposite of the goal: the point is
that a wire-contract change becomes a *scheduled* host update instead of a
fleet-wide launch outage discovered at admission time.

Storage only. This module gates nothing by itself; ``host_readiness`` reads it
and the placement path applies the verdict.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from constants import DEFAULT_PROJECT, SWITCHBOARD_DB_PATH
from db.connection import _conn, _write_through

SCHEMA = "switchboard.host_release.v1"

#: Where signed bundle archives live. Beside the database on purpose: the same
#: volume the operator already backs up, so a restored box can still hand hosts
#: the release its own rows point at. A row whose archive is gone would leave
#: every host permanently "update available" with nothing to fetch.
ARCHIVE_DIR = Path(os.environ.get("PM_HOST_RELEASE_DIR")
                   or (Path(SWITCHBOARD_DB_PATH).parent / "host-releases"))


def archive_path(release_id: str) -> Path:
    """On-disk archive for one release. The id is a hash, so it is path-safe."""
    safe = "".join(ch for ch in _text(release_id) if ch.isalnum() or ch in "-_")
    if not safe:
        raise HostReleaseError("host_release_id_required")
    return ARCHIVE_DIR / f"{safe}.tar.gz"


def store_archive(release_id: str, data: bytes) -> Path:
    """Persist a bundle archive, atomically, so a partial write is never served."""
    target = archive_path(release_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_suffix(f".{os.getpid()}.tmp")
    staging.write_bytes(data)
    os.replace(staging, target)
    return target


class HostReleaseError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _text(value: Any) -> str:
    return str(value or "").strip()


def _row(row: Any) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    d = dict(row)
    d["schema"] = SCHEMA
    d["promoted"] = bool(d.get("promoted"))
    # Reported, not assumed: a row without its archive cannot serve a download,
    # and the caller needs to know that before telling a host to fetch one.
    try:
        d["archive_present"] = archive_path(str(d.get("release_id") or "")).is_file()
    except HostReleaseError:
        d["archive_present"] = False
    return d


def _release_id(version: str, digest: str) -> str:
    seed = f"{version}\x1f{digest}".encode("utf-8")
    return "hostrel-" + hashlib.sha256(seed).hexdigest()[:20]


def _table_present_in(c: Any) -> bool:
    row = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='host_releases'"
    ).fetchone()
    return row is not None


def get_promoted_release_in(c: Any) -> Optional[Dict[str, Any]]:
    """The promoted release on the caller's connection, or None.

    Returns None when the table is absent so a board that has not migrated
    keeps working: with no promoted release the readiness model has no opinion
    and hosts stay eligible.
    """
    if not _table_present_in(c):
        return None
    return _row(c.execute(
        "SELECT * FROM host_releases WHERE promoted = 1 LIMIT 1").fetchone())


def get_promoted_release(*, project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    with _conn(project) as c:
        return get_promoted_release_in(c)


def list_releases(*, project: str = DEFAULT_PROJECT,
                  limit: int = 50) -> List[Dict[str, Any]]:
    with _conn(project) as c:
        if not _table_present_in(c):
            return []
        rows = c.execute(
            "SELECT * FROM host_releases ORDER BY created_at DESC LIMIT ?",
            (max(1, int(limit)),)).fetchall()
    return [r for r in (_row(row) for row in rows) if r]


def record_release(payload: Mapping[str, Any], *, actor: str = "system",
                   promote: bool = False,
                   project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Register a built bundle, optionally promoting it in the same transaction.

    Promotion is a single-statement flip guarded by a partial unique index, so
    two concurrent promotions cannot both win.
    """
    version = _text(payload.get("version"))
    digest = _text(payload.get("bundle_digest"))
    if not version:
        raise HostReleaseError("host_release_version_required")
    if not digest:
        # The digest is the identity. A release without one cannot detect the
        # hand-patched-tree case that caused the 2026-07-31 outage.
        raise HostReleaseError("host_release_bundle_digest_required")

    release_id = _release_id(version, digest)
    now = time.time()
    fingerprint = _text(payload.get("contract_fingerprint"))
    url = _text(payload.get("download_url"))
    signature = _text(payload.get("signature"))
    notes = _text(payload.get("notes"))

    def write() -> Dict[str, Any]:
        with _conn(project) as c:
            if not _table_present_in(c):
                raise HostReleaseError("host_releases_absent")
            c.execute(
                "INSERT INTO host_releases("
                "release_id, version, bundle_digest, contract_fingerprint, "
                "download_url, signature, notes, promoted, promoted_at, "
                "promoted_by, created_at) VALUES (?,?,?,?,?,?,?,0,NULL,'',?) "
                "ON CONFLICT(release_id) DO UPDATE SET "
                "version=excluded.version, bundle_digest=excluded.bundle_digest, "
                "contract_fingerprint=excluded.contract_fingerprint, "
                "download_url=excluded.download_url, "
                "signature=excluded.signature, notes=excluded.notes",
                (release_id, version, digest, fingerprint, url, signature, notes, now))
            if promote:
                c.execute("UPDATE host_releases SET promoted = 0 WHERE promoted = 1")
                c.execute(
                    "UPDATE host_releases SET promoted = 1, promoted_at = ?, "
                    "promoted_by = ? WHERE release_id = ?",
                    (now, _text(actor) or "system", release_id))
            return _row(c.execute(
                "SELECT * FROM host_releases WHERE release_id = ?",
                (release_id,)).fetchone()) or {}

    return _write_through(project, write)


def promote_release(release_id: str, *, actor: str = "system",
                    project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    release_id = _text(release_id)
    if not release_id:
        raise HostReleaseError("host_release_id_required")
    now = time.time()

    def write() -> Dict[str, Any]:
        with _conn(project) as c:
            if not _table_present_in(c):
                raise HostReleaseError("host_releases_absent")
            existing = c.execute(
                "SELECT * FROM host_releases WHERE release_id = ?",
                (release_id,)).fetchone()
            if existing is None:
                raise HostReleaseError("host_release_not_found")
            c.execute("UPDATE host_releases SET promoted = 0 WHERE promoted = 1")
            c.execute(
                "UPDATE host_releases SET promoted = 1, promoted_at = ?, "
                "promoted_by = ? WHERE release_id = ?",
                (now, _text(actor) or "system", release_id))
            return _row(c.execute(
                "SELECT * FROM host_releases WHERE release_id = ?",
                (release_id,)).fetchone()) or {}

    return _write_through(project, write)
