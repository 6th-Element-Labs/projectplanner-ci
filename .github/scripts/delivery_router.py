#!/usr/bin/env python3
"""Route canonical revisions to standard runners in this public repository."""
import json
import os
import subprocess

PUBLIC_REPO = "6th-Element-Labs/projectplanner-ci"
ACTIONENGINE = "6th-Element-Labs/ActionEngine"
SIMPLEMARK = "6th-Element-Labs/simplemark"


def api(path, token, method="GET", body=None):
    command = ["gh", "api", "--method", method, path]
    if body is not None:
        command += ["--input", "-"]
    result = subprocess.run(command, input=json.dumps(body) if body is not None else None,
                            text=True, capture_output=True, check=True,
                            env={**os.environ, "GH_TOKEN": token})
    return json.loads(result.stdout) if result.stdout.strip() else None


def latest_state(statuses, context):
    return next((s["state"] for s in statuses if s["context"] == context), None)


def dispatch(workflow, inputs, token):
    api(f"repos/{PUBLIC_REPO}/actions/workflows/{workflow}/dispatches", token, "POST",
        {"ref": "master", "inputs": inputs})


def route(source_token, public_token):
    for branch, environment in (("totalPreProd", "preprod"), ("main", "production")):
        if environment == "preprod":
            flag = api(f"repos/{ACTIONENGINE}/actions/variables/DEPLOY_PREPROD_AUTO", source_token)
            if flag["value"] != "on":
                print("Preprod automatic deployment is paused by DEPLOY_PREPROD_AUTO")
                continue
        sha = api(f"repos/{ACTIONENGINE}/commits/{branch}", source_token)["sha"]
        statuses = api(f"repos/{ACTIONENGINE}/commits/{sha}/status", source_token)["statuses"]
        context = f"ActionEngine Deploy / public {environment}"
        if latest_state(statuses, "ActionEngine CI / public mirror gate") != "success":
            print(f"{branch}: waiting for exact-SHA public verification")
            continue
        # A failure requires an explicit manual retry. Never loop deployments.
        if latest_state(statuses, context) is not None:
            print(f"{branch}: public deployment already recorded")
            continue
        dispatch("actionengine-preprod-deploy.yml",
                 {"source_sha": sha, "environment": environment}, public_token)
        api(f"repos/{ACTIONENGINE}/statuses/{sha}", source_token, "POST",
            {"state": "pending", "context": context,
             "description": f"{environment}: queued on free public runners"})
        print(f"Queued {environment} deployment for {sha}")

    # Tags are the release request; no private Actions runner is needed to relay it.
    page = 1
    while True:
        tags = api(f"repos/{SIMPLEMARK}/tags?per_page=100&page={page}", source_token)
        for tag in tags:
            if not tag["name"].startswith("v"):
                continue
            sha = tag["commit"]["sha"]
            context = "SimpleMark Release / public " + tag["name"]
            statuses = api(f"repos/{SIMPLEMARK}/commits/{sha}/status", source_token)["statuses"]
            if latest_state(statuses, context) is not None:
                continue
            # An existing draft or published release is never replaced by the router.
            releases = api(f"repos/{SIMPLEMARK}/releases?per_page=100", source_token)
            if any(r["tag_name"] == tag["name"] for r in releases):
                continue
            dispatch("simplemark-release.yml", {"source_ref": tag["name"],
                     "tag": tag["name"], "validation_only": "false"}, public_token)
            api(f"repos/{SIMPLEMARK}/statuses/{sha}", source_token, "POST",
                {"state": "pending", "context": context,
                 "description": "Release queued on free public runners"})
            print(f"Queued SimpleMark {tag['name']}")
        if len(tags) < 100:
            break
        page += 1


if __name__ == "__main__":
    if (os.environ.get("GITHUB_REPOSITORY") != PUBLIC_REPO
            or os.environ.get("GITHUB_REF") != "refs/heads/master"):
        raise SystemExit("Delivery routing is restricted to the trusted public repository")
    route(os.environ["SOURCE_TOKEN"], os.environ["PUBLIC_TOKEN"])
