# Autopilot dock — condition ladder and honest liveness

Date: 2026-07-27 (revised 2026-07-28: renamed, widened to all three tabs)
Status: design approved by the operator; implementation plan to follow
Surface: `static/app.js` `_dockRunnerHtml` / `_dockPrHtml` / `_dockDeploymentHtml`,
`static/js/fleet-dock.js`, `static/index.html`, the collapsed dock pill

Wireframes: `docs/ui/fleet-dock-wireframe.html` (desktop, all three tabs) and
`docs/mobile/fleet-dock-mobile-wireframe.html` (phone).

## Rename: Fleet → Autopilot

Operator decision 2026-07-28. Four user-facing strings change:

| Where | Was | Becomes |
|---|---|---|
| `static/index.html:252` left nav title | Fleet | Autopilot |
| `static/index.html:599` page title | Fleet | Autopilot |
| `static/app.js:1256` dock header | Fleet | Autopilot |
| `static/app.js:1201` collapsed pill | Fleet clear | All clear |

Internal identifiers do NOT change: `fleet-dock` element id, `toptab-fleet`,
`#tab-fleet`, `_renderFleetDock`, `_loadFleetDock`, `SwitchboardFleetDock`,
`static/js/fleet-dock.js`, and every `/ixp/v1` route keep their names. Renaming
them is churn across a busy repo with real conflict risk and no user benefit.
The file header comments gain one line noting the surface is called Autopilot.

### Name collision with the autopilot scope object

"Autopilot" is already a first-class object: scopes with a `live` / `armed` /
`paused` / `stale` liveness verdict, `api/autopilot/coverage`, and the per-task
pill UI-66 shipped on PR rows. With the surface itself called Autopilot, that
pill must stop repeating the noun. It renders the state verb alone:

| `cov.liveness` | Pill label |
|---|---|
| `live` | Driving |
| `armed` | Armed |
| `paused` | Paused |
| `stale` | Stale |
| (none) | Arm |

Tones and click actions are unchanged from `FleetDock.autopilotHtml`; only the
label text loses the word. The tooltip keeps the full sentence, so the
distinction between a task scope and a deliverable scope is not lost.

## Problem

The Fleet dock is the exception surface, but its runner card cannot answer the
only question an operator asks of a runner: *is this thing actually moving?*

`_dockRunnerHtml` (static/app.js:1062) reports `status` and uptime. `status ==
"running"` means the lease is live, not that work is happening. A runner that
has been sitting at a `(y/n)` prompt for twenty minutes and a runner actively
writing code render identically — same green badge, same growing uptime number,
same two buttons. Uptime without progress actively misleads: "47m" reads as
productivity when it may be 47 minutes of silence.

This is the same defect UI-66 fixed for autopilot scopes: a status field was
reported as truth while the underlying lease said otherwise. The fix there was
`scope_liveness()` — one honest verdict derived from real evidence rather than a
status column. This design applies that rule to runners.

The dock already ranks PR conditions worst-first through
`FleetDock.prConditions` (static/js/fleet-dock.js), shows one authoritative
chip, and tints the card's left edge. Runners never got that treatment.

## What is already on the wire

Every field below is returned today and read by nothing in the dock.
`_runner_environment` (src/switchboard/storage/repositories/runner.py:786):

| Field | Meaning | Today |
|---|---|---|
| `environment.last_output_at` | host-reported last PTY output | unread |
| `environment.log_tail` | trailing output text | unread |
| `environment.progress_fault` | WATCH-19 verdict: live lease, silent PTY | unread |
| `environment.progress_fault.output_age_s` | seconds since output | unread |
| `environment.progress_fault.cpu_percent` | host CPU for the process | unread |
| `environment.failure_reason` | why it died | joined into a prose string |
| `environment.last_command` | what it was told to run | unread |
| `available_actions` | `open`/`inject`/`kill`/`restart` | only `kill` offered |
| `last_snapshot.status_porcelain` | dirty workspace | count only, in prose |

