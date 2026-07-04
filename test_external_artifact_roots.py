#!/usr/bin/env python3
"""HARDEN-29 external artifact/root provenance regression."""
import subprocess
import tempfile
from pathlib import Path

import external_artifact_roots as roots


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


def commit_file(repo, name, text):
    path = Path(repo) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-m", f"add {name}")


with tempfile.TemporaryDirectory(prefix="external-roots-main-") as main_tmp, \
        tempfile.TemporaryDirectory(prefix="external-roots-versioned-") as ext_tmp, \
        tempfile.TemporaryDirectory(prefix="external-roots-temp-", dir="/tmp") as temp_tmp:
    repo = Path(main_tmp)
    git(repo, "init")
    git(repo, "config", "user.email", "switchboard@example.test")
    git(repo, "config", "user.name", "Switchboard Test")
    commit_file(repo, "reports/review.md", "repo report\n")

    versioned = Path(ext_tmp)
    git(versioned, "init")
    git(versioned, "config", "user.email", "switchboard@example.test")
    git(versioned, "config", "user.name", "Switchboard Test")
    commit_file(versioned, "approval/input.json", "{}\n")
    external_head = git(versioned, "rev-parse", "HEAD").stdout.strip()

    temp_root = Path(temp_tmp) / "helm-forge14"
    temp_root.mkdir()
    (temp_root / "approval.json").write_text("{}\n", encoding="utf-8")
    missing_tmp = Path(temp_tmp) / "missing-root"

    red = roots.run_external_artifact_preflight(
        [
            {"input_id": "repo-report", "path": "reports/review.md", "finding_ids": ["F-1"]},
            {"input_id": "missing", "path": str(missing_tmp), "required": True, "finding_ids": ["F-2"]},
            {"input_id": "temp", "path": str(temp_root), "required": True, "finding_ids": ["F-3"]},
        ],
        repo,
        workflow_id="forge-audit",
        project="switchboard",
    )
    ok(red["status"] == "red" and not red["ok"], "missing/temp roots fail the workflow red")
    ok(any(f["code"] == "external_root_missing" for f in red["findings"]),
       "missing required external root is named")
    ok(any(f["code"] == "external_temp_root_unprovenanced" for f in red["findings"]),
       "temp root without provenance is named")
    ok(red["source_counts"]["repo"] == 1 and red["source_counts"]["external_temp"] == 2,
       "report counts repo and external temp inputs")

    yellow = roots.run_external_artifact_preflight(
        [
            {
                "input_id": "temp-non-repro",
                "path": str(temp_root),
                "required": True,
                "non_reproducible": True,
                "reason": "legacy operator supplied temp approval root",
                "finding_ids": ["F-3"],
            }
        ],
        repo,
    )
    ok(yellow["status"] == "yellow" and yellow["inputs"][0]["provenance"] == "non_reproducible",
       "declared non-reproducible temp input stays visible yellow")

    pass_report = roots.run_external_artifact_preflight(
        [
            {"input_id": "repo-report", "path": "reports/review.md", "finding_ids": ["F-1"]},
            {"input_id": "versioned-root", "path": str(versioned), "provenance": "versioned",
             "finding_ids": ["F-4"]},
            {"input_id": "url-report", "url": "https://example.test/report.json", "finding_ids": ["F-5"]},
            {"input_id": "repo-ref", "ref": git(repo, "rev-parse", "HEAD").stdout.strip(),
             "finding_ids": ["F-6"]},
        ],
        repo,
    )
    ok(pass_report["status"] == "pass" and pass_report["ok"], "repo/versioned/url/ref inputs pass")
    versioned_item = next(item for item in pass_report["inputs"] if item["input_id"] == "versioned-root")
    ok(versioned_item["git_head"] == external_head, "versioned external root records git head")

    annotated = roots.attribute_findings(
        [{"finding_id": "F-1"}, {"finding_id": "F-4"}, {"finding_id": "F-X"}],
        pass_report,
    )
    by_id = {item["finding_id"]: item for item in annotated}
    ok(by_id["F-1"]["source_classes"] == ["repo"], "repo findings are attributed to repo state")
    ok(by_id["F-4"]["source_classes"] == ["external_versioned"], "external findings keep source class")
    ok(by_id["F-X"]["source_classes"] == ["repo"], "unmapped findings default to repo state")

    header = roots.format_external_roots_header(red)
    ok("external_temp_root_unprovenanced" in header and "source_counts=" in header,
       "human header includes findings and source counts")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
