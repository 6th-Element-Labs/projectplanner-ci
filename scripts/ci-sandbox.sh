#!/usr/bin/env bash
# Public CI sandbox helpers for projectplanner.
#
# Push feature branches to 6th-Element-Labs/projectplanner-ci (full actual tree, all
# workflows) so GitHub Actions minutes stay on the public sandbox instead of a
# private projectplanner origin. The sandbox is deliberately unscrubbed so CI
# tests the exact tree that will land on the canonical repo. After CI is green,
# open/merge the PR on the canonical repo and delete the sandbox
# branch with: scripts/ci-sandbox.sh refresh-main && scripts/ci-sandbox.sh delete <branch>.
#
# Requires: git, gh (authenticated), jq, column, network.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Public CI sandbox for this project, under the same org as the canonical repo.
# Public repos get free/unlimited GitHub Actions minutes, so CI runs here and
# never spends the org's private-repo Actions budget. Canonical stays the private
# org repo (`CANONICAL_REPO`), which remains the only code-truth / Done authority.
CI_REPO="${CI_REPO:-6th-Element-Labs/projectplanner-ci}"
CI_REMOTE="${CI_REMOTE:-ci}"
CI_REMOTE_URL="${CI_REMOTE_URL:-https://github.com/${CI_REPO}.git}"
CANONICAL_REPO="${CANONICAL_REPO:-6th-Element-Labs/projectplanner}"
ORIGIN_REMOTE="${ORIGIN_REMOTE:-origin}"
WAIT_TIMEOUT_SEC="${WAIT_TIMEOUT_SEC:-7200}"
POLL_INTERVAL_SEC="${POLL_INTERVAL_SEC:-20}"
MAIN_REF="${MAIN_REF:-origin/master}"
SANDBOX_WORKFLOWS="${SANDBOX_WORKFLOWS:-backend-tests.yml}"
STATUS_CONTEXT="${STATUS_CONTEXT:-projectplanner-ci/full-suite}"

die() {
  echo "ci-sandbox: $*" >&2
  exit 1
}

need_tools() {
  command -v git >/dev/null || die "git is required"
  command -v gh >/dev/null || die "GitHub CLI gh is required (https://cli.github.com/)"
  command -v jq >/dev/null || die "jq is required"
  command -v column >/dev/null || die "column is required"
}

repo_root() {
  git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "run from the projectplanner git worktree"
}

remote_url() {
  git -C "$ROOT" remote get-url "$CI_REMOTE" 2>/dev/null || true
}

ensure_remote() {
  if [ -n "$(remote_url)" ]; then
    return 0
  fi
  echo "ci-sandbox: adding git remote '$CI_REMOTE' -> $CI_REMOTE_URL"
  git -C "$ROOT" remote add "$CI_REMOTE" "$CI_REMOTE_URL"
}

ensure_repo() {
  if gh repo view "$CI_REPO" --json nameWithOwner >/dev/null 2>&1; then
    return 0
  fi
  echo "ci-sandbox: creating public repo $CI_REPO"
  if gh repo create "$CI_REPO" \
    --public \
    --description "Public CI sandbox for projectplanner — full tree, all GitHub Actions workflows" 2>/tmp/ci-sandbox-create-repo.$$; then
    rm -f /tmp/ci-sandbox-create-repo.$$
    return 0
  fi
  cat /tmp/ci-sandbox-create-repo.$$ >&2
  rm -f /tmp/ci-sandbox-create-repo.$$
  cat >&2 <<EOF
ci-sandbox: could not create $CI_REPO automatically (token may lack repo-create scope).
Create it once as the repo owner, then re-run setup:

  gh repo create $CI_REPO --public \\
    --description "Public CI sandbox for projectplanner — full tree, all GitHub Actions workflows"

Then:

  scripts/ci-sandbox.sh setup
  scripts/ci-sandbox.sh refresh-main
EOF
  die "missing CI sandbox repo $CI_REPO"
}

