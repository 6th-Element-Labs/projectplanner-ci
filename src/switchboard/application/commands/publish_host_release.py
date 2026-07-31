"""Publish a signed Agent Host bundle as the release the fleet must run.

This is the step that turns host-release sync from a detector into a fix. The
readiness model, the placement gate, and the host's self-update all key off one
promoted release; with nothing promoted the control plane has no opinion and
every host stays eligible, which is safe but inert.

Three things happen here, in this order, and the order matters:

1. **Verify before believing.** The archive's Ed25519 signature and every
   per-file hash are checked with the public key this deployment already ships,
   before any row is written. The private key is never on the server: bundles
   are signed where the key lives and arrive already signed, so publishing
   cannot mint a release nobody signed.

2. **Derive the digest the host will compute.** Not a hash of the archive, and
   not a value the uploader supplies — the payload is hashed exactly the way a
   host hashes its installed tree. ``_install_release`` copies ``payload/``
   verbatim and refuses anything that does not match the signed manifest, so the
   two are the same bytes in the same layout. If these ever diverged, every host
   would sit permanently at "update available", re-download, and never converge.

3. **Store the archive, then promote.** The archive is written before the row is
   promoted, so a host acting on the new requirement always has something to
   fetch. The reverse order has a window where the fleet is told to update to a
   release that cannot be downloaded.
"""

from __future__ import annotations

import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict

from constants import DEFAULT_PROJECT


class PublishError(ValueError):
    """This archive cannot become a release."""


#: The verification key that ships with the deployment. Overridable for tests
#: and for a fleet that rotates its signing key.
PUBLIC_KEY_PATH = os.environ.get("PM_AGENT_HOST_RELEASE_PUBLIC_KEY") or ""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _public_key_path() -> Path:
    if PUBLIC_KEY_PATH:
        return Path(PUBLIC_KEY_PATH)
    return _repo_root() / "deploy" / "agent-host-release-public.pem"


def _import_host_modules():
    """Import the host's own bundler and attestation, from this checkout.

    Deliberately the same modules the host runs. A server-side reimplementation
    of either the verification or the digest would be a second definition of
    "the same bundle", and the two would drift.
    """
    root = _repo_root()
    for candidate in (root / "adapters", root):
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
    import agent_host_enrollment
    import host_attestation
    return agent_host_enrollment, host_attestation


def _extract(archive: bytes, into: Path) -> Path:
    """Unpack an untrusted archive and return the directory holding manifest.json."""
    staged = into / "bundle.tar.gz"
    staged.write_bytes(archive)
    extracted = into / "bundle"
    extracted.mkdir()
    try:
        with tarfile.open(staged, "r:gz") as tar:
            for member in tar.getmembers():
                # Nothing is trusted until the signature is checked, and
                # extraction happens first. Traversal and links are refused here
                # rather than after they have written outside the work directory.
                name = PurePosixPath(member.name)
                if name.is_absolute() or ".." in name.parts:
                    raise PublishError(f"unsafe path in bundle archive: {member.name}")
                if member.issym() or member.islnk():
                    raise PublishError(f"bundle archive contains a link: {member.name}")
            tar.extractall(extracted)
    except tarfile.TarError as exc:
        raise PublishError(f"bundle archive is not a readable .tar.gz: {exc}") from exc
    if (extracted / "manifest.json").is_file():
        return extracted
    nested = [child for child in extracted.iterdir()
              if child.is_dir() and (child / "manifest.json").is_file()]
    if len(nested) != 1:
        raise PublishError("bundle archive does not contain a single manifest.json")
    return nested[0]


def execute(*, archive: bytes, project: str = DEFAULT_PROJECT,
            promote: bool = True, notes: str = "",
            actor: str = "operator",
            base_url: str = "") -> Dict[str, Any]:
    """Verify, record, store, and (by default) promote one Agent Host bundle."""
    from switchboard.storage.repositories import host_releases

    enrollment, attestation = _import_host_modules()
    public_key = _public_key_path()
    if not public_key.is_file():
        raise PublishError(
            f"no Agent Host release public key at {public_key}; "
            "an unverifiable bundle is never published")

    work = Path(tempfile.mkdtemp(prefix="host-release-publish-"))
    try:
        bundle_dir = _extract(archive, work)
        try:
            manifest = enrollment.verify_bundle(bundle_dir, public_key)
        except Exception as exc:
            raise PublishError(f"bundle verification failed: {exc}") from exc

        payload = bundle_dir / "payload"
        if not payload.is_dir():
            raise PublishError("verified bundle has no payload directory")

        # The digest a host will compute from this exact tree once installed.
        digest = attestation.compute_bundle_digest(str(payload))
        if not digest:
            raise PublishError("could not digest the bundle payload")

        # The contract shape THIS bundle builds — read from the bundle's own
        # copy, not from the server's. They are usually the same file; when they
        # are not, that difference is precisely what must be recorded.
        fingerprint = _bundle_contract_fingerprint(payload)

        version = str(manifest.get("version") or "")
        release = host_releases.record_release(
            {"version": version, "bundle_digest": digest,
             "contract_fingerprint": fingerprint, "notes": notes,
             "download_url": ""},
            actor=actor, promote=False, project=project)
        release_id = str(release.get("release_id") or "")

        # Archive first, promote second. Promoting first opens a window where
        # the fleet is told to update to something it cannot download.
        host_releases.store_archive(release_id, archive)

        download_url = (f"{base_url.rstrip('/')}" if base_url else "") + (
            f"/ixp/v1/host_releases/{release_id}/bundle?project={project}")
        release = host_releases.record_release(
            {"version": version, "bundle_digest": digest,
             "contract_fingerprint": fingerprint, "notes": notes,
             "download_url": download_url},
            actor=actor, promote=promote, project=project)

        return {"schema": "switchboard.host_release_published.v1",
                "release_id": release_id, "version": version,
                "bundle_digest": digest, "contract_fingerprint": fingerprint,
                "download_url": download_url, "promoted": bool(promote),
                "files": len(manifest.get("files") or [])}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _bundle_contract_fingerprint(payload: Path) -> str:
    """Load the bundle's own execution_assignment and ask it for its shape.

    Loaded from the payload by path rather than imported by name: the server has
    its own copy already in ``sys.modules``, and importing would silently answer
    with the server's fingerprint for every bundle — making a skewed release look
    identical to a correct one, which is the exact bug being prevented.
    """
    import importlib.util

    module_path = payload / "src" / "switchboard" / "connect" / "execution_assignment.py"
    if not module_path.is_file():
        raise PublishError(
            "bundle has no switchboard/connect/execution_assignment.py; "
            "it cannot state the contract it builds")
    spec = importlib.util.spec_from_file_location(
        "_published_execution_assignment", module_path)
    if spec is None or spec.loader is None:
        raise PublishError("could not load the bundle's execution_assignment module")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        fingerprint = str(module.contract_fingerprint() or "")
    except AttributeError:
        raise PublishError(
            "this bundle predates contract attestation and cannot be promoted; "
            "hosts running it would be blocked with no way to prove otherwise")
    except Exception as exc:
        raise PublishError(f"bundle's execution_assignment is not loadable: {exc}") from exc
    if not fingerprint:
        raise PublishError("bundle reported an empty contract fingerprint")
    return fingerprint
