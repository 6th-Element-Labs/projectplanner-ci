"""Durable, tenant-scoped state for the Compand pilot runtime."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@dataclass(frozen=True)
class FrozenExchange:
    request_sha256: str
    provider_body: bytes
    transformed: bool
    artifact_sha256: str | None
    capability: str | None


class CompandStateRepository:
    """SQLite state with no prompt/tool content in receipts or observations."""

    def __init__(self, path: str, *, capability_secret: str = "") -> None:
        if path != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._secret = (capability_secret or secrets.token_hex(32)).encode("utf-8")
        self._cipher = AESGCM(hashlib.sha256(self._secret).digest())
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _migrate(self) -> None:
        with self._lock, self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS compand_exchanges (
                    tenant_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    provider_body BLOB NOT NULL,
                    transformed INTEGER NOT NULL,
                    artifact_sha256 TEXT,
                    capability TEXT,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (tenant_id, session_id, request_sha256)
                );
                CREATE TABLE IF NOT EXISTS compand_artifacts (
                    capability_digest TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    artifact_sha256 TEXT NOT NULL,
                    body BLOB NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS compand_receipts (
                    correlation_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    technique TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    original_tokens INTEGER,
                    candidate_tokens INTEGER,
                    original_bytes INTEGER,
                    candidate_bytes INTEGER,
                    artifact_sha256 TEXT,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS compand_ledgers (
                    tenant_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    client_input_json TEXT NOT NULL,
                    provider_input_json TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (tenant_id, session_id)
                );
                CREATE TABLE IF NOT EXISTS compand_observations (
                    correlation_id TEXT NOT NULL,
                    observer TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    classification TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (correlation_id, observer)
                );
                """
            )

    def _seal(self, context: str, body: bytes) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._cipher.encrypt(nonce, body, context.encode())

    def _open(self, context: str, body: bytes) -> bytes:
        if len(body) < 13:
            raise ValueError("encrypted Compand state is malformed")
        return self._cipher.decrypt(body[:12], body[12:], context.encode())

    @staticmethod
    def sha256(body: bytes) -> str:
        return "sha256:" + hashlib.sha256(body).hexdigest()

    def get_exchange(
        self, tenant_id: str, session_id: str, request_sha256: str
    ) -> FrozenExchange | None:
        with self._lock:
            row = self._db.execute(
                """SELECT request_sha256, provider_body, transformed,
                          artifact_sha256, capability
                   FROM compand_exchanges
                   WHERE tenant_id=? AND session_id=? AND request_sha256=?""",
                (tenant_id, session_id, request_sha256),
            ).fetchone()
        if row is None:
            return None
        context = f"exchange\0{tenant_id}\0{session_id}\0{request_sha256}"
        return FrozenExchange(
            request_sha256=row["request_sha256"],
            provider_body=self._open(context, bytes(row["provider_body"])),
            transformed=bool(row["transformed"]),
            artifact_sha256=row["artifact_sha256"],
            capability=row["capability"],
        )

    def freeze_exchange(
        self,
        tenant_id: str,
        session_id: str,
        request_sha256: str,
        provider_body: bytes,
        *,
        transformed: bool,
        artifact_sha256: str | None,
        capability: str | None,
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                """INSERT OR IGNORE INTO compand_exchanges
                   (tenant_id, session_id, request_sha256, provider_body, transformed,
                    artifact_sha256, capability, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    tenant_id,
                    session_id,
                    request_sha256,
                    self._seal(
                        f"exchange\0{tenant_id}\0{session_id}\0{request_sha256}",
                        provider_body,
                    ),
                    int(transformed),
                    artifact_sha256,
                    capability,
                    time.time(),
                ),
            )

    def get_ledger(
        self, tenant_id: str, session_id: str
    ) -> tuple[list[object], list[object]] | None:
        with self._lock:
            row = self._db.execute(
                """SELECT client_input_json, provider_input_json FROM compand_ledgers
                   WHERE tenant_id=? AND session_id=?""",
                (tenant_id, session_id),
            ).fetchone()
        if row is None:
            return None
        context = f"ledger\0{tenant_id}\0{session_id}"
        client_items = json.loads(
            self._open(context + "\0client", bytes(row["client_input_json"]))
        )
        provider_items = json.loads(
            self._open(context + "\0provider", bytes(row["provider_input_json"]))
        )
        if not isinstance(client_items, list) or not isinstance(provider_items, list):
            raise ValueError("stored Compand ledger is malformed")
        return client_items, provider_items

    def save_ledger(
        self,
        tenant_id: str,
        session_id: str,
        client_items: list[object],
        provider_items: list[object],
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO compand_ledgers
                   (tenant_id, session_id, client_input_json, provider_input_json, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(tenant_id, session_id) DO UPDATE SET
                     client_input_json=excluded.client_input_json,
                     provider_input_json=excluded.provider_input_json,
                     updated_at=excluded.updated_at""",
                (
                    tenant_id,
                    session_id,
                    self._seal(
                        f"ledger\0{tenant_id}\0{session_id}\0client",
                        json.dumps(
                            client_items, separators=(",", ":"), ensure_ascii=False
                        ).encode(),
                    ),
                    self._seal(
                        f"ledger\0{tenant_id}\0{session_id}\0provider",
                        json.dumps(
                            provider_items, separators=(",", ":"), ensure_ascii=False
                        ).encode(),
                    ),
                    time.time(),
                ),
            )

    def store_artifact(
        self,
        tenant_id: str,
        session_id: str,
        body: bytes,
        *,
        retention_seconds: int,
    ) -> tuple[str, str] | None:
        if retention_seconds <= 0:
            return None
        artifact_sha256 = self.sha256(body)
        nonce = secrets.token_urlsafe(32)
        capability = nonce + "." + hmac.new(
            self._secret,
            f"{tenant_id}\0{session_id}\0{artifact_sha256}\0{nonce}".encode(),
            hashlib.sha256,
        ).hexdigest()
        digest = hashlib.sha256(capability.encode()).hexdigest()
        nonce_bytes = os.urandom(12)
        aad = f"{tenant_id}\0{session_id}\0{artifact_sha256}".encode()
        encrypted_body = nonce_bytes + self._cipher.encrypt(nonce_bytes, body, aad)
        now = time.time()
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO compand_artifacts
                   (capability_digest, tenant_id, session_id, artifact_sha256, body,
                    expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    digest,
                    tenant_id,
                    session_id,
                    artifact_sha256,
                    encrypted_body,
                    now + retention_seconds,
                    now,
                ),
            )
        return artifact_sha256, capability

    def recover_artifact(
        self, tenant_id: str, session_id: str, capability: str
    ) -> bytes | None:
        digest = hashlib.sha256(capability.encode()).hexdigest()
        with self._lock:
            row = self._db.execute(
                """SELECT body, expires_at, artifact_sha256 FROM compand_artifacts
                   WHERE capability_digest=? AND tenant_id=? AND session_id=?""",
                (digest, tenant_id, session_id),
            ).fetchone()
        if row is None or float(row["expires_at"]) <= time.time():
            return None
        encrypted_body = bytes(row["body"])
        if len(encrypted_body) < 13:
            return None
        aad = f"{tenant_id}\0{session_id}\0{row['artifact_sha256']}".encode()
        try:
            return self._cipher.decrypt(
                encrypted_body[:12], encrypted_body[12:], aad
            )
        except (InvalidTag, ValueError):
            return None

    def purge_expired(
        self, *, now: float | None = None, session_retention_seconds: int | None = None
    ) -> int:
        cutoff = time.time() if now is None else now
        with self._lock, self._db:
            cursor = self._db.execute(
                "DELETE FROM compand_artifacts WHERE expires_at <= ?", (cutoff,)
            )
            purged = cursor.rowcount
            if session_retention_seconds is not None:
                session_cutoff = cutoff - session_retention_seconds
                purged += self._db.execute(
                    "DELETE FROM compand_exchanges WHERE created_at <= ?",
                    (session_cutoff,),
                ).rowcount
                purged += self._db.execute(
                    "DELETE FROM compand_ledgers WHERE updated_at <= ?",
                    (session_cutoff,),
                ).rowcount
            return purged

    def record_receipt(self, **values: object) -> None:
        allowed = {
            "correlation_id", "tenant_id", "session_id", "request_sha256",
            "technique", "outcome", "original_tokens", "candidate_tokens",
            "original_bytes", "candidate_bytes", "artifact_sha256",
        }
        if set(values) != allowed:
            raise ValueError("receipt fields do not match the content-free schema")
        with self._lock, self._db:
            self._db.execute(
                """INSERT OR REPLACE INTO compand_receipts
                   (correlation_id, tenant_id, session_id, request_sha256, technique,
                    outcome, original_tokens, candidate_tokens, original_bytes,
                    candidate_bytes, artifact_sha256, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(values[name] for name in (
                    "correlation_id", "tenant_id", "session_id", "request_sha256",
                    "technique", "outcome", "original_tokens", "candidate_tokens",
                    "original_bytes", "candidate_bytes", "artifact_sha256",
                )) + (time.time(),),
            )

    def receipts(self) -> list[dict[str, object]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM compand_receipts ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def record_observation(
        self, correlation_id: str, observer: str, endpoint: str, classification: str
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                """INSERT OR REPLACE INTO compand_observations
                   (correlation_id, observer, endpoint, classification, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (correlation_id, observer, endpoint, classification, time.time()),
            )

    def evidence_snapshot(self) -> dict[str, object]:
        with self._lock:
            observations = [
                dict(row) for row in self._db.execute(
                    "SELECT * FROM compand_observations ORDER BY created_at"
                ).fetchall()
            ]
        return {"receipts": self.receipts(), "observations": observations}

    def debug_serialized_state(self) -> str:
        """Content-free diagnostic view used by the executable safety tests."""
        return json.dumps(self.evidence_snapshot(), sort_keys=True, default=str)
