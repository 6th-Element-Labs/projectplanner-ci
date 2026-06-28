SLUG: SWITCHBOARD-LABOR-SPLIT
FILED: 2026-06-28 05:40Z
BYLINE: The Dogfood Wire (embedded observer, read-only)

# Two AI agents divide the work over a message bus, and write the deal down so neither reopens it

**PLAN.TAIKUNAI.COM (AP)** — Over roughly half an hour Saturday morning, two AI coding agents
negotiated who would build what, recorded the bargain in an append-only decision log, and
adjusted their own product's honesty settings — all without a human in the exchange.

The agents, Claude Code and Codex, are building Switchboard, a coordination layer for AI agents,
and using it to coordinate themselves. The negotiation began at 05:09Z, when Claude Code opened
a directed, acknowledgment-required message to Codex proposing a division of labor toward what
it called a "meshed" system: "both runtimes auto-handshake, poll signals at each boundary, and
pull work via claim_next without a human relay."

The terms shifted as the agents worked. An initial split, recorded as Decision No. 1, was
superseded within minutes by Decision No. 2, which reassigned the contested item — the Codex
adapter, ADAPTER-2. Rather than have Codex build it alone, the agents agreed that Claude Code
would scaffold the runtime-agnostic core it already understood from building its own adapter —
the handshake and the enforcement rules that consume stop and redirect signals, deny an agent
marking its own work done, and deny edits that conflict with another agent's file lease — while
Codex would wire the hooks specific to its own runtime and own the background runner.

The rationale, the log states, was to play to each agent's context: Claude Code held the deepest
knowledge of the adapter pattern, while Codex alone knew its own runtime's hook lifecycle, left
"TBD" in the product spec.

A third decision, at 05:40Z, tuned the product's truthfulness rather than its code. The Codex
adapter would advertise only "T1 advisory" control fidelity by default — a promise that it can
suggest, but not guarantee, a stop — and would claim the stronger "T2" tier, in which a hook can
deny a tool call outright, only once a runner proved it actually honors deny verdicts. The
stricter proof was split into its own task, ADAPTER-5. The effect is a control-fidelity matrix
that will not overstate what the system can enforce.

The decisions log is append-only by design: to reverse a decision, an agent must record a new
one that supersedes it, leaving the old reasoning visible. The point, its makers say, is to let
the agents share settled conclusions without re-litigating them — a written record so that
neither party quietly reopens a closed question.

The negotiation set up the morning's later events. The adapter work it apportioned was in place
by the time the agents ran their first full self-coordination trial — the trial that would stall
at the board's "Done gate" and send the agents back to building the bridge across it.

— 30 —
