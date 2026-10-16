#!/usr/bin/env python3
"""Authorize releases and keep installer handoffs in private draft assets."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess

REPO = "6th-Element-Labs/simplemark"


def api(path, body=None):
    args = ["gh", "api", f"repos/{REPO}/{path}"]
    if body is not None:
        args += ["--method", "POST", "--input", "-"]
    result = subprocess.run(args, input=json.dumps(body) if body is not None else None,
                            text=True, capture_output=True, check=True)
    return json.loads(result.stdout) if result.stdout.strip() else None


def gh(*args):
    subprocess.run(["gh", "release", *args, "--repo", REPO], check=True)


def output(**values):
    with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
        for name, value in values.items():
            if "\n" in str(value):
                raise ValueError("Multiline workflow outputs are forbidden")
            stream.write(f"{name}={value}\n")


def authorize(source, tag, validation, emergency, reason):
    sha = api(f"commits/{source}")["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("An exact canonical source SHA is required")
    checked_out = subprocess.run(["git", "-C", "source", "rev-parse", "HEAD"],
                                 text=True, capture_output=True, check=True).stdout.strip()
    if checked_out != sha:
        raise ValueError("Canonical ref changed while checking out source; retry explicitly")
    if not emergency:
        main = api("commits/main")["sha"]
        if api(f"compare/{sha}...{main}")["status"] not in ("identical", "ahead"):
            raise ValueError("Ordinary releases must be contained in canonical main")
        statuses = api(f"commits/{sha}/status")["statuses"]
        state = next((s["state"] for s in statuses
                      if s["context"] == "simplemark-ci/full-suite"), None)
        if state != "success":
            raise ValueError("Exact source SHA has not passed the public full suite")
    elif not reason.strip():
        raise ValueError("An emergency release requires an explicit reason")
    version = json.loads(Path("source/package.json").read_text())["version"]
    effective_tag = tag or f"v{version}"
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", effective_tag):
        raise ValueError("Invalid release tag")
    subprocess.run(["node", "scripts/assert-release-version.mjs", "--tag", effective_tag],
                   cwd="source", check=True)
    if not validation:
        if not tag:
            raise ValueError("A release tag is required outside validation mode")
        if not emergency and api(f"commits/{tag}")["sha"] != sha:
            raise ValueError("Release tag does not identify the requested source SHA")
        if any(r["tag_name"] == tag for r in api("releases?per_page=100")):
            raise ValueError("An existing release must not be overwritten")
    staging = "ci-staging-" + os.environ["GITHUB_RUN_ID"] + "-" + os.environ["GITHUB_RUN_ATTEMPT"]
    gh("create", staging, "--draft", "--target", sha,
       "--title", "BUILD IN PROGRESS - private installer staging",
       "--notes", "Temporary private build handoff. Not a release candidate. Removed after the run.")
    output(sha=sha, tag=effective_tag, version=version, staging_tag=staging)


def verdict(sha, tag, result):
    if not tag:
        return
    api(f"statuses/{sha}", {"state": "success" if result == "success" else "failure",
        "context": "SimpleMark Release / public " + tag,
        "description": "Complete private draft built on free public runners" if result == "success"
        else "Public release build failed; no release was published",
        "target_url": f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"})


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["authorize", "verdict"])
    a = p.parse_args()
    if a.command == "authorize":
        authorize(os.environ["SOURCE_REF"], os.environ.get("RELEASE_TAG", ""),
                  os.environ.get("VALIDATION_ONLY") == "true",
                  os.environ.get("EMERGENCY") == "true", os.environ.get("REASON", ""))
    else:
        verdict(os.environ["SOURCE_SHA"], os.environ.get("RELEASE_TAG", ""),
                os.environ["RELEASE_RESULT"])
