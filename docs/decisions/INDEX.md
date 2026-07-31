# Architecture decision register

This register identifies the small set of decisions that governs work now while
preserving superseded proposals, completed programs, and incident rationale.
The corpus was reviewed against repository source, current documentation, and
live Switchboard deliverables on 2026-07-31.

Historical retention does not imply current authority. Start from the active
set, then follow a historical record only when changing the boundary it explains.

## Decision test

Keep a record active when at least one is true:

- it assigns durable authority or ownership across architectural boundaries;
- it defines an invariant enforced by current code, schema, CI, or operations;
- reversing it would be costly and its rejected alternatives still matter; or
- a current product contract or runbook depends on its rationale.

Execution sequences, route inventories, ports, temporary gates, and completed
program exit criteria belong in specs, configuration, runbooks, evidence, or the
board. They do not each require a permanent ADR.

## Active decisions

| Decision | Current authority |
|---|---|
| [ADR-0003 — Work provenance and reconciliation](0003-work-provenance-and-reconciliation.md) | Git/default-branch-proven Done, evidence-backed completion into In Review, squash-aware reconciliation, and the working agreement. Ratified from Proposed on 2026-07-31. |
| [ADR-0006 — Control plane done enough](0006-control-plane-done-enough.md) | Subtraction rule, provenance ownership, and the control-plane stop condition. Its kill list and horizons are retained program history. |
| [ADR-0008/three-plane](0008-three-plane-separation.md) | Capacity, communication, and coordination authority: C1–C3, M1–M3, and W1–W4. This is the protected normative half of the [three-plane architecture packet](../COMPLETION-LIFECYCLE-PIPELINE.md). |
| [ADR-0019 — Repo constitution](0019-repo-constitution.md) | Checkout layout and front-door truth, distinct from repository topology and Done authority. |
| [ADR-0020 — Merge gates observe](0020-merge-gates-observe-not-enforce.md) | Enforce at dispatch, observe at merge, stamp at Done; GitHub owns landing. |
| [ADR-0023 — Thin merge-queue CI](0023-thin-merge-queue-ci.md) | One trusted workflow, one context, native queue ownership, enqueue-once Autopilot, and the audited repair boundary, as amended by ADR-0024. |
| [ADR-0024 — Fast admission and Markdown queue lanes](0024-merge-queue-admission-and-docs-lanes.md) | Bounded PR admission, full merge-group verification by default, fail-closed Markdown classification, and no agent rebase loop solely because the base advanced. |
| [ADR-0025 — Bounded-context service extraction](0025-bounded-context-service-extraction.md) | One reusable independence, No-Go, cutover, rollback, and evidence policy for all process cuts. |
| [ADR-0026 — Compand benchmark publication](0026-compand-benchmark-publication.md) | Claims cannot exceed evidence; complete tasks are the economic denominator; publication and reproducibility gate certification and Value Index movement. |

## Current specifications and contracts

These documents may be normative, but they are product or protocol contracts
rather than repository-wide architecture decisions:

| Contract | Scope |
|---|---|
| [Narration event contract](../NARRATION-EVENT-CONTRACT.md) | Transactional narration outbox, envelope, delivery, retries, retention, and publication semantics. Historical links continue through [ADR-0008/narration-event](0008-narration-event-contract-and-delivery.md). |
| [Tally specification](../TALLY-SPEC.md) | Cost-to-outcome ledger, confidence, verified outcomes, and KPI links. |
| [Switchboard Connect](../SWITCHBOARD-CONNECT.md) | Content-blind capacity/launch contract and host-local runtime configuration under the three-plane architecture. |
| [Compand Benchmark Publication Standard — CES-1](../COMPAND-BENCHMARK-STANDARD.md) | Experimental arms, statistics, hard gates, evidence release layout, reproduction, and certification procedure governed by ADR-0026. |

## Unresolved decisions and debt

| Record | Required disposition |
|---|---|
| [ADR-0021 — Decision corpus subtraction](0021-ratify-decision-corpus-subtraction.md) | Accepted deletion verdict with unresolved implementation debt. `coordination_receipts.py`, `receipt_projection_batch`, and `get_preflight_calibration` were still present at the 2026-07-31 audit. Archive only after executable removal or explicit supersession evidence. |
| [ADR-0018/storage-ports](0018-storage-ports-backend-agnostic-data-layer.md) | Retired unaccepted proposal. Its board deliverable is absent and its documented storage leaks remain. Fresh scoping and acceptance are required before revival. |

