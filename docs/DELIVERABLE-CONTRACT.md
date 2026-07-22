# Deliverable Contract and Review Room

`switchboard.deliverable_contract.v1` is the sole normative authority. Published revisions are immutable snapshots identified by both a monotonically increasing revision and a SHA-256 hash of canonical UTF-8 JSON. `switchboard.deliverable_brief.v1` is derived display data and has no policy authority.

Contract, delivery, and acceptance are independent lifecycles. Contract decisions are `approve_contract`, `request_changes`, `defer`, and `no_go`; Acceptance Review additionally supports `accept`. Every binding decision supplies the expected revision and hash and fails with `stale_revision` unless both match the latest published revision.

The `lite` profile carries outcome, acceptance criteria, and owner. The `full` profile additionally requires milestones and proof requirements and may carry rationale, risks, stakeholders, and policy constraints. Changes to outcome, acceptance, constraints, ownership, proof, milestones, risks, stakeholders, policy, or profile are material; title-only edits are not.

Legacy fields migrate reversibly as follows: `id→contract_id`, `end_state→outcome`, `owner_person_or_role→owner`, while title, acceptance criteria, milestones, proof requirements, why-it-matters, and policy constraints retain their names. The baseline adapter is intentionally lossless for this mapped subset.

All schemas and policy functions are transport-neutral. Policy evaluation is deterministic code; no LLM or generated brief participates in normalization, hashing, lifecycle transitions, stale-revision checks, waiver checks, or acceptance decisions.
