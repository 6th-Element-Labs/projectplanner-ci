"""What this Agent Host process is actually running, as reported to the server.

The 2026-07-31 drain canary lost three Wave A missions to a host that was
online, heartbeating, and green while carrying a bundled
``execution_assignment.py`` that predated ``session_policy_profile``. Admission
compares whole contract dicts, so every launch was refused — but only after the
wake was claimed and the 90s hold burned. Nothing before launch could see it,
because a heartbeat proved liveness and nothing proved compatibility.

This module is the missing proof. Two values ride every heartbeat:

``contract_fingerprint``
    The shape of the execution-assignment contract THIS BUNDLE can build,
    derived from its own copy of ``switchboard.connect.execution_assignment``.
    A host whose fingerprint differs from the server's cannot produce an
    acceptable contract, and the server can now know that before dispatching
    instead of discovering it at admission.

``bundle_digest``
    The identity of the code actually on disk. Deliberately hashed from the
    live payload rather than read out of ``manifest.json``: the incident was
    recovered by hand-copying one file into the deployed 0.4.15 tree, which
    left the manifest — and the version string — completely unchanged. Only a
    content hash of the real files notices that.

Both are computed once, on first use, and cached for the life of the process.
Caching is not an optimization here but the correct semantics: this reports what
the running process loaded. A file edited after startup is not what this process
is executing, and it will be picked up on the restart that makes it live.

First use rather than import time, because ``switchboard`` is only importable
once the host has set up its path — computing eagerly produced an empty
fingerprint, which the server correctly reads as "cannot prove it is compatible"
and would have blocked every host on a technicality.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_PAYLOAD_ROOT = os.path.dirname(_HERE)

#: Mirrors the payload the bundler ships (see ``create_signed_bundle``). Kept in
#: sync by tests/test_host_attestation.py, which fails if the bundler starts
#: shipping a tree this digest does not cover — an uncovered tree is code that
#: can drift invisibly, which is the whole failure being fixed here.
PAYLOAD_TREES: tuple[str, ...] = ("adapters", "src/switchboard", "db")

ATTESTATION_SCHEMA = "switchboard.host_attestation.v1"

_UNKNOWN = ""


def _payload_files(root: str) -> list[str]:
    """Every ``.py`` file in the shipped payload, as sorted relative posix paths."""
    found: list[str] = []
    for tree in PAYLOAD_TREES:
        base = os.path.join(root, *tree.split("/"))
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                if name.endswith(".py"):
                    full = os.path.join(dirpath, name)
                    found.append(os.path.relpath(full, root).replace(os.sep, "/"))
    # Repo-root modules: store.py's import closure ships with the bundle, so a
    # drifted root module is just as capable of breaking a launch.
    try:
        found.extend(name for name in os.listdir(root)
                     if name.endswith(".py") and os.path.isfile(os.path.join(root, name)))
    except OSError:
        pass
    return sorted(set(found))


def compute_bundle_digest(root: str = _PAYLOAD_ROOT) -> str:
    """Content hash of the installed payload, or "" if it cannot be read.

    Path and content are both folded in, so moving a file changes the digest
    even when no byte of any file changes.
    """
    digest = hashlib.sha256()
    counted = 0
    for relative in _payload_files(root):
        full = os.path.join(root, *relative.split("/"))
        try:
            with open(full, "rb") as handle:
                content = hashlib.sha256()
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    content.update(chunk)
        except OSError:
            # A payload we cannot read is a payload we cannot vouch for. Report
            # nothing rather than a digest that silently omits files: the server
            # treats an absent digest as unproven, which is the safe reading.
            return _UNKNOWN
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content.digest())
        counted += 1
    if not counted:
        return _UNKNOWN
    return "sha256:" + digest.hexdigest()


def compute_contract_fingerprint() -> str:
    """The contract shape this bundle's own execution_assignment produces."""
    try:
        from switchboard.connect import execution_assignment
        return str(execution_assignment.contract_fingerprint() or "")
    except Exception:
        # A bundle too old to expose contract_fingerprint reports nothing, and
        # the server blocks it: "cannot prove it is compatible" is exactly the
        # 0.4.15 state, and assuming good is what cost the fleet three missions.
        return _UNKNOWN


_CACHE: dict[str, str] = {}


def _cached(key: str, compute) -> str:
    if key not in _CACHE:
        _CACHE[key] = compute()
    return _CACHE[key]


def bundle_digest() -> str:
    return _cached("bundle_digest", compute_bundle_digest)


def contract_fingerprint() -> str:
    return _cached("contract_fingerprint", compute_contract_fingerprint)


def attestation(*, update_state: str = "", update_error: str = "") -> dict[str, Any]:
    """The heartbeat payload describing this running host.

    Rides beside ``runtime_profile`` rather than inside it: profile components
    are canonically hashed for placement eligibility, so putting attestation
    there would move every host's profile hash for a reason unrelated to
    capability.
    """
    return {
        "schema": ATTESTATION_SCHEMA,
        "bundle_digest": bundle_digest(),
        "contract_fingerprint": contract_fingerprint(),
        "update_state": str(update_state or "").strip(),
        "update_error": str(update_error or "").strip(),
    }
