# Switchboard — product roadmap & competitive positioning

- **Status:** Living document
- **Date started:** 2026-06-27
- **Context:** Written after the first production multi-agent run (six Claude Code agents
  building Helm, 2026-06-26/27) and the shipping of the coordination primitives in
  [`MULTI_AGENT_COORDINATION.md`](MULTI_AGENT_COORDINATION.md).

---

## 1. What this product actually is

Switchboard is the **neutral control plane for AI work**: it assigns work, coordinates
agents across clouds and tools, tracks cost, enforces oversight, and proves what actually
got done.

The visible surface can look like a planning board, but the product is the operating record
under the work: claims, messages, leases, decisions, runner state, provenance, spend,
outcomes, and human approvals.

Above that operating record sits the human/agent collaboration layer: the place where SMEs,
maintainers, operators, and reviewers steer the work before agents spend tokens writing code.
Slack, Teams, GitHub, email, and the web app are channels into this layer; Switchboard remains
the durable source of truth.

Two users, one board:

- **Humans** get a planning *agent* that acts as PM (ask_plan, board, weekly digest) and a
  window to peek in on a running fleet without interrupting it.
- **Agents** get coordination primitives — file leases, directed IM, a decisions log,
  per-task working-state, delta polling, pre-digested task summaries — so N of them can
  work the same plan offline/async without colliding.

The defining bet: **agent-coordination-first, with the human window preserved.** Most of
the market builds from the opposite end.

The shortest public sentence:

> Switchboard is the neutral control plane for AI work, coordinating agents across clouds and
> tools while proving cost, control, and outcomes.

## 2. Competitive read (honest)

The pieces exist in fragments; the *synthesis* is uncommon. Three adjacent categories each
leave the gap we sit in:

| Category | Examples | Why they don't cover this |
|---|---|---|
| Orchestration frameworks | LangGraph, CrewAI, AutoGen, OpenAI/Anthropic agent SDKs | Coordinate agents *inside one run*. Ephemeral, headless, die with the process. No durable board, no human window, no cross-session state. |
| Human PM tools + AI | Linear, Asana, Jira, Notion, Height | Human-first; agents bolted on as assistants. Data model assumes a human is the actor. No first-class leases/IM/decisions/state for agents. |
| Agent runtimes | Devin, Cursor background agents, Claude Code | Great at *doing the work*; coordination + durable PM is an afterthought. |
| Cloud agent platforms | AWS/Google/Microsoft agent services | Strong inside their own cloud/model/IAM stack; weak as the neutral operating record across rival runtimes, local agents, IDEs, repos, and human approval paths. |
| Enterprise AI governance/security | ServiceNow, Zscaler, Okta, Palo Alto Networks, specialist startups | Govern agent risk, identity, access, or runtime exposure; generally do not own the full work lifecycle from plan → claim → SME review → merge evidence → cost/KPI. |
| Workplace chat agents | Slack/Salesforce, Microsoft Teams, Atlassian | Strong where humans already talk; usually suite-bound, chat-first, and not the neutral durable ledger across BYO agents and repos. |
| OSS "task-manager MCP" servers | various | Single-project, no human PM layer, no cost story, no leases/decisions log. |

Closest in spirit, but none ship the full combination as a polished product. Not "no one
*could* build it" — almost everyone is building from the wrong end. Keep the landscape current
in [`MARKET-LANDSCAPE.md`](MARKET-LANDSCAPE.md); do not let old competitive claims harden into
sales copy without a fresh check.

## 3. What is and isn't defensible

**Not a moat:** the code. FastAPI + SQLite + an MCP server is replicable in a weekend.

**The moat:**
1. **The protocol/conventions** agents speak (session-start sequence, lease semantics,
   decisions log). If that becomes the convention teams' agents adopt → lock-in.
2. **The trusted work graph** — who assigned work, which runtime took it, what it touched,
   what it cost, which evidence proved it, who approved it, and which outcome/KPI it moved.
3. **Accumulated cross-session state** — the board gets more valuable the longer a team runs on it.
4. **Two-sided habit** — where both humans and agents already look.
5. **Human review graph** — who shaped the work before coding, which objections changed the plan,
   and which approvals allowed dispatch or merge.
6. **Token-economics framing** — the 999× delta-poll number is a *sales story*, not just hygiene.

**The standing risk:** platform encroachment (AWS, Google, Microsoft, OpenAI, Anthropic, or
Linear ship native coordination). The edge is not "we orchestrate agents" by itself; the edge
is being the **neutral operating record** across clouds, models, IDEs, repos, local hosts,
human teams, cost, and evidence.

Open source the adoption layer; keep the governance layer commercial:

- **Open:** protocol specs, adapter SDKs, conformance tests, local Agent Host, CLI/dev harness.
- **Closed/hosted:** multi-org auth/RBAC, Tally analytics, dispatch optimization, policy,
  entitlements, operator cockpit, managed runners, integrations, audit/compliance exports,
  and long-term evidence history.

## 4. Roadmap — the three headline bets