No backend change is required for any of it.

## The "waiting on you" signal

The one state not derivable from the runner payload alone is *a runner is
blocked asking a question*. It does not need a new host-side flag.

Attention requests carry a first-class `runner_session_id` with an asserted
delivery binding (`src/switchboard/application/attention.py:71`,
`_assert_delivery_binding`). ADAPTER-31 landed Connect PTY prompts as
provider attention requests. So the join is a direct key match:

    pending attention request where request.runner_session_id == s.runner_session_id

`_provider_item` (src/switchboard/api/routers/attention.py:237) already exposes
`payload.choices`, `payload.recommended_default`, `version`, and the decide
endpoint `POST /api/attention/requests/{request_id}/decide`. That is both the
chip and its action — the dock answers the prompt through the same path the
inbox uses, with `expected_version` for optimistic concurrency.

`Answer` renders from the request shape, not a guess: when `payload.choices` is
non-empty the dock shows those choices with `recommended_default` preselected;
when it is empty the prompt is free-text and the dock shows a single-line input.
Either way the answer posts to the same decide endpoint. If the request's
`version` has moved on since the poll, the post fails on `expected_version` and
the card re-reads rather than overwriting someone else's decision.

Delivery: the dock's existing 10s poll adds one request to the attention feed,
keyed by project, and indexes pending items by `runner_session_id`. The feed is
already cached server-side; no new endpoint.

## Condition ladder

`FleetDock.runnerConditions(app, s)` — a new pure function living beside
`prConditions` in the same module, same contract: return every condition that
holds, worst-first. The card renders `[0]` as the authoritative chip and tints
the left edge with its tone.

| Rank | Key | Condition | Label | Tone |
|---|---|---|---|---|
| 1 | `exited` | not stale, `status != running` | `Exited` + reason | red |
| 2 | `lost_host` | `s.stale` (lease expired, heartbeat gone) | `Lost host <age>` | red |
| 3 | `waiting_on_you` | pending attention request bound to this session | `Waiting on you` | orange |
| 4 | `silent` | `progress_fault` present | `Silent <output age>` | yellow |
| 5 | `idle` | running, no `task_id` bound | `Idle` | secondary |
| 6 | `working` | running, output age known and recent | `Working <output age>` | green |
| 7 | `running_unknown` | running, output age unknown | `Running <uptime>` | secondary |

`idle` is evaluated before the two running states on purpose: a runner with no
task bound is not working on anything, so "Working 4s" would be a true statement
about output and a false one about progress.

`waiting_on_you` deliberately outranks `silent`: a prompt is a silent PTY whose
cause is known, and the actionable label must win. `exited` outranks
`lost_host` because a process that returned a code is a different diagnosis
from a host that stopped answering.

Secondary conditions render as one muted outline chip, never as the primary:
`+ N uncommitted` from `status_porcelain`.

Sort: ladder rank ascending, then output age descending within rank. The top of
the list is always the thing to look at.

### Unknown is not zero

`last_output_at` is host-reported and may be absent — an older Agent Host, or a
runtime that does not stamp it. The card must never render a missing value as
`0s` or as `Silent`. Resolution order for output age:

1. `progress_fault.output_age_s` when a fault exists (already computed)
2. derived from `environment.last_output_at`
3. unknown → rank 6 `running_unknown`, label falls back to uptime, and the
   output line is omitted rather than blanked

A card that cannot prove progress says so. It does not guess in either
direction. This is the rule the diagnostic-discard class keeps violating: a
verdict that drops its own evidence.

## Card anatomy

Line 1 — primary chip, optional muted secondary chip, right-aligned autopilot pill.
Line 2 — `task_id` in mono plus the task title, linking to task detail.
Line 3 — mono provenance: runtime, host, branch, dirty count, uptime.
Line 4 — one line of `log_tail`, plus `quiet <age>` and cpu when known.
Line 5 — actions.

