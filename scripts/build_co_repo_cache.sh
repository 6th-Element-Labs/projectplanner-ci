#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 SOURCE_REPO SCM_PROVIDER REPOSITORY_SLUG EXPECTED_COMMIT OUTPUT_ARCHIVE" >&2
  echo "       $0 --verify ARCHIVE SCM_PROVIDER REPOSITORY_SLUG EXPECTED_COMMIT" >&2
  exit 64
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

validate_identity() {
  local provider="$1"
  local repository="$2"
  local commit="$3"
  [[ "$provider" =~ ^[a-z0-9][a-z0-9._-]{0,31}$ ]] || {
    echo "SCM provider must be a lowercase portable identifier" >&2
    return 64
  }
  [[ "$repository" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] || {
    echo "repository slug must be owner/name" >&2
    return 64
  }
  [[ "$commit" =~ ^[0-9a-f]{40}$ ]] || {
    echo "expected commit must be a full lowercase SHA-1" >&2
    return 64
  }
}

validate_origin_repository() {
  python3 - "$1" "$2" <<'PY'
import re
import sys
from urllib.parse import urlparse

origin, expected = sys.argv[1:]
if re.match(r"^[^/@:]+@[^:]+:", origin):
    path = origin.split(":", 1)[1]
else:
    path = urlparse(origin).path.lstrip("/")
if path.endswith(".git"):
    path = path[:-4]
if path.rstrip("/") != expected:
    raise SystemExit(
        f"cache origin repository mismatch: expected {expected!r}, got {path!r}")
PY
}

verify_archive() {
  local archive="$1"
  local provider="$2"
  local repository="$3"
  local expected_commit="$4"
  local verify_dir

  validate_identity "$provider" "$repository" "$expected_commit"
  [[ -f "$archive" ]] || {
    echo "cache archive does not exist: $archive" >&2
    return 66
  }

  python3 - "$archive" "$provider" "$repository" "$expected_commit" <<'PY'
import json
import pathlib
import sys
import tarfile

archive_path, provider, repository, commit = sys.argv[1:]
with tarfile.open(archive_path, "r:gz") as archive:
    members = archive.getmembers()
    names = [member.name for member in members]
    if any(pathlib.PurePosixPath(name).name.startswith("._") for name in names):
        raise SystemExit("cache archive contains forbidden AppleDouble metadata")
    required = {"cache-manifest.json"}
    if not any(name.startswith("repository.git/") for name in names):
        raise SystemExit("cache archive is missing repository.git")
    if not required.issubset(names):
        raise SystemExit("cache archive is missing cache-manifest.json")
    manifest_file = archive.extractfile("cache-manifest.json")
    manifest = json.load(manifest_file)
expected = {
    "schema": "switchboard.repository_cache.v1",
    "scm_provider": provider,
    "repository": repository,
    "object_sha": commit,
}
for key, value in expected.items():
    if manifest.get(key) != value:
        raise SystemExit(
            f"cache identity mismatch for {key}: expected {value!r}, got {manifest.get(key)!r}")
PY

  verify_dir="$(mktemp -d "${TMPDIR:-/tmp}/switchboard-co-cache-verify.XXXXXX")"
  trap 'rm -rf "$verify_dir"' RETURN
  COPYFILE_DISABLE=1 tar -xzf "$archive" -C "$verify_dir"
  git --git-dir="$verify_dir/repository.git" fsck --full >/dev/null
  git --git-dir="$verify_dir/repository.git" cat-file -e "${expected_commit}^{commit}"
  local origin_url
  origin_url="$(git --git-dir="$verify_dir/repository.git" config --get remote.origin.url || true)"
  [[ -n "$origin_url" ]] || {
    echo "cache archive is missing its canonical origin URL" >&2
    return 65
  }
  validate_origin_repository "$origin_url" "$repository"
  if [[ "$(git --git-dir="$verify_dir/repository.git" config --bool --get remote.origin.mirror || true)" == "true" ]]; then
    echo "cache archive still enables remote.origin.mirror and cannot push task refs" >&2
    return 65
  fi
  rm -rf "$verify_dir"
  trap - RETURN
}

if [[ "${1:-}" == "--verify" ]]; then
  [[ "$#" -eq 5 ]] || usage
  verify_archive "$2" "$3" "$4" "$5"
  printf 'verified archive=%s cache_key=%s/%s/%s sha256=%s\n' \
    "$2" "$3" "$4" "$5" "$(sha256_file "$2")"
  exit 0
fi

[[ "$#" -eq 5 ]] || usage
source_repo="$1"
provider="$2"
repository="$3"
expected_commit="$4"
output_archive="$5"
validate_identity "$provider" "$repository" "$expected_commit"
git -C "$source_repo" cat-file -e "${expected_commit}^{commit}"
source_origin="$(git -C "$source_repo" remote get-url origin 2>/dev/null || true)"
[[ -n "$source_origin" ]] || {
  echo "source repository must have a canonical origin remote" >&2
  exit 65
}
case "$source_origin" in
  https://*|http://*|ssh://*|git://*|git@*:*) ;;
  *) echo "source origin must be a portable network URL, not a local build path" >&2; exit 65 ;;
esac
if [[ "$source_origin" =~ ^https?://[^/]*@ ]]; then
  echo "source origin must not contain embedded credentials" >&2
  exit 65
fi
validate_origin_repository "$source_origin" "$repository"

output_dir="$(cd "$(dirname "$output_archive")" && pwd)"
output_archive="$output_dir/$(basename "$output_archive")"
staging="$(mktemp -d "${TMPDIR:-/tmp}/switchboard-co-cache-build.XXXXXX")"
archive_tmp="$(mktemp "$output_dir/.switchboard-co-cache.XXXXXX")"
cleanup() {
  rm -rf "$staging"
  rm -f "$archive_tmp"
}
trap cleanup EXIT

git clone --mirror --no-hardlinks "$source_repo" "$staging/repository.git" >/dev/null
git --git-dir="$staging/repository.git" remote set-url origin "$source_origin"
git --git-dir="$staging/repository.git" config --unset-all remote.origin.mirror
git --git-dir="$staging/repository.git" repack -a -d >/dev/null
git --git-dir="$staging/repository.git" fsck --full >/dev/null
git --git-dir="$staging/repository.git" cat-file -e "${expected_commit}^{commit}"
python3 - "$staging/cache-manifest.json" "$provider" "$repository" "$expected_commit" <<'PY'
import json
import pathlib
import sys

path, provider, repository, commit = sys.argv[1:]
pathlib.Path(path).write_text(json.dumps({
    "schema": "switchboard.repository_cache.v1",
    "scm_provider": provider,
    "repository": repository,
    "object_sha": commit,
}, sort_keys=True) + "\n", encoding="utf-8")
PY

export COPYFILE_DISABLE=1
tar -czf "$archive_tmp" -C "$staging" cache-manifest.json repository.git
verify_archive "$archive_tmp" "$provider" "$repository" "$expected_commit"
mv "$archive_tmp" "$output_archive"
printf 'built archive=%s cache_key=%s/%s/%s sha256=%s\n' \
  "$output_archive" "$provider" "$repository" "$expected_commit" "$(sha256_file "$output_archive")"
