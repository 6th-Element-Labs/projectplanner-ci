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
# Re-assert data ownership before touching the coordination checkout. On the
# second and later deploys this checkout is already service-owned; running Git
# as root then trips Git's dubious-ownership protection. Keep every checkout
# operation under the same identity that owns and uses the checkout instead.
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$DATA_ROOT"
chmod 750 "$DATA_ROOT"
# CI-12 needs a writable coordination checkout because ProtectSystem=strict keeps
# /opt/projectplanner read-only. Seed from the already-provisioned code clone so
# setup needs no second private fetch; runtime fetches exact PR refs using GH_TOKEN
# (promoted from the normal Switchboard token variables by external_ci_mirror).
if [ ! -d "$CI_SOURCE_ROOT/.git" ]; then
  runuser --user "$SERVICE_USER" -- git clone --no-checkout "$CODE_ROOT" "$CI_SOURCE_ROOT"
fi
runuser --user "$SERVICE_USER" -- \
  git -C "$CI_SOURCE_ROOT" remote set-url origin "$CI_SOURCE_REMOTE"
runuser --user "$SERVICE_USER" -- \
  git -C "$CI_SOURCE_ROOT" config credential.helper '!gh auth git-credential'

# 3. Code tree + venv: root-owned and not group/other-writable. The runtime
#    reads/executes it but cannot rewrite it (defense in depth behind ProtectSystem=strict).
chown -R root:root "$CODE_ROOT"
chmod -R go-w "$CODE_ROOT"

# 4. Secrets: .env holds credentials. Root-owned and readable only by the service
#    group (640) — not world. systemd reads EnvironmentFile= as root before dropping
#    privileges, and operator jobs run as `sudo -u $SERVICE_USER` can still read it.
#    Managed worktrees + CI mutate the service-owned clone under DATA_ROOT — never CODE_ROOT.
if [ -f "$CODE_ROOT/.env" ]; then
  _ensure_env_kv() {
    local key="$1" value="$2"
    if grep -q "^${key}=" "$CODE_ROOT/.env"; then
      sed -i "s|^${key}=.*|${key}=${value}|" "$CODE_ROOT/.env"
    else
      printf '%s=%s\n' "$key" "$value" >>"$CODE_ROOT/.env"
    fi
  }
  _ensure_env_kv "PM_REPO_PATH" "$CI_SOURCE_ROOT"
  _ensure_env_kv "PM_WORKSPACE_ROOT" "$DATA_ROOT/workspaces"
  if ! grep -q "^GH_TOKEN=" "$CODE_ROOT/.env"; then
    if grep -q "^SWITCHBOARD_CI_GITHUB_TOKEN=" "$CODE_ROOT/.env"; then
      awk -F= '/^SWITCHBOARD_CI_GITHUB_TOKEN=/{print "GH_TOKEN=" substr($0, index($0,"=")+1); exit}' \
        "$CODE_ROOT/.env" >>"$CODE_ROOT/.env"
    elif grep -q "^PM_GITHUB_TOKEN=" "$CODE_ROOT/.env"; then
      awk -F= '/^PM_GITHUB_TOKEN=/{print "GH_TOKEN=" substr($0, index($0,"=")+1); exit}' \
        "$CODE_ROOT/.env" >>"$CODE_ROOT/.env"
    fi
  fi
  chown "root:$SERVICE_GROUP" "$CODE_ROOT/.env"
  chmod 640 "$CODE_ROOT/.env"
fi

# 5. Strip accidental sandbox widenings that make CODE_ROOT writable. HARDEN-55
#    forbids mutating /opt from the runtime; CI/managed sessions use DATA_ROOT.
_dropin_removed=0
shopt -s nullglob
for dropin in /etc/systemd/system/*.service.d/*.conf; do
  if grep -E "^ReadWritePaths=" "$dropin" | grep -Fq "$CODE_ROOT"; then
    echo "removing CODE_ROOT write drop-in: $dropin"
    rm -f "$dropin"
    _dropin_removed=1
  fi
done
shopt -u nullglob
if [ "$_dropin_removed" -eq 1 ]; then
  systemctl daemon-reload || true
fi

echo "HARDEN-55 least-privilege applied:"
echo "  service account : $SERVICE_USER (nologin, home=$DATA_ROOT)"
echo "  code tree       : root-owned, not group/other-writable ($CODE_ROOT)"
echo "  writable data   : $DATA_ROOT"
echo "  CI source clone : $CI_SOURCE_ROOT -> $CI_SOURCE_REMOTE"
echo "  managed source  : PM_REPO_PATH=$CI_SOURCE_ROOT"
echo "  workspaces      : PM_WORKSPACE_ROOT=$DATA_ROOT/workspaces"
echo ""
echo "Restart units so User=$SERVICE_USER + the systemd sandbox take effect:"
echo "  sudo systemctl restart projectplanner projectplanner-mcp projectplanner-gateway projectplanner-agent-host"
