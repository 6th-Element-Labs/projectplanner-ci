"""Exercise the exact-source fetch with an isolated Git remote, never a token."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


class AuditSourceTests(unittest.TestCase):
    def test_fetch_pins_source_without_persisting_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "canonical"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            helper = repo / ".github/scripts/ssm_release_audit.sh"
            helper.parent.mkdir(parents=True)
            helper.write_text("#!/bin/bash\nexit 0\n")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "-c", "user.name=Synthetic", "-c",
                            "user.email=synthetic@example.invalid", "commit", "-qm", "fixture"],
                           cwd=repo, check=True)
            sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            run = root / "runner"
            run.mkdir()
            env = {**os.environ, "GH_TOKEN": "synthetic-not-a-secret", "SOURCE_SHA": sha,
                   "CANONICAL_REPO": "6th-Element-Labs/ActionEngine",
                   "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"url.{repo.as_uri()}.insteadOf",
                   "GIT_CONFIG_VALUE_0": "https://github.com/6th-Element-Labs/ActionEngine.git"}
            result = subprocess.run(["bash", str(HERE / "actionengine_audit_source.sh")],
                                    cwd=run, env=env, text=True, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            checkout = run / "audit-source"
            self.assertEqual(subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip(), sha)
            config = (checkout / ".git/config").read_text()
            self.assertNotIn("synthetic-not-a-secret", config + result.stdout + result.stderr)
            self.assertNotIn("credential", config)
            self.assertNotIn("extraheader", config.lower())
            self.assertEqual((checkout / helper.relative_to(repo)).read_bytes(), helper.read_bytes())

    def test_invalid_source_or_repository_fails_before_checkout(self):
        for sha, repo in [("main", "6th-Element-Labs/ActionEngine"), ("a" * 40, "other/source")]:
            with self.subTest(sha=sha, repo=repo), tempfile.TemporaryDirectory() as directory:
                result = subprocess.run(["bash", str(HERE / "actionengine_audit_source.sh")],
                                        cwd=directory, env={**os.environ, "GH_TOKEN": "synthetic",
                                        "SOURCE_SHA": sha, "CANONICAL_REPO": repo}, capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((Path(directory) / "audit-source").exists())

    def test_audit_uses_the_gated_sha_and_target_without_deploying(self):
        source = (HERE.parent / "workflows/actionengine-preprod-deploy.yml").read_text()
        self.assertIn("client-id: ${{ vars.SWITCHBOARD_APP_CLIENT_ID }}", source)
        self.assertIn("EXPECTED_SHA: ${{ inputs.source_sha }}", source)
        self.assertIn("TARGET_ENVIRONMENT: ${{ inputs.environment || 'preprod' }}", source)
        self.assertIn("if: ${{ !inputs.audit_only }}", source)
        self.assertIn("run: bash .github/scripts/actionengine_audit_source.sh", source)
        self.assertIn("test \"$gate\" = success", source)


if __name__ == "__main__":
    unittest.main()
