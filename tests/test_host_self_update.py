#!/usr/bin/env python3
"""The host acts on the release the server says it should be running.

Companion to tests/test_host_release_sync.py, which pins the server half. This
pins the host half: what it reports about itself, and how it decides to replace
itself.

The failure being prevented is the 2026-07-31 drain canary, where an operator
had to notice a fleet-wide launch outage, diagnose version skew by hand, and
re-run the installer. The properties that make that unnecessary — and that make
the fix itself safe — are all here: the digest sees a hand-patched tree, the
drain terminates, and a bad release is not retried forever.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401

from adapters import host_attestation as att  # noqa: E402
from adapters import host_self_update as up  # noqa: E402
from switchboard.domain import host_readiness as hr  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


TMP = tempfile.mkdtemp(prefix="host-self-update-")
try:
    # ── attestation: what am I actually running ────────────────────────────
    a = att.attestation()
    ok(a["schema"] == att.ATTESTATION_SCHEMA, "attestation is schema-tagged")
    ok(a["bundle_digest"].startswith("sha256:"),
       f"the running payload has a digest: {a['bundle_digest'][:24]}")
    ok(a["contract_fingerprint"].startswith("eac1:"),
       f"the bundle reports its own contract shape: {a['contract_fingerprint']}")

    # The property the whole design rests on: editing one file changes the
    # digest even though the version string cannot. This is the hand-copied-file
    # recovery that made 0.4.15 look healthy.
    tree = Path(TMP) / "payload"
    (tree / "adapters").mkdir(parents=True)
    (tree / "adapters" / "agent_host.py").write_text("VERSION = '0.4.15'\n")
    (tree / "adapters" / "execution.py").write_text("FIELDS = ('a', 'b')\n")
    before = att.compute_bundle_digest(str(tree))
    ok(before.startswith("sha256:"), "a payload tree hashes")
    ok(att.compute_bundle_digest(str(tree)) == before, "the digest is stable")

    (tree / "adapters" / "execution.py").write_text("FIELDS = ('a', 'b', 'c')\n")
    after = att.compute_bundle_digest(str(tree))
    ok(after != before,
       "hand-patching one file changes the digest — the version string cannot")

    # Moving a file is a change too: same bytes, different import surface.
    (tree / "adapters" / "execution.py").rename(tree / "adapters" / "renamed.py")
    ok(att.compute_bundle_digest(str(tree)) != after,
       "moving a file changes the digest even with identical content")

    ok(att.compute_bundle_digest(str(Path(TMP) / "nope")) == "",
       "an unreadable payload reports no digest rather than a partial one")

    # An empty digest must not read as 'matches'. The server treats absent as
    # unproven; this checks the host cannot manufacture a false match.
    ok(hr.evaluate({"stale": False, "contract_fingerprint": ""},
                   {"version": "1", "bundle_digest": "d",
                    "contract_fingerprint": "eac1:x"})["state"] == hr.BLOCKED,
       "a host with no attestation is blocked, not assumed current")

    # ── the payload the digest covers must match what the bundler ships ────
    from adapters import agent_host_enrollment  # noqa: E402
    source = Path(agent_host_enrollment.__file__).parent.parent
    ok((source / "adapters").is_dir() and (source / "db").is_dir(),
       "the bundler's payload trees exist in this checkout")
    for tree_name in att.PAYLOAD_TREES:
        ok((source / tree_name).is_dir(),
           f"attested tree is a real payload tree: {tree_name}")

    # ── the decision ───────────────────────────────────────────────────────
    REQUIRED = {"version": "0.4.16", "bundle_digest": "sha256:new",
                "download_url": "https://example.invalid/host-0.4.16.tar.gz"}

    def plan(**over):
        kwargs = {"required": REQUIRED, "installed_digest": "sha256:old",
                  "installed_version": "0.4.15", "state": {}, "now": 1000.0}
        kwargs.update(over)
        return up.decide(**kwargs)

    p = plan()
    ok(p.act is True and p["phase"] == up.DRAINING,
       f"a promoted release starts a drain: {p['phase']}")
    ok(p["target_digest"] == "sha256:new", "the plan names the target digest")

    current = plan(installed_digest="sha256:new")
    ok(current.act is False and current["reason"] == up.SKIP_ALREADY_CURRENT,
       "a host already on the promoted digest does nothing")

    # Version equality is NOT currency: this is the hand-patched tree again.
    same_version = plan(installed_version="0.4.16", installed_digest="sha256:patched")
    ok(same_version.act is True,
       "matching the version but not the digest still updates")

    ok(plan(required=None)["reason"] == up.SKIP_NO_REQUIREMENT,
       "no promoted release means no action")
    ok(plan(required={**REQUIRED, "download_url": ""})["reason"] == up.SKIP_NO_DOWNLOAD,
       "a release with nowhere to download from degrades to the manual path")
    ok(plan(enrolled=False)["reason"] == up.SKIP_NOT_ENROLLED,
       "a host running from a checkout does not replace itself")

    # ── it must not retry a bad bundle forever ─────────────────────────────
    burned = plan(state={"failed_digest": "sha256:new",
                         "failed_request_id": "",
                         "failed_error": "signature verification failed"})
    ok(burned.act is False and burned["reason"] == up.SKIP_PREVIOUSLY_FAILED,
       "a release that already failed is not retried")
    ok("signature" in burned["error"],
       f"the failure reason survives for the operator: {burned['error']}")
    ok(plan(state={"failed_digest": "sha256:other"}).act is True,
       "a different promoted release clears the block")
    retried = plan(
        required={**REQUIRED, "update_request_id": "hostupdate-new"},
        state={"failed_digest": "sha256:new", "failed_request_id": "",
               "failed_error": "relative URL refused"})
    ok(retried.act is True and retried["update_request_id"] == "hostupdate-new",
       "an explicit Capacity request retries the same signed digest")

    # ── the download URL stays on the enrolled Switchboard origin ─────────
    resolved = up.resolve_download_url(
        "/ixp/v1/host_releases/hostrel-1/bundle?project=switchboard",
        "https://plan.example")
    ok(resolved == "https://plan.example/ixp/v1/host_releases/hostrel-1/bundle?project=switchboard",
       "a relative release URL resolves against the trusted Switchboard base")
    ok(up.resolve_download_url("https://plan.example/releases/host.tgz",
                               "https://plan.example")
       == "https://plan.example/releases/host.tgz",
       "an absolute same-origin HTTPS URL remains valid")
    for unsafe, label in [
        ("http://plan.example/host.tgz", "plain HTTP"),
        ("https://evil.example/host.tgz", "cross-origin HTTPS"),
    ]:
        try:
            up.resolve_download_url(unsafe, "https://plan.example")
            ok(False, f"{label} was accepted")
        except up.UpdateError:
            ok(True, f"{label} is refused explicitly")

    # ── it must terminate ──────────────────────────────────────────────────
    mid = plan(state={"phase": up.DRAINING, "started_at": 1000.0}, now=1060.0)
    ok(mid.act is True and mid["phase"] == up.DRAINING,
       "an in-flight drain is not restarted every heartbeat")

    stuck = plan(state={"phase": up.DRAINING, "started_at": 1000.0},
                 now=1000.0 + up.DRAIN_DEADLINE_S + 1)
    ok(stuck.act is False and stuck.get("abandon") is True,
       "a drain that never quiesces is abandoned, not waited on forever")
    ok(stuck["phase"] == up.IDLE, "abandoning returns the host to work")
    ok("did not finish" in stuck["error"], f"and says why: {stuck['error']}")

    # ── draining waits for live runners, then installs ─────────────────────
    busy = up.advance(plan=plan(), active_sessions=2)
    ok(busy["phase"] == up.DRAINING, "a host with live runners keeps draining")
    quiet = up.advance(plan=plan(), active_sessions=0)
    ok(quiet["phase"] == up.INSTALLING, "a quiet host proceeds to install")
    ok(up.advance(plan=up.UpdatePlan(act=False), active_sessions=0).act is False,
       "advance never promotes a no-op plan into an install")

    # ── the server side of the same handshake ──────────────────────────────
    updating = hr.evaluate(
        {"stale": False, "agent_host_version": "0.4.15",
         "contract_fingerprint": "eac1:x", "bundle_digest": "sha256:old",
         "update_state": up.DRAINING, "update_started_at": 1000.0},
        {"version": "0.4.16", "bundle_digest": "sha256:new",
         "contract_fingerprint": "eac1:x"}, now=1060.0)
    ok(updating["state"] == hr.UPDATING, "the server sees the host updating")
    ok(updating["withholds_work"] is True,
       "and stops feeding it, so the drain can actually reach zero")

    # A host that died mid-update must not withhold work for good.
    stale_claim = hr.evaluate(
        {"stale": False, "agent_host_version": "0.4.16",
         "contract_fingerprint": "eac1:x", "bundle_digest": "sha256:new",
         "update_state": up.DRAINING, "update_started_at": 1000.0},
        {"version": "0.4.16", "bundle_digest": "sha256:new",
         "contract_fingerprint": "eac1:x"},
        now=1000.0 + hr.UPDATE_STATE_MAX_AGE_S + 1)
    ok(stale_claim["state"] == hr.READY,
       f"a stale updating claim stops being believed: {stale_claim['state']}")
    ok(stale_claim["withholds_work"] is False,
       "an abandoned update does not withhold work forever")
    # ── the install path refuses what it cannot trust ──────────────────────
    import tarfile  # noqa: E402

    def install_from(members, *, url="https://example.invalid/b.tar.gz"):
        """Build an archive, run install() against it, return the raised error."""
        work = Path(TMP) / f"arch{len(os.listdir(TMP))}"
        work.mkdir(parents=True, exist_ok=True)
        archive = work / "bundle.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for name, body in members:
                payload = work / "stage" / name.replace("../", "up_")
                payload.parent.mkdir(parents=True, exist_ok=True)
                payload.write_text(body)
                info = tar.gettarinfo(str(payload), arcname=name)
                with open(payload, "rb") as handle:
                    tar.addfile(info, handle)
        def fake_download(_url, target):
            if not _url.lower().startswith("https://"):
                raise up.UpdateError("refusing plain http")
            shutil.copy(archive, target)
        try:
            up.install(up.UpdatePlan(download_url=url),
                       download=fake_download,
                       update=lambda **kw: {"updated": True},
                       state_path=str(Path(TMP) / "state.json"),
                       public_key_path=str(Path(TMP) / "key.pem"))
            return None
        except Exception as exc:
            return exc

    traversal = install_from([("../escape.py", "x = 1\n")])
    ok(isinstance(traversal, up.UpdateError) and "unsafe path" in str(traversal),
       f"a traversal path in the archive is refused: {traversal}")

    no_manifest = install_from([("payload/adapters/a.py", "x = 1\n")])
    ok(isinstance(no_manifest, up.UpdateError)
       and "manifest.json" in str(no_manifest),
       f"an archive with no manifest never reaches the installer: {no_manifest}")

    plain_http = install_from([("manifest.json", "{}\n")],
                              url="http://example.invalid/b.tar.gz")
    ok(plain_http is not None and "http" in str(plain_http),
       f"a bundle offered over plain http is refused: {plain_http}")

    # Missing enrollment inputs must fail before anything is fetched, not after.
    try:
        up.install(up.UpdatePlan(download_url="https://example.invalid/b.tar.gz"),
                   download=lambda *a: ok(False, "download ran without a state path"),
                   state_path="", public_key_path="/tmp/key.pem")
        ok(False, "an unenrolled host must refuse to install")
    except up.UpdateError as exc:
        ok("STATE_PATH" in str(exc), f"and says what is missing: {exc}")

    try:
        up.install(up.UpdatePlan(download_url="https://example.invalid/b.tar.gz"),
                   download=lambda *a: ok(False, "download ran without a public key"),
                   state_path="/tmp/state.json", public_key_path="")
        ok(False, "a bundle is never installed without a verification key")
    except up.UpdateError as exc:
        ok("unverifiable" in str(exc), f"and refuses unverifiable code: {exc}")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nHost self-update: {passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
