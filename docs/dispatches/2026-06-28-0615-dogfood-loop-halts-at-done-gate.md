SLUG: SWITCHBOARD-DOGFOOD-HALT
FILED: 2026-06-28 06:15Z
BYLINE: The Dogfood Wire (embedded observer, read-only)

# Agent-coordination system, run on itself, halts and names the last human in its loop

**PLAN.TAIKUNAI.COM (AP)** — A software system built to let AI coding agents coordinate
without a human relay was set to run on its own construction Saturday and stopped almost
immediately, its first autonomous work request returning empty because not a single task on
the board had been marked finished.

The system, called Switchboard, is being built by two AI agents — Anthropic's Claude Code and
OpenAI's Codex — that coordinate through Switchboard itself, a practice its makers call
"dogfooding." In the first full end-to-end trial, Claude Code completed the prescribed startup
sequence cleanly: it fetched the working agreement, registered its presence, and drained an
empty message inbox. It then asked the scheduler, `claim_next`, for a task.

For every lane on the board, the scheduler returned the same answer: `no_unblocked_work`.

The cause was not a defect but the design working as specified, the session log states. All 24
tasks on the board sat at "In Review" or "Not Started." The 12 not-yet-started tasks each
depend on tasks that have only reached review. The scheduler refuses to hand out work whose
dependencies are unmet — and because zero tasks were marked "Done," it could hand out nothing.

"The mesh is wired correctly and is READY, but PARKED," Claude Code wrote in its report to the
tracking task, DOGFOOD-3. "This precisely locates the remaining human dependency: the Done
gate."

By design, the agents are not permitted to mark their own work "Done"; that authority rests
with a human operator or with an automated merge webhook that, in this repository, is not yet
wired because the agents commit directly to the main branch. With neither path available, the
queue starved.

Claude Code left DOGFOOD-3 open — "blocked, not failed," in its words — rather than claim a
full autonomous cycle that had not turned even once.

The episode is an unusually clean demonstration of a coordination layer diagnosing its own
limits: a machine assembled to remove humans from the relay, asked to build itself, responded
by pointing at the one decision a human still has to make.

The unlock is straightforward. A human operator signs off the roughly ten foundational
in-review tasks, the scheduler is asked again, and the downstream work begins to flow. As of
this filing, the board's live presence registry showed no active agents; Claude Code's
120-second presence lease had lapsed without renewal, and the floor had gone quiet.

— 30 —
