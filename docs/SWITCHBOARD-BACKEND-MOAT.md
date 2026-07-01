# Switchboard backend moat architecture

- **Status:** Strategic architecture
- **Board anchor:** DOGFOOD-10
- **Strategy anchor:** DOGFOOD-7
- **Related docs:** [`SWITCHBOARD-MANIFESTO.md`](SWITCHBOARD-MANIFESTO.md),
  [`PRODUCT_ROADMAP.md`](PRODUCT_ROADMAP.md),
  [`PRD-AGENT-COORDINATION-LAYER.md`](PRD-AGENT-COORDINATION-LAYER.md),
  [`SWITCHBOARD-ACTIONENGINE-BORROWING-MAP.md`](SWITCHBOARD-ACTIONENGINE-BORROWING-MAP.md),
  [`CLAIM-NEXT-SPEC.md`](CLAIM-NEXT-SPEC.md), [`TALLY-SPEC.md`](TALLY-SPEC.md),
  [`WORKING-AGREEMENT.md`](WORKING-AGREEMENT.md)

---

## 1. Purpose

This document consolidates the backend strategy behind Switchboard's open-core posture.
The protocol can be open. The hosted backend should still become the best place to
coordinate agent fleets because it accumulates durable operational truth, learns from
outcomes, and enforces policy across many runtimes.

The simple rule:

> Open the coordination contract. Make the hosted control plane better through state,
> scheduler quality, evidence, policy, reliability learning, and cost intelligence.

For the shorter founder-readable version of this belief, see
[`SWITCHBOARD-MANIFESTO.md`](SWITCHBOARD-MANIFESTO.md).

Copying the public specs should make an adapter compatible with Switchboard. It should
not give a competitor the trusted work graph, reliability history, replay tools, policy
engine, cost-to-outcome ledger, or operational habits that make a real fleet safer and
cheaper over time.

---

## 2. The moat stack

| Layer | Open or hosted | Why it matters |
|---|---|---|
| Protocol contract | Open | IXP/TXP/OXP adoption lets agents and runtimes plug in without fear of lock-in. |
| Adapter ecosystem | Open | SDKs, fixtures, and local Agent Host make Switchboard easy to load into Claude, Codex, Cursor, LangGraph, and raw loops. |
| Coordination kernel | Hosted / product | Identity, presence, leases, message acks, work queue, wake intents, runner control, and the append-only log must stay durable and low-tail-latency. |
| Trusted work graph | Hosted / product | Long-lived record of who assigned work, who took it, what changed, what evidence proved it, who approved it, and what outcome moved. |
| Dispatch intelligence | Hosted / product | `claim_next` and coordinator policy improve by learning from dependency state, cost, risk, capability, reliability, and business priority. |
| Tally economics | Hosted / product | Provider reconciliation, budgets, KPI links, cost-per-verified-outcome, and spend confidence are paid control-plane value. |
| Policy and governance | Hosted / product | Auth, RBAC, project boundaries, scoped tokens, approval gates, entitlements, and audit exports are buyer-facing trust features. |
| Replay and simulation | Hosted / product | Event replay, dispatch preflight, and failure simulation make the platform safer and improve the scheduler without risking live work. |
| Fail-fix learning loop | Hosted / product | Every broken connection, missing signal, invalid input, or stale branch becomes an auditable event and, when real, a task. |

The public protocol gets agents into the network. The hosted backend gets better because
it sees the work actually happen.

---

## 3. Coordination kernel

The kernel is the hot path. It is not the web UI, RAG, OCR, inbox triage, or marketing
surface. It is the durable substrate every agent action depends on.

Kernel-owned responsibilities:

- identity, presence, heartbeat, and control-fidelity advertisement;
- resource leases for files, ports, branches, worktrees, binaries, runners, and tasks;
- directed message delivery, acks, monitors, and escalation;
- wake intents and Agent Host routing for absent runtimes;
- `claim_next`, exact task claims, abandon, revoke, and idempotency;
- append-only activity, decision, provenance, and policy events;
- Done gates that trust merge/default-branch evidence rather than optimistic status;
- budget and interrupt signals that can steer or halt work before spend runs away.

Kernel constraints:

- project scoped by default;
- deterministic, idempotent writes;
- no silent success on missing data;
- low tail latency under concurrent agent load;
- explainable decisions whenever work is skipped, assigned, halted, or escalated.

Python can remain userland for slow, product-facing workflows. If load demands a Go/NATS
or Postgres-backed kernel later, the IXP/TXP/OXP envelopes should stay stable while the
implementation under them changes.

---

## 4. Trusted work graph

The hosted product's hardest-to-copy asset is the work graph, not the task rows.

Minimum graph edges:

- human, agent, runtime, host, project, role, and permission;
- task, dependency, claim, lease, resource, and branch;
- message, ack, monitor, wake intent, interrupt, and runner control request;
- decision, approval, blocker, reviewer, and exception;
- PR, head SHA, merged SHA, default-branch proof, CI result, and reconcile finding;
- spend event, budget, outcome, KPI link, confidence, and provider reconciliation.

This graph powers:

- replay of what happened;
- audit and compliance exports;
- cost-per-outcome reporting;
- reliability scoring by agent/runtime/model/task type;
- coordinator recommendations;
- dispatch policy evaluation;
- human review routing;
- "why did Switchboard do that?" explanations.

