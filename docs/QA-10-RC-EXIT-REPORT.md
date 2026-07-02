# QA-10 Release-Candidate Exit Report

Date: 2026-07-02T19:44:15Z / 2026-07-03T07:44:15+1200 Fiji
Project: Switchboard
Decision: GO to exit the release-candidate QA freeze after this report lands.

## Decision

Switchboard can resume planned feature and moat work. The QA freeze found real
faults, repaired them through normal branch/PR/provenance paths, and left no
current P0/P1 release-candidate regression open.

This is a GO with tracked follow-ups, not a claim that all product work is done.
The remaining items are non-blocking productization or cleanup tasks with owners:
`HARDEN-22`, `HARDEN-23`, `PROOF-2`, and the normal ACCESS/COORD/RECON/DISPATCH
roadmap queue.

## Baseline And Exit State

| Field | Freeze baseline | Exit state |
| --- | --- | --- |
| Timestamp | 2026-07-01T23:29:05Z | 2026-07-02T19:44:15Z |
| Default branch SHA | `61e53459ef0cd984a48765d07d85ced86dbb31b7` | `2540514189723f8e6b49ea78d7d03bf8870f28bb` |
| Deployed VM SHA | `61e53459ef0cd984a48765d07d85ced86dbb31b7` | `2540514189723f8e6b49ea78d7d03bf8870f28bb` |
| Open GitHub PRs | none | none |
| Service health | active services, `/health` OK | `projectplanner`, `projectplanner-mcp`, and `projectplanner-monitors.timer` active; `/health` OK |
| Pending monitors/acks | none | none after resolving superseded reconcile alert #105 |
| Reconcile | one expected low QA-1 in-progress no-head finding | only expected low QA-10 in-progress no-head finding before this report PR is pushed |

During QA-10, reconcile briefly surfaced a high
`canonical_main_sha_not_found` signal for `2540514189723f8e6b49ea78d7d03bf8870f28bb`.
Root cause: ACCESS-3 merged after QA-9 and advanced canonical main before the VM
checkout was fast-forwarded. The fix was to deploy `2540514` to
`/opt/projectplanner` and restart the web, MCP, and monitor services. Reconcile
then returned to the expected QA-10-only low finding.

## QA Battery Results

| Task | Result | Evidence |
| --- | --- | --- |
| `QA-1` Release-candidate freeze and baseline snapshot | Done | Offline evidence with corrected SHA-256 hash. Baseline comment captured origin/deploy SHA, services, projects, board rollups, agents, leases, monitors, webhook, reconcile, and debt. |
| `QA-2` Project isolation and permissions | Done | Scratch projects proved isolation. Found/fixed `BUG-19` and `BUG-20`; deployed at `0762a1556aa7aa2747f0e8c6d7fd5e2c27e2ae8c`. |
| `QA-3` PR-backed lifecycle | Done | Fixture PRs proved claim, In Review, PR open/merge, Done provenance, dependency unblock, and late-complete preservation. Fixed `BUG-12`; artifact `docs/QA-3-PR-LIFECYCLE-PROOF.md`. |
| `QA-4` Offline/non-PR provenance | Done | Proved evidence-required offline Done, invalid hash rejection, In Review prerequisite, idempotent replay, correction audit, MCP/API/UI provenance, and reconcile acceptance. |
| `QA-5` Messaging, identity, and wake | Done | Proved active delivery/ack, unreachable fallback, ack-timeout wake, unbound principal detection, identity-unbound takeover refusal, and bound-after-unbound recovery. |
| `QA-6` Operator controls | Done | PR #59, merged `c6f31a31113a7006798d716fb0df052cda6f6843`; added restart fail-closed coverage and verified stop/ack, revoke, runner snapshot/kill, and Done safety. |
| `QA-7` Reconcile/webhook cross-project audit | Done | PR #58, merged `4073f78cbf8be81effb447042b605a301b030c2c`; fixed explicit cross-repo PR URL reconcile behavior and separated historical Vulkan debt from current regressions. |
| `QA-8` UI/MCP/REST/board parity | Done | PR #61, merged `ee049e42ff99c896bafd1952333b5e90b12bbb25`; added full parity test for task truth, stale rationale, provenance, identity, monitors, and Tally fields. |
| `QA-9` Fail-early negative pass | Done | PR #67, merged `374cd9892f36e3ca23f944cc9e147ef4c84d53c2`; added 24-case negative pass and fixed structured offline-verification errors plus typed stale-rationale signals. |
| `QA-10` Exit report | In progress in this branch | This document records the freeze decision and will close through PR/default-branch provenance. |

