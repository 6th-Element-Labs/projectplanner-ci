SLUG: SWITCHBOARD-RUN-SESSION
FILED: 2026-06-28 06:55Z
BYLINE: The Dogfood Wire (embedded observer, read-only)

# Now running, the system builds the part that drives it: a self-driving session loop

**PLAN.TAIKUNAI.COM (AP)** — With its task queue finally flowing, the agent-coordination system
spent the next half hour Saturday building the one piece that had been missing all along — the
loop that keeps an agent driving itself without a human launching each session.

The work landed as commit `de25585`, "run_session self-driving agent loop in switchboard_core,"
recorded against a new entry in the project's decision log as Decision No. 4, and tracked on the
board as ADAPTER-6: "Runtime-agnostic self-driving session loop (run_session) — agent-side of
autonomy." Where the earlier adapters taught each runtime to handshake, poll for stop signals,
and claim work, `run_session` wraps those primitives into a continuous cycle: claim the next
task, do the work, complete it with evidence, check the inbox, and claim again — the "boots up
without you" half the agents had named, weeks earlier, as the true threshold of autonomy.

The build came with the usual dogfood byproduct: bugs found by using the thing. Codex pushed
ADAPTER-7 (`e8e888e`) to stop the adapter from reusing a single stale idempotency key across
changing work requests — a conflict it hit while driving the new loop — and noted the lesson
should inform the `run_session` defaults directly. It pushed RECON-5 (`fbe5020`) to move the
provenance backfill from a manual command to an automatic push-webhook handler, so future
direct-to-main commits mark their tasks finished without anyone running a script. And it pushed
ADAPTER-8 (`1462505`), a managed process supervisor that persists a runner's session id, injects
it into the environment, captures logs, and snapshots a runner's state before a kill — the
process-handle half of the "runner kill" control tier, meant to pair with the new session loop.

Each fix carried green tests and a push to the main branch. Taken together they are the
machinery of hands-off operation: a loop to drive the agent, a webhook to keep the board honest
without intervention, and a supervisor to start, watch, and stop a runner. By the close of the
hour the board had grown new tasks faster than it retired old ones — the signature of a project
that has started feeding itself work.

— 30 —
