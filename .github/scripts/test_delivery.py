import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


router = load("delivery_router")
release = load("simplemark_release")
SHA = "a" * 40


class RouterTests(unittest.TestCase):
    def route(self, statuses, tags=(), releases=(), product="all", preprod_auto="on", macos_signing="off"):
        calls = []

        def api(path, token, method="GET", body=None):
            calls.append((path, method, body))
            if method != "GET":
                return None
            if path.endswith("DEPLOY_PREPROD_AUTO"):
                return {"value": "on"}
            if "/commits/" in path and not path.endswith("/status"):
                return {"sha": SHA}
            if path.endswith("/status"):
                return {"statuses": statuses}
            if "/tags?" in path:
                return list(tags)
            if "/releases?" in path:
                return list(releases)
            raise AssertionError(path)

        with patch.object(router, "api", side_effect=api), patch.dict(os.environ, {
                "PREPROD_AUTO": preprod_auto, "SIMPLEMARK_MACOS_SIGNING_ENABLED": macos_signing}):
            router.route("source-token", "public-token", product)
        return calls

    def test_failed_exact_sha_gate_cannot_dispatch_deployment(self):
        calls = self.route([{"context": "ActionEngine CI / public mirror gate", "state": "failure"}])
        self.assertFalse(any("dispatches" in p for p, _, _ in calls))

    def test_both_environments_route_to_public_repo_exact_sha(self):
        calls = self.route([{"context": "ActionEngine CI / public mirror gate", "state": "success"}])
        dispatches = [b for p, _, b in calls if "dispatches" in p]
        self.assertEqual([d["inputs"]["environment"] for d in dispatches], ["preprod", "production"])
        self.assertTrue(all(d["inputs"]["source_sha"] == SHA and d["ref"] == "master" for d in dispatches))
        self.assertTrue(all("projectplanner-ci/" in p for p, _, _ in calls if "dispatches" in p))

    def test_existing_failure_requires_manual_retry(self):
        statuses = [{"context": "ActionEngine CI / public mirror gate", "state": "success"}]
        statuses += [{"context": f"ActionEngine Deploy / public {e}", "state": "failure"}
                     for e in ("preprod", "production")]
        calls = self.route(statuses)
        self.assertFalse(any("dispatches" in p for p, _, _ in calls))

    def test_actionengine_router_needs_no_simplemark_access(self):
        calls = self.route([{"context": "ActionEngine CI / public mirror gate", "state": "success"}],
                           product="actionengine")
        self.assertFalse(any("simplemark" in p.lower() for p, _, _ in calls))

    def test_paused_preprod_requests_read_only_provenance_audit(self):
        calls = self.route([{"context": "ActionEngine CI / public mirror gate", "state": "success"}],
                           product="actionengine", preprod_auto="off")
        dispatches = [b["inputs"] for p, _, b in calls if "dispatches" in p]
        self.assertEqual(dispatches[0]["audit_only"], "true")
        self.assertEqual(dispatches[1]["audit_only"], "false")

    def test_existing_release_is_never_overwritten(self):
        calls = self.route([], [{"name": "v0.1.0", "commit": {"sha": SHA}}], [{"tag_name": "v0.1.0"}])
        self.assertFalse(any("dispatches" in p for p, _, _ in calls))

    def test_version_tag_routes_to_private_draft_workflow(self):
        calls = self.route([], [{"name": "v0.1.0", "commit": {"sha": SHA}}])
        dispatches = [(p, b) for p, _, b in calls if "dispatches" in p]
        self.assertEqual(len(dispatches), 1)
        self.assertIn("simplemark-release.yml", dispatches[0][0])
        self.assertEqual(dispatches[0][1]["inputs"]["validation_only"], "false")
        self.assertEqual(dispatches[0][1]["inputs"]["sign_macos"], "false")

    def test_configured_tag_route_requests_protected_mac_signing(self):
        calls = self.route([], [{"name": "v0.1.0", "commit": {"sha": SHA}}],
                           product="simplemark", macos_signing="on")
        dispatches = [b for p, _, b in calls if "dispatches" in p]
        self.assertEqual(len(dispatches), 1)
        self.assertEqual(dispatches[0]["inputs"]["sign_macos"], "true")
        self.assertEqual(dispatches[0]["ref"], "master")