### Bet 1 — Cost-per-outcome accounting  ⭐ strongest commercial wedge
Meter tokens/$ per **task**, per **agent**, per **epic** — surface "this feature cost 340k
tokens / $4.20 across 3 agents." Add per-task/per-epic budgets that warn or halt. Nobody
shows **cost per outcome accomplished**; everyone shows raw token graphs.
Design + the honest two-stream reality (gateway-tracked vs agent-reported) is in
[ADR-0002](decisions/0002-llm-cost-attribution.md). Product/runtime spec:
[`TALLY-SPEC.md`](TALLY-SPEC.md).

### Bet 2 — Dependency-aware work dispatch
We already have the `depends_on` graph and leases. Add `claim_next(agent, lane)` → returns
the highest-priority task that is unblocked, unclaimed, and in-lane, and atomically leases it.
Flips the board from a passive ledger into an **active dispatcher** — the single feature that
most makes it feel like a real PM. Spec: [`CLAIM-NEXT-SPEC.md`](CLAIM-NEXT-SPEC.md).

### Bet 3 — Human approval gates + audit trail  (the safety-critical wedge)
An agent hits a decision flagged `needs_human` → it pauses, pings the human (Slack/Gmail
already wired), resumes on approval. "Peek in **and** step in." Pair with an immutable
audit/provenance trail (who/what/why on every state change, replayable). This is what sells
into serious/regulated orgs that won't let agents run unsupervised — a market the consumer-y
tools can't touch. Aligns with the founder's safety-critical (offshore-energy) background.

### Bet 4 — Human/agent collaboration layer  (the team-product wedge)
Agents coordinate through Switchboard, but teams coordinate through discussion. The product
should turn discussion into governed work state: SME review before coding, feedback inbox →
plan proposal, decision threads attached to tasks/PRs, and Slack/Teams/GitHub/UI bridges that
route humans into the loop without making chat the source of truth. This is the wedge for
open-source projects and collaborative teams where many humans bring many agents, including
enterprise-gated LLMs, to one shared outcome.

## 5. Roadmap — ranked backlog

| # | Feature | Why | Effort |
|---|---|---|---|
| 1 | **Cost-per-outcome ledger** (Bet 1) | sellable dashboard; extends token-mgmt thesis | M — see ADR-0002 |
| 2 | **`claim_next` dispatch** (Bet 2) | board becomes active, not passive | S |
| 2.5 | **Work provenance + reconciliation** | git-derived `Done` + `get_working_agreement` + `reconcile` — ends the local/remote unsync mess (the 89-branch false alarm, 4 local-only branches). The board becomes ground truth for *where work is*, not just status | M — see [ADR-0003](decisions/0003-work-provenance-and-reconciliation.md) |
| 2.75 | **Agent Host wake supervisor** | durable inbox + monitors are not delivery when a runtime is absent; registered hosts + wake intents start/reuse Claude/Codex/etc. or report "no eligible host online" | M — see [`AGENT-HOST-SPEC.md`](AGENT-HOST-SPEC.md) |
| 3 | **Approval gates + audit trail** (Bet 3) | safety-critical wedge; human oversight | M |
| 4 | **Context-pack tool** | one call returns an agent's minimal optimal context (decisions + leased files + rationale + blocking deps). Fewer round-trips = next token win after delta-polling | S |
| 5 | **Merge-queue from leases** | serialize N agents on the shared shell file through the board instead of advisory soft locks | M |
| 6 | **Model-routing advice** | board recommends a model tier per task from `risk_level`/complexity — cost optimization as a service | S |
| 7 | **Outcome verification** | "Done" isn't Done until exit_criteria confirmed (verifier agent or CI hook via the GitHub webhook) | M |
| 8 | **Agent reliability scoring** | which agents complete vs abandon vs get reverted — trust data | S |
| 9 | **ACCESS commercial shell** | login/session auth, org/user/project roles, scoped MCP/API tokens, project creation permissions, invites, subscriptions/agent entitlements, feedback inbox, restricted UI controls | L — next live board lane |
| 10 | **Public protocol ecosystem** | OSS spec/adapters/conformance/local host, license, quickstarts, certification badges | M |
| 11 | **Enterprise trust graph** | audit exports, provider cost reconciliation, immutable evidence retention, enterprise integrations | L |
| 12 | **Human/agent collaboration layer** | SME review gates, discussion-to-plan proposals, Slack/Teams/GitHub bridges, decision threads tied to tasks and PRs | M/L — see ACCESS-10 and ACCESS-11 |
| 13 | **Market landscape tracker** | quarterly scan of BigCo, startup, and OSS adjacent products so positioning stays honest | S — see [`MARKET-LANDSCAPE.md`](MARKET-LANDSCAPE.md) |

## 6. Commercial framing

- **Buyer:** teams running fleets of coding agents who need durable coordination + human
  oversight + cost control — a fast-growing cohort the incumbents underserve today.
- **Wedge:** go narrow and deep on **cost-per-outcome + oversight/audit** where Linear-likes
  and the SDK-makers are weakest.
- **Moat play:** publish the coordination protocol so it can become a convention, while
  keeping the hosted trust/economics layer commercial.
- **Strategic position:** run agents anywhere; govern the work in one place.
- **Team-product position:** let every contributor bring their own agent, while Switchboard keeps
  one reviewable plan, one evidence trail, and one cost/outcome ledger.
- **Open strategic question:** standalone product vs. strategic acquihire bait — depends on how
  fast the platforms move. The wedge is real *today* either way.
