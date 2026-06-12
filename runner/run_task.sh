#!/bin/bash
# Maxwell dispatch: run Claude Code headless on a task brief -> push claude/<task> branch -> emit PR URL.
# args: <task_id> <brief_file> <job_dir>
set -uo pipefail
TASK_ID="$1"; BRIEF_FILE="$2"; JOB="$3"
REPO=/home/claude-runner/ActionEngine
BASE=development
export HOME=/home/claude-runner
export ANTHROPIC_API_KEY="$(cat /home/claude-runner/.maxwell/key)"
log(){ echo "[$(date -u +%H:%M:%S)] $*" >> "$JOB/claude.log"; }
cd "$REPO" || { echo no_repo > "$JOB/status"; exit 1; }
git fetch -q origin "$BASE" 2>>"$JOB/claude.log" || git fetch -q origin 2>>"$JOB/claude.log"
SLUG=$(echo "$TASK_ID" | tr '[:upper:] ' '[:lower:]-' | tr -cd 'a-z0-9-'); SLUG=${SLUG:-task}
BRANCH="claude/${SLUG}-$(date +%s)"
git checkout -q -B "$BRANCH" "origin/$BASE" 2>>"$JOB/claude.log" || git checkout -q -B "$BRANCH" 2>>"$JOB/claude.log"
log "branch $BRANCH off origin/$BASE"
claude -p "$(cat "$BRIEF_FILE")" --dangerously-skip-permissions --output-format text >> "$JOB/claude.log" 2>&1 || log "claude exited non-zero"
git add -A
if git diff --cached --quiet; then echo no_changes > "$JOB/status"; log "no changes"; exit 0; fi
git commit -q -m "Maxwell dispatch: $TASK_ID" >> "$JOB/claude.log" 2>&1 || true
if git push -q origin "$BRANCH" 2>>"$JOB/claude.log"; then
  echo "$BRANCH" > "$JOB/branch"
  echo "https://github.com/6th-Element-Labs/ActionEngine/compare/${BASE}...${BRANCH}?expand=1" > "$JOB/pr_url"
  echo pushed > "$JOB/status"; log "pushed $BRANCH"
else echo push_failed > "$JOB/status"; fi