Uptime moves off the top-right slot into the mono provenance line. It is
context, not status, and it was previously being read as status. The freed slot
carries the autopilot pill below.

`runner_session_id` moves off the card face into the `title` attribute of the
task line. It is an opaque key; it was occupying the most valuable position on
the card.

Deletions, not additions: the `attn` array prose-join, the `tone` ternary, and
the `dead` boolean in `_dockRunnerHtml` all go. The ladder replaces them.

## Autopilot pill (scope addition — flagged for decision)

UI-66 shipped `FleetDock.autopilotHtml` and a batched
`api/autopilot/coverage?task_ids=…` read, but `loadCoverage` collects task IDs
from PRs only (static/js/fleet-dock.js:57). Runner sessions carry `task_id`, so
adding them to the same batch gives the runner card the identical pill — same
honest liveness vocabulary (live / armed / paused / stale / none), same
double-drive guard on click — for one extra set of IDs in a request that
already fires.

This is not in the five changes agreed; it is listed separately because it is
nearly free and answers UI-66's own stated motivation ("the Fleet dock is the
exception surface, but it could not answer *is autopilot driving this?*") for
the half of the dock UI-66 did not reach. Include or drop as one call.

## Action mapping

Primary action follows the condition; `kill` moves behind an overflow menu in
every state. Every action stays gated on `available_actions`.

| Condition | Primary | Secondary | Overflow |
|---|---|---|---|
| `waiting_on_you` | `Answer` (attention decide) | `Watch` | inject, kill |
| `silent` | `Watch` | `Nudge` (inject newline) if `inject` | restart, kill |
| `working` / `idle` / `running_unknown` | `Watch` | — | inject, kill |
| `exited` | `Restart` if available | — | open, kill |
| `lost_host` | none yet — see below | `Watch` | kill |

`lost_host` has no primary action in this change. Reclaiming an orphaned lease
is the in-flight lease-orphan work rescoped to the ADR-8 two-task cut
(`docs/superpowers/specs/` + `tests/test_adapter35_runner_lease_retry.py`).
This design shows the condition honestly and defers the verb to that work
rather than inventing a second reclaim path.

## Collapsed pill

Today: `N blocked · X working · Y PRs`. "Blocked" does not distinguish a crashed
runner from a runner asking a question, and those need opposite reactions.

Proposed: dot tone and lead text from the worst condition present —
`N waiting on you` (orange) > `N broken` (red) > `Fleet clear` (green) — with
`silent` counted in the muted secondary line alongside working and PR counts.

## Testing

The repo has no behavioural JS test today; `node --check` gates syntax only,
and CI runs each Python test file as `python <file>`
(`scripts/switchboard_ci.sh:44`).

1. `tests/test_ui67_runner_condition_ladder.py` — script-style, with a real
   `__main__` that calls every test. It shells `node -e`, stubs `globalThis.window`,
   loads `static/js/fleet-dock.js` as-is (the IIFE assigns to `window`), and
   asserts on `runnerConditions` output as JSON. Cases: each rank wins over the
   one below it; `waiting_on_you` beats `silent`; unknown output age yields
   `running_unknown` and never `Silent`; dirty count is secondary, never
   primary; sort is rank-then-age.
2. A source assertion that `_dockRunnerHtml` no longer renders
   `runner_session_id` on the card face and no longer emits a bare `Kill`
   button outside the overflow.
3. Playwright render check against the dock (`UI_PLAYWRIGHT_REPORT` is already
   a CI artifact), one runner per condition from a seeded fixture.

No pytest fixtures anywhere in (1) or (2). A test file that CI cannot execute is
worse than no test, because it reports PASS.

## Shared list structure (all three tabs)

Approved 2026-07-28 and now assumed by every tab. Each tab groups its cards into
ladder buckets, worst bucket first, with a count in the bucket header. Anything
healthy collapses into one summary row that expands on click. The dock shows
exceptions; the Autopilot page stays the full inventory.