If a clone has the protocol but not the graph, it can move messages. It cannot prove
work, cost, trust, or outcomes across a real fleet.

---

## 5. Scheduler intelligence

`claim_next` starts as deterministic dependency-aware dispatch. The defensible version
becomes a scheduler that can explain expected value.

Scheduler inputs:

- dependency readiness and critical-path position;
- task risk, required capabilities, and approval gates;
- active claims, resource leases, WIP limits, and runner availability;
- model/runtime control fidelity;
- remaining budget and spend confidence;
- reliability history by agent/runtime/model/lane;
- abandon, revert, stale-claim, timeout, and failed-gate rates;
- verified outcomes and cost per accepted outcome;
- business priority and human override.

Scheduler outputs:

- selected task or no-work reason;
- selected agent/runtime/model tier;
- dispatch score and factor breakdown;
- skipped-candidate reasons;
- expected cost and risk band;
- monitor/interrupt thresholds;
- replayable policy version.

The first scheduler moat is not ML. It is complete, trustworthy data plus explainable
policy. More advanced optimization should come only after the deterministic baseline is
well instrumented and replayable.

---

## 6. Replay, simulation, and preflight

Replay is the bridge between audit and improvement.

Required capabilities:

- reconstruct a task, claim, PR, or agent-session lifecycle from append-only events;
- replay a board interval and prove derived state matches the current board;
- run `claim_next` policies against historical snapshots without mutating live work;
- simulate absent agent, expired lease, stale branch, missing token, bad project, and
  malformed evidence cases;
- compare policy versions by outcome, cost, starvation, review delay, and failure rate;
- produce human-readable explanations for every changed dispatch decision.

This is how the backend gets smarter without hiding behind vibes. Every policy change can
be tested against yesterday's fleet before it touches tomorrow's work.

---

## 7. Fail and fix early

Switchboard should treat failures as product data, not noise.

Fail closed when any required signal is missing or invalid:

- project, task, actor, permission, token, host, runtime, claim, or lease identity;
- branch, PR, head SHA, merged SHA, CI status, or default-branch proof;
- spend source, confidence, outcome, KPI link, or provider reconciliation;
- message target, ack deadline, wakeability, runner status, or control fidelity.

Visible fallbacks are allowed only when they preserve the original failing signal and
name the fallback path. The system should emit an auditable red/yellow event, reconcile
finding, task comment, monitor, or blocker.

Every repeated or product-level failure should map into the BUG/QA lanes with:

- failure class;
- observed command or API call;
- invalid/missing input;
- expected signal;
- actual signal;
- severity and blast radius;
- repro or replay pointer;
- next owner.

That loop is part of the moat. It makes the platform better every time a real workflow
breaks.

---

## 8. Strategy to epics and tasks

| Strategy area | Current task anchors | Gap / next task owner |
|---|---|---|
| Open-core moat and commercial boundary | DOGFOOD-7 | DOGFOOD-10 keeps this architecture mapped to docs and tasks. |
| Public protocol ecosystem | PROTO-6, ADAPTER-11, DOGFOOD-8 | Keep open specs, adapters, conformance, and release packaging outside hosted-only code. |
| Coordination kernel and hot path | PRD section 6, P0-SPEC, IXP-SPEC | Future kernel extraction should happen only when load justifies it; keep envelopes stable. |
| Reliability-weighted dispatch | DISPATCH-6, COORD-12 | Add policy evaluation and replay before advanced optimization becomes live routing. |
| Replay and simulation | RECON-7, CLAIM-NEXT-SPEC activity events | RECON-8 should build event replay and dispatch simulation. |
| Scheduler v2 evaluation | DISPATCH-2, DISPATCH-6 | DISPATCH-7 should add policy simulator, scorecards, and regret analysis. |
| Tally economics | TALLY-3, TALLY-4, TALLY-5 | Provider reconciliation and KPI baselines turn token spend into buyer-facing value. |
| Evidence retention and audit export | RECON-7, HARDEN-13 | Enterprise evidence history remains a hosted/commercial layer. |
| Fail-fix early and failure taxonomy | WORKING-AGREEMENT, SWITCHBOARD-RUNBOOK, QA-9 | BUG-15 should define failure classes and signal schema consumed by QA and intake. |
| Human review and collaboration | ACCESS-10, ACCESS-11, DISPATCH-3 | Human feedback must become structured plan state, not loose chat. |
| ActionEngine durable-workflow borrowing | DOGFOOD-11 | HARDEN-21, RECON-9, BUG-16, and RECON-10 turn the borrow map into side effects, receipts, retry discipline, and DBOS evaluation. |

---

## 9. Operating rules

1. Prefer protocol compatibility over proprietary adapters.
2. Keep hosted-only value in governance, history, optimization, policy, and economics.
3. Make every dispatch decision explainable before making it clever.
4. Treat unknown or missing data as a failing signal.
5. Preserve replayability for every lifecycle-changing event.
6. Tie strategy to live tasks; if a strategic pillar has no task owner, create one.
7. Do not extract a new kernel prematurely, but keep the boundary clean enough that it can
   move when load demands it.
