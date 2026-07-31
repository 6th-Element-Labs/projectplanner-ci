#!/usr/bin/env python3
"""The whole loop, end to end, with a real signed bundle and a real HTTP server.

The unit tests pin each half. This one exists because the halves passing proves
nothing about the join, and the join is where this feature was inert: the
readiness model, the gate, and the host's self-update were all correct and none
of them could ever fire, because nothing published a release and nothing served
a bundle.

The run below is the actual sequence:

    sign a bundle → publish it to the server → server verifies and promotes
    → a host heartbeats an OLD contract → server BLOCKS it and names the release
    → host downloads that release from the server → digests match

The load-bearing assertion is the last one. The digest the server records from
the payload and the digest a host computes from its installed tree must be the
same value, or every host sits permanently at "update available", re-downloads,
and never converges.
"""
from __future__ import annotations

import json
import os
import shutil
import tarfile
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401

TMP = tempfile.mkdtemp(prefix="host-release-e2e-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_HOST_RELEASE_DIR"] = str(Path(TMP) / "host-releases")
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
store.init_db("switchboard")

from adapters import agent_host_enrollment as enrollment  # noqa: E402
from adapters import host_attestation as attestation  # noqa: E402
from adapters import host_self_update as self_update  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from switchboard.application.commands import publish_host_release  # noqa: E402
from switchboard.domain import host_readiness as hr  # noqa: E402
from switchboard.storage.repositories import host_releases  # noqa: E402

