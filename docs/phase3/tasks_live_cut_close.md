# Tasks live cut — Path A closeout (ARCH-MS-95)

**Decision:** Path A live cut is **closed** for deliverable `arch-ms-tasks-live-cut`
after operator G6 (ARCH-MS-94).

| Field | Value |
|---|---|
| Board task | **ARCH-MS-95** (this closeout) |
| Operator G6 | ARCH-MS-94 / PR #526 |
| Cut execution | ARCH-MS-92 / PR #525 (Caddy + dual-strip + unit) |
| Parity drill | ARCH-MS-91 / PR #524 (superseded as live-cut gate; retained as proof) |
| Package | ARCH-MS-90 / PR #522 |
| Verdict | [`tasks_independence_verdict.json`](tasks_independence_verdict.json) |
| Playbook | [`tasks_cut_playbook.md`](tasks_cut_playbook.md) |
| Rollback | [`../runbooks/tasks-caddy-cutover-rollback.md`](../runbooks/tasks-caddy-cutover-rollback.md) |
| Exit gate | `scripts/arch_ms_phase3_exit_gate.py` → Path A green |

## AC (ARCH-MS-95)

1. **Caddy live routes Tasks** — `deploy/Caddyfile` Mode A `/api/tasks*` + claim-only TXP → `:8122`.
2. **Day-one surface not on monolith** — `PM_TASKS_HTTP_PRIMARY=service`; monolith mounts `sibling_bc_only`.
3. **Gate Path A green** — independence Go+G6 + path_a_tasks_cut.

## Supersedes (live-cut scope)

For deliverable `arch-ms-tasks-live-cut` / milestone `finish-path-a`:

- **ARCH-MS-92** remains the cut *execution* commit; ARCH-MS-95 is the post-G6
  *close* that the segmentation mission tracks.
- **ARCH-MS-91** parity stays as supporting evidence; it is not the live-traffic gate.
