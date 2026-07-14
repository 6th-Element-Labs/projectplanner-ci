#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 SOURCE_REPO EXPECTED_COMMIT OUTPUT_ARCHIVE" >&2
  echo "       $0 --verify ARCHIVE EXPECTED_COMMIT" >&2
  exit 64
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

verify_archive() {
  local archive="$1"
  local expected_commit="$2"
  local verify_dir

  [[ "$expected_commit" =~ ^[0-9a-f]{40}$ ]] || {
    echo "expected commit must be a full lowercase SHA-1" >&2
    return 64
  }
  [[ -f "$archive" ]] || {
    echo "cache archive does not exist: $archive" >&2
    return 66
  }

  python3 - "$archive" <<'PY'
import pathlib
import sys
import tarfile

with tarfile.open(sys.argv[1], "r:gz") as archive:
    names = [member.name for member in archive.getmembers()]
if any(pathlib.PurePosixPath(name).name.startswith("._") for name in names):
    raise SystemExit("cache archive contains forbidden AppleDouble metadata")
if not any(name.startswith("projectplanner.git/") for name in names):
    raise SystemExit("cache archive is missing projectplanner.git")
PY

  verify_dir="$(mktemp -d "${TMPDIR:-/tmp}/switchboard-co-cache-verify.XXXXXX")"
  trap 'rm -rf "$verify_dir"' RETURN
  COPYFILE_DISABLE=1 tar -xzf "$archive" -C "$verify_dir"
  git --git-dir="$verify_dir/projectplanner.git" fsck --full >/dev/null
  git --git-dir="$verify_dir/projectplanner.git" cat-file -e "${expected_commit}^{commit}"
  rm -rf "$verify_dir"
  trap - RETURN
}

if [[ "${1:-}" == "--verify" ]]; then
  [[ "$#" -eq 3 ]] || usage
  verify_archive "$2" "$3"
  printf 'verified archive=%s commit=%s sha256=%s\n' \
    "$2" "$3" "$(sha256_file "$2")"
  exit 0
fi

[[ "$#" -eq 3 ]] || usage
source_repo="$1"
expected_commit="$2"
output_archive="$3"
[[ "$expected_commit" =~ ^[0-9a-f]{40}$ ]] || usage
git -C "$source_repo" cat-file -e "${expected_commit}^{commit}"

output_dir="$(cd "$(dirname "$output_archive")" && pwd)"
output_archive="$output_dir/$(basename "$output_archive")"
staging="$(mktemp -d "${TMPDIR:-/tmp}/switchboard-co-cache-build.XXXXXX")"
archive_tmp="$(mktemp "$output_dir/.switchboard-co-cache.XXXXXX")"
cleanup() {
  rm -rf "$staging"
  rm -f "$archive_tmp"
}
trap cleanup EXIT

git clone --mirror --no-hardlinks "$source_repo" "$staging/projectplanner.git" >/dev/null
git --git-dir="$staging/projectplanner.git" repack -a -d >/dev/null
git --git-dir="$staging/projectplanner.git" fsck --full >/dev/null
git --git-dir="$staging/projectplanner.git" cat-file -e "${expected_commit}^{commit}"

export COPYFILE_DISABLE=1
tar -czf "$archive_tmp" -C "$staging" projectplanner.git
verify_archive "$archive_tmp" "$expected_commit"
mv "$archive_tmp" "$output_archive"
printf 'built archive=%s commit=%s sha256=%s\n' \
  "$output_archive" "$expected_commit" "$(sha256_file "$output_archive")"
