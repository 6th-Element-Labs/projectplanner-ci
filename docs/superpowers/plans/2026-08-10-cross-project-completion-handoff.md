# Cross-Project Completion Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve an exact managed completion handoff when an Agent Host enrolled in one project completes work in a granted target project.

**Architecture:** Replace target-local host-principal classification with the existing project-grant-aware `check_agent_host_identity` authority. Make terminal completion resolve that authority directly, and fence generic orphan cleanup whenever a valid completion handoff remains pending.

**Tech Stack:** Python 3.12, SQLite repositories, direct executable regression tests, Switchboard code-strict workflow.

## Global Constraints

- Do not change Autopilot behavior.
- Preserve execution generation, host, role, head, and lease-epoch fencing.
- Preserve GitHub/default-branch provenance as the only code Done authority.
- Do not require duplicate host principal or enrollment rows in a granted target project.
- A pending completion handoff must never be archived or released as an orphan.

---

### Task 1: Cross-project terminal completion regression

**Files:**
- Create: `tests/test_bug337_cross_project_completion_handoff.py`
- Create: `tests/test_bug337_agent_host_pending_receipt.py`
- Modify: `src/switchboard/storage/repositories/runner.py`
- Modify: `src/switchboard/storage/repositories/claims.py`
- Modify: `adapters/agent_host.py`

**Interfaces:**
- Consumes: `check_agent_host_identity(host_id, principal_id, project)` and the existing `complete_claim`/runner terminal-receipt contracts.
- Produces: project-grant-aware runner authority, direct terminal acknowledgment authorization, and a typed pending-handoff response that prevents generic cleanup.

- [x] **Step 1: Write the failing end-to-end regression test**

Create a source-only Host enrollment, a target grant, a managed target task/Work Session/claim/runner, and a valid completion handoff. Assert that the current target-local host test incorrectly produces orphan cleanup. Add a failure-path assertion that a temporarily invalid stopping lease leaves the claim and Work Session owned.

- [x] **Step 2: Run the regression test and verify RED**

Run:

```bash
PYTHONPATH=. /Users/steveridder/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 tests/test_bug337_cross_project_completion_handoff.py
```

Expected: fail because the terminal receipt archives/releases the valid target-project completion instead of reaching `In Review`.

- [x] **Step 3: Replace target-local host classification**

In `_upsert_runner_session_in`, remove the local `principals`/enrollment prerequisite. Resolve host authority with:

```python
host_identity = check_agent_host_identity(
    submitted_host, principal_id, project=project)
host_authorized = bool(
    host_identity.get("required") is True
    and host_identity.get("allowed") is True)
```

Fail explicitly when host identity is required but denied. Retain registered-host and existing-runner ownership comparisons.

- [x] **Step 4: Make terminal acknowledgment own its authority check**

Remove `narrow_host` from `terminal_ack_claim_completion_in`. Resolve the persisted runner's host identity with `check_agent_host_identity` and require `required=true`, `allowed=true` before evaluating the existing terminalizer, host, principal, generation, epoch, claim, and stopping-lease checks.

- [x] **Step 5: Fence generic cleanup behind pending completion**

When `completion_handoff` is present and terminal finalization does not complete, skip yielded cleanup, terminal-task cleanup, Work Session archival, and `_release_terminal_runner_ownership_in`. Return a typed pending result and record one auditable pending-ack activity event.

Keep the Agent Host's persisted stop receipt whenever the response contains `error` or `error_code`. Keep terminal unacknowledged handoffs in bounded pending discovery, and apply the same cleanup fence to verified-kill completion.

- [x] **Step 6: Run the regression test and verify GREEN**

Run the Step 2 command. Expected: PASS with target task `In Review`, completed claim/Work Session, canonical PR/head, and zero orphan-release activity.

- [x] **Step 7: Run focused adjacent regressions**

Run:

```bash
PYTHONPATH=. /Users/steveridder/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 tests/test_bug326_multi_project_agent_host_auth.py
PYTHONPATH=. /Users/steveridder/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 tests/test_bug154_complete_claim_runner_lease.py
PYTHONPATH=. /Users/steveridder/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 tests/test_bug172_exact_generation_fence.py
git diff --check
```

Expected: all commands exit 0.

- [x] **Step 8: Run the canonical validation gate and commit**

Run `scripts/switchboard_ci.sh`, inspect the complete result, then commit the tested code and documentation with message `fix(BUG-337): preserve cross-project completion handoff`.