current_branch() {
  git -C "$ROOT" branch --show-current
}

resolve_branch() {
  local branch="${1:-}"
  if [ -z "$branch" ]; then
    branch="$(current_branch)"
  fi
  [ -n "$branch" ] || die "could not determine branch; pass one explicitly"
  if [ "$branch" = "HEAD" ]; then
    die "detached HEAD; checkout a branch first"
  fi
  printf '%s' "$branch"
}

usage() {
  cat <<EOF
Usage: scripts/ci-sandbox.sh <command> [options] [branch]

Commands:
  setup                 Create $CI_REPO (if missing) and add git remote '$CI_REMOTE'
  push [--no-wait]      Push <branch> (default: current), dispatch workflows, wait for dispatched Actions
  wait                  Wait for in-progress Actions on <branch> (default: current)
  status                Print recent Actions conclusions for <branch> (default: current)
  prove                 Stamp canonical repo status after exact SHA passed projectplanner-ci
  doctor                Verify repo/remotes/workflows/baseline/branch wiring
  protect-main          Require the projectplanner-ci status before main can move
  delete                Delete <branch> from the CI sandbox remote
  sync-main             Push local main to the CI sandbox (refresh baseline after merges)
  refresh-main          Fetch canonical main, then sync it to the CI sandbox
  open-pr               Push/wait on sandbox, then open a projectplanner PR for <branch>

Environment:
  CI_REPO               Sandbox repo (default: $CI_REPO)
  CI_REMOTE             Git remote name (default: $CI_REMOTE)
  CI_REMOTE_URL         Sandbox clone URL (default: derived from CI_REPO)
  CANONICAL_REPO        projectplanner PR target (default: $CANONICAL_REPO)
  ORIGIN_REMOTE         Canonical git remote name (default: $ORIGIN_REMOTE)
  WAIT_TIMEOUT_SEC      Max wait for Actions (default: $WAIT_TIMEOUT_SEC)
  POLL_INTERVAL_SEC     Poll interval while waiting (default: $POLL_INTERVAL_SEC)
  MAIN_REF              Ref to seed sandbox main from (default: $MAIN_REF)
  SANDBOX_WORKFLOWS     Space-separated workflow files to dispatch after push
  SANDBOX_WAIT_EVENT    Optional event filter for manual wait/status, e.g. workflow_dispatch
  STATUS_CONTEXT        Canonical required status context (default: $STATUS_CONTEXT)

Typical agent loop:
  scripts/ci-sandbox.sh setup
  scripts/ci-sandbox.sh doctor
  git checkout -b claude/MY-TASK-slug
  # ... edit, commit ...
  scripts/ci-sandbox.sh open-pr claude/MY-TASK-slug
  # after merge on projectplanner:
  scripts/ci-sandbox.sh refresh-main
  scripts/ci-sandbox.sh delete claude/MY-TASK-slug

See docs/CI-SANDBOX.md for Switchboard + private-origin notes.
EOF
}

cmd_setup() {
  need_tools
  repo_root
  ensure_repo
  ensure_remote
  echo "ci-sandbox: ready — remote '$CI_REMOTE' -> $(remote_url)"
  echo "ci-sandbox: next: scripts/ci-sandbox.sh refresh-main   # once, to seed main"
}

