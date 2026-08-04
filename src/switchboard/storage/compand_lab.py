"""Filesystem evidence adapter for deterministic Compand lab runs."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Mapping

from switchboard.domain.compand.lab import sha256_evidence


_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class AppendOnlyRunWriter:
    """One exclusive run directory with an immutable manifest and JSONL ledger."""

    def __init__(self, run_dir: Path, run_id: str) -> None:
        self._run_dir = run_dir
        self._run_id = run_id
        self._events_path = run_dir / "events.jsonl"
        self._next_sequence = 1

    @property
    def run_location(self) -> str:
        return str(self._run_dir)

    def append_event(self, event: Mapping[str, object]) -> None:
        if event.get("run_id") != self._run_id:
            raise ValueError("event run_id does not match its run writer")
        if event.get("sequence") != self._next_sequence:
            raise ValueError("event sequence is not monotonic")
        rendered = (
            json.dumps(dict(event), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        descriptor = os.open(self._events_path, os.O_WRONLY | os.O_APPEND)
        try:
            written = os.write(descriptor, rendered)
            if written != len(rendered):
                raise OSError("short append to events.jsonl")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._next_sequence += 1


class ContentAddressedLabStore:
    """Content-addressed objects plus exclusive, append-only run evidence."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.objects_root = root / "objects" / "sha256"
        self.runs_root = root / "runs"
        self.objects_root.mkdir(parents=True, exist_ok=True)
        self.runs_root.mkdir(parents=True, exist_ok=True)

    def object_path(self, evidence_hash: str) -> Path:
        prefix = "sha256:"
        if not evidence_hash.startswith(prefix):
            raise ValueError("object hash must use sha256 evidence")
        digest = evidence_hash[len(prefix) :]
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("object hash must use lowercase sha256")
        return self.objects_root / digest[:2] / digest

    def put_object(self, value: bytes) -> str:
        evidence_hash = sha256_evidence(value)
        path = self.object_path(evidence_hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as output:
                output.write(value)
                output.flush()
                os.fsync(output.fileno())
            path.chmod(0o444)
        except FileExistsError:
            if path.read_bytes() != value:
                raise ValueError("content-addressed object does not match its hash")
        return evidence_hash

    def begin_run(
        self, run_id: str, manifest: Mapping[str, object]
    ) -> AppendOnlyRunWriter:
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError("run_id is not filesystem safe")
        if manifest.get("run_id") != run_id:
            raise ValueError("manifest run_id does not match requested run")
        run_dir = self.runs_root / run_id
        run_dir.mkdir(parents=False, exist_ok=False)
        manifest_path = run_dir / "run_manifest.json"
        events_path = run_dir / "events.jsonl"
        rendered = (json.dumps(dict(manifest), indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        try:
            with manifest_path.open("xb") as output:
                output.write(rendered)
                output.flush()
                os.fsync(output.fileno())
            manifest_path.chmod(0o444)
            with events_path.open("xb") as output:
                output.flush()
                os.fsync(output.fileno())
        except Exception:
            # Preserve any materialized evidence rather than hiding a partial red state.
            raise
        return AppendOnlyRunWriter(run_dir, run_id)
