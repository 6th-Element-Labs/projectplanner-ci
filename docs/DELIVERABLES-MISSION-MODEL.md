# Deliverables And Mission Model

Switchboard now has a product-outcome layer above boards, workstreams, and tasks.

The core rule is:

```text
Boards own execution.
Deliverables own outcomes.
```

Tasks still live in exactly one project database and one workstream. A deliverable lives in one
owning project database, but it may link tasks from any project through explicit
`project_id + task_id` references. This lets an operator track a mission such as "Helm C++ + WebGPU
Renderer" across `helmrenderer`, `helm`, and `vulkan` without moving tasks or cross-polluting board
state.

## Data Model

`deliverables`

- `id`: stable outcome id, such as `helm-cpp-webgpu-renderer`
- `title`
- `status`: `proposed`, `approved`, `in_progress`, `blocked`, `in_review`, `done`, or `archived`
- `owner_org`
- `owner_person_or_role`
- `end_state`: plain-English description of what exists when the mission ships
- `why_it_matters`
- `confidence`: optional `0.0` to `1.0`
- `acceptance_criteria_json`
- `policy_constraints_json`
- `proof_requirements_json`
- `kpi_links_json`
- `metadata_json`

`deliverable_milestones`

- `id`: scoped under the deliverable when generated, such as
  `helm-cpp-webgpu-renderer:build-webgpu-ingest`
- `deliverable_id`
- `title`
- `description`
- `status`: `not_started`, `in_progress`, `blocked`, `in_review`, `done`, or `skipped`
- `sort_order`
- `acceptance_criteria_json`
- `proof_requirements_json`

`deliverable_task_links`

- `deliverable_id`
- `milestone_id`
- `project_id`
- `task_id`
- `role`
- `blocks_deliverable`
- `proof_required_json`
- `metadata_json`

`project_id` is always explicit. Unknown projects fail closed. Linked tasks are validated by
reading the target project directly; the link operation does not mutate the target task or write
into the target project database.

## Progress Semantics

Mission progress is derived from linked task state:

- `Done` counts as done only when the task has terminal provenance.
- `In Review` is reported separately.
- `Blocked` is reported separately.
- External CI mirror proof is reported separately as required/passed/blocked counts when a
  deliverable link or task gate requires `external_ci_passed`.
- Missing or unreachable linked task snapshots remain visible as errors.

This mirrors Switchboard's Done policy: green means merged/proven, not "an agent said it was done."
Public CI proof can satisfy a review/verification gate for a private source SHA, but it is not
merge provenance and does not make a task Done.

## Example: Helm C++ + WebGPU Renderer

Owning project: `helmrenderer`

End state:

```text
Helm renders chart layers in the browser from shared C++ nautical semantics, with WebGPU visible to
users and deterministic fixture parity proving the pipeline.
```

Milestones:

- Define shared render model.
- Export first deterministic fixture.
- Build WebGPU ingest.
- Integrate into Helm runtime.
- Prove parity and performance.
- Ship visible demo.

Linked tasks may come from:

- `helmrenderer`: integrated visible-renderer acceptance work
- `helm`: boat/runtime C++ policy and Helm web integration
- `vulkan`: backend-neutral renderer proof slices

## Example: Switchboard Access Rollout

Owning project: `switchboard`

End state:

```text
Multiple humans can safely access Switchboard, invite collaborators, scope agents to projects, and
provide feedback without gaining unauthorized project-creation or agent-dispatch powers.
```

Milestones:

- Auth and session protection.
- Org/user/project role model.
- Scoped MCP/API tokens.
- Project creation permissions.
- Human invites and management.
- Subscription/agent entitlement ledger.
- Feedback inbox to plan proposal flow.
- UI permissions and restricted controls.

## Next Surfaces

`DELIVERABLES-2` should expose this model through MCP tools:

- `create_deliverable`
- `get_deliverable`
- `list_deliverables`
- `add_deliverable_milestone`
- `link_task_to_deliverable`
- `unlink_task_from_deliverable`
- `mission_status`

Later tasks should add breakdown proposal approval, deliverable-aware scheduling, Mission Page UI,
generated narrative, coordinator loops, and KPI/cost rollups.
