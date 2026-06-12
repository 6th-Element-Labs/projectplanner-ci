#!/bin/bash
# Maxwell dispatch: run Claude Code headless on a task brief in an ISOLATED git worktree ->
# push a claude/<task> branch -> emit the PR compare URL -> post the result back onto the plan
# task. Each job gets its own worktree, so concurrent dispatches never clobber each other's
# files (the old shared-working-dir approach let a second dispatch sweep the first's changes).
# args: <task_id> <brief_file> <job_dir>
set -uo pipefail
TASK_ID="$1"; BRIEF_FILE="$2"; JOB="$3"
REPO=/home/claude-runner/ActionEngine
BASE=development
PLAN_URL="${PLAN_URL:-https://plan.taikunai.com}"
export HOME=/home/claude-runner
export ANTHROPIC_API_KEY="$(cat /home/claude-runner/.maxwell/key)"
log(){ echo "[$(date -u +%H:%M:%S)] $*" >> "$JOB/claude.log"; }
notify_plan(){ MSG="$1" TID="$TASK_ID" PU="$PLAN_URL" python3 -c '
import json, os, urllib.request as u
try:
    d = json.dumps({"actor": "Maxwell (runner)", "text": os.environ["MSG"]}).encode()
    r = u.Request(os.environ["PU"] + "/api/tasks/" + os.environ["TID"] + "/comment",
                  data=d, headers={"Content-Type": "application/json"})
    u.urlopen(r, timeout=15)
except Exception:
    pass' 2>/dev/null || true; }

cd "$REPO" || { echo no_repo > "$JOB/status"; exit 1; }
git fetch -q origin "$BASE" 2>>"$JOB/claude.log" || git fetch -q origin 2>>"$JOB/claude.log"
SLUG=$(echo "$TASK_ID" | tr '[:upper:] ' '[:lower:]-' | tr -cd 'a-z0-9-'); SLUG=${SLUG:-task}
BRANCH="claude/${SLUG}-$(date +%s)-$$"
WT="/home/claude-runner/runner/work/$BRANCH"
mkdir -p /home/claude-runner/runner/work
git worktree add -q -B "$BRANCH" "$WT" "origin/$BASE" 2>>"$JOB/claude.log" \
  || { echo failed_branch > "$JOB/status"; log "worktree add failed"; exit 1; }
log "isolated worktree $WT on $BRANCH off origin/$BASE"
cd "$WT"
claude -p "$(cat "$BRIEF_FILE")" --dangerously-skip-permissions --output-format text >> "$JOB/claude.log" 2>&1 \
  || log "claude exited non-zero"
git add -A
# safety net for anything Claude left uncommitted (it usually commits itself); empty commit just fails
git commit -q -m "Maxwell dispatch: $TASK_ID" >> "$JOB/claude.log" 2>&1 || true
# "no changes" = no commits ahead of base (NOT an empty index — Claude commits its own work)
AHEAD=$(git rev-list --count "origin/$BASE..HEAD" 2>/dev/null || echo 0)
if [ "${AHEAD:-0}" -eq 0 ]; then
  echo no_changes > "$JOB/status"; log "no commits vs origin/$BASE"
  notify_plan "Claude Code ran $TASK_ID but produced no code changes — see the runner log."
elif git push -q origin "$BRANCH" 2>>"$JOB/claude.log"; then
  PRURL="https://github.com/6th-Element-Labs/ActionEngine/compare/${BASE}...${BRANCH}?expand=1"
  echo "$BRANCH" > "$JOB/branch"; echo "$PRURL" > "$JOB/pr_url"; echo pushed > "$JOB/status"; log "pushed $BRANCH"
  notify_plan "Claude Code finished $TASK_ID — branch $BRANCH pushed. Open the PR: $PRURL"
else
  echo push_failed > "$JOB/status"
  notify_plan "Claude Code made changes for $TASK_ID but the push failed — check the runner."
fi
# tear down the worktree (the pushed remote branch stays for the PR)
cd "$REPO"; git worktree remove --force "$WT" 2>>"$JOB/claude.log" || true
git branch -D "$BRANCH" 2>>"$JOB/claude.log" || true
