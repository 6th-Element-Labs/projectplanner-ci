# Autopilot Execution-Phase Boundary

- Status: Approved
- Date: 2026-08-09
- Scope: Mission Bot V4 assignments launched after an explicit Switchboard Start

## Decision

An immutable Mission Bot V4 execution assignment means planning and scope approval are complete.
The task contract and acceptance criteria are the approved implementation plan. The shared host
launch note will therefore tell every provider runtime to execute the assigned role without
re-running brainstorming, writing-plans, design approval, or routine scope approval workflows.

Execution-focused practices remain required where applicable: investigation, systematic
debugging, test-driven development, review, and verification. If execution reaches a genuine
authority boundary that cannot be resolved from the assignment or persisted project policy, the
runner must use the typed `agent_requires_human` route with the concrete missing decision or
permission. Assistant prose is not an authority request.

Before Switchboard Start, interactive planning remains unchanged. This boundary applies only when
the launcher receives the existing immutable execution contract.

## Placement

The rule belongs in `switchboard.connect.launcher.assignment_note`, the shared Mission Bot V4
translation seam used to launch provider CLIs from the same server-owned execution assignment.
It is derived from the presence of that assignment, so it applies across projects, hosts, roles,
and supported runtimes without adding project flags or provider-specific prompt branches.

## ADR-0008 compliance

- W2 remains the execution fence: explicit Start provides the immutable assignment.
- W3 remains intact: this change does not add a scheduler or roaming work driver.
- W4 remains intact: runners still use the existing role handoff and completion paths.
- M1 remains intact: prose does not acquire lifecycle authority.
- No assignment field, status, owner, gate, or lifecycle transition is added.

## Rejected alternatives

- Editing the installed Superpowers plugin globally would also remove planning help before scope
  and would be overwritten by plugin updates.
- Adding a per-project or per-host toggle would make the rule inconsistent and fail the requirement
  for current and future projects.
- Adding a new execution-contract field would change the immutable wire fingerprint even though
  the existing contract already proves the work is past planning.

## Proof

A focused regression test will prove that:

- assignments with an immutable execution contract carry the execution-only instruction;
- the instruction forbids routine approval questions while retaining typed exceptional escalation;
- both Codex and Claude provider translations receive it;
- unscoped Connect notes without the contract remain unchanged.
