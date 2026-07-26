# Shared vocabulary conformance inventory

This inventory records duplicated vocabularies that cross an emitter/validator or
producer/consumer boundary. A vocabulary is guarded when a test derives values from
both sides and fails on drift.

## Guarded

| Vocabulary | Producer / emitter | Consumer / validator | Guard |
|---|---|---|---|
| Completion-run state | completion classifier | `completion_runs.STATES` | `tests/test_bug184_completion_state_vocabulary.py` |
| Coordinator `reason_code` | coordinator decision producers | decision-corpus registry | `tests/test_coord50_decision_corpus.py` |
| Required CI status contexts | canonical scratchpad `verify.yml` | public-CI `verify.yml` contract | `tests/test_coord57_required_context_vocabulary.py` |

## Knowingly not guarded

| Vocabulary | Why not yet guarded |
|---|---|
| GitHub webhook action strings | GitHub owns the source vocabulary; unknown actions are intentionally ignored and retained in receipts rather than rejected against a copied enum. |
| GitHub check/status conclusions | Provider-owned and normalized at the adapter boundary; normalization tests cover behavior, but there is no second local authoritative enum to compare. |
| MCP tool names advertised in session prompts | Generated from the registered tool surface at runtime; a copied static list would create the same defect class this inventory is intended to prevent. |

The public-CI workflow lives in a separate repository, so this repository keeps a
minimal contract snapshot of its context declarations. Reconciliation must update the
live workflow and this snapshot together. More importantly, the public-CI workflow runs
the reusable checker against its own workflow and the checked-out canonical workflow
before executing the suite, so drift in either repository fails the recovery run loudly.
The checker can also compare two full workflow files directly:

```shell
python scripts/check_ci_workflow_contexts.py canonical-verify.yml public-ci-verify.yml
```
