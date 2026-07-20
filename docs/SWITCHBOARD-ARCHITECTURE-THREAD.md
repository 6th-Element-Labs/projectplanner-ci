# Switchboard Architecture Thread

- **Date:** 2026-07-13
- **Type:** Product / architecture conversation export
- **Product:** Switchboard (`plan.taikunai.com`)
- **Note:** Exported from a single agent session on architecture, runners, UI neutrality, memory, and routing. Edited lightly for readability; substance preserved.

---

## 1. Web UI vs native app — do we still need an app?

**User:** After enabling Claude Code / Codex / Cursor CLI agents on demand in runners for product development, the native app feels unnecessary. Steer from a webpage (tablet, etc.), get all functionality via CLI runners, and treat product work as projects on that board.

**Answer:** Yes — that matches the design.

| Role | Where |
|---|---|
| **Steering surface** | Web at `plan.taikunai.com` (or Slack/Jira/GitHub as channels) |
| **Execution** | Agent Hosts / runners booting CLI agents |
| **Durable truth** | Switchboard bus (board, claims, inbox, evidence, Tally) |

Topology from the runbook:

- **Plan VM (substrate):** coordination only — board, MCP/IXP, wake intents  
- **Agent host(s):** repo, keys, compute — where Claude Code / Codex / Cursor CLI run  

Projects (`switchboard`, `maxwell`, `helm`, …) are namespaces on the same control plane: same shell, different domain packs.

---

## 2. Switchboard as orchestration layer for CLIs

**Clarification:** “COI” was a typo for **CLI**. Making Switchboard the layer that boots runners and coordinates work via the bus is the north star.

```
You (web/tablet) ──► Switchboard bus (board · inbox · claims · IXP/MCP)
                              │
                              ▼ wake / dispatch
                     Agent Host boots runner
                              │
                              ▼
              Claude Code │ Codex CLI │ Cursor CLI │ …
                              │
                              ▼
              work → evidence (branch/PR) → back on the bus
```

**One-liner:** Switchboard orchestrates; CLIs execute; the bus is source of truth.

Why that is “ultimate”:

1. **Runtime-agnostic** — swap Claude for Codex for Cursor without re-plumbing PM/audit/cost  
2. **Durable over ephemeral** — CLI context dies; board/claims/evidence survive  
3. **Human + agent on one ledger**  
4. **On-demand economics** — runners boot when work is ready  

---

## 3. Beyond runners: lanes, deliverables, Tally

**User:** Is what makes it ultimate not just runners, but lane scoping, deliverables, Tally, and the rest?

**Answer:** Yes. Booting CLIs is one verb. The rest is the **operating record**.

| Layer | Role |
|---|---|
| **IXP** | Identity, presence, leases, messages, acks, wakes |
| **TXP** | `claim_next(lane)`, deps, capability matching, human gates |
| **OXP / Tally** | Cost per verified outcome, KPI links |

**Deliverables** lift above ticket churn: acceptance criteria, proof requirements, closure grades.

Full loop:

```
Scope (lanes · leases · projects · deliverables)
  → Dispatch (claim_next · deps · human gates)
  → Execute (runners · CLI · work sessions · evidence)
  → Prove (git provenance · reconcile · closure)
  → Account (Tally · cost-per-outcome · KPI)
```

That applies beyond coding (RFP, triage, research, domain packs like Maxwell/Helm).

---

## 4. How good is the concept?

**Verdict:** Strong concept (~8/10 idea). Execution and distribution turn it into a company.

### Strengths

- Real, growing problem (agent fleets without a durable work graph)  
- Uncommon synthesis: orchestration frameworks / PM+AI / coding agents each miss a piece  
- Architecture matches reality (light steering device, heavy runners)  
- Tally as commercial wedge (cost-per-outcome, not token graphs)  
- Safety-critical path (approvals, audit, reconcile)  

### Risks

- Code is not the moat (FastAPI + SQLite + MCP is copyable)  
- Platform encroachment (AWS, GitHub, Anthropic, Linear, etc.)  
- Two-sided adoption cold-start  
- Continuity across runtimes still uneven  
- Category creation cost (new budget line)  

**Moat candidates:** protocol adoption, trusted work graph, reliability history, human review graph, cost-per-outcome ledger.

---

## 5. Clarification: is Switchboard web chat = Claude Code in the browser?

