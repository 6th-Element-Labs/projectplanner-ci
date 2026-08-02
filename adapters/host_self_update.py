"""Act on the release the server says this host should be running.

The server already knows the answer — ``host_readiness`` decides it and the
heartbeat response carries ``required_host_release``. What was missing was a
host that does anything about it. Before this, a contract-breaking change meant
an operator noticed a fleet-wide launch outage, found the version skew by hand,
and re-ran the installer. That is the loop this closes.

The sequence is deliberately boring:

    idle → draining → installing → (service restart) → idle

``draining`` stops taking new work and waits for live runners to finish; it does
not interrupt them. Nothing is downloaded until the host is quiet, and nothing
is installed that does not pass ``verify_bundle``'s signature and per-file hash
check. ``update_host`` performs the atomic switch and rolls back if the restarted
service fails to come up.

Two rules keep this from becoming its own outage:

*Never retry the same failure forever.* A digest that failed to install is
recorded and skipped until a different release is promoted. A host that cannot
update reports ``update_error`` and shows a red light — an operator seeing the
reason beats a host silently looping on a bad bundle.

*Always terminate.* The drain has a deadline. Past it the update is abandoned,
the state returns to idle, and the host goes back to work on the old bundle. A
host stuck at ``draining`` withholds work from itself, so an unbounded wait
would take the host offline in all but name.
"""

from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
import time
import urllib.request
import urllib.parse
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional

#: Longest a host waits for live runners to finish before giving up on an
#: update. Runners are long — a real implementation session can run for tens of
#: minutes — but the host is withholding work from itself the whole time, so the
#: wait cannot be open-ended.
DRAIN_DEADLINE_S = 15 * 60

#: A bundle download that stalls must not hold the update phase open until the
#: drain deadline; failing fast returns the host to work.
DOWNLOAD_TIMEOUT_S = 120


class UpdateError(RuntimeError):
    """This host cannot safely become the release it was told to run."""

IDLE = "idle"
DRAINING = "draining"
INSTALLING = "installing"

#: Reasons a host declines to act, reported verbatim so the UI can say why the
#: light is not green instead of just that it is not.
SKIP_ALREADY_CURRENT = "already_current"
SKIP_NO_REQUIREMENT = "no_required_release"
SKIP_NO_DOWNLOAD = "release_has_no_download_url"
SKIP_PREVIOUSLY_FAILED = "release_previously_failed"
SKIP_NOT_ENROLLED = "host_not_enrolled"


class UpdatePlan(dict):
    """A decision about whether to update, and why."""

    @property
    def act(self) -> bool:
        return bool(self.get("act"))


def _text(value: Any) -> str:
    return str(value or "").strip()


def decide(*, required: Optional[Mapping[str, Any]],
           installed_digest: str,
           installed_version: str,
           state: Mapping[str, Any],
           enrolled: bool = True,
           now: Optional[float] = None) -> UpdatePlan:
    """Pure decision: should this host update, and to what?

    Split from the doing so the interesting half is testable without a bundle,
    a network, or a service manager.
    """
    now = time.time() if now is None else float(now)
    phase = _text(state.get("phase")) or IDLE

    if not required:
        return UpdatePlan(act=False, reason=SKIP_NO_REQUIREMENT, phase=IDLE)

    target_digest = _text(required.get("bundle_digest"))
    target_version = _text(required.get("version"))
    download_url = _text(required.get("download_url"))
    request_id = _text(required.get("update_request_id"))

    # Matching the promoted digest is the only definition of current. Version
    # equality is not enough: the whole point of the digest is that a
    # hand-patched tree keeps its version.
    if target_digest and installed_digest and target_digest == installed_digest:
        return UpdatePlan(act=False, reason=SKIP_ALREADY_CURRENT, phase=IDLE)

    # An update already under way owns the decision until it finishes or times
    # out. Re-deciding every heartbeat would restart the drain on every tick.
    if phase in {DRAINING, INSTALLING}:
        started = float(state.get("started_at") or 0.0)
        if phase == DRAINING and started and (now - started) > DRAIN_DEADLINE_S:
            return UpdatePlan(
                act=False, phase=IDLE, abandon=True,
                reason="drain_deadline_exceeded",
                error=(f"Runners did not finish within "
                       f"{DRAIN_DEADLINE_S // 60}m; staying on "
                       f"{installed_version or 'the installed release'}."))
        return UpdatePlan(act=True, phase=phase, target_digest=target_digest,
                          target_version=target_version,
                          download_url=download_url, update_request_id=request_id,
                          reason="in_progress")

    if (target_digest and target_digest == _text(state.get("failed_digest"))
            and request_id == _text(state.get("failed_request_id"))):
        return UpdatePlan(act=False, reason=SKIP_PREVIOUSLY_FAILED, phase=IDLE,
                          error=_text(state.get("failed_error")))

    if not download_url:
        # Nothing to fetch. The server still blocks an incompatible host, so
        # this degrades to "red light, operator installs by hand" rather than
        # to a host quietly running the wrong code.
        return UpdatePlan(act=False, reason=SKIP_NO_DOWNLOAD, phase=IDLE)

    if not enrolled:
        return UpdatePlan(act=False, reason=SKIP_NOT_ENROLLED, phase=IDLE)

    return UpdatePlan(act=True, phase=DRAINING, target_digest=target_digest,
                      target_version=target_version, download_url=download_url,
                      update_request_id=request_id,
                      reason="release_promoted", started_at=now)


