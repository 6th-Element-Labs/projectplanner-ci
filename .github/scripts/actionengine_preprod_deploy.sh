#!/usr/bin/env bash
# Send the canonical ActionEngine deploy door to the preproduction box over
# SSM, then relay its canary/rollback verdict. This script intentionally cannot
# target production.
set -euo pipefail

: "${INSTANCE_ID:?}" "${SHA:?}" "${ACTOR:?}"
SKIP_FMP_SYNC="${SKIP_FMP_SYNC:-0}"

[[ "$INSTANCE_ID" =~ ^i-[0-9a-f]{17}$ ]]
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]]
[[ "$ACTOR" =~ ^[A-Za-z0-9_-]+$ ]]
case "$SKIP_FMP_SYNC" in
  0|1) ;;
  *) echo "SKIP_FMP_SYNC must be 0 or 1" >&2; exit 2 ;;
esac

root=/home/ubuntu/ActionEngine
remote_command="set -euo pipefail
cd $root
git fetch --quiet origin $SHA
git show FETCH_HEAD:deploy/total/deploy.sh > /tmp/deploy-$SHA.sh
SKIP_FMP_SYNC='$SKIP_FMP_SYNC' DEPLOY_ACTOR='github-public:$ACTOR' bash /tmp/deploy-$SHA.sh preprod $SHA"

payload="$(
  jq -n \
    --arg id "$INSTANCE_ID" \
    --arg cmd "sudo -u ubuntu -H bash -lc \"$(printf '%s' "$remote_command" | sed 's/"/\\"/g')\"" \
    --arg comment "deploy preprod $SHA by $ACTOR skip_fmp_sync=$SKIP_FMP_SYNC" \
    '{InstanceIds:[$id], DocumentName:"AWS-RunShellScript", Comment:$comment,
      TimeoutSeconds:600, Parameters:{commands:[$cmd], executionTimeout:["2400"]}}'
)"

command_id="$(
  aws ssm send-command --cli-input-json "$payload" \
    --query Command.CommandId --output text
)"
echo "ssm command $command_id -> $INSTANCE_ID (preprod) sha=$SHA"

command_status=Pending
for _attempt in $(seq 1 170); do
  sleep 15
  command_status="$(
    aws ssm get-command-invocation \
      --command-id "$command_id" \
      --instance-id "$INSTANCE_ID" \
      --query Status --output text 2>/dev/null || echo Pending
  )"
  case "$command_status" in
    Success|Failed|Cancelled|TimedOut|Undeliverable|Terminated|DeliveryTimedOut|ExecutionTimedOut)
      break
      ;;
  esac
  echo "  $command_status ..."
done

echo "::group::deploy.sh output ($command_status)"
aws ssm get-command-invocation \
  --command-id "$command_id" \
  --instance-id "$INSTANCE_ID" \
  --query StandardOutputContent --output text | tail -c 24000
echo "::endgroup::"

error_output="$(
  aws ssm get-command-invocation \
    --command-id "$command_id" \
    --instance-id "$INSTANCE_ID" \
    --query StandardErrorContent --output text | tail -c 8000
)"
if [ -n "$error_output" ]; then
  echo "::group::stderr"
  printf '%s\n' "$error_output"
  echo "::endgroup::"
fi

if [ "$command_status" = Success ]; then
  echo "::notice::preprod is on $SHA (canary green)."
  exit 0
fi

echo "::error::preprod deploy $SHA ended $command_status; deploy.sh restored the prior commit and config."
exit 1
