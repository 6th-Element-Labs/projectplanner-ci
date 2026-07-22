#!/usr/bin/env python3
"""HARDEN-55 — static checks for runtime least-privilege: dedicated service account,
read-only code tree, and declarative systemd sandboxing on every projectplanner unit."""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
passed = failed = 0

# Every long-running + oneshot unit that executes code from the deployed tree.
UNITS = (
    "deploy/projectplanner.service",
    "deploy/projectplanner-mcp.service",
    "deploy/projectplanner-gateway.service",
    "deploy/projectplanner-agent-host.service",
    "deploy/projectplanner-narrate.service",
    "deploy/projectplanner-reconcile.service",
    "deploy/projectplanner-monitors.service",
    "deploy/projectplanner-inbox.service",
    "deploy/projectplanner-summarize.service",
    "deploy/projectplanner-digest.service",
    "deploy/projectplanner-claim-gate.service",
    "deploy/projectplanner-backup.service",
)

# Directives every unit must declare (task-mandated sandbox + confinement).
REQUIRED_DIRECTIVES = (
    "User=projectplanner",
    "Group=projectplanner",
    "NoNewPrivileges=yes",
    "PrivateTmp=yes",
    "ProtectSystem=strict",
    "ProtectHome=yes",
    "ReadWritePaths=/var/lib/projectplanner",
)


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def read(rel_path):
    with open(os.path.join(ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


for path in UNITS:
    text = read(path)
    for directive in REQUIRED_DIRECTIVES:
        ok(directive in text, f"{path} declares {directive}")
    # RestrictAddressFamilies must be present and confine to inet/unix/netlink.
    ok("RestrictAddressFamilies=" in text
       and "AF_INET" in text and "AF_UNIX" in text,
       f"{path} restricts socket address families")
    # The runtime must never run as the general login account any more.
    ok("User=ubuntu" not in text and "Group=ubuntu" not in text,
       f"{path} no longer runs as the ubuntu login account")

# reconcile needs a writable RuntimeDirectory for its flock under ProtectSystem=strict.
reconcile = read("deploy/projectplanner-reconcile.service")
ok("RuntimeDirectory=projectplanner" in reconcile,
   "reconcile keeps a writable RuntimeDirectory for its flock")

# The imperative half: the provisioning helper that creates the account + fixes ownership.
alp = read("deploy/apply-least-privilege.sh")
ok("useradd" in alp and "groupadd" in alp, "apply-least-privilege creates the service account")
ok("nologin" in alp, "service account has no login shell")
ok("chown -R root:root" in alp, "apply-least-privilege makes the code tree root-owned")
ok("chmod -R go-w" in alp, "apply-least-privilege makes the code tree non-writable to the runtime")
ok("PM_REPO_PATH" in alp and "PM_WORKSPACE_ROOT" in alp,
   "apply-least-privilege points managed sessions at the service-owned clone")
ok("removing CODE_ROOT write drop-in" in alp,
   "apply-least-privilege strips accidental /opt ReadWritePaths drop-ins")

# Provisioning + redeploy wire the helper in and drop the old ubuntu chown.
provision = read("deploy/PROVISION.md")
ok("apply-least-privilege.sh" in provision, "PROVISION.md runs the least-privilege helper")
ok("chown -R ubuntu /opt/projectplanner" not in provision,
   "PROVISION.md no longer chowns the code tree to ubuntu")
ok("HARDEN-55" in provision, "PROVISION.md documents the least-privilege posture")

redeploy = read("deploy/redeploy.sh")
ok("apply-least-privilege.sh" in redeploy, "redeploy re-asserts least-privilege on every deploy")
ok("sudo git pull" in redeploy, "redeploy pulls the root-owned code tree as root")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