cmd_push() {
  local wait=1
  local dispatch=1
  local branch=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --no-wait) wait=0; shift ;;
      --no-dispatch) dispatch=0; shift ;;
      -h|--help) usage; exit 0 ;;
      *) branch="$(resolve_branch "$1")"; shift ;;
    esac
  done
  branch="$(resolve_branch "$branch")"

  need_tools
  repo_root
  ensure_repo
  ensure_remote

  local sha
  sha="$(git -C "$ROOT" rev-parse "$branch")"
  echo "ci-sandbox: pushing $branch @ ${sha:0:12} -> $CI_REPO"
  git -C "$ROOT" push -u "$CI_REMOTE" "refs/heads/${branch}:refs/heads/${branch}"

  local dispatch_since=""
  if [ "$dispatch" = 1 ]; then
    dispatch_since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    dispatch_workflows "$branch"
  fi

  if [ "$wait" = 1 ]; then
    if [ "$dispatch" = 1 ]; then
      wait_for_runs "$branch" "workflow_dispatch" "$dispatch_since" "$(workflow_count)"
    else
      wait_for_runs "$branch" "${SANDBOX_WAIT_EVENT:-}" "" 1
    fi
    maybe_stamp_existing_canonical_branch "$branch"
  else
    echo "ci-sandbox: pushed; check https://github.com/${CI_REPO}/actions?query=branch%3A${branch}"
  fi
}

workflow_count() {
  set -- $SANDBOX_WORKFLOWS
  printf '%s' "$#"
}

actions_url() {
  local branch="$1"
  local encoded="${branch//\//%2F}"
  printf 'https://github.com/%s/actions?query=branch%%3A%s' "$CI_REPO" "$encoded"
}

dispatch_workflows() {
  local branch="$1"
  local workflow
  echo "ci-sandbox: dispatching workflows on $CI_REPO@$branch"
  for workflow in $SANDBOX_WORKFLOWS; do
    echo "ci-sandbox: gh workflow run $workflow --ref $branch"
    gh workflow run "$workflow" --repo "$CI_REPO" --ref "$branch"
  done
}

matching_runs_for_sha_json() {
  local branch="$1"
  local sha="$2"
  list_runs_json "$branch" | jq --arg sha "$sha" '[.[] | select(.headSha == $sha and .event == "workflow_dispatch")]'
}

latest_runs_by_workflow_for_sha_json() {
  local branch="$1"
  local sha="$2"
  matching_runs_for_sha_json "$branch" "$sha" | jq 'sort_by(.name, .createdAt) | group_by(.name) | map(max_by(.createdAt))'
}

assert_sandbox_green_for_sha() {
  local branch="$1"
  local sha="$2"
  local matching matching_count pending failed required
  required="$(workflow_count)"
  matching="$(latest_runs_by_workflow_for_sha_json "$branch" "$sha")"
  matching_count="$(printf '%s' "$matching" | jq 'length')"
  if [ "$matching_count" -lt "$required" ]; then
    return 10
  fi
  pending="$(printf '%s' "$matching" | jq '[.[] | select(.status != "completed")] | length')"
  if [ "$pending" != "0" ]; then
    return 11
  fi
  failed="$(printf '%s' "$matching" | jq '[.[] | select(.conclusion != "success" and .conclusion != "skipped")] | length')"
  if [ "$failed" != "0" ]; then
    return 12
  fi
  return 0
}

set_canonical_status() {
  local sha="$1"
  local state="$2"
  local description="$3"
  local target_url="$4"
  echo "ci-sandbox: setting $CANONICAL_REPO status $STATUS_CONTEXT=$state on ${sha:0:12}"
  gh api "repos/${CANONICAL_REPO}/statuses/${sha}" \
    -f state="$state" \
    -f context="$STATUS_CONTEXT" \
    -f description="$description" \
    -f target_url="$target_url" >/dev/null
}

canonical_status_state() {
  local sha="$1"
  local jq_filter
  jq_filter='[.statuses[]? | select(.context == "'"$STATUS_CONTEXT"'") | .state][0] // ""'
  gh api "repos/${CANONICAL_REPO}/commits/${sha}/status" \
    --jq "$jq_filter" \
    2>/dev/null
}

maybe_stamp_existing_canonical_branch() {
  local branch="$1"
  local sha origin_branch
  sha="$(git -C "$ROOT" rev-parse "$branch")"
  origin_branch="$(ls_remote_sha "$ORIGIN_REMOTE" "refs/heads/$branch")"
  if [ "$origin_branch" = "$sha" ]; then
    cmd_prove "$branch"
  else
    echo "ci-sandbox: $STATUS_CONTEXT not stamped yet; push the exact SHA to $CANONICAL_REPO and run: scripts/ci-sandbox.sh prove $branch"
  fi
}