The tab strip gains a tone dot per tab, coloured by that tab's worst condition,
so a closed tab still reports. Today it shows counts only, which means you must
open each tab to find out where the problem is.

## Tab 2 — pull requests

`prConditions` already ranks PR conditions well and is not changing. Three
things are:

1. **Task first.** The card leads with the GitHub PR title and buries the board
   task in the mono footnote. The task is the unit of work; the PR is the
   artifact. Task id and title move to the headline for parity with runners.
2. **The merge queue renders as a queue.** `fetch_merge_queue_positions` returns
   real 1-based positions, which `prConditions` flattens into a per-card
   `Queued #N` chip; the list then sorts blocked-first, so positions scatter
   down the stack. They become one ordered block showing what lands next.
3. **Actions.** The card has none today — every condition it diagnoses requires
   leaving the dock. Three actions, three different mechanisms, and the
   difference is load-bearing:

| Action | Mechanism | Status |
|---|---|---|
| `Decide` | `POST /api/attention/requests/{id}/decide` | browser-callable today; same path as the runner Answer |
| `Merge` | `gh pr merge --squash --auto`, server-side | path exists (`merge_coordinator.py:452`); needs a route |
| `Re-gate` | audited agent dispatch | `/ixp/v1/external_ci_mirror/request` needs a `source_path` and `write:ixp`; the browser has neither, so it must create a task and dispatch, like `/api/deployments/request` |

`Merge` shows only when the PR is genuinely mergeable and is absent when the PR
is already in the queue — the queue owns those, and a second control would fight
it. It confirms first, and the confirmation states that autodeploy ships master
to production within two minutes, because merging from a floating card is a
production event.

`classify()` computes `blocked_reason` (`open_prs.py:153`) and nothing renders
it; the ladder recomputes labels from raw fields instead. Surface it on the card
so a blocked PR says why.

**Not in scope:** forcing a red PR through. Branch-protection rulesets on this
repo may not honour admin bypass, in which case a force-merge fails at GitHub
rather than at us — that is an `enforce_admins` change, not a UI feature. Verify
against live settings before designing anything on it.

## Tab 3 — deploy

Autodeploy (`projectplanner-autodeploy.timer`) fetches every 2 minutes and ships
master whenever it moves. So "production is current" is the resting state and
this tab should normally be almost empty. Today it renders N un-deployed PRs
each with its own Deploy button — and every one of those buttons posts a
`pr_number` while deploying `canonical_sha`
(`src/switchboard/api/routers/board.py:120`), with task dedupe on
`[deploy <sha12>]`. N buttons, one repo-wide action.

Replaced by one banner reading from `health_view`, all of it currently unread:
`running_sha` → `canonical_sha`, `commits_behind`, `last_deploy_at`,
`last_deploy_ok`, and `checked_at`. Three states:

| State | Signal | Treatment |
|---|---|---|
| current | `commits_behind == 0` | green line, no action |
| behind, shipping | behind, `last_deploy_ok` not false, `checked_at` recent | yellow, no action — autodeploy has it |
| autodeploy failing | behind, `last_deploy_ok == false` | red, break-glass dispatch |

The third is the only one that needs a human and the only one today's UI cannot
express: it renders as "you have six things to ship" when the truth is "the
automation has failed for three hours and nothing is reaching production".
`checked_at` is what proves the timer is still alive.

The break-glass button is labelled "Dispatch deploy agent" because that is
literally what it does — the browser never receives shell or systemd authority.
The PR list below becomes a manifest of what ships, not a row of actions.

**Gap:** manifest rows have no board-task join. `open_prs` does a `_board_join`;
`deployment_status` does not. Adding the task id to each row needs that join.

## Out of scope

- Forcing a red PR through (see tab 2).
- Renaming internal identifiers (see the rename section).
- Fixing `tests/test_ui_deployment_fleet_tab.py`, which uses pytest fixtures
  with no `__main__` and therefore asserts nothing under CI. Found while
  confirming the test convention; a real defect, but a separate task.
