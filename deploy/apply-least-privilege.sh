#!/usr/bin/env bash
# HARDEN-55: runtime least-privilege. Provision the dedicated non-login service
# account, make the code tree + venv root-owned/read-only, and give the service
# account ownership of ONLY its data dir. This is the imperative half of the
# fix; the declarative half lives in the checked-in unit files (User=, ProtectSystem,
# ReadWritePaths, …). Run once on a fresh box and again on redeploy — it is
# idempotent, so re-running only re-asserts ownership/permissions.
#
# Why: provisioning used to `chown -R ubuntu /opt/projectplanner` and run both
# long-lived services as the general `ubuntu` login account, so the runtime could
# rewrite its own code. After this the runtime can read/execute its code but never
# write it, and it can only write /var/lib/projectplanner.
set -euo pipefail

SERVICE_USER="${PM_SERVICE_USER:-projectplanner}"
SERVICE_GROUP="${PM_SERVICE_GROUP:-$SERVICE_USER}"
CODE_ROOT="${PM_CODE_ROOT:-/opt/projectplanner}"
DATA_ROOT="${PM_DATA_ROOT:-/var/lib/projectplanner}"
CI_SOURCE_ROOT="${SWITCHBOARD_CI_SOURCE_PATH:-$DATA_ROOT/ci-source}"
CI_SOURCE_REMOTE="${SWITCHBOARD_CI_SOURCE_REMOTE:-https://github.com/6th-Element-Labs/projectplanner.git}"

if [ "$(id -u)" != "0" ]; then
  echo "apply-least-privilege.sh must run as root (use sudo)." >&2
  exit 1
fi

# 1. Dedicated system group + user: no login shell, home on the data dir (NOT
#    /home, which ProtectHome=yes hides from the sandboxed service).
getent group "$SERVICE_GROUP" >/dev/null || groupadd --system "$SERVICE_GROUP"
getent passwd "$SERVICE_USER" >/dev/null || \
  useradd --system --gid "$SERVICE_GROUP" --home-dir "$DATA_ROOT" \
          --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"

# 2. Data dir is the ONLY tree the runtime may write (matches ReadWritePaths=).
mkdir -p "$DATA_ROOT" "$DATA_ROOT/runner" "$DATA_ROOT/repo-hygiene-archive" \
         "$DATA_ROOT/workspaces"
# CI-12 needs a writable coordination checkout because ProtectSystem=strict keeps
# /opt/projectplanner read-only. Seed from the already-provisioned code clone so
# setup needs no second private fetch; runtime fetches exact PR refs using GH_TOKEN
# (promoted from the normal Switchboard token variables by external_ci_mirror).
if [ ! -d "$CI_SOURCE_ROOT/.git" ]; then
  git clone --no-checkout "$CODE_ROOT" "$CI_SOURCE_ROOT"
fi
git -C "$CI_SOURCE_ROOT" remote set-url origin "$CI_SOURCE_REMOTE"
git -C "$CI_SOURCE_ROOT" config credential.helper '!gh auth git-credential'
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$DATA_ROOT"
chmod 750 "$DATA_ROOT"

# 3. Code tree + venv: root-owned and not group/other-writable. The runtime
#    reads/executes it but cannot rewrite it (defense in depth behind ProtectSystem=strict).
chown -R root:root "$CODE_ROOT"
chmod -R go-w "$CODE_ROOT"

# 4. Secrets: .env holds credentials. Root-owned and readable only by the service
#    group (640) — not world. systemd reads EnvironmentFile= as root before dropping
#    privileges, and operator jobs run as `sudo -u $SERVICE_USER` can still read it.
if [ -f "$CODE_ROOT/.env" ]; then
  chown "root:$SERVICE_GROUP" "$CODE_ROOT/.env"
  chmod 640 "$CODE_ROOT/.env"
fi

echo "HARDEN-55 least-privilege applied:"
echo "  service account : $SERVICE_USER (nologin, home=$DATA_ROOT)"
echo "  code tree       : root-owned, not group/other-writable ($CODE_ROOT)"
echo "  writable data   : $DATA_ROOT"
echo "  CI source clone : $CI_SOURCE_ROOT -> $CI_SOURCE_REMOTE"
echo ""
echo "Restart units so User=$SERVICE_USER + the systemd sandbox take effect:"
echo "  sudo systemctl restart projectplanner projectplanner-mcp projectplanner-gateway projectplanner-agent-host"