cmd_prove() {
  local branch
  branch="$(resolve_branch "${1:-}")"
  need_tools
  repo_root
  ensure_remote

  local sha ci_branch origin_branch target_url
  sha="$(git -C "$ROOT" rev-parse "$branch")"
  ci_branch="$(ls_remote_sha "$CI_REMOTE_URL" "refs/heads/$branch")"
  origin_branch="$(ls_remote_sha "$ORIGIN_REMOTE" "refs/heads/$branch")"
  target_url="$(actions_url "$branch")"

  [ "$ci_branch" = "$sha" ] || die "$CI_REPO branch $branch is not the local tested SHA ${sha:0:12}"
  [ "$origin_branch" = "$sha" ] || die "$CANONICAL_REPO branch $branch is not the local tested SHA ${sha:0:12}; push it first"

  if assert_sandbox_green_for_sha "$branch" "$sha"; then
    set_canonical_status "$sha" "success" "Full projectplanner-ci suite passed for exact SHA ${sha:0:12}" "$target_url"
    echo "ci-sandbox: proof stamped on $CANONICAL_REPO for ${sha:0:12}"
    return 0
  fi

  local result=$?
  case "$result" in
    10)
      set_canonical_status "$sha" "pending" "Waiting for full projectplanner-ci workflow_dispatch suite" "$target_url"
      die "not enough projectplanner-ci workflow_dispatch runs found for ${sha:0:12}"
      ;;
    11)
      set_canonical_status "$sha" "pending" "projectplanner-ci suite still running" "$target_url"
      die "projectplanner-ci workflow_dispatch suite still running for ${sha:0:12}"
      ;;
    12)
      set_canonical_status "$sha" "failure" "projectplanner-ci suite failed for exact SHA ${sha:0:12}" "$target_url"
      die "projectplanner-ci workflow_dispatch suite failed for ${sha:0:12}"
      ;;
    *)
      set_canonical_status "$sha" "error" "projectplanner-ci proof could not be evaluated" "$target_url"
      die "projectplanner-ci proof could not be evaluated for ${sha:0:12}"
      ;;
  esac
}

list_runs_json() {
  local branch="$1"
  gh run list \
    --repo "$CI_REPO" \
    --branch "$branch" \
    --limit 30 \
    --json databaseId,name,status,conclusion,createdAt,event,headSha \
    2>/dev/null || printf '[]'
}

