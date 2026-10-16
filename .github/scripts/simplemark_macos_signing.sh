#!/usr/bin/env bash
# Used only by explicitly requested macOS builds in the protected signing environment.
set -euo pipefail

die() { echo "simplemark-signing: $*" >&2; exit 1; }
required() {
  local name
  for name in "$@"; do
    [[ -n "${!name:-}" ]] || die "missing $name"
  done
}

required RUNNER_TEMP
keychain="$RUNNER_TEMP/simplemark-build.keychain-db"
certificate="$RUNNER_TEMP/simplemark-developer-id.p12"
notary_key="$RUNNER_TEMP/simplemark-notary.p8"

case "${1:-}" in
  prepare)
    required APPLE_CERTIFICATE APPLE_CERTIFICATE_PASSWORD APPLE_SIGNING_IDENTITY \
      KEYCHAIN_PASSWORD APPLE_API_ISSUER APPLE_API_KEY APPLE_API_KEY_P8 APPLE_TEAM_ID GITHUB_ENV
    [[ "$APPLE_SIGNING_IDENTITY" == 'Developer ID Application: '* ]] \
      || die 'a Developer ID Application identity is required'
    [[ "$APPLE_TEAM_ID" =~ ^[A-Z0-9]{10}$ ]] || die 'invalid Apple team ID'
    umask 077
    printf '%s' "$APPLE_CERTIFICATE" | base64 --decode > "$certificate"
    printf '%s' "$APPLE_API_KEY_P8" > "$notary_key"
    security create-keychain -p "$KEYCHAIN_PASSWORD" "$keychain"
    security unlock-keychain -p "$KEYCHAIN_PASSWORD" "$keychain"
    security set-keychain-settings -lut 7200 "$keychain"
    security import "$certificate" -k "$keychain" -P "$APPLE_CERTIFICATE_PASSWORD" \
      -T /usr/bin/codesign > /dev/null
    security set-key-partition-list -S apple-tool:,apple:,codesign: -s \
      -k "$KEYCHAIN_PASSWORD" "$keychain" > /dev/null
    security list-keychains -d user -s "$keychain"
    security find-identity -v -p codesigning "$keychain" \
      | grep -F -- "$APPLE_SIGNING_IDENTITY" > /dev/null \
      || die 'the imported certificate has no matching valid signing identity'
    printf 'APPLE_API_KEY_PATH=%s\n' "$notary_key" >> "$GITHUB_ENV"
    echo 'simplemark-signing: Developer ID and API notarization material prepared'
    ;;
  verify)
    required APPLE_SIGNING_IDENTITY APPLE_TEAM_ID APPLE_API_ISSUER APPLE_API_KEY
    target="${2:-}"
    [[ "$target" == aarch64-apple-darwin || "$target" == x86_64-apple-darwin ]] \
      || die 'unsupported macOS build target'
    bundle="src-tauri/target/$target/release/bundle"
    app="$bundle/macos/SimpleMark.app"
    [[ -d "$app" ]] || die 'the signed SimpleMark.app is missing'
    shopt -s nullglob
    dmgs=("$bundle"/dmg/*.dmg)
    [[ ${#dmgs[@]} -eq 1 ]] || die 'expected exactly one DMG'
    dmg="${dmgs[0]}"
    codesign --verify --deep --strict "$app"
    identity="$(codesign -dv --verbose=4 "$app" 2>&1)"
    grep -Fx -- "TeamIdentifier=$APPLE_TEAM_ID" <<< "$identity" > /dev/null \
      || die 'the app was signed by an unexpected Apple team'
    grep -F -- 'Authority=Developer ID Application: ' <<< "$identity" > /dev/null \
      || die 'the app does not have a Developer ID Application signature'
    xcrun stapler validate "$app"
    spctl --assess --type execute --verbose=2 "$app"
    # Tauri notarizes the app. The outer installer needs its own accepted ticket.
    codesign --force --sign "$APPLE_SIGNING_IDENTITY" --timestamp "$dmg"
    codesign --verify --strict "$dmg"
    xcrun notarytool submit "$dmg" --key "$notary_key" --key-id "$APPLE_API_KEY" \
      --issuer "$APPLE_API_ISSUER" --wait --output-format json > "$RUNNER_TEMP/simplemark-notary.json"
    python3 - "$RUNNER_TEMP/simplemark-notary.json" <<'PY'
import json
import sys
with open(sys.argv[1]) as stream:
    result = json.load(stream)
if result.get("status") != "Accepted":
    raise SystemExit("simplemark-signing: outer DMG notarization was not accepted")
PY
    xcrun stapler staple "$dmg"
    xcrun stapler validate "$dmg"
    spctl --assess --type open --context context:primary-signature --verbose=2 "$dmg"
    echo 'simplemark-signing: app and final DMG passed Apple signature and notarization checks'
    ;;
  cleanup)
    security delete-keychain "$keychain" 2>/dev/null || true
    rm -f "$certificate" "$notary_key" "$RUNNER_TEMP/simplemark-notary.json"
    ;;
  *) die 'use prepare, verify <target>, or cleanup' ;;
esac
