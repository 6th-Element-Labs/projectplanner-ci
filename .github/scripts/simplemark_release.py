#!/usr/bin/env python3
"""Authorize releases and keep installer handoffs in private draft assets."""
import argparse
import hashlib
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


def promotion_evidence(sha, tag, directory):
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("An exact canonical source SHA is required")
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", tag):
        raise ValueError("Invalid release tag")
    version = tag[1:]
    root = Path(directory)
    installers = [
        ("macos-arm64", "macos", f"SimpleMark-{version}-macos-arm64.dmg"),
        ("macos-x64", "macos", f"SimpleMark-{version}-macos-x64.dmg"),
        ("windows-msi", "windows", f"SimpleMark-{version}-windows-x64.msi"),
        ("windows-nsis", "windows", f"SimpleMark-{version}-windows-x64-setup.exe"),
        ("linux", "linux", f"SimpleMark-{version}-linux-x64.AppImage"),
    ]
    verified = []
    for leg, target, name in installers:
        artifact = root / name
        evidence = root / f"platform-smoke-{leg}.json"
        if not artifact.is_file() or not artifact.stat().st_size or not evidence.is_file():
            raise ValueError(f"Missing installer or exact smoke evidence for {leg}")
        body = json.loads(evidence.read_text())
        if body.get("schema") != "simplemark.platform-smoke.v1" or body.get("target") != target:
            raise ValueError(f"Wrong smoke evidence schema or target for {leg}")
        if body.get("commit") != sha:
            raise ValueError(f"Smoke evidence does not identify the released commit for {leg}")
        recorded = body.get("artifact", {})
        digest = hashlib.sha256()
        with artifact.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if recorded.get("name") != name or recorded.get("sha256") != digest.hexdigest():
            raise ValueError(f"Smoke evidence does not identify the exact installer bytes for {leg}")
        verified.append((target, artifact))
    return verified


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["authorize", "verdict", "verify-promotion"])
    a = p.parse_args()
    if a.command == "authorize":
        authorize(os.environ["SOURCE_REF"], os.environ.get("RELEASE_TAG", ""),
                  os.environ.get("VALIDATION_ONLY") == "true",
                  os.environ.get("EMERGENCY") == "true", os.environ.get("REASON", ""))
    elif a.command == "verdict":
        verdict(os.environ["SOURCE_SHA"], os.environ.get("RELEASE_TAG", ""),
                os.environ["RELEASE_RESULT"])
    else:
        for target, artifact in promotion_evidence(os.environ["SOURCE_SHA"], os.environ["RELEASE_TAG"],
                                                  ".artifacts/release"):
            subprocess.run(["node", "scripts/verify-native-artifact.mjs", "--target", target,
                            "--artifact", str(artifact)], check=True)
        print("Every installer has smoke evidence bound to the exact released commit and bytes")
