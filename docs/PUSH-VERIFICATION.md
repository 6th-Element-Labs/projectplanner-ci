# Push verification — closing the silent-failed-push leak

## Symptom

Worked tasks (BUG-31, BUG-32, HARDEN-*, …) never turned blue/green on the board.
The board color was honest: the work never landed on the ledger. Agents wrote
code into local git worktrees but never completed the two-part handshake the
working agreement requires — **claim the task** (→ In Progress) and **push the
branch** (→ merge provenance → Done). Failed pushes were silent.

## Root cause (three layers)

1. **Prod host is message-only.** `adapters/agent_host.py:host_policy_from_env`
   returns `mode="message_only"` when `PM_AGENT_HOST_ALLOW_WORK` is unset — which
   it is on the prod box (`host/plan-vm-message-wake`). Every wake routes to
   `run_agent.py --inbox-only`, which does `handshake → read inbox → ack → exit`.
   It never calls `claim_next` and never touches git.

2. **Work-capable hosts claim-and-abandon without a work module.**
   `agent_host.py:launch_command` falls back to `--dry` whenever
   `PM_AGENT_WORK_MODULE` is empty. `--dry` claims then raises → abandons. No
   completion, no push.

3. **`complete_claim` never verified the push.** It stamped `pushed_at = now`
   from the mere presence of `head_sha` in agent-supplied evidence, with no check
   that the branch/SHA existed on the canonical remote. The managed loop
   (`switchboard_core.run_session`) made it worse by **fabricating**
   `remote_ref = refs/heads/<branch>` without ever pushing — the exact "hidden
   fallback" the fail-fix policy forbids. A committed-but-unpushed branch was
   recorded as pushed and moved to In Review.

Layer 3 is the universal one: it affects **every** agent that calls
`complete_claim`, including raw-CLI `claude` sessions that never go through the
adapter.

## The fix (this change)

`push_verification.verify_push_evidence` proves the branch/`head_sha` is actually
on the canonical remote via the GitHub API (no local clone; runs **outside** the
sqlite transaction so a slow call never holds the write lock). Policy —
*fail-closed, warn on unreachable*:

| Verification result | Meaning | complete_claim behavior |
|---|---|---|
| `present` | ref proven on the remote | proceeds to In Review |
| `absent` | remote reachable, ref is **not** there | **rejected** (`push_not_on_remote`, `failure_class: stale_branch`); claim stays active so a real push can retry; `task.complete_blocked_push` activity recorded |
| `unverified` | remote unreachable / no token / rate-limited | allowed, but flagged `push_unverified` in the response + git_state evidence for reconcile to re-check |
| `skipped` | no git evidence (docs/offline) or already merged | unaffected |

The managed loop (`run_session`) now performs a **real** `git push` + `ls-remote`
verification instead of fabricating `remote_ref`, and abandons (loudly) rather
than completing when the push fails. The loop also inspects the `complete_claim`
result and stops with `complete_rejected:` instead of looping on as if done.

## Rollout — `PM_VERIFY_COMPLETION_PUSH`

The whole behavior is gated by the env flag `PM_VERIFY_COMPLETION_PUSH`
(`1/true/yes/on`). This is a **staged rollout** control on a live hot path:

- **Unset (default):** byte-for-byte legacy behavior. No GitHub call on
  completion; the managed loop keeps the legacy `remote_ref` backfill so the
  code_strict completion gate still passes. dev/CI/test runs never reach GitHub.
- **Set:** server-side remote verification + real verified managed pushes are
  active.

Enable on prod (canonical repo already configured; token resolves from
`PM_GITHUB_TOKEN` / `GITHUB_TOKEN` / `SWITCHBOARD_CI_GITHUB_TOKEN`):

```
# /etc/projectplanner.env  (or the systemd EnvironmentFile)
PM_VERIFY_COMPLETION_PUSH=1
# then: systemctl restart projectplanner projectplanner-mcp
```

## Still open (layers 1 & 2 — separate infra follow-up)

This change makes completion *honest* but does not by itself make the prod box
*do work*. To actually run tasks end-to-end, the host must be work-capable:

- set `PM_AGENT_HOST_ALLOW_WORK=1` (+ lanes) so the host stops advertising
  `message_only`, and
- wire `PM_AGENT_WORK_MODULE=pkg.mod:attr` (a work_fn that runs the runtime's
  model and returns `{branch, head_sha}`) so wakes stop falling back to `--dry`.

The prod box is intentionally message-only today (tiny 2-vCPU/911MB box; a
work-capable Agent Host is a separate deployment). Nothing server-side can force
a raw-CLI agent that never calls the MCP to claim — that half is enforced at PR
time by the `Switchboard / claim gate` commit status (SESSION-12).