PROJECT = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    # ── 1. sign a real bundle from this checkout ───────────────────────────
    keydir = Path(TMP) / "keys"
    keydir.mkdir()
    private = Ed25519PrivateKey.generate()
    (keydir / "private.pem").write_bytes(private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()))
    (keydir / "public.pem").write_bytes(private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo))
    os.environ["PM_AGENT_HOST_RELEASE_PUBLIC_KEY"] = str(keydir / "public.pem")
    publish_host_release.PUBLIC_KEY_PATH = str(keydir / "public.pem")

    built = Path(TMP) / "built"
    manifest = enrollment.create_signed_bundle(
        Path(ROOT), built, "9.9.9", keydir / "private.pem")
    ok(len(manifest["files"]) > 100,
       f"a real bundle was signed from this checkout: {len(manifest['files'])} files")

    archive_path = Path(TMP) / "bundle.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(built, arcname="bundle")
    archive = archive_path.read_bytes()
    ok(len(archive) > 10_000, f"and packed into an archive: {len(archive) // 1024}KB")

    # ── 2. publish it ──────────────────────────────────────────────────────
    published = publish_host_release.execute(
        archive=archive, project=PROJECT, promote=True, actor="test")
    ok(published["version"] == "9.9.9", f"the release is published: {published['version']}")
    ok(published["bundle_digest"].startswith("sha256:"),
       f"with a payload digest: {published['bundle_digest'][:26]}")
    ok(published["contract_fingerprint"].startswith("eac1:"),
       f"and the contract THIS bundle builds: {published['contract_fingerprint']}")
    ok(published["download_url"].endswith(f"bundle?project={PROJECT}"),
       f"and somewhere to fetch it: {published['download_url']}")

    stored = host_releases.archive_path(published["release_id"])
    ok(stored.is_file() and stored.read_bytes() == archive,
       "the archive is stored byte-for-byte, so the download is the signed bundle")

    promoted = host_releases.get_promoted_release(project=PROJECT)
    ok(promoted and promoted["version"] == "9.9.9",
       "and it is the promoted release the fleet is measured against")
    ok(promoted.get("archive_present") is True,
       "the row knows its archive exists — a row without one cannot serve a host")

    # ── 3. a tampered archive must not become a release ────────────────────
    tampered_dir = Path(TMP) / "tampered"
    shutil.copytree(built, tampered_dir)
    target = tampered_dir / "payload" / "adapters" / "agent_host.py"
    target.write_text(target.read_text() + "\n# smuggled\n")
    tampered_archive = Path(TMP) / "tampered.tar.gz"
    with tarfile.open(tampered_archive, "w:gz") as tar:
        tar.add(tampered_dir, arcname="bundle")
    try:
        publish_host_release.execute(archive=tampered_archive.read_bytes(),
                                     project=PROJECT, actor="test")
        ok(False, "a tampered payload must never be published")
    except publish_host_release.PublishError as exc:
        ok("verification failed" in str(exc),
           f"a tampered payload is refused before any row is written: {str(exc)[:60]}")

    # ── 4. the incident: a host on an older contract ───────────────────────
    old_host = {"stale": False, "agent_host_version": "0.4.15",
                "bundle_digest": "sha256:old",
                "contract_fingerprint": "eac1:0000000000000000"}
    verdict = hr.evaluate(old_host, promoted)
    ok(verdict["state"] == hr.BLOCKED,
       f"a host on an older contract is blocked, not merely behind: {verdict['state']}")
    ok(verdict["withholds_work"] is True,
       "so it is never handed a wake it would refuse 90 seconds later")

    # ── 5. it decides to replace itself, and knows where from ──────────────
    plan = self_update.decide(
        required=promoted, installed_digest="sha256:old",
        installed_version="0.4.15", state={}, enrolled=True)
    ok(plan.act is True, "the host decides to update")
    ok(plan["download_url"] == promoted["download_url"],
       "from the URL the server published — not one an operator typed")

    # ── 6. serve it over real HTTP and fetch it back ───────────────────────
    from fastapi.testclient import TestClient  # noqa: E402
    import app as web_app  # noqa: E402
    client = TestClient(web_app.app)

    listed = client.get(f"/ixp/v1/host_releases/promoted?project={PROJECT}")
    ok(listed.status_code == 200, f"the promoted release is readable: {listed.status_code}")
    ok(listed.json()["bundle_digest"] == published["bundle_digest"],
       "and reports the same digest the publisher recorded")

    fetched = client.get(plan["download_url"])
    ok(fetched.status_code == 200,
       f"the bundle downloads from the server itself: {fetched.status_code}")
    ok(fetched.content == archive,
       f"and the bytes are the signed archive: {len(fetched.content)} bytes")

    missing = client.get(f"/ixp/v1/host_releases/hostrel-nope/bundle?project={PROJECT}")
    ok(missing.status_code == 404,
       f"an unknown release 404s rather than serving something else: {missing.status_code}")

    # ── 7. THE JOIN: install it and confirm the digests agree ──────────────
    # This is the assertion the whole design rests on. If the digest the server
    # recorded differs from the one the host computes after installing, every
    # host re-downloads forever and never reaches ready.
    fetched_dir = Path(TMP) / "fetched"
    fetched_dir.mkdir()
    (fetched_dir / "bundle.tar.gz").write_bytes(fetched.content)
    with tarfile.open(fetched_dir / "bundle.tar.gz", "r:gz") as tar:
        tar.extractall(fetched_dir)
    installed_root = fetched_dir / "bundle" / "payload"
    ok((installed_root / "adapters" / "agent_host.py").is_file(),
       "the downloaded bundle contains a real host payload")

    host_digest = attestation.compute_bundle_digest(str(installed_root))
    ok(host_digest == published["bundle_digest"],
       f"THE JOIN: the host's digest of the installed tree equals the server's "
       f"({host_digest[:26]} == {published['bundle_digest'][:26]})")

    # And with that tree installed, the host is ready — the loop closes.
    now_ready = hr.evaluate(
        {"stale": False, "agent_host_version": "9.9.9",
         "bundle_digest": host_digest,
         "contract_fingerprint": published["contract_fingerprint"]}, promoted)
    ok(now_ready["state"] == hr.READY,
       f"a host running the promoted bundle is ready: {now_ready['state']}")
    ok(now_ready["withholds_work"] is False, "and is given work again")

    settled = self_update.decide(
        required=promoted, installed_digest=host_digest,
        installed_version="9.9.9", state={}, enrolled=True)
    ok(settled.act is False and settled["reason"] == self_update.SKIP_ALREADY_CURRENT,
       "and it stops updating — the loop converges instead of re-downloading")

    # ── 8. the operator-facing command really publishes ────────────────────
    # Exercised through a live HTTP server, because the multipart body it builds
    # by hand is exactly the kind of thing that parses fine and uploads nothing.
    import threading  # noqa: E402
    import uvicorn  # noqa: E402

    config = uvicorn.Config(web_app.app, host="127.0.0.1", port=8137,
                            log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        __import__("time").sleep(0.05)
    ok(server.started, "a real HTTP server is listening")

    try:
        cli = enrollment.publish_release(
            source_root=Path(ROOT), signing_key=keydir / "private.pem",
            base_url="http://127.0.0.1:8137", project=PROJECT,
            version="9.9.10", notes="published by CLI")
    except Exception as exc:
        import urllib.error
        detail = exc.read()[:300] if isinstance(exc, urllib.error.HTTPError) else exc
        ok(False, f"publish-release upload failed: {type(exc).__name__}: {detail}")
        cli = {}
    ok(cli.get("version") == "9.9.10",
       f"publish-release uploaded a signed bundle over the wire: {cli.get('version')}")
    ok(cli.get("promoted") is True, "and promoted it in the same command")
    ok(str(cli.get("bundle_digest", "")).startswith("sha256:"),
       f"the server digested what it received: {str(cli.get('bundle_digest'))[:26]}")

    now_promoted = host_releases.get_promoted_release(project=PROJECT)
    ok(now_promoted["version"] == "9.9.10",
       "the fleet's required release moved to the one just published")
    ok(host_releases.archive_path(now_promoted["release_id"]).is_file(),
       "and its archive is on disk, ready to serve")

    # The digest must still equal what a host computes — via the CLI path too.
    cli_fetch = client.get(now_promoted["download_url"])
    ok(cli_fetch.status_code == 200, "the CLI-published bundle downloads")
    cli_dir = Path(TMP) / "cli-fetched"
    cli_dir.mkdir()
    (cli_dir / "b.tar.gz").write_bytes(cli_fetch.content)
    with tarfile.open(cli_dir / "b.tar.gz", "r:gz") as tar:
        tar.extractall(cli_dir)
    ok(attestation.compute_bundle_digest(str(cli_dir / "bundle" / "payload"))
       == now_promoted["bundle_digest"],
       "and its digest matches too — the CLI path converges like the direct one")
    # ── 8b. the Fleet button, clicked in a browser, against this server ────
    # The endpoint returning bytes proves the server works. It does not prove
    # the operator can get them — that is a different failure, and the one that
    # has actually shipped here repeatedly.
    from playwright.sync_api import sync_playwright  # noqa: E402
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("http://127.0.0.1:8137/", wait_until="domcontentloaded")
        clicked = page.evaluate("""async (project) => {
            const ctx = Object.create(TeepPlan);
            ctx.isAdmin = true; ctx.project = project;
            const opened = [];
            const alerts = [];
            window.open = (u) => { opened.push(u); return null; };
            window.alert = (m) => alerts.push(m);
            document.body.innerHTML =
                '<table><tbody>' + ctx._hostRow({
                    host_id: 'host/mac-1', hostname: 'mac', stale: false,
                    heartbeat_at: 0, capacity: {}, limits: {},
                    runtimes: [{runtime: 'codex'}],
                    readiness: {state: 'blocked', installed_version: '0.4.15',
                                required_version: '9.9.10', actionable: true,
                                detail: 'incompatible'},
                }) + '</tbody></table>';
            const button = document.querySelector('[data-host-update]');
            if (!button) return {error: 'no update button rendered'};
            await ctx._updateHost(button.getAttribute('data-host-update'));
            return {opened, alerts};
        }""", PROJECT)
        ok(not clicked.get("error"), f"the update button renders: {clicked.get('error')}")
        opened = clicked.get("opened") or []
        ok(len(opened) == 1,
           f"clicking it opens exactly one download: {opened}")
        ok(not (clicked.get("alerts") or []),
           f"and does not fall back to an instruction dialog: {clicked.get('alerts')}")

        # Follow the URL the button opened and confirm real bundle bytes come back.
        got = page.request.get(opened[0] if opened[0].startswith("http")
                               else f"http://127.0.0.1:8137{opened[0]}")
        ok(got.status == 200, f"the opened URL serves: {got.status}")
        body = got.body()
        ok(body[:2] == b"\x1f\x8b" and len(body) > 100_000,
           f"and it is a real gzip bundle: {len(body)} bytes, magic {body[:2]!r}")
        browser.close()

    server.should_exit = True
    thread.join(timeout=10)

    # ── 9. the manifest is what was signed, not what was claimed ───────────
    signed = json.loads((built / "manifest.json").read_text())
    ok(signed["version"] == "9.9.9" and len(signed["files"]) == len(manifest["files"]),
       "the stored manifest matches what was signed")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nHost release end-to-end: {passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
