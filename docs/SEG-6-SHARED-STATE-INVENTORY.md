# SEG-6 shared-state inventory

Project-sensitive data must enter application code as a validated `ProjectContext` and reach
storage with an explicit project id. Global data is limited to control-plane registry,
authentication configuration, process configuration, and immutable static assets.

| Surface | Classification | Enforced boundary |
|---|---|---|
| Contacts and people | Project | `activity` repository project database |
| Activity deltas | Project | `activity_since(..., project=)` |
| Digests and digest metadata | Project | `project_digest` command + digest repository |
| Notification recipients | Project | `notify.send(..., project=)` + communications config |
| Inbox rows and dedupe | Project | existing per-project inbox repository |
| Board/signals/mission read cache | Project | project-bearing keys, hard entry cap |
| Dynamic RAG cache | Project | per-project keys, version invalidation, LRU project cap |
| Gateway attribution metadata | Project | digest request metadata includes project |
| Exports and audit reads | Project | explicit-project REST/application query boundaries |
| Timers, narrators, summarizers, reconciliation | Project | explicit project arguments at job entry |
| Project registry, auth grants, process config | Global | control-plane database/configuration |

`scripts/seg6_scope_ratchet.py` fails when a sensitive digest read loses its explicit
`project=` argument. Project-cardinality and cache-bound proofs live in
`tests/test_seg6_project_shared_state.py`.