## Superseded and historical records

| Decision | Why it is not current authority |
|---|---|
| [ADR-0001 — Multi-agent coordination primitives](0001-multi-agent-coordination-primitives.md) | Early phased precursor; current leases, messaging, provenance, and liveness authority evolved under later decisions. |
| [ADR-0002 — LLM cost attribution](0002-llm-cost-attribution.md) | Superseded by the broader Tally specification. |
| [ADR-0004 — Adoption and enforcement](0004-adoption-and-enforcement.md) | Runtime-adapter lineage; current behavior lives in the working agreement, IXP/adapter contracts, server enforcement, and three-plane architecture. |
| [ADR-0005 — Store decomposition](0005-store-module-decomposition.md) | Foundation shipped; remaining horizontal decomposition plan was retired by ADR-0006. |
| [ADR-0007 — Application shell](0007-application-shell-cleanup.md) | Mixed cleanup and modernization record; surviving code-placement and edge rules now live in `AGENTS.md`, ADR-0019, and ADR-0025. |
| [ADR-0008/narration-event](0008-narration-event-contract-and-delivery.md) | Stable historical alias for the current narration protocol specification. It is unrelated to ADR-0008/three-plane. |
| [ADR-0009 — Modernization Phase 0](0009-microservices-modernization.md) | Archived completed program charter; durable process-cut policy consolidated into ADR-0025. |
| [ADR-0010 — CI concurrency](0010-ci-concurrency.md) | Original traffic-jam program; current CI and queue behavior is owned by ADR-0023, ADR-0024, and `CI-STRATEGY.md`. |
| [ADR-0011 — Auth process strangler](0011-phase2-process-strangler.md) | Completed service charter consolidated into ADR-0025. |
| [ADR-0012 — Tasks process strangler](0012-phase3-tasks-process-strangler.md) | Completed service charter consolidated into ADR-0025. |
| [ADR-0013 — Coordination process strangler](0013-coord-board-process-strangler.md) | Completed service charter consolidated into ADR-0025. |
| [ADR-0014 — Deliverables process strangler](0014-deliverables-mission-process-strangler.md) | Completed service charter consolidated into ADR-0025. |
| [ADR-0015 — Tally process strangler](0015-tally-economics-process-strangler.md) | Archived incomplete/No-Go process-cut charter; it does not prove a live Tally service. |
| [ADR-0016 — Ingest process strangler](0016-ingest-inbox-process-strangler.md) | Completed service charter consolidated into ADR-0025. |
| [ADR-0017 — Ordinary-message boundary delivery](0017-boundary-delivery-of-ordinary-messages.md) | Unimplemented proposal retired; future delivery mechanics belong in a communication protocol specification. |
| [ADR-0018/connect-communicate](0018-connect-communicate-plane-boundary.md) | Logical boundary absorbed by ADR-0008/three-plane; product detail retained in `SWITCHBOARD-CONNECT.md`. |
| [ADR-0022 — One fail-closed CI verdict](0022-one-fail-closed-ci-verdict.md) | Incident-era CI decision superseded by ADR-0023, ADR-0024, and current CI strategy. |

## Numbering and stable references

The historical corpus contains two ADR-0008 files and two ADR-0018 files. Do
not renumber accepted history in place:

- use `ADR-0008/three-plane` and `ADR-0008/narration-event`;
- use `ADR-0018/connect-communicate` and `ADR-0018/storage-ports`; and
- link the full filename.

The unmerged Compand decision was renumbered from ADR-0024 to ADR-0026 during
this audit because accepted ADR-0024 already owned merge-queue admission and
Markdown lanes. No Compand canonical reference should use ADR-0024.

## Maintenance rules

- Add an ADR only for a durable choice with meaningful alternatives and
  consequences.
- Put coding rules in `AGENTS.md`, protocol mechanics in specs, operational
  procedures in runbooks, and temporary sequencing on the board.
- Never delete accepted decision history to make the corpus tidy. Add an
  explicit supersession or demotion record.
- When accepted architecture, executable enforcement, and current code
  disagree, expose the mismatch and assign resolution; do not silently choose.
