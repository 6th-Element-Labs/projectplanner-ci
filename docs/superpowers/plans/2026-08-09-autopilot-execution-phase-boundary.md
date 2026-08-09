# Autopilot Execution-Phase Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make every Mission Bot V4 assignment launched after Switchboard Start execute the approved scope without repeating Superpowers planning or routine approval prompts.

**Architecture:** Treat the existing immutable execution contract as the phase boundary. Add one shared execution-phase instruction in `switchboard.connect.launcher.assignment_note`, which already translates the same server-owned assignment for provider CLIs. Keep ADR-0008 lifecycle owners, the assignment schema, and pre-Start interactive planning unchanged.

**Tech Stack:** Python 3.12, script-style regression tests, Switchboard Connect/Mission Bot V4.

---

### Task 1: Lock the boundary with a failing test

**Files:**

- Create: `tests/test_coord128_autopilot_execution_phase_boundary.py`
- Test: `tests/test_coord128_autopilot_execution_phase_boundary.py`

- [ ] Build representative Codex and Claude Acks plus one immutable execution contract.
- [ ] Assert both launch notes say planning is complete, forbid planning/approval repetition,
      retain execution-focused practices, and require typed `agent_requires_human` for genuine
      authority exceptions.
- [ ] Assert a launch note without an execution contract does not carry the execution-only rule.
- [ ] Run the test and confirm it fails because the shared rule is absent.

### Task 2: Add the shared launch instruction

**Files:**

- Modify: `src/switchboard/connect/launcher.py`
- Test: `tests/test_coord128_autopilot_execution_phase_boundary.py`

- [ ] Add the minimal instruction only inside the immutable execution-contract branch.
- [ ] Keep the execution assignment schema and fingerprint unchanged.
- [ ] Run the focused test and confirm it passes.

### Task 3: Verify compatibility and publish

**Files:**

- Verify: `src/switchboard/connect/launcher.py`
- Verify: `tests/test_coord128_autopilot_execution_phase_boundary.py`

- [ ] Run the related immutable-assignment, provider-parity, thin-launcher, stale-assignment,
      completion-admission, and Mission Bot V4 tests.
- [ ] Run `bash scripts/switchboard_ci.sh` and `git diff --check`.
- [ ] Review the final diff for ADR-0008 and Mission Bot V4 compliance.
- [ ] Commit, push, open the canonical PR, and record executed-test evidence.
- [ ] After the canonical change lands, promote the Agent Host bundle through the existing release
      path and verify a live Autopilot assignment passes the former approval stop.