## Bugs Fixed During The Freeze

| Bug/finding | Outcome |
| --- | --- |
| `BUG-12` late `complete_claim` after merge could briefly regress Done to In Review | Fixed and proved by QA-3 fixture. |
| `BUG-18` offline evidence hash could accept placeholder-looking provenance | Fixed with `invalid_evidence_hash` rejection and audited correction behavior. |
| `BUG-19` production web writes accepted missing bearer when auth mode was unset | Fixed; production services force `PM_AUTH_MODE=required`. |
| `BUG-20` local MCP write tests inherited production auth mode from `.env` | Fixed; tests declare their intended auth mode explicitly. |
| `BUG-21` reconcile could flood ancestry findings when canonical main was missing | Fixed; missing canonical main is a single loud blocked git check. |
| QA-7 cross-repo reconcile bug | Fixed; explicit PR URLs fetch from their URL repo, not only the project default repo. |
| QA-8 dependency normalization parity bug | Fixed at the store boundary and covered by surface parity tests. |
| QA-9 structured negative-path gaps | Fixed; REST offline verification preserves structured store errors, and stale generated rationales carry `failure_class` and `expected_signal`. |

## Deferred Non-Blocking Work

These items should stay visible, but they do not block exiting the QA freeze:

- `HARDEN-22`: pin PR gate Python/runtime selection instead of trusting ambient
  `python3`. This remains valuable hardening even though the current strict gate
  can be run with an explicit Python 3.12 runtime.
- `HARDEN-23`: stale session, claim, wake, and historical-proof cleanup lifecycle.
  QA-10 resolved one superseded reconcile alert; broader lifecycle cleanup belongs
  here.
- `PROOF-2`: existing blocked sentinel, historical proof fixture debt rather than
  a current release-candidate regression.
- ACCESS/COORD/RECON/DISPATCH/TALLY roadmap tasks: normal product and moat work
  that should resume after the QA-10 exit merge.

## Residual Risks

- The freeze boundary was not perfectly quiet: ACCESS-1, ACCESS-2, and ACCESS-3
  landed while the QA lane was still closing. They are deployed, tested, and not
  current blockers, but future freezes should enforce the queue pause more
  strictly or explicitly record approved exceptions.
- Some completed-agent registrations may remain visible until TTL expiry. There
  are no pending acks or pending monitors at exit, and `HARDEN-23` covers durable
  cleanup policy.
- QA-10 itself will produce a temporary low `progress_without_pushed_head`
  reconcile finding until this branch is pushed and completed with PR evidence.

## Go/No-Go Criteria

GO:

- All QA tasks before QA-10 are Done with merge or offline evidence provenance.
- All current P0/P1 QA findings discovered during the freeze are fixed, deployed,
  or explicitly classified as non-blocking debt.
- No open GitHub PRs remain at the time this report was drafted.
- Web, MCP, and monitor services are active on the deployed VM.
- `/health` returns OK.
- Pending ack/monitor queues are empty.
- Current reconcile has no high/critical current product finding after deploying
  `2540514`; only QA-10's expected pre-push low finding remains.

Recommended immediate next steps after the QA-10 merge:

1. Clear small hardening debt: `HARDEN-22`, then `HARDEN-23`.
2. Resume product/moat work from the ready queue: `COORD-1`, `RECON-8`, and then
   `DISPATCH-6` once replay/simulation evidence exists.
3. Keep the fail-and-fix-early rule active: any new red/yellow runtime,
   provenance, auth, or workflow signal should become a fix or a structured BUG
   report before more work builds on top of it.