def advance(*, plan: UpdatePlan, active_sessions: int) -> UpdatePlan:
    """Move a live update forward one heartbeat.

    ``draining`` becomes ``installing`` only when no runner is live. Runners are
    allowed to finish; they are never interrupted for a version bump.
    """
    if not plan.act:
        return plan
    if plan.get("phase") == DRAINING and int(active_sessions or 0) <= 0:
        return UpdatePlan({**plan, "phase": INSTALLING})
    return plan


def _download(url: str, target: "Path") -> None:
    """Fetch a bundle archive over https only.

    Plain http is refused rather than warned about: this artifact becomes the
    code the host executes, and the signature check that follows only proves the
    bundle was signed — not that the transport chose which signed bundle.
    """
    trusted_base = (os.environ.get("PM_SWITCHBOARD_PUBLIC_BASE")
                    or os.environ.get("PM_BASE") or "").strip()
    resolved = resolve_download_url(url, trusted_base)
    with urllib.request.urlopen(resolved, timeout=DOWNLOAD_TIMEOUT_S) as response:
        with open(target, "wb") as handle:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                handle.write(chunk)


def resolve_download_url(url: str, trusted_base: str) -> str:
    """Resolve one release URL and keep it on the trusted HTTPS origin.

    Older published releases carried a relative API path. The Host rejected it
    because only absolute HTTPS was accepted, leaving automatic update red while
    the same signed package worked manually. Resolution belongs at this trust
    boundary: relative is accepted only against the enrolled Switchboard base;
    cross-origin, downgraded, credentialed, and fragment URLs fail loudly.
    """
    raw = _text(url)
    base = _text(trusted_base).rstrip("/")
    if not raw:
        raise UpdateError("host release download URL is empty")
    if not base:
        raise UpdateError("trusted Switchboard base URL is unset")
    base_parts = urllib.parse.urlsplit(base)
    if base_parts.scheme.lower() != "https" or not base_parts.netloc:
        raise UpdateError("trusted Switchboard base URL must be absolute HTTPS")
    resolved = urllib.parse.urljoin(base + "/", raw)
    parts = urllib.parse.urlsplit(resolved)
    if parts.scheme.lower() != "https" or not parts.netloc:
        raise UpdateError("Host release download URL must resolve to absolute HTTPS")
    if parts.username or parts.password:
        raise UpdateError("Host release download URL must not contain credentials")
    if parts.fragment:
        raise UpdateError("Host release download URL must not contain a fragment")
    if (parts.hostname, parts.port or 443) != (base_parts.hostname, base_parts.port or 443):
        raise UpdateError("Host release download URL must stay on the trusted Switchboard origin")
    return urllib.parse.urlunsplit(parts)


def install(plan: UpdatePlan, *,
            download=_download,
            update=None,
            state_path: str = "",
            public_key_path: str = "") -> dict[str, Any]:
    """Download, verify, and switch to the planned release.

    The verification and the atomic switch are not reimplemented here:
    ``agent_host_enrollment.update_host`` already verifies the Ed25519 signature
    and every per-file hash, installs to a new release directory, flips the
    ``current`` symlink, restarts the service, and rolls back if the restarted
    service fails to come up. This function's only job is to put a verified
    bundle directory in front of it.
    """
    try:
        from adapters import agent_host_enrollment as enrollment
    except ImportError:  # Installed host payload executes from adapters/.
        import agent_host_enrollment as enrollment

    state_path = state_path or os.environ.get("PM_AGENT_HOST_STATE_PATH") or ""
    public_key_path = public_key_path or os.environ.get(
        "PM_AGENT_HOST_PUBLIC_KEY_PATH") or ""
    if not state_path:
        raise UpdateError("PM_AGENT_HOST_STATE_PATH is unset; host is not enrolled")
    if not public_key_path:
        raise UpdateError("PM_AGENT_HOST_PUBLIC_KEY_PATH is unset; "
                          "an unverifiable bundle is never installed")

    update = update or enrollment.update_host
    workdir = Path(tempfile.mkdtemp(prefix="switchboard-host-update-"))
    try:
        archive = workdir / "bundle.tar.gz"
        download(str(plan.get("download_url") or ""), archive)
        extracted = workdir / "bundle"
        extracted.mkdir()
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                # An archive is untrusted until the manifest signature is
                # checked, and extraction happens first. Path traversal and
                # symlinks are refused here rather than after they have already
                # written outside the work directory.
                name = PurePosixPath(member.name)
                if name.is_absolute() or ".." in name.parts:
                    raise UpdateError(f"unsafe path in host bundle: {member.name}")
                if member.issym() or member.islnk():
                    raise UpdateError(f"host bundle contains a link: {member.name}")
            tar.extractall(extracted)
        root = extracted
        if not (root / "manifest.json").is_file():
            candidates = [child for child in root.iterdir()
                          if (child / "manifest.json").is_file()]
            if len(candidates) != 1:
                raise UpdateError("host bundle does not contain a single manifest.json")
            root = candidates[0]

        # Belt and braces: update_host verifies too, but checking here means a
        # tampered bundle never reaches the installer at all.
        manifest = enrollment.verify_bundle(root, Path(public_key_path))
        result = update(bundle_dir=root, public_key_path=Path(public_key_path),
                        state_path=Path(state_path))
        return {"version": manifest.get("version"), **dict(result or {})}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