summarize_runs() {
  local branch="$1"
  local event_filter="${2:-}"
  local json
  json="$(list_runs_json "$branch")"
  if [ "$json" = "[]" ] || [ -z "$json" ]; then
    echo "ci-sandbox: no Actions runs yet for branch '$branch' on $CI_REPO"
    return 1
  fi
  printf '%s\n' "$json" | jq -r --arg event "$event_filter" '
    sort_by(.createdAt) | reverse | .[]
    | select(($event == "") or (.event == $event)) |
    [
      (.conclusion // .status),
      .name,
      (.headSha[0:12] // "?"),
      .event,
      .createdAt
    ] | @tsv' | column -t -s $'\t'
}

wait_for_runs() {
  local branch="$1"
  local event_filter="${2:-}"
  local since="${3:-}"
  local required_count="${4:-1}"
  local deadline=$(( $(date +%s) + WAIT_TIMEOUT_SEC ))
  local head_sha
  head_sha="$(git -C "$ROOT" rev-parse "$branch")"

  echo "ci-sandbox: waiting for Actions on $CI_REPO@$branch (${head_sha:0:12}), timeout ${WAIT_TIMEOUT_SEC}s"
  if [ -n "$event_filter" ]; then
    echo "ci-sandbox: gating $required_count $event_filter run(s)"
  fi

  while [ "$(date +%s)" -lt "$deadline" ]; do
    local json pending matching
    json="$(list_runs_json "$branch")"
    matching="$(printf '%s' "$json" | jq \
      --arg sha "$head_sha" \
      --arg event "$event_filter" \
      --arg since "$since" \
      '[.[] | select(.headSha == $sha)
        | select(($event == "") or (.event == $event))
        | select(($since == "") or (.createdAt >= $since))]')"
    pending="$(printf '%s' "$matching" | jq '[.[] | select(.status != "completed")] | length')"

    local matching_count
    matching_count="$(printf '%s' "$matching" | jq 'length')"
    if [ "$matching_count" -lt "$required_count" ]; then
      echo "ci-sandbox: found $matching_count/$required_count gated run(s) for ${head_sha:0:12}; sleeping ${POLL_INTERVAL_SEC}s"
      sleep "$POLL_INTERVAL_SEC"
      continue
    fi

    if [ "$pending" = "0" ]; then
      local failed
      failed="$(printf '%s' "$matching" | jq '[.[] | select(.conclusion != "success" and .conclusion != "skipped")] | length')"
      echo ""
      summarize_runs "$branch" || true
      if [ "$failed" != "0" ]; then
        die "CI sandbox failed ($failed run(s) not success/skipped) — see https://github.com/${CI_REPO}/actions?query=branch%3A${branch}"
      fi
      echo "ci-sandbox: all Actions green for ${head_sha:0:12} on $CI_REPO"
      return 0
    fi

    echo "ci-sandbox: $pending run(s) still in progress..."
    sleep "$POLL_INTERVAL_SEC"
  done

  summarize_runs "$branch" || true
  die "timed out waiting for CI sandbox runs on $branch"
}

cmd_wait() {
  local branch
  branch="$(resolve_branch "${1:-}")"
  need_tools
  repo_root
  ensure_remote
  wait_for_runs "$branch" "${SANDBOX_WAIT_EVENT:-}" "" 1
}

cmd_status() {
  local branch
  branch="$(resolve_branch "${1:-}")"
  need_tools
  repo_root
  summarize_runs "$branch" "${SANDBOX_WAIT_EVENT:-}" || exit 1
}

ok_count=0
warn_count=0
fail_count=0

doctor_ok() {
  ok_count=$((ok_count + 1))
  echo "ok: $*"
}

doctor_warn() {
  warn_count=$((warn_count + 1))
  echo "warn: $*"
}

doctor_fail() {
  fail_count=$((fail_count + 1))
  echo "fail: $*"
}

ls_remote_sha() {
  local remote="$1"
  local ref="$2"
  git -C "$ROOT" ls-remote "$remote" "$ref" 2>/dev/null | awk '{print $1}'
}

repo_visibility() {
  local repo="$1"
  gh repo view "$repo" --json visibility --jq .visibility 2>/dev/null || true
}

cmd_doctor() {
  local branch=""
  if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    cat <<EOF
Usage: scripts/ci-sandbox.sh doctor [branch]

Verifies that this checkout can use the full-tree public CI sandbox:
  - required local tools exist
  - canonical and sandbox GitHub repos are reachable
  - ci remote exists and points at the expected sandbox URL
  - origin/main and projectplanner-ci/main point at the same baseline
  - configured workflows exist locally, are visible on projectplanner-ci, and support workflow_dispatch
  - optional branch is pushed consistently to projectplanner-ci and, when present, origin
EOF
    return 0
  fi
  branch="${1:-$(current_branch)}"

  repo_root
  need_tools

  echo "ci-sandbox: doctor for $CI_REPO (canonical $CANONICAL_REPO)"

  local canonical_visibility ci_visibility
  canonical_visibility="$(repo_visibility "$CANONICAL_REPO")"
  ci_visibility="$(repo_visibility "$CI_REPO")"
  if [ -n "$canonical_visibility" ]; then
    doctor_ok "canonical repo reachable: $CANONICAL_REPO ($canonical_visibility)"
  else
    doctor_fail "canonical repo not reachable: $CANONICAL_REPO"
  fi
  if [ "$ci_visibility" = "PUBLIC" ]; then
    doctor_ok "CI sandbox repo reachable and public: $CI_REPO"
  elif [ -n "$ci_visibility" ]; then
    doctor_fail "CI sandbox repo is reachable but not public: $CI_REPO ($ci_visibility)"
  else
    doctor_fail "CI sandbox repo not reachable: $CI_REPO"
  fi

  local configured_ci_url
  configured_ci_url="$(remote_url)"
  if [ -n "$configured_ci_url" ]; then
    if [ "$configured_ci_url" = "$CI_REMOTE_URL" ]; then
      doctor_ok "remote '$CI_REMOTE' points at $CI_REMOTE_URL"
    else
      doctor_warn "remote '$CI_REMOTE' points at $configured_ci_url (expected $CI_REMOTE_URL)"
    fi
  else
    doctor_fail "remote '$CI_REMOTE' is missing; run scripts/ci-sandbox.sh setup"
  fi

  if git -C "$ROOT" remote get-url "$ORIGIN_REMOTE" >/dev/null 2>&1; then
    doctor_ok "canonical remote '$ORIGIN_REMOTE' exists"
  else
    doctor_fail "canonical remote '$ORIGIN_REMOTE' is missing"
  fi

  local local_main origin_main ci_main
  local_main="$(git -C "$ROOT" rev-parse --verify "${MAIN_REF}^{commit}" 2>/dev/null || true)"
  origin_main="$(ls_remote_sha "$ORIGIN_REMOTE" refs/heads/master)"
  ci_main="$(ls_remote_sha "$CI_REMOTE_URL" refs/heads/master)"
  if [ -n "$local_main" ]; then
    doctor_ok "$MAIN_REF resolves to ${local_main:0:12}"
  else
    doctor_fail "$MAIN_REF does not resolve locally; run git fetch $ORIGIN_REMOTE main"
  fi
  if [ -n "$origin_main" ]; then
    doctor_ok "$ORIGIN_REMOTE/main is reachable at ${origin_main:0:12}"
  else
    doctor_fail "$ORIGIN_REMOTE/main is not reachable"
  fi
  if [ -n "$ci_main" ]; then
    doctor_ok "$CI_REPO/main is reachable at ${ci_main:0:12}"
  else
    doctor_fail "$CI_REPO/main is not reachable"
  fi
  if [ -n "$origin_main" ] && [ -n "$local_main" ] && [ "$origin_main" != "$local_main" ]; then
    doctor_warn "$MAIN_REF is stale versus $ORIGIN_REMOTE/main; run scripts/ci-sandbox.sh refresh-main"
  fi
  if [ -n "$origin_main" ] && [ -n "$ci_main" ]; then
    if [ "$origin_main" = "$ci_main" ]; then
      doctor_ok "$CI_REPO/main matches $ORIGIN_REMOTE/main"
    else
      doctor_fail "$CI_REPO/main (${ci_main:0:12}) differs from $ORIGIN_REMOTE/main (${origin_main:0:12}); run scripts/ci-sandbox.sh refresh-main"
    fi
  fi

  local protection_contexts
  protection_contexts="$(gh api "repos/${CANONICAL_REPO}/branches/master/protection/required_status_checks" --jq '.contexts[]?' 2>/dev/null || true)"
  if printf '%s\n' "$protection_contexts" | grep -Fx "$STATUS_CONTEXT" >/dev/null 2>&1; then
    doctor_ok "main requires canonical status: $STATUS_CONTEXT"
  else
    doctor_warn "main does not require $STATUS_CONTEXT yet; run scripts/ci-sandbox.sh protect-main"
  fi

  local workflow
  for workflow in $SANDBOX_WORKFLOWS; do
    if [ -f "$ROOT/.github/workflows/$workflow" ]; then
      doctor_ok "workflow file exists locally: $workflow"
      if grep -q 'workflow_dispatch:' "$ROOT/.github/workflows/$workflow"; then
        doctor_ok "workflow supports manual dispatch locally: $workflow"
      else
        doctor_fail "workflow lacks workflow_dispatch locally: $workflow"
      fi
    else
      doctor_fail "workflow file missing locally: $workflow"
    fi
    if gh workflow view "$workflow" --repo "$CI_REPO" >/dev/null 2>&1; then
      doctor_ok "workflow visible on $CI_REPO: $workflow"
    else
      doctor_fail "workflow not visible on $CI_REPO: $workflow"
    fi
  done

  if [ -n "$branch" ]; then
    if git -C "$ROOT" rev-parse --verify "$branch^{commit}" >/dev/null 2>&1; then
      local branch_sha ci_branch origin_branch
      branch_sha="$(git -C "$ROOT" rev-parse "$branch")"
      ci_branch="$(ls_remote_sha "$CI_REMOTE_URL" "refs/heads/$branch")"
      origin_branch="$(ls_remote_sha "$ORIGIN_REMOTE" "refs/heads/$branch")"
      doctor_ok "local branch $branch resolves to ${branch_sha:0:12}"
      if [ -n "$ci_branch" ]; then
        if [ "$ci_branch" = "$branch_sha" ]; then
          doctor_ok "$CI_REPO branch $branch matches local SHA"
        else
          doctor_fail "$CI_REPO branch $branch is ${ci_branch:0:12}, local is ${branch_sha:0:12}"
        fi
      else
        doctor_warn "$CI_REPO branch $branch is not pushed yet; run scripts/ci-sandbox.sh push $branch"
      fi
      if [ -n "$origin_branch" ]; then
        if [ "$origin_branch" = "$branch_sha" ]; then
          doctor_ok "$CANONICAL_REPO branch $branch matches local SHA"
          local status_state
          status_state="$(canonical_status_state "$branch_sha")"
          if [ "$status_state" = "success" ]; then
            doctor_ok "$STATUS_CONTEXT status is success for ${branch_sha:0:12}"
          elif assert_sandbox_green_for_sha "$branch" "$branch_sha"; then
            doctor_ok "$CI_REPO full workflow_dispatch suite is green for ${branch_sha:0:12}"
          else
            doctor_warn "$CI_REPO full workflow_dispatch suite is not green/proven and $STATUS_CONTEXT is not success for ${branch_sha:0:12}"
          fi
        else
          doctor_warn "$CANONICAL_REPO branch $branch is ${origin_branch:0:12}, local is ${branch_sha:0:12}"
        fi
      else
        doctor_warn "$CANONICAL_REPO branch $branch is not pushed yet"
      fi
    else
      doctor_warn "branch $branch does not resolve locally; branch-specific checks skipped"
    fi
  else
    doctor_warn "not on a branch; branch-specific checks skipped"
  fi

  echo "ci-sandbox: doctor summary: ${ok_count} ok, ${warn_count} warn, ${fail_count} fail"
  if [ "$fail_count" -ne 0 ]; then
    die "doctor found $fail_count failure(s)"
  fi
}

cmd_protect_main() {
  need_tools
  repo_root
  local main_sha ci_main payload
  main_sha="$(ls_remote_sha "$ORIGIN_REMOTE" refs/heads/master)"
  ci_main="$(ls_remote_sha "$CI_REMOTE_URL" refs/heads/master)"
  [ -n "$main_sha" ] || die "$ORIGIN_REMOTE/main is not reachable"
  [ "$ci_main" = "$main_sha" ] || die "$CI_REPO/main must match $ORIGIN_REMOTE/main first; run scripts/ci-sandbox.sh refresh-main"

  set_canonical_status "$main_sha" "success" "projectplanner-ci/main matches canonical main ${main_sha:0:12}" "https://github.com/${CI_REPO}/actions"

  payload="$(jq -n --arg context "$STATUS_CONTEXT" '{
    required_status_checks: {
      strict: true,
      contexts: [$context]
    },
    enforce_admins: true,
    required_pull_request_reviews: null,
    restrictions: null
  }')"

  printf '%s' "$payload" | gh api \
    --method PUT \
    -H "Accept: application/vnd.github+json" \
    "repos/${CANONICAL_REPO}/branches/master/protection" \
    --input - >/dev/null

  echo "ci-sandbox: protected $CANONICAL_REPO main; required status: $STATUS_CONTEXT"
}

