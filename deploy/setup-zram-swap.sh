#!/usr/bin/env bash
# PERF-3: route swap through zram (compressed RAM) instead of disk.
#
# Disk swap on the 1 GB t4g.micro turns a memory spike into ~100000x slower
# page faults for the interactive tier. zram keeps swap in RAM with zstd
# compression — fast enough that batch pressure does not jam web/MCP/gateway.
#
# Idempotent: safe to re-run after reboot or deploy. Persists via
# /etc/systemd/zram-generator.conf + systemd generator units.
set -euo pipefail

ZRAM_CONF="/etc/systemd/zram-generator.conf"
FSTAB="/etc/fstab"

need_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "run as root (sudo bash $0)" >&2
    exit 1
  fi
}

install_generator() {
  if ! command -v systemd-zram-setup >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y systemd-zram-generator
  fi
}

write_zram_conf() {
  local tmp
  tmp="$(mktemp)"
  cat >"$tmp" <<'EOF'
# PERF-3 — compressed RAM swap for the small Switchboard box.
# Half of physical RAM, capped at 512M on the 1 GB t4g.micro.
[zram0]
zram-size = min(ram / 2, 512M)
compression-algorithm = zstd
swap = on
EOF
  if [[ -f "$ZRAM_CONF" ]] && cmp -s "$tmp" "$ZRAM_CONF"; then
    rm -f "$tmp"
    echo "zram-generator config already current: $ZRAM_CONF"
    return 0
  fi
  install -m 0644 "$tmp" "$ZRAM_CONF"
  rm -f "$tmp"
  echo "installed $ZRAM_CONF"
}

disable_disk_swap() {
  if [[ -f "$FSTAB" ]]; then
    if grep -E '^[^#[:space:]].*[[:space:]]swap[[:space:]]' "$FSTAB" | grep -qv 'PERF-3-disabled'; then
      cp -a "$FSTAB" "${FSTAB}.bak.perf3.$(date +%Y%m%d%H%M%S)"
      sed -i -E 's/^([^#[:space:]].*[[:space:]]swap[[:space:]].*)$/# PERF-3-disabled: \1/' "$FSTAB"
      echo "commented disk swap entries in $FSTAB (backup kept alongside)"
    fi
  fi
  while read -r _ type _; do
    [[ "$type" == "partition" || "$type" == "file" ]] || continue
    swapoff -a 2>/dev/null || true
    break
  done < <(swapon --show=NAME,TYPE 2>/dev/null | tail -n +2 || true)
}

activate_zram() {
  systemctl daemon-reload
  systemctl restart systemd-zram-setup@zram0.service 2>/dev/null \
    || systemctl start systemd-zram-setup@zram0.service 2>/dev/null \
    || true
  sleep 1
  if ! swapon --show | grep -q zram; then
    echo "WARN: zram swap not visible yet — check: systemctl status systemd-zram-setup@zram0" >&2
    return 1
  fi
  echo "active swap devices:"
  swapon --show
}

main() {
  need_root
  install_generator
  write_zram_conf
  disable_disk_swap
  activate_zram
  echo "zram swap ready. verify with: swapon --show && free -h"
}

main "$@"