class AuthorizationTests(unittest.TestCase):
    def test_ungated_source_cannot_create_private_staging(self):
        def api(path):
            if path.startswith("commits/") and not path.endswith("/status"):
                return {"sha": SHA}
            if path.startswith("compare/"):
                return {"status": "identical"}
            return {"statuses": []}

        with patch.object(release, "api", side_effect=api), patch.object(release, "gh") as gh:
            with patch.object(release.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, SHA + "\n")):
                with self.assertRaisesRegex(ValueError, "public full suite"):
                    release.authorize("main", "", True, False, "")
            gh.assert_not_called()

    def test_ref_changed_during_checkout_is_rejected(self):
        with patch.object(release, "api", return_value={"sha": SHA}), patch.object(release, "gh") as gh:
            with patch.object(release.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "b" * 40 + "\n")):
                with self.assertRaisesRegex(ValueError, "ref changed"):
                    release.authorize("main", "", True, False, "")
            gh.assert_not_called()

    def test_emergency_cannot_silently_skip_governance(self):
        with patch.object(release, "api", return_value={"sha": SHA}), patch.object(release, "gh") as gh:
            with patch.object(release.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, SHA + "\n")):
                with self.assertRaisesRegex(ValueError, "explicit reason"):
                    release.authorize("feature", "v0.1.0", False, True, "")
            gh.assert_not_called()

    def test_production_cannot_skip_fmp_before_any_aws_request(self):
        env = {**os.environ, "INSTANCE_ID": "i-" + "0" * 17,
               "SHA": SHA, "ACTOR": "tester", "DEPLOY_ENV": "prod", "SKIP_FMP_SYNC": "1"}
        result = subprocess.run(["bash", str(HERE / "actionengine_deploy.sh")],
                                env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot skip", result.stderr)

    def test_automatic_bot_actor_reaches_ssm_and_preserves_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            aws = root / "aws"
            aws.write_text("#!/usr/bin/env bash\n"
                           "printf '%s\\n' \"$@\" >> \"$AWS_LOG\"\n"
                           "if [[ $2 == send-command ]]; then echo test-command;\n"
                           "elif [[ $* == *'--query Status'* ]]; then echo Success; fi\n")
            sleep = root / "sleep"
            sleep.write_text("#!/bin/sh\nexit 0\n")
            aws.chmod(0o755)
            sleep.chmod(0o755)
            log = root / "aws.log"
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"],
                   "AWS_LOG": str(log), "INSTANCE_ID": "i-" + "0" * 17,
                   "SHA": SHA, "ACTOR": "github-actions[bot]", "DEPLOY_ENV": "prod",
                   "SKIP_FMP_SYNC": "0"}
            result = subprocess.run(["bash", str(HERE / "actionengine_deploy.sh")],
                                    env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("github-public:github-actions[bot]", log.read_text())
            self.assertIn("canary green", result.stdout)


class MacSigningTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.runner = self.root / "runner"
        self.runner.mkdir()
        self.log = self.root / "commands.log"
        self.env = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                    "RUNNER_TEMP": str(self.runner), "GITHUB_ENV": str(self.root / "github-env"),
                    "APPLE_CERTIFICATE": "dGVzdC1jZXJ0aWZpY2F0ZQ==",
                    "APPLE_CERTIFICATE_PASSWORD": "test-password",
                    "APPLE_SIGNING_IDENTITY": "Developer ID Application: Test (AAAAAAAAAA)",
                    "KEYCHAIN_PASSWORD": "test-keychain-password", "APPLE_API_ISSUER": "test-issuer",
                    "APPLE_API_KEY": "test-key-id", "APPLE_API_KEY_P8": "test-p8",
                    "APPLE_TEAM_ID": "AAAAAAAAAA", "SIGNING_TEST_LOG": str(self.log),
                    "TEST_SIGNED_TEAM": "AAAAAAAAAA", "TEST_NOTARY_STATUS": "Accepted"}
        commands = {
            "security": 'if [[ $1 == find-identity ]]; then echo "$APPLE_SIGNING_IDENTITY"; fi',
            "codesign": 'if [[ $1 == -dv ]]; then printf "TeamIdentifier=%s\\nAuthority=Developer ID Application: Test\\n" "$TEST_SIGNED_TEAM" >&2; fi',
            "spctl": "true",
            "xcrun": 'if [[ $1 == notarytool ]]; then printf \'{"status":"%s"}\\n\' "$TEST_NOTARY_STATUS"; fi',
        }
        for name, body in commands.items():
            path = self.bin / name
            path.write_text('#!/usr/bin/env bash\nset -eu\n'
                            'printf "%s\\n" "${0##*/} $*" >> "$SIGNING_TEST_LOG"\n' + body + "\n")
            path.chmod(0o755)
        bundle = self.root / "src-tauri/target/aarch64-apple-darwin/release/bundle"
        (bundle / "macos/SimpleMark.app").mkdir(parents=True)
        (bundle / "dmg").mkdir()
        (bundle / "dmg/SimpleMark.dmg").write_bytes(b"test-installer")

    def run_signing(self, command, *args):
        return subprocess.run(["bash", str(HERE / "simplemark_macos_signing.sh"), command, *args],
                              env=self.env, cwd=self.root, text=True, capture_output=True, timeout=10)

    def test_missing_api_key_fails_before_import(self):
        self.env.pop("APPLE_API_KEY_P8")
        result = self.run_signing("prepare")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing APPLE_API_KEY_P8", result.stderr)
        self.assertFalse(self.log.exists())

    def test_development_certificate_cannot_enter_the_release_lane(self):
        self.env["APPLE_SIGNING_IDENTITY"] = "Apple Development: Test"
        result = self.run_signing("prepare")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Developer ID Application", result.stderr)
        self.assertFalse(self.log.exists())

    def test_api_material_is_private_and_cleanup_removes_it(self):
        result = self.run_signing("prepare")
        self.assertEqual(result.returncode, 0, result.stderr)
        key = self.runner / "simplemark-notary.p8"
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertIn(str(key), (self.root / "github-env").read_text())
        result = self.run_signing("cleanup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(key.exists())
        self.assertFalse((self.runner / "simplemark-developer-id.p12").exists())

    def test_wrong_team_never_submits_the_installer(self):
        self.env["TEST_SIGNED_TEAM"] = "BBBBBBBBBB"
        result = self.run_signing("verify", "aarch64-apple-darwin")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected Apple team", result.stderr)
        self.assertNotIn("notarytool submit", self.log.read_text())

    def test_rejected_notarization_never_staples_the_installer(self):
        self.env["TEST_NOTARY_STATUS"] = "Invalid"
        result = self.run_signing("verify", "aarch64-apple-darwin")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("notarization was not accepted", result.stderr)
        self.assertNotIn("stapler staple", self.log.read_text())

    def test_accepted_installer_is_stapled_and_assessed(self):
        result = self.run_signing("verify", "aarch64-apple-darwin")
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.log.read_text()
        self.assertIn("notarytool submit", log)
        self.assertIn("stapler staple", log)
        self.assertIn("spctl --assess --type open", log)


class PromotionBindingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.records = {}
        for leg, target, suffix in [
            ("macos-arm64", "macos", "macos-arm64.dmg"),
            ("macos-x64", "macos", "macos-x64.dmg"),
            ("windows-msi", "windows", "windows-x64.msi"),
            ("windows-nsis", "windows", "windows-x64-setup.exe"),
            ("linux", "linux", "linux-x64.AppImage"),
        ]:
            name = f"SimpleMark-0.1.0-{suffix}"
            content = f"distinct installer bytes for {leg}".encode()
            (self.root / name).write_bytes(content)
            body = {"schema": "simplemark.platform-smoke.v1", "target": target, "commit": SHA,
                    "artifact": {"name": name, "sha256": hashlib.sha256(content).hexdigest()}}
            path = self.root / f"platform-smoke-{leg}.json"
            path.write_text(json.dumps(body))
            self.records[leg] = (path, body)

    def test_all_five_exact_installers_are_required(self):
        self.assertEqual(len(release.promotion_evidence(SHA, "v0.1.0", self.root)), 5)
        self.records["windows-nsis"][0].unlink()
        with self.assertRaisesRegex(ValueError, "windows-nsis"):
            release.promotion_evidence(SHA, "v0.1.0", self.root)

    def test_smoke_from_another_commit_is_rejected(self):
        path, body = self.records["macos-x64"]
        body["commit"] = "b" * 40
        path.write_text(json.dumps(body))
        with self.assertRaisesRegex(ValueError, "released commit for macos-x64"):
            release.promotion_evidence(SHA, "v0.1.0", self.root)

    def test_modified_installer_bytes_are_rejected(self):
        name = self.records["windows-msi"][1]["artifact"]["name"]
        (self.root / name).write_bytes(b"replaced after the smoke test")
        with self.assertRaisesRegex(ValueError, "exact installer bytes for windows-msi"):
            release.promotion_evidence(SHA, "v0.1.0", self.root)

    def test_arm_smoke_cannot_stand_in_for_the_intel_installer(self):
        path, _ = self.records["macos-x64"]
        path.write_text(json.dumps(self.records["macos-arm64"][1]))
        with self.assertRaisesRegex(ValueError, "exact installer bytes for macos-x64"):
            release.promotion_evidence(SHA, "v0.1.0", self.root)

    def test_cross_platform_evidence_is_rejected(self):
        path, body = self.records["linux"]
        body["target"] = "windows"
        path.write_text(json.dumps(body))
        with self.assertRaisesRegex(ValueError, "target for linux"):
            release.promotion_evidence(SHA, "v0.1.0", self.root)


if __name__ == "__main__":
    unittest.main()
