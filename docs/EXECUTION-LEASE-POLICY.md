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
- Terminal task status (`Done` / `Cancelled`) is an automatic capacity event:
  host heartbeat projects it into `terminal_runner_cleanup` with
  `action=make_lease_due`, force-stales the runner, and fences a bound
  execution generation. It does not send a process signal.
- The owning Agent Host lease reaper stops the supervised generation, persists
  a terminal receipt, and retries acknowledgement after restart.
- A stopped, fenced, or due generation cannot heartbeat, renew, or mutate state.
- Stopping acknowledgement retries use the durable receipt and do not create a
  second stop clock.

Lease enforcement is unconditional on every work-capable host. Placement
requires both lease capabilities, and no observe-only or rollback branch exists.

## Explicit exceptions

Operator Kill is explicit, authenticated, and audited human authority.
Fail-closed spawn cleanup may stop a child that never obtained a valid runner
binding. Neither exception is a timer or an inferred lifecycle transition.

## Promotion evidence

SIMPLIFY-16 recorded the hands-off proof, supported-host census, durable restart
acknowledgement, and passing kill census. SIMPLIFY-11 retired the rollout flag
and observe-only compatibility branch.
