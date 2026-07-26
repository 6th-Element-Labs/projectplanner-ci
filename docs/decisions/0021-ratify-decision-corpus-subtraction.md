# ADR-0021 — Ratify the decision corpus and pay its subtraction debt

- **Status:** Accepted
- **Date:** 2026-07-26
- **Owner:** Task Execution / Coordinator (COORD-59)
- **Relates to:** [ADR-0006](0006-control-plane-done-enough.md) ·
  [Decision corpus spec](../DECISION-CORPUS-SPEC.md) · COORD-50 · COORD-60 ·
  RECON-12 · SESSION-18

## Context

ADR-0006 forbids adding a coordination mechanism without deleting an overlapping one. It also
states that every parked mechanism without a defender becomes a deletion. COORD-50 shipped Phase 1
of the decision corpus, so the record required by the decision corpus spec must exist before Phase 2
proceeds.

Phase 1 does not create authority. `decision_records` gates nothing, routes nothing, and cannot
block or unblock work. It is storage in the category ADR-0006 explicitly preserves: “supporting
ledgers (activity log, git_state) are storage, not mechanisms; they stay.” The subtraction debt
therefore belongs to Phase 2, where the replacement capabilities begin, and Phase 2 must pay that
debt by deleting the mechanisms it replaces.

## Decision

The decision corpus is ratified, subject to the following binding verdicts:

| Mechanism | Verdict | Delivery |
|---|---|---|
| RECON-8 event replay (`test_event_replay.py`, `replay_verify_batch`) | **Delete.** Generic board-event replay is superseded by replay against the pure completion classifier. It has no named defender. | COORD-60 deletes it while shipping its replacement; the old and new replay mechanisms must not stand together. |
| RECON-9 coordination receipts (`coordination_receipts.py`, `receipt_projection_batch`) | **Delete.** The parked mechanism remains undefended and duplicates evidence already held by the activity log and reconcile paths. | RECON-12, owned by **Reconciliation / Control Plane**. |
| `get_preflight_calibration` bespoke recommender | **Delete by absorption.** Its narrower predicted-vs-actual recommendations have no consumer; there will be one signature-calibration path, not two recommendation mechanisms. | SESSION-18, owned by **Work Sessions / Decision Calibration**. |

“Tracked for later” is not a verdict. Each mechanism is decided here; the separate tasks only
decouple delivery.

## Subtraction accounting

The accounting uses the same numerical form as ADR-0006 Consequences:

- Mechanisms deleted: **3**
- Mechanisms added: **2** — one signature-calibration path (a merge of the existing preflight
  calibration path) and one non-convergence circuit breaker
- Net authority change: **−1**

The signature-calibration path consolidates an existing mechanism rather than creating an
additional authority. The single genuinely new authority is the circuit breaker, and it is
strictly stop-authority: it may stop dispatch and file attention, but cannot authorize progress,
route around a gate, or block/unblock board work directly.

## Consequences

- COORD-60 may proceed and must delete RECON-8 as part of delivering classifier replay.
- RECON-12 and SESSION-18 own the two unrelated deletions; neither is bundled into COORD-60.
- Phase 2 violates this ADR if it ships a replacement while its overlapping predecessor remains.
- If the deletion work is abandoned or silently left parked, Phase 1 has unpaid subtraction debt
  and is grounds for reversion under the decision corpus spec.

## Alternatives rejected

- **Keep a parked mechanism without a named defender.** This recreates the indefinite, unowned
  state ADR-0006 was adopted to end.
- **Bundle all three deletions into COORD-60.** Coordination receipts and preflight calibration are
  unrelated to replay and deserve independently owned changes.
- **Treat Phase 1 storage as a new authority.** The ledger cannot decide or gate anything; counting
  it as authority would erase ADR-0006's explicit storage/mechanism distinction.