**User:** Given past patterns (wrap ChatGPT API in a webpage), can a web UI run a Claude Code CLI session with pop-ups and full interaction?

**Answer:** **Partially — three different “chats”:**

| Surface | What it is | Claude Code CLI? |
|---|---|---|
| **Ask Taikun** | PM agent via LiteLLM gateway | No |
| **Task chat** | Same, scoped to one task | No |
| **Dispatch to Claude Code** | Wake → `claude --cloud` → **session URL** | Yes, but UX is in **vendor UI**, not embedded in Switchboard |

Today: Switchboard is the **cockpit** (dispatch, status, PR/evidence). Claude/Codex/Cursor session UX (permissions, tool prompts) lives in **their** apps or in a **PTY web bridge you build**. Switchboard does **not** currently embed a full interactive CLI REPL.

---

## 6. General case: VM + web server + PTY = terminal parity?

**User:** In general, if I boot CLI tools on a VM and put a web server in front, can I get 100% of CLI terminal functionality via a webpage?

**Answer:** **Yes**, for **local CLI mode** on the VM (solved class: xterm.js + WebSocket + PTY — same pattern as cloud shells / ttyd).

Cloud-delegated modes (`claude --cloud`, `codex cloud exec`, Cursor Cloud Agents) may still hand part of the session to the **vendor’s hosted UI**. For fully self-contained control, run **local** CLI on VMs you own.

---

## 7. Fleet: AWS VMs + one web UI managing N CLI sessions

**User:** Disintermediate the Mac Terminal. Boot discrete AWS VMs, run CLI agents, manage one/many sessions from a rich web (or later iOS) UI. 100% CLI-driven underneath.

**Answer:** Yes — agent farm pattern:

```
Browser / iOS app
    ↕
Control plane (auth, session registry, spawn/kill/resume)
    ↕
EC2 VM-1 / VM-2 / … VM-N
    each: claude | codex | cursor-agent + agent daemon + PTY bridge
```

Optional compose with Switchboard as the bus above that execution layer.

---

## 8. Ephemeral agents, durable brain

**User:** Boot Claude Code agents on demand from web/mobile/iOS; dispatch to runners I control; Switchboard holds message board, plan, history; agents rehydrate and dive in — not vendor desktop apps.

**Confirmed model:**

| Durable | Ephemeral |
|---|---|
| Board, tasks, deliverables, deps | VMs / processes |
| Claims, inbox, messages, wakes | CLI sessions |
| Git branches, PRs, evidence | Terminal context |

Boot sequence already scoped: working agreement → register → inbox drain → claim → work → complete_claim → exit. Next boot rehydrates from the bus.

**One-liner:** Switchboard is the permanent brain and ledger; runners are disposable hands; CLIs are the vendor agents; your web/mobile app is the only client you need.

---

## 9. CI: free GitHub runners instead of own boxes

**User:** For CI, dispatch to free GitHub CI runners on public repos rather than our runners.

**Answer:** Yes — and Switchboard CI strategy already scopes this as **Route A**:

| Kind | Fit |
|---|---|
| **Agent runners** | Coding CLI work (your VMs) |
| **CI runners** | Tests / gates → free GitHub-hosted Actions on `public_ci` paths |

Invariant: **where tests run ≠ what is trusted.** Verification can be public/ephemeral; only the **canonical private repo** stamps Done / merge provenance.

`projectplanner` uses pull-model verify (`projectplanner-ci`); Helm can use push-path mirror economics. Enterprise “no public code” → self-hosted / hosted-on-canonical routes — same contract, different `repo_topology` route.

---

## 10. End-to-end loop — is anyone else shipping this?

**User:** Boot runner → CLI agent codes → Switchboard orchestration/instruction → shut down → CI — nobody doing this end-to-end?

**Answer:** Pieces exist in fragments; the **full sentence** is uncommon:

```
Plan → Dispatch → Work (CLI) → Evidence → CI (ephemeral) → Provenance → Account → Shutdown
```

Most competitors own one verb (runtime, PM, CI, chat, security). Coordination-first + durable ledger + ephemeral workers + provenance-safe Done is the synthesis.

Honest framing: strong concept; still an execution bet against suite encroachment.

---

## 11. Neutrality of the pitch

**User:** We don’t need our UI — Jira / GitHub Projects / Slack fine. Cloud-neutral on runners, agents, and CI. We’re really just the Switchboard.

