# Architecture decision register

This register answers which decisions govern work now and which records are retained for
rationale, program history, or a proposal. It does not rewrite ADR history.

Statuses below describe the current repository audit as of 2026-07-26. Where file metadata
and repository evidence disagree, the register calls for reconciliation instead of inventing
authority.

## Fidelity and retention test

Keep an ADR in the active decision set when at least one is true:

- it assigns authority or ownership across architectural boundaries;
- it defines an invariant enforced by current code, schema, CI, or operations;
- reversing it would be costly and the rejected alternatives still matter;
- a current contract or runbook depends on its rationale.

Retain but demote an ADR from the active reading path when it is a completed program charter
whose durable rules have moved into code, tests, and current guides.

Call an ADR legacy only when it is explicitly superseded or retired. Preserve it with a
supersession link; do not delete or silently rewrite it.

## Cornerstone decisions

| Decision | Why it remains essential |
|---|---|
| [ADR-0003: work provenance and reconciliation](0003-work-provenance-and-reconciliation.md) | Its canonical-provenance invariant is active and reaffirmed by ADR-0006 and the working agreement. The file’s `Proposed` status needs reconciliation. |
| [ADR-0006: control plane done enough](0006-control-plane-done-enough.md) | Owns the subtraction rule, provenance consolidation, and stop condition for control-plane mechanisms. |
| [ADR-0008/three-plane](0008-three-plane-separation.md) | Owns capacity, communication, and coordination authority. This is the normative half of the protected [three-plane architecture packet](../COMPLETION-LIFECYCLE-PIPELINE.md). |
| [ADR-0019: repo constitution](0019-repo-constitution.md) | Owns code layout and front doors. REPO-6 merged and its required front doors now exist. |
| [ADR-0020: merge gates observe](0020-merge-gates-observe-not-enforce.md) | Owns the “enforce at dispatch, observe at merge, stamp at Done” authority boundary. |

## Active or partly active decisions

| Decision | Current disposition |
|---|---|
| [ADR-0004: adoption and enforcement](0004-adoption-and-enforcement.md) | Its adapter tiers and connect-time contract explain current behavior, but `Proposed` no longer describes how heavily accepted documents rely on it. Status review required. |
| [ADR-0007: application shell](0007-application-shell-cleanup.md) | Mixed but active. Caddy, code placement, typed boundaries, and frontend choices remain current. Decision 2’s exact size counter is explicitly retired. |
| [ADR-0008/narration-event](0008-narration-event-contract-and-delivery.md) | Accepted specialized contract with current implementation and tests. |
| [ADR-0009: microservices modernization](0009-microservices-modernization.md) | Structural rules remain active; Phase 0 sequencing and exit criteria are program history. |
| [ADR-0010: CI concurrency](0010-ci-concurrency.md) | Current CI principles with a mixture of shipped and historical lever status. |
| [ADR-0018/connect-communicate](0018-connect-communicate-plane-boundary.md) | Accepted logical boundary. |
| [ADR-0021: decision corpus subtraction](0021-ratify-decision-corpus-subtraction.md) | Accepted subtraction verdict. Its follow-on deletion tasks may complete later without making the decision historical. |

## Accepted program charters and as-built records

These record why current process boundaries and routing exist. Ordinary implementation
should start from `AGENTS.md`, Caddy, service units, and current contracts, then consult the
relevant charter when changing a boundary.

| Decision | Durable value |
|---|---|
| [ADR-0011: Phase 2 process strangler](0011-phase2-process-strangler.md) | Auth-first extraction rules and independence gate. |
| [ADR-0012: Phase 3 Tasks strangler](0012-phase3-tasks-process-strangler.md) | Tasks ownership and Mode A process boundary. |
| [ADR-0013: coordination/board strangler](0013-coord-board-process-strangler.md) | Coordination read boundary and cutover rationale. |
| [ADR-0014: deliverables/mission strangler](0014-deliverables-mission-process-strangler.md) | Deliverables read boundary and cutover rationale. |
| [ADR-0015: Tally/economics strangler](0015-tally-economics-process-strangler.md) | Tally boundary charter; current physical deployment remains code/config truth. |
| [ADR-0016: ingest/inbox strangler](0016-ingest-inbox-process-strangler.md) | Ingest/intake ownership and thin process boundary. |

## Proposed or unresolved decisions

These are not binding merely because they are in the ADR directory.

| Decision | Current disposition |
|---|---|
| [ADR-0001: multi-agent coordination primitives](0001-multi-agent-coordination-primitives.md) | Proposed sequencing record and historical precursor. Many capabilities later shipped under newer decisions; reconcile or supersede its status before treating it as current authority. |
| [ADR-0002: LLM cost attribution](0002-llm-cost-attribution.md) | Proposed design. Tally now carries broader cost-to-outcome behavior, but no ADR formally supersedes this record. |
| [ADR-0017: boundary delivery of ordinary messages](0017-boundary-delivery-of-ordinary-messages.md) | Proposed. Do not treat its unshipped requirements as current behavior without code/test evidence. |
| [ADR-0018/storage-ports](0018-storage-ports-backend-agnostic-data-layer.md) | The ADR says Proposed while its execution tracker says accepted. This status drift requires authoritative reconciliation. |

## Superseded or retired material

| Decision | Legacy scope |
|---|---|
| [ADR-0005: store module decomposition](0005-store-module-decomposition.md) | The shipped foundation remains useful history. The unfinished 17-step decomposition schedule is superseded by ADR-0006 and is legacy. |
| [ADR-0007 Decision 2](0007-application-shell-cleanup.md#decision-2--the-ratchet-the-promotion-adr-0006-pre-authorized) | Only the exact shared size-counter mechanism is retired. The whole ADR is not legacy. |

No other accepted ADR is classified wholesale as legacy by current repository evidence.

## Numbering collisions

The repository has two ADR-0008 files and two ADR-0018 files. Until a deliberate renumbering
and alias migration is approved:

- use the full filename in links;
- use the stable references `ADR-0008/three-plane`, `ADR-0008/narration-event`,
  `ADR-0018/connect-communicate`, and `ADR-0018/storage-ports`;
- do not renumber accepted files in place, because external references and historical
  evidence would silently change meaning.
