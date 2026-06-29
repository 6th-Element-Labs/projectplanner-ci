# Switchboard Market Landscape Tracker

- **Status:** Living tracker
- **Last reviewed:** 2026-06-29
- **Purpose:** keep Switchboard positioning honest as BigCo, startups, and OSS projects move
  around agent collaboration, governance, security, and orchestration.

This is not sales copy. Treat it as a lightweight radar. Before using any claim in public
positioning, re-check the current vendor docs and note the date.

## Product thesis to test

Switchboard is not only an agent coordination board. The larger product is a human/agent
collaboration control plane:

- agents coordinate through durable primitives: identity, claims, leases, messages, decisions,
  wake intents, provenance, and Tally;
- humans steer the work through SME review, approvals, objections, decision threads, and
  discussion-to-plan proposals;
- teams can bring their own agents, IDE runtimes, local hosts, or enterprise-gated LLMs;
- Slack, Teams, GitHub, email, and the Switchboard UI are surfaces into the work graph, not the
  source of truth.

The question to keep asking: **who owns the durable work graph across humans, agents, repos,
runtimes, costs, approvals, and outcomes?**

## Landscape map

| Segment | Examples to track | What they are strong at | Switchboard gap / wedge |
|---|---|---|---|
| Workplace suites and chat agents | [Slack AI](https://slack.com/ai), [Salesforce Agentforce](https://www.salesforce.com/agentforce/), [Microsoft Copilot Studio](https://www.microsoft.com/en-us/microsoft-copilot/microsoft-copilot-studio), [Microsoft Agent 365](https://www.microsoft.com/en-us/microsoft-agent-365), [Atlassian Rovo Agents](https://support.atlassian.com/rovo/docs/agents/) | Human work surfaces, enterprise distribution, chat-native collaboration, suite context | Usually suite-bound and chat-first. Switchboard should integrate with these surfaces while owning the neutral work ledger underneath. |
| Coding agents and IDE runtimes | [GitHub Copilot coding agent](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/coding-agent), Claude Code, Codex, Cursor | Getting code written inside a runtime or repo workflow | They are excellent workers, not the neutral cross-runtime scheduler/evidence graph. Switchboard should coordinate them, not replace them. |
| Agent orchestration frameworks | [LangGraph Platform](https://www.langchain.com/langgraph-platform), [CrewAI](https://www.crewai.com/), AutoGen, vendor agent SDKs | In-run graphs, tool routing, persistence inside a framework, agent app development | They coordinate a run. Switchboard coordinates work across runs, humans, repos, runtimes, and sessions. |
| Agent security and governance | [Zscaler AI agent security](https://www.zscaler.com/zpedia/how-to-secure-ai-agents), [Palo Alto Networks Prisma AIRS](https://www.paloaltonetworks.com/prisma/prisma-ai-runtime-security), [Okta AI agent identity](https://www.okta.com/products/govern-ai-agent-identity/), ServiceNow AI Control Tower | Identity, runtime risk, policy, discovery, monitoring, compliance posture | They protect and govern agent access. Switchboard should govern the lifecycle of the work itself: plan, claim, review, approve, merge, cost, outcome. |
| Human PM tools with AI | Jira, Linear, Asana, Notion, Height | Human planning, issue tracking, docs, AI assistants inside human workflows | Human-first data model. Switchboard's durable primitives make agents first-class actors with leases, presence, claims, acks, and control fidelity. |
| OSS MCP task servers and local tools | Project-specific MCP servers, local task boards, custom repo bots | Fast adoption, hackable tool surfaces | Often single-project and lightweight. Switchboard can open the protocol/adapters while keeping hosted governance, Tally, policy, audit, and managed runners commercial. |

## Signals to track

- Does any suite expose first-class **claims**, **leases**, **agent presence**, or **control
  fidelity** across third-party runtimes?
- Does any vendor connect **SME feedback before coding** to dispatch eligibility or merge gates?
- Does any product make **Slack/Teams/GitHub discussion** a structured plan proposal rather than
  only a chat summary?
- Does any platform let teams bring **mixed agents and enterprise-gated LLMs** into one neutral
  project graph?
- Does any competitor show **cost per verified outcome/KPI**, not just raw token spend?
- Does any BigCo publish a credible open protocol for **cross-vendor agent work coordination**?
- Does an agent security/governance vendor move upward into work planning, dispatch, and
  outcome accounting?

## Switchboard product implications

1. **Do not build a Slack clone.** Build durable collaboration objects and connect Slack/Teams as
   channels.
2. **Make SME review first-class.** A task should be able to require product, security, domain,
   design, or maintainer review before `claim_next` dispatches implementation.
3. **Normalize discussion into work state.** Ingest comments, transcripts, and chat threads into
   proposals, blockers, approvals, decisions, and task edits.
4. **Keep ACCESS central.** Multi-human collaboration requires sessions, org/project roles,
   scoped tokens, invites, project creation permissions, and agent entitlements.
5. **Keep Tally visible.** The commercial story is not only safer agents; it is smarter work:
   spend connected to verified outcomes and KPIs.
6. **Open adoption, sell governance.** Publish protocol/adapters/conformance/local host where
   trust and ecosystem matter; keep hosted policy, identity, Tally analytics, managed runners,
   evidence retention, and enterprise integrations commercial.

## Review cadence

Refresh this doc when:

- a major vendor announces agent collaboration, agent governance, or AI security products;
- Switchboard adds a new commercial surface such as ACCESS, Tally dashboards, or collaboration
  channels;
- public positioning is prepared for customers, investors, open-source launch, or partnership
  conversations.

Minimum cadence: monthly while the market is moving quickly; quarterly once positioning stabilizes.