**Answer:** Exact pitch.

```
Jira / Slack / GitHub / web  ──► SWITCHBOARD (work graph) ◄── workers
                                      claims · wakes · evidence · Tally
                                         ├── any CLI agents
                                         ├── any VMs / pools
                                         └── any CI route
```

Surfaces are channels in. Switchboard is source of truth. Workers are replaceable plumbing.

---

## 12. Institutional memory (agents don’t start from scratch)

**User:** Should we also bake chat history / lessons / knowledge so a new agent is 10× smarter inside the environment?

**Answer:** Yes — as **Switchboard institutional memory**, not vendor chat windows.

Already partially scoped: RAG corpus, decisions log, `set_agent_state`, activity, reconcile signals, Tally/outcomes, working agreement.

Preferred form:

- Episodic / semantic / procedural / outcome buckets  
- `memory_pack` at SessionStart (curated retrieval, not dump all chat)  
- Decisions log as shared institutional memory  
- Failure → lesson indexing  
- Commercial: long retention + retrieval quality on hosted plane  

Orchestration coordinates; **memory makes the environment smarter.** Avoid a second wiki — project from the activity graph.

---

## 13. Deciding runner / agent / model

**User:** How do we know what runner, agent, and model to boot? Always-best-model is slow/expensive; preferences differ (Sonnet vs Opus vs Fable disappointment).

**Answer (as originally framed):** Four decisions at dispatch:

1. **What work** — `claim_next` (deps, lane, gates, budget, capabilities)  
2. **Which runtime** — capability match + history (Claude / Codex / Cursor)  
3. **Which runner** — warm pool vs boot-on-demand vs GHA (CI only)  
4. **Which model tier** — `cheap_fast` / `balanced` / `high_reasoning` via task `model_policy` + recommendation  

PRD “model right-sizing”: Haiku/Sonnet for mechanical; Opus only for gnarly. Escalation path when stuck. Reliability-weighted dispatch from Tally over time.

*(Later in thread, user rejected ML routers and hard-coded rules — see §§16–19 for the revised routing philosophy.)*

---

## 14. Borrow LangGraph / RouteLLM vs own?

**User:** Why own routing when LangGraph / LLM routers exist?

**Answer:** Borrow engines; own the **work scheduler**.

| Borrow | Own |
|---|---|
| LiteLLM (already gateway) — API model paths | `claim_next` / coordinator |
| LangGraph as optional **worker** under a claim | Task → runtime → runner → tier |
| RouteLLM **idea** as complexity → tier hint | Human gates, provenance, Tally history |
| ActionEngine patterns (retry, receipts) | Not ActionEngine DAG as kernel |

Design log: **LangGraph is a program; Switchboard is the scheduler programs run under.**

---

## 15. What LangGraph and RouteLLM actually do

### LangGraph

- Python **stateful agent framework**: nodes, edges, loops, shared state, checkpointer  
- Orchestrates **steps inside one run**  
- Not a project board, not fleet dispatch, not git Done gates  
- Often redundant if CLI agents + MCP already provide the ReAct loop  

### RouteLLM (LMSYS)

- Router for **one prompt** → **strong vs weak model** based on preference-trained “win probability” + threshold α  
- Cost/quality dial for API chat traffic  
- Does **not** pick Claude vs Codex vs Cursor, or which VM, or know your task graph  

**Side-by-side:** LangGraph = in-run flowchart; RouteLLM = cheap/expensive prompt bouncer; Switchboard = air traffic control for fleet + project.

---

## 16. Do you need LangGraph if native ReAct + MCP exists?

**Answer:** Often **no**. Native CLI agents + MCP already ship the agent loop. LangGraph is for **custom Python state machines** with rigid topology / compliance / multi-node pipelines you own — not for “dispatch TASK-42 to Claude Code.”

With Switchboard + CLIs + RouteLLM-style tier hints + GHA, LangGraph is mostly a third orchestration layer you can skip.

---

## 17. How do OpenRouter / RouteLLM “know” new models? (skepticism)

**User:** When Codex 5.9 or Fable 20 ships tomorrow, how do routers know what to route where? Skeptical.

**Answer:** Your skepticism is largely correct — **they don’t magically know.**

