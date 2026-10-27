#!/usr/bin/env bash
# Use the same ephemeral gh credential helper as the proven verification lane.
# Never persist the App token or print private source. Authorization is upstream.
set -euo pipefail
: "${GH_TOKEN:?}" "${SOURCE_SHA:?}" "${CANONICAL_REPO:?}"
[[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] || exit 2
[[ "$CANONICAL_REPO" == 6th-Element-Labs/ActionEngine ]] || exit 2
test ! -e audit-source
git init -q audit-source
git -C audit-source remote add origin "https://github.com/$CANONICAL_REPO.git"
for attempt in 1 2 3; do
  if git -C audit-source \
      -c credential.https://github.com.helper='!gh auth git-credential' \
      fetch --no-tags --no-recurse-submodules --depth=1 origin "$SOURCE_SHA"; then
    break
  fi
  test "$attempt" -lt 3 || exit 1
  sleep "$((attempt * 10))"
done
git -C audit-source checkout --detach "$SOURCE_SHA"
test "$(git -C audit-source rev-parse HEAD)" = "$SOURCE_SHA"
test -f audit-source/.github/scripts/ssm_release_audit.sh
