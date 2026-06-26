# ProjectPlanner — product roadmap & competitive positioning

- **Status:** Living document
- **Date started:** 2026-06-27
- **Context:** Written after the first production multi-agent run (six Claude Code agents
  building Helm, 2026-06-26/27) and the shipping of the coordination primitives in
  [`MULTI_AGENT_COORDINATION.md`](MULTI_AGENT_COORDINATION.md).

---

## 1. What this product actually is

A **durable, human-inspectable PM board that is simultaneously the async coordination
substrate for a fleet of agents** — with token/cost economics as a first-class design
constraint.

Two users, one board:

- **Humans** get a planning *agent* that acts as PM (ask_plan, board, weekly digest) and a
  window to peek in on a running fleet without interrupting it.
- **Agents** get coordination primitives — file leases, directed IM, a decisions log,
  per-task working-state, delta polling, pre-digested task summaries — so N of them can
  work the same plan offline/async without colliding.

The defining bet: **agent-coordination-first, with the human window preserved.** Most of
the market builds from the opposite end.

## 2. Competitive read (honest)

The pieces exist in fragments; the *synthesis* is uncommon. Three adjacent categories each
leave the gap we sit in:

| Category | Examples | Why they don't cover this |
|---|---|---|
| Orchestration frameworks | LangGraph, CrewAI, AutoGen, OpenAI/Anthropic agent SDKs | Coordinate agents *inside one run*. Ephemeral, headless, die with the process. No durable board, no human window, no cross-session state. |
| Human PM tools + AI | Linear, Asana, Jira, Notion, Height | Human-first; agents bolted on as assistants. Data model assumes a human is the actor. No first-class leases/IM/decisions/state for agents. |
| Agent runtimes | Devin, Cursor background agents, Claude Code | Great at *doing the work*; coordination + durable PM is an afterthought. |
| OSS "task-manager MCP" servers | various | Single-project, no human PM layer, no cost story, no leases/decisions log. |

Closest in spirit, but none ship the full combination as a polished product. Not "no one
*could* build it" — almost everyone is building from the wrong end.

## 3. What is and isn't defensible

**Not a moat:** the code. FastAPI + SQLite + an MCP server is replicable in a weekend.

**The moat:**
1. **The protocol/conventions** agents speak (session-start sequence, lease semantics,
   decisions log). If that becomes the convention teams' agents adopt → lock-in.
2. **Accumulated cross-session state** — the board gets more valuable the longer a team runs on it.
3. **Two-sided habit** — where both humans and agents already look.
4. **Token-economics framing** — the 999× delta-poll number is a *sales story*, not just hygiene.

**The standing risk:** platform encroachment (Anthropic/OpenAI ship native multi-agent
coordination; Linear ships agent-native primitives). The edge is being narrowly excellent at
the **coordination + oversight + cost** triangle while they're distracted, and publishing the
protocol so it can become a convention.

## 4. Roadmap — the three headline bets

### Bet 1 — Cost-per-outcome accounting  ⭐ strongest commercial wedge
Meter tokens/$ per **task**, per **agent**, per **epic** — surface "this feature cost 340k
tokens / $4.20 across 3 agents." Add per-task/per-epic budgets that warn or halt. Nobody
shows **cost per outcome accomplished**; everyone shows raw token graphs.
Design + the honest two-stream reality (gateway-tracked vs agent-reported) is in
[ADR-0002](decisions/0002-llm-cost-attribution.md).

### Bet 2 — Dependency-aware work dispatch
We already have the `depends_on` graph and leases. Add `claim_next(agent, lane)` → returns
the highest-priority task that is unblocked, unclaimed, and in-lane, and atomically leases it.
Flips the board from a passive ledger into an **active dispatcher** — the single feature that
most makes it feel like a real PM.

### Bet 3 — Human approval gates + audit trail  (the safety-critical wedge)
An agent hits a decision flagged `needs_human` → it pauses, pings the human (Slack/Gmail
already wired), resumes on approval. "Peek in **and** step in." Pair with an immutable
audit/provenance trail (who/what/why on every state change, replayable). This is what sells
into serious/regulated orgs that won't let agents run unsupervised — a market the consumer-y
tools can't touch. Aligns with the founder's safety-critical (offshore-energy) background.

## 5. Roadmap — ranked backlog

| # | Feature | Why | Effort |
|---|---|---|---|
| 1 | **Cost-per-outcome ledger** (Bet 1) | sellable dashboard; extends token-mgmt thesis | M — see ADR-0002 |
| 2 | **`claim_next` dispatch** (Bet 2) | board becomes active, not passive | S |
| 3 | **Approval gates + audit trail** (Bet 3) | safety-critical wedge; human oversight | M |
| 4 | **Context-pack tool** | one call returns an agent's minimal optimal context (decisions + leased files + rationale + blocking deps). Fewer round-trips = next token win after delta-polling | S |
| 5 | **Merge-queue from leases** | serialize N agents on the shared shell file through the board instead of advisory soft locks | M |
| 6 | **Model-routing advice** | board recommends a model tier per task from `risk_level`/complexity — cost optimization as a service | S |
| 7 | **Outcome verification** | "Done" isn't Done until exit_criteria confirmed (verifier agent or CI hook via the GitHub webhook) | M |
| 8 | **Agent reliability scoring** | which agents complete vs abandon vs get reverted — trust data | S |
| 9 | **Commercial table stakes** | real agent identity/auth (ADR-0001 open question), multi-tenant workspaces, RBAC, self-serve onboarding | L |

## 6. Commercial framing

- **Buyer:** teams running fleets of coding agents who need durable coordination + human
  oversight + cost control — a fast-growing cohort the incumbents underserve today.
- **Wedge:** go narrow and deep on **cost-per-outcome + oversight/audit** where Linear-likes
  and the SDK-makers are weakest.
- **Moat play:** publish the coordination protocol so it can become a convention.
- **Open strategic question:** standalone product vs. strategic acquihire bait — depends on how
  fast the platforms move. The wedge is real *today* either way.