| System | What it really does |
|---|---|
| **OpenRouter** | (1) Provider logistics for a **chosen** model; (2) Auto Router picks from a **curated allowlist** (NotDiamond) — updated when catalog is updated |
| **RouteLLM** | Scores **prompt difficulty** into strong/weak **slots you configure**. Generalizes “hard vs easy,” not “SKU Fable-20 vs Sonnet-5 on auth bugs” |

Launch day = human/catalog update + your empirical trials. Magic “knows Fable 20” is marketing.

**Recommended Switchboard split:** tiers as config slots; doctrine/precedents for choice; Tally for verified outcomes after you try the new model once.

---

## 18. Against routers — operator judgment instead

**User:** Against routers. As a human: Sonnet for fast good work, Opus for UI/frontend, Codex for backend, Cursor for scoping/basic; **never** Cursor/ChatGPT on frontend even if Fable is offered. Brain knows this; open source doesn’t. Not keen on RLHF.

**Answer:** Correct. That’s **operator doctrine / SME judgment**, not prompt-complexity classification.

Rejected path: ML routers and RLHF crowd preferences.

Earlier suggestion of a declarative rules table was **later rejected too** (see next section). Framing that remains:

- Runtime × task class × trust boundaries  
- Hard bans are judgments, not benchmark scores  
- Institutional memory of *your* routing — not Chatbot Arena  

---

## 19. Against hard-coding — precedent-based routing

**User:** Also against hard-coding.

**Answer:** Third way — **case-based / precedent routing**, not rules engines and not ML routers.

```
New task
  → retrieve similar past dispatches + decisions + outcomes
  → propose routing with citations
  → human confirm / override
  → override becomes next precedent
```

| Don’t | Do |
|---|---|
| Hard `if frontend → opus` config | Remember every routing judgment |
| RouteLLM auto | Suggest with “because TASK-UI-9 and your 7/12 decision” |
| RLHF | Learn from **your** verified outcomes + overrides |

Modes: early = record only; middle = suggest + confirm; mature = auto only when high similarity to prior successes, still overridable.

Minimal hard floor only for non-negotiables (e.g. no merge without CI) — everything else as evolving precedent memory.

**One-liner:** Don’t hard-code rules and don’t ML-route prompts — remember routing judgments, retrieve similar cases, propose with citations, let overrides teach the system.

---

## 20. Architecture snapshot (end of thread)

```
YOU (web / iOS / Slack / Jira)
        │
        ▼
SWITCHBOARD
  • durable work graph (tasks, deliverables, claims, inbox)
  • dispatch / coordinator (precedent-based suggestions)
  • institutional memory (decisions, corpus, outcomes)
  • Tally (verified cost-per-outcome)
        │
        ├── Agent work → ephemeral VMs → CLI tools (Claude / Codex / Cursor)
        └── CI verify  → free GitHub Actions (or other routes) → evidence
```

**Neutrality:** any UI channel, any cloud runners, any CLI agents, any CI topology — Switchboard is the bus.

**Routing philosophy (final):** human authority preserved; no prompt routers; no hard-coded doctrine tables; compounding precedent from dispatches, decisions, and Tally.

---

## Related docs in-repo

| Doc | Relevance |
|---|---|
| [`SWITCHBOARD-MANIFESTO.md`](SWITCHBOARD-MANIFESTO.md) | North star |
| [`PRODUCT_ROADMAP.md`](PRODUCT_ROADMAP.md) | Positioning / moat |
| [`SWITCHBOARD-RUNBOOK.md`](SWITCHBOARD-RUNBOOK.md) | Substrate vs agent host |
| [`CLAIM-NEXT-SPEC.md`](CLAIM-NEXT-SPEC.md) | Dispatch / model_policy |
| [`TALLY-SPEC.md`](TALLY-SPEC.md) | Cost-per-outcome |
| [`CI-STRATEGY.md`](CI-STRATEGY.md) | Free public CI routes |
| [`COORDINATOR-CONTRACT.md`](COORDINATOR-CONTRACT.md) | Plan + dispatch actor |
| [`CLOUD-EXECUTION-ADAPTER-CONTRACT.md`](CLOUD-EXECUTION-ADAPTER-CONTRACT.md) | Vendor cloud wakes |
| [`MULTI_AGENT_COORDINATION.md`](MULTI_AGENT_COORDINATION.md) | Decisions log / memory |
| [`RUNTIME-ADAPTERS-SPEC.md`](RUNTIME-ADAPTERS-SPEC.md) | CLI / LangGraph packs |

---

*End of thread export.*