cmd_delete() {
  local branch
  branch="$(resolve_branch "${1:-}")"
  need_tools
  repo_root
  ensure_remote
  echo "ci-sandbox: deleting $CI_REPO:$branch"
  git -C "$ROOT" push "$CI_REMOTE" --delete "$branch"
  echo "ci-sandbox: deleted $branch from $CI_REPO"
}

cmd_sync_main() {
  need_tools
  repo_root
  ensure_repo
  ensure_remote
  local sha
  sha="$(git -C "$ROOT" rev-parse --verify "${MAIN_REF}^{commit}")"
  echo "ci-sandbox: syncing $MAIN_REF @ ${sha:0:12} -> $CI_REPO:main"
  git -C "$ROOT" push "$CI_REMOTE" "${sha}:refs/heads/master"
  echo "ci-sandbox: main synced — https://github.com/${CI_REPO}"
}

cmd_refresh_main() {
  need_tools
  repo_root
  ensure_repo
  ensure_remote
  echo "ci-sandbox: fetching $ORIGIN_REMOTE master"
  git -C "$ROOT" fetch "$ORIGIN_REMOTE" master
  local MAIN_REF="${ORIGIN_REMOTE}/master"
  cmd_sync_main
}

cmd_open_pr() {
  local branch
  branch="$(resolve_branch "${1:-}")"
  need_tools
  repo_root

  if ! git -C "$ROOT" show-ref --verify --quiet "refs/heads/${branch}"; then
    die "branch '$branch' not found locally"
  fi

  cmd_push "$branch"

  echo "ci-sandbox: pushing $branch to canonical repo ($CANONICAL_REPO)"
  git -C "$ROOT" push -u "$ORIGIN_REMOTE" "$branch"
  cmd_prove "$branch"

  if gh pr view --repo "$CANONICAL_REPO" --head "$branch" >/dev/null 2>&1; then
    gh pr view --repo "$CANONICAL_REPO" --head "$branch" --web
    die "PR already exists for $branch on $CANONICAL_REPO"
  fi

  gh pr create \
    --repo "$CANONICAL_REPO" \
    --head "$branch" \
    --title "$branch" \
    --body "$(cat <<EOF
## CI sandbox

Extensive GitHub Actions ran on [\`${CI_REPO}\`](https://github.com/${CI_REPO}/actions?query=branch%3A${branch}) before opening this PR on \`${CANONICAL_REPO}\`.

After merge, refresh the sandbox baseline and delete the temporary sandbox branch:
\`\`\`bash
scripts/ci-sandbox.sh refresh-main
scripts/ci-sandbox.sh delete ${branch}
\`\`\`
EOF
)"
}

main() {
  local cmd="${1:-}"
  shift || true
  case "$cmd" in
    setup) cmd_setup "$@" ;;
    push) cmd_push "$@" ;;
    wait) cmd_wait "$@" ;;
    status) cmd_status "$@" ;;
    prove) cmd_prove "$@" ;;
    doctor) cmd_doctor "$@" ;;
    protect-main) cmd_protect_main "$@" ;;
    delete) cmd_delete "$@" ;;
    sync-main) cmd_sync_main "$@" ;;
    refresh-main) cmd_refresh_main "$@" ;;
    open-pr) cmd_open_pr "$@" ;;
    -h|--help|help|"") usage ;;
    *) die "unknown command '$cmd' (try: scripts/ci-sandbox.sh --help)" ;;
  esac
}

main "$@"
