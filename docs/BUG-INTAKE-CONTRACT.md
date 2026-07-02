# Bug Intake Contract

Status: `BUG-1` P0 contract

Switchboard agents are expected to surface bugs as soon as they find them, but the system must
not turn every discovered bug into unsupervised implementation work. Bug intake is a triage lane,
not a dispatch bypass.

## Role

A Bug Intake Agent receives agent-discovered bugs, normalizes them into reproducible reports,
deduplicates them against existing tasks, assigns a severity hint, and prepares approval-ready
conversion proposals.

It may file and triage `BUG` work automatically. It may not create, prioritize, dispatch, claim,
or wake implementation work outside the `BUG` lane unless a human operator or explicit
coordinator policy has approved that conversion.

## Required Bug Report Fields

Every submitted bug report should preserve the failing signal and include:

| Field | Required | Purpose |
|---|---:|---|
| `source_task` | Yes | Task where the bug surfaced. |
| `source_agent` | Yes | Reporting agent or runtime. |
| `observed_behavior` | Yes | What actually happened. |
| `expected_behavior` | Yes | What should have happened. |
| `repro_steps` | Yes | Minimal steps or command to reproduce. |
| `evidence` | Yes | Logs, PR, URL, file path, screenshot, trace, or command output summary. |
| `severity_hint` | Yes | Reporter estimate: `low`, `medium`, `high`, or `critical`. |
| `affected_surface` | Yes | UI, MCP, REST, adapter, reconcile, CI, docs, scheduler, auth, etc. |
| `failure_class` | Recommended | Missing data, broken connection, invalid input, stale branch, absent permission, malformed payload, failed gate, unreachable agent, unbound identity, hidden fallback. |
| `duplicate_of` | If known | Canonical `BUG-*` task. |

Missing required fields keep the bug in intake/needs-info state. The intake agent should ask the
reporting agent for the smallest missing piece instead of inventing data.

## Severity Rubric

| Severity | Meaning | Default disposition |
|---|---|---|
| `critical` | Can corrupt board truth, bypass approval/auth, mark false Done, lose work, or dispatch unsafe work. | Human alert and block dependent release work. |
| `high` | Breaks core coordination, CI/reconcile, task provenance, identity, or operator trust. | Triage immediately; conversion requires approval. |
| `medium` | Causes confusing UX, noisy state, flaky workflow, or localized adapter failure. | Triage and schedule by lane owner. |
| `low` | Cosmetic, docs-only, or minor friction with no false-green risk. | Batch unless it blocks active work. |

The reporter's `severity_hint` is not final. The intake agent may lower or raise it, but must
record why.

## Dedupe Rules

Two reports are duplicates when they share the same affected surface, same root failure, and same
fix owner, even if they appeared in different tasks. The canonical BUG task keeps:

- the earliest observed signal;
- every duplicate report link;
- each distinct reproduction path;
- the current severity and rationale;
- the proposed target lane, if any.

Duplicates should not spawn additional implementation work.

## Approval States

Bug intake uses these states:

| State | Meaning |
|---|---|
| `new` | Structured report exists but has not been triaged. |
| `needs_info` | Required evidence is missing. |
| `duplicate` | Linked to a canonical BUG task. |
| `triaged` | Severity, owner surface, and repro quality are set. |
| `conversion_proposed` | Intake recommends implementation work in another lane. |
| `approved` | Human/coordinator policy approved conversion. |
| `rejected` | Human/coordinator declined conversion. |
| `deferred` | Valid bug, not scheduled now. |

Only `approved` conversion may create claimable implementation work.

## Human Gate

Converted work must carry a structured `human_gate` state until approved:

```json
{
  "required": true,
  "source_bug_task_id": "BUG-123",
  "target_workstream": "HARDEN",
  "severity": "high",
  "approval_reason": "Why this bug should become implementation work",
  "approved_by": "switchboard/operator",
  "approved_at": "2026-07-02T00:00:00Z"
}
```

Before `approved_by` is present, `claim_task` returns `human_approval_required` and `claim_next`
skips the task under `dispatch_reason.skipped.human_approval`. This prevents intake from becoming
a hidden dispatch path.

## Conversion Policy

When a bug is approved for implementation, the converted task must preserve:

- source BUG task id;
- reporter and source task;
- severity and affected surface;
- evidence or artifact references;
- duplicate links;
- target workstream;
- acceptance criteria;
- approver and approval time;
- rationale for why implementation is warranted now.

Rejected and deferred bugs remain auditable in BUG intake. They are not deleted and do not enter
`claim_next`.
