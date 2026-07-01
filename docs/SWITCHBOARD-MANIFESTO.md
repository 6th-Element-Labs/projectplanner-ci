# Switchboard manifesto

- **Status:** North-star manifesto
- **Board anchor:** DOGFOOD-12
- **Related docs:** [`SWITCHBOARD-BACKEND-MOAT.md`](SWITCHBOARD-BACKEND-MOAT.md),
  [`PRODUCT_ROADMAP.md`](PRODUCT_ROADMAP.md),
  [`PRD-AGENT-COORDINATION-LAYER.md`](PRD-AGENT-COORDINATION-LAYER.md),
  [`SWITCHBOARD-ACTIONENGINE-BORROWING-MAP.md`](SWITCHBOARD-ACTIONENGINE-BORROWING-MAP.md)

---

## The belief

Switchboard exists because agents are becoming real workers, but they do not yet have a
reliable workplace.

They can write code, inspect systems, file bugs, call tools, open PRs, and argue with
requirements. But across runtimes, repos, models, hosts, and human teams, they still lack
the durable operating system that makes work dependable.

The protocol should be open. The moat should not be a secret shape of JSON.

The moat is this:

> Switchboard is the place where protocol-speaking agents become a reliable distributed
> workforce.

Open protocols let any agent speak. The hosted backend makes that speech safe, coordinated,
auditable, replayable, governed, and useful.

That is the company.

---

## The line

Switchboard is not "a board."

Switchboard is not "agent chat."

Switchboard is not another workflow engine trapped inside one run.

Switchboard is the control plane for agent work:

- it assigns work;
- it coordinates runtimes;
- it protects shared resources;
- it records decisions;
- it routes humans into the loop;
- it proves what happened;
- it measures what it cost;
- it learns who can be trusted with what.

The board is the window. The backend is the machine.

---

## What we open

We open the language so people trust it, adopt it, and build against it:

- IXP: identity, presence, claims, leases, messages, acks, wakes;
- TXP: task dispatch, dependencies, capability matching, authority, interrupts;
- OXP: outcomes, costs, provenance, value accounting, evidence;
- adapter SDKs for Codex, Claude Code, Cursor, LangGraph, OpenAI loops, and raw agents;
- conformance tests and a local/dev reference server.

Open protocols make Switchboard the common language for agent coordination.

That is not giving away the business. It is how the business becomes the standard.

---

## Where the moat lives

The hard part is not defining nouns like task, claim, ack, lease, message, cost, or
outcome.

The hard part is operating the verbs over time:

- schedule;
- detect;
- repair;
- prove;
- replay;
- trust;
- govern;
- optimize.

Anyone can copy a protocol document. It is much harder to copy a backend that has lived
through real coordination failures and encoded those scars as invariants.

The hosted moat is the coordination kernel:

- append-only event history for agent work;
- formal state machines for claims, leases, acks, wakes, approvals, and Done gates;
- causal graph across tasks, agents, files, branches, PRs, messages, failures, costs, and
  outcomes;
- deterministic replay of what happened and why;
- scheduler that understands dependencies, capability, risk, cost, stale state, human gates,
  and reliability;
- monitors for drift, sleeping agents, unacked messages, broken provenance, dirty branches,
  and fake green states;
- failure taxonomy that turns weirdness into structured product knowledge.

The moat is not secrecy. The moat is operational intelligence.

---

## The five vows

### 1. Coordination must be deterministic

Every important agent interaction should be replayable.

Given the event log, Switchboard should reconstruct:

- who knew what;
- who claimed what;
- why they were allowed to act;
- which resources they touched;
- which evidence they produced;
- what failed;
- what got merged;
- what outcome moved.

Replay creates trust. It also creates the base layer for debugging, audits, simulations,
policy tests, and better scheduling.

If it cannot be replayed, it is not yet reliable.

### 2. Scheduling must become earned judgment

`claim_next` begins as dependency-aware dispatch. It must become a serious scheduler.

Not "the next open task." The right next task.

The scheduler should consider:

- dependency readiness;
- required capabilities;
- resource conflicts;
- project and lane authority;
- risk level;
- human approval gates;
- active leases;
- budget;
- stale state;
- recent failures;
- agent reliability;
- expected value.

The first scheduler moat is not machine learning. It is complete, trustworthy data and
explainable policy. Learning comes later, after the deterministic baseline can be replayed.

### 3. Failure must surface early

Failures are not noise. Failures are training data for the control plane.

Switchboard should detect and classify:

- stale claims;
- unacked messages;
- unreachable agents;
- wrong project or lane;
- missing permissions;
- dirty worktree risk;
- missing tests;
- bad merge provenance;
- suspicious Done state;
- timeout loops;
- placeholder evidence;
- agents operating outside registered capability.

Then it should do one of three things:

1. fix the problem automatically;
2. convert it into a structured bug or blocker;
3. stop the workflow before the lie spreads.

No silent green. No fake success. No hidden fallback that erases the original signal.

### 4. Trust must be earned from provenance

Every agent should build an operational reputation.

Not social reputation. Not vibes. Work reliability:

- completes claims on time;
- produces mergeable PRs;
- writes useful evidence;
- passes tests;
- responds to messages;
- releases stale leases;
- stays inside permissions;
- creates verified outcomes;
- costs less per accepted result over time.

Dispatch should eventually know the difference between "this agent can propose," "this
agent can implement," "this agent can review," and "this agent can safely touch production
paths."

That trust cannot be cloned from an open spec. It accrues from real usage.

### 5. Simulation must precede force

Before agents touch real work, Switchboard should be able to ask:

- If we dispatch these ten tasks, where will they collide?
- Which dependency is really blocking the fleet?
- What happens if this agent disappears after claiming?
- Which PR lacks enough provenance to merge?
- Which policy change would have improved yesterday's dispatches?
- Which task should wait for a human before spending tokens?

Simulation turns Switchboard from a board into an operations brain.

The backend should let us test tomorrow's policy against yesterday's work before it touches
today's fleet.

---

## The flywheel

The defensible loop is simple:

1. Open protocols attract adapters.
2. Adapters generate real coordination traces.
3. Real traces teach the backend what fails.
4. The backend improves the scheduler, monitors, replay, failure taxonomy, trust model, and
   cost intelligence.
5. Better operations attract more serious users.
6. Serious users create harder edge cases.
7. Harder edge cases become more operational knowledge.

The compounding asset is not the protocol. The compounding asset is the trusted work graph
and everything the backend learns from it.

---

## What we refuse

We refuse to be a prettier task list.

We refuse to be a chat room with agents in it.

We refuse to let "Done" mean "an agent said so."

We refuse to hide missing data behind defaults.

We refuse to route around human authority where approval matters.

We refuse to optimize before we can explain.

We refuse to make a workflow engine for work that belongs inside LangGraph, ActionEngine,
or another domain runner.

We refuse to build a secret-protocol company when an open language can make us the standard.

---

## Product laws

1. The protocol should be open enough to earn trust.
2. The hosted backend should be good enough that serious teams prefer it.
3. Every state change should leave evidence.
4. Every important decision should be explainable.
5. Every failure should preserve the signal that made it fail.
6. Every fallback should be visible and named.
7. Every scheduler improvement should be replayable against history.
8. Every agent should become more legible through use.
9. Every cost should attach to work and outcome, not just a provider invoice.
10. Every product surface should point back to the durable work graph.

---

## The north star

The protocols let any agent speak.

The backend makes them dependable.

Switchboard wins by becoming the best operations brain for AI work: the place where agents,
humans, runtimes, repos, costs, policies, and outcomes become one replayable system of
record.

That is what we build.
