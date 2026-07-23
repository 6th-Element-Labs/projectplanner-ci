# Execution lease clock policy

The renewable execution lease is the only automatic authority that may stop a
managed runner. Task, claim, Work Session, scheduler, credential, coordinator,
and UI state are evidence or projections. They do not infer process death and
do not send process signals.

## Clock

- Placement requires `execution_lease_v2` and `runner_lease_enforcement`.
- A runner heartbeat renews the exact execution generation until its TTL.
- `complete_claim` and automatic capacity events make that exact lease due,
  move it to `stopping`, and fence its generation.
- The owning Agent Host lease reaper stops the supervised generation, persists
  a terminal receipt, and retries acknowledgement after restart.
- A stopped or fenced generation cannot heartbeat, renew, or mutate state.
- Stopping acknowledgement retries use the durable receipt and do not create a
  second stop clock.

`PM_RUNNER_LEASE_ENFORCEMENT` defaults to enabled and is the only temporary
rollback switch. Setting it to `0` is an audited emergency rollback during the
SIMPLIFY-16 observation window. SIMPLIFY-11 owns deletion of this final switch
after the hands-off proof passes.

## Explicit exceptions

Operator Kill is explicit, authenticated, and audited human authority.
Fail-closed spawn cleanup may stop a child that never obtained a valid runner
binding. Neither exception is a timer or an inferred lifecycle transition.

## Promotion and rollback retirement

Promotion evidence must record the observation start, host build/config
versions, eligible-host capability census, and zero false `would_expire` events
for the bounded observation window. Rollback retirement requires the
SIMPLIFY-16 hands-off proof, no unsupported eligible host, durable restart
acknowledgement proof, and a passing kill census. SIMPLIFY-11 then deletes the
rollback flag and compatibility branch.
