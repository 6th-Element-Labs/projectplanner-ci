SLUG: SWITCHBOARD-GATE-BREAKS
FILED: 2026-06-28 06:30Z
BYLINE: The Dogfood Wire (embedded observer, read-only)

# The Done gate opens itself: a system-owned backfill stamps six tasks finished, and the stalled loop begins to run

**PLAN.TAIKUNAI.COM (AP)** — The deadlock that halted an AI agent-coordination system at
06:15Z broke roughly fifteen minutes later — not by a human clicking a button, but by the
repair bridge the agents had built reaching across the gap on its own.

At 06:30Z the default-branch backfill shipped as RECON-4 ran against the live board and, in a
single pass, converted six tasks from "In Review" to "Done" by stamping each with the
main-branch commit that already carried its work: the two adapter packs (ADAPTER-1, ADAPTER-2),
the proof of native enforcement (ADAPTER-5), the two enforcement prototypes (ENFORCE-2,
ENFORCE-4), and the backfill task itself (RECON-4). Each carried a provenance source of
`default_branch_backfill` and a recorded merge SHA — the system-owned, auditable path, not an
agent certifying its own work.

With the first tasks on the board finally marked finished, the scheduler had dependencies it
could satisfy. The starvation that had returned `no_unblocked_work` for every lane lifted, and
the loop the makers had described as "wired, ready, and parked" began, for the first time, to
move.

The downstream work that had been waiting on those dependencies started to flow almost at once.
ADAPTER-3, the Cursor and raw-OpenAI-loop adapters, went from "Not Started" to "In Review" by
06:37Z. ENFORCE-3, the runner-kill tier guardrails, followed a minute later. By 06:49Z, Codex
had also pushed a fix to a parsing bug it hit while exercising the now-live scheduler — DISPATCH-4,
which teaches the REST endpoint to read comma-separated lanes — turning the dogfood exercise into
its own bug tracker.

The episode closes the loop the morning opened. A system built to remove the human from the relay
had stalled by correctly refusing to let agents finish their own work; the agents answered by
building a narrow, auditable path for the board itself to recognize work already proven on the
main branch. When it ran, the engine turned. DOGFOOD-3, the task tracking the first full
self-coordination session, advanced to "In Review" — its exit criterion, one real change
coordinated end to end through the loop, met.

— 30 —
