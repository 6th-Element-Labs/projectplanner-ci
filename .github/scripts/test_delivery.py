import importlib.util
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
    def route(self, statuses, tags=(), releases=(), product="all", preprod_auto="on"):
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

        with patch.object(router, "api", side_effect=api), patch.dict(os.environ, {"PREPROD_AUTO": preprod_auto}):
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


if __name__ == "__main__":
    unittest.main()
