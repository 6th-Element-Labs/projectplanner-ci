# Runbook — Event-driven narration cutover & rollback (NARRATE-14)

Migrate the CEO-voice narrator from the legacy 45s poll (`pending_narrations` → `narrate.run_pending`)
to the durable event-driven path (transactional outbox → wakeable worker → compare-and-swap publish),
prove the SLOs, and retire the legacy poll. This is the M4 rollout for the
`deliverable-event-driven-llm-narration` deliverable.

The cutover is **operator-gated by a single flag** — merging the code changes nothing until the flag
is flipped, and flipping it back is the instant rollback lever.

## The one lever

`PM_NARRATION_EVENT_PRIMARY` (in `/opt/projectplanner/.env`)

| State | Primary trigger | Publisher | Legacy 45s timer |
|-------|-----------------|-----------|------------------|
| unset / `0` (default) | legacy `pending_narrations` poll | `narrate.run_pending` | active |
| `1` | outbox **wake** (accelerator) + `narrate_events` recovery sweep | `narration_cutover` CAS publish | `narrate_pending` self-skips (no double publish) |

Supporting knobs (safe defaults; tune only during soak):
`PM_NARRATION_WAKE_MAX_ITEMS` (12), `PM_NARRATION_SWEEP_MAX_ITEMS` (100),
`PM_NARRATION_DAILY_COST_USD` (5.0/project), `PM_NARRATION_OUTBOX` (emit kill switch, default on).

## Architecture after cutover

1. A task/deliverable write commits its domain change **and** a `narration_requested` outbox row in
   one transaction (NARRATE-8). Post-commit, `request_wake` fires.
2. The wake accelerator (registered in the web process at startup, `register_production_wake_sink`)
   runs a bounded, debounced background drain per project — the near-real-time primary trigger.
3. The worker claims under a lease, coalesces stale revisions, and calls the generate+publish
   callback (NARRATE-9 + NARRATE-12 + this cutover's **boundary-3 compare-and-swap publish** into
   `task_narrations` / deliverable `ceo_narrative`).
4. `projectplanner-narrate-events.timer` (~5 min) runs `jobs.py narrate_events` — the **slow recovery
   sweep** backstop. The durable outbox is the source of truth, so a missed/failed wake only delays a
   narrative to the next sweep; it never loses one.

## Pre-cutover gate — shadow must be clean

Before flipping the flag, the event path must cover everything the legacy path would narrate.
`narration_shadow.compare_narration_paths(project)` must show `in_sync: true` (empty `only_legacy`) for
each project. `only_event` being non-empty is expected (the outbox emits on all material changes).

## Rollout steps

1. **Deploy the code.** Wake sink registers but is inert (flag unset). No behavior change.
2. **Install the recovery-sweep timer** (leave the legacy timer running):
   ```
   sudo cp deploy/projectplanner-narrate-events.{service,timer} /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now projectplanner-narrate-events.timer
   ```
   With the flag still off, `narrate_events` self-skips — a safe no-op you can watch in the journal.
3. **Canary.** Confirm `compare_narration_paths` is `in_sync` and `scripts/narration_slo.py --all`
   reads a clean baseline.
4. **Flip primary.** Set `PM_NARRATION_EVENT_PRIMARY=1` in `.env`, then
   `sudo systemctl restart projectplanner` (registers the live wake). The event path is now primary;
   `narrate_pending` self-skips so only one path publishes.
5. **Soak & prove SLOs** (see below). Watch `/api/narration/health` (NARRATE-13) and
   `scripts/narration_slo.py --all` for the soak window.
6. **Retire legacy** once SLOs hold: `sudo systemctl disable --now projectplanner-narrate.timer`.
   The `pending_narrations` table becomes vestigial (safe to leave; a later cleanup task can drop it).

## SLO targets (NARRATE-14 exit criteria)

`scripts/narration_slo.py --all` exits non-zero if any target is breached:

- **Freshness**: request→delivery p95 **≤ 60s** under agreed load.
- **Idle cost**: near-zero actionable depth on a quiet board (wake path does not busy-poll); idle
  narration CPU near zero — the batch-slice sweep yields to the web/MCP paths.
- **Durability**: no lost or duplicate published narrative across crash/restart (proved by
  `test_narration_rollout.py`; re-verify on prod with a controlled restart during the soak).
- **Reconciliation**: window cost ≤ per-project budget ceiling; every attempt has a receipt; no
  dead letters (or each is triaged via `reactivate_narration`).

## Rollback (instant)

1. Set `PM_NARRATION_EVENT_PRIMARY=0` (or unset) in `/opt/projectplanner/.env`.
2. `sudo systemctl enable --now projectplanner-narrate.timer` (if already disabled).
3. `sudo systemctl restart projectplanner`.

The wake accelerator and recovery sweep go inert immediately; the legacy poll resumes as primary
publisher. Because both paths publish to the same `task_narrations` surface and the flag guarantees
only one is active, no narrative is lost or doubled across the flip. If the outbox itself misbehaves,
`PM_NARRATION_OUTBOX=0` additionally halts emit (deeper lever; legacy path is unaffected).

## Drills (run against a staging copy or during a monitored soak)

Automated equivalents live in `test_narration_rollout.py`; the production drills confirm them live:

- **Crash/restart**: `sudo systemctl restart projectplanner` mid-backlog → recovery sweep + wake
  redeliver; assert each entity has exactly one delivered receipt and the visible narrative updated.
- **Backlog**: pause the worker (flag off) while emitting a burst, re-enable → confirm bounded sweeps
  drain it and freshness recovers under target.
- **Provider outage**: point `PM_LLM_BASE_URL` at a dead port → confirm **visible fallback**
  narratives and zero dead letters, then restore and confirm recovery to real narratives.
- **Database contention**: run the drill under concurrent board load → confirm no `database is locked`
  failures escape (worker retries) and no lost/duplicate delivery.

## Related

- ADR-0008 — narration event contract & delivery (boundaries, state machine, invariants).
- `docs/CEO-NARRATOR-CONTRACT.md` — voice, cost policy, env-var contract.
- NARRATE-13 — `/api/narration/health`, `narrate_now`, `reactivate_narration` operator surfaces.
