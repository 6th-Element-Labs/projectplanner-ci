# IXP Public Package

- **Status:** Public adoption package v0.1
- **Board anchor:** PROTO-6
- **Date:** 2026-07-06
- **Related docs:** [`IXP-SPEC.md`](IXP-SPEC.md), [`CLAIM-NEXT-SPEC.md`](CLAIM-NEXT-SPEC.md),
  [`TALLY-SPEC.md`](TALLY-SPEC.md), [`IXP-CONFORMANCE.md`](IXP-CONFORMANCE.md),
  [`RUNTIME-ADAPTERS-SPEC.md`](RUNTIME-ADAPTERS-SPEC.md),
  [`SWITCHBOARD-BACKEND-MOAT.md`](SWITCHBOARD-BACKEND-MOAT.md)

This is the public-facing package boundary for Switchboard's protocol layer. It answers four
questions a runtime author, customer, or open-source contributor will ask first:

1. What can I implement without adopting the hosted product?
2. What does a conformance badge mean?
3. What license and governance posture should the public materials use?
4. What remains hosted/commercial?

## 1. Public package contents

The public package should contain the parts required for trust and ecosystem adoption:

| Package item | Public status | Notes |
|---|---|---|
| `IXP-core` spec | Public | Normative identity, presence, leases, messages, signals, delta, handshake, activity log. |
| `+TXP` dispatch profile | Public spec, product implementation | Task claims, dependency-aware dispatch, completion evidence, merge gates. |
| `+OXP`/Tally profile | Public spec, hosted product depth | Outcome and cost vocabulary can be public; provider reconciliation and analytics are commercial value. |
| Runtime adapters | Public | Claude Code, Codex, Cursor, LangGraph, raw OpenAI loop, and generic REST/MCP guidance. |
| Conformance fixture | Public | `adapters/conformance.py` is the reference smoke and badge evidence source. |
| Local/dev reference server | Public | Enough to validate the protocol and run a small team locally. |
| Hosted Switchboard service | Commercial | Managed auth, policy, Tally, durable graph, replay, audit, entitlements, managed runners. |

## 2. License posture

Recommended public extraction license:

- Protocol documents, schemas, examples, and badge text: **Apache-2.0**.
- Runtime adapter packs, conformance fixture, and local/dev reference server: **Apache-2.0**.
- Hosted Switchboard service, production policy engine, managed runner operations, enterprise
  identity/entitlement plumbing, long-term evidence graph, replay/simulation tooling, and Tally
  provider reconciliation: **commercial/proprietary unless explicitly released later**.

Important: this repository does not currently include a root `LICENSE` file. Until that file lands,
do not imply that the entire private `projectplanner` repository has been relicensed. Public
publishing should either add the intended license files during extraction or publish from a
separate repository/package with explicit license metadata.

## 3. Governance posture

The public protocol should be governed like an interoperability standard, not like an internal
feature backlog.

Public governance rules:

- Version the wire contract (`ixp.v1`, future `ixp.v2`) and keep compatibility notes in the spec.
- Accept public issues and pull requests for clarifications, examples, adapter fixtures, and
  conformance checks.
- Require conformance changes to include tests or fixture updates.
- Treat new mandatory wire fields as versioned changes, not quiet edits.
- Keep hosted/commercial policy decisions out of the normative protocol unless they affect wire
  interoperability.

Maintainer authority remains with 6th Element Labs for:

- version acceptance;
- conformance badge wording;
- trademark/name usage;
- security policy;
- hosted/commercial product boundaries.

## 4. Open-core boundary

The shortest version:

> Open the language. Sell the governed workplace.

Open materials should let any runtime speak Switchboard. They should not give away the accumulated
operational advantage of the hosted control plane.

Open:

- stable IXP/TXP/OXP envelopes;
- adapter SDKs and examples;
- conformance smoke tests;
- local/dev reference runtime;
- protocol issue process and compatibility notes.

Commercial / hosted:

- production identity, RBAC, scoped tokens, enterprise SSO, and entitlement ledgers;
- managed runner control, wake routing, kill/snapshot policies, and fleet supervision;
- Tally dashboards, provider bill reconciliation, budgets, and cost confidence grading;
- durable evidence graph, audit exports, replay, simulation, and reliability learning;
- hosted policy, approval gates, coordinator recommendations, and enterprise integrations.

## 5. Public badge language

Use the badge language in [`IXP-CONFORMANCE.md`](IXP-CONFORMANCE.md). In short:

- **IXP-core conformant** means the implementation passes all core handshake, presence, leases,
  messaging, delta, auth, idempotency, and activity-log checks.
- **IXP-core + TXP/OXP tested** means the implementation also passed the optional dispatch and
  Tally checks in the reference fixture.
- **Switchboard-compatible adapter** means the runtime pack advertises its protocol envelope,
  fails closed on incompatible versions, and publishes a passing conformance statement.

Do not claim:

- mid-token interrupt delivery;
- hard process kill as an IXP-core wire feature;
- hosted policy/Tally/replay equivalence from a local-only protocol implementation;
- conformance without naming the fixture version, command, and verification date.

## 6. Release checklist

Before publishing a public package:

- Add explicit license files or publish from a repository that already has them.
- Include `IXP-SPEC.md`, `CLAIM-NEXT-SPEC.md`, `TALLY-SPEC.md`, `IXP-CONFORMANCE.md`, and this file.
- Include `adapters/conformance.py` and at least one adapter README.
- Run `python3 adapters/conformance.py --json` and commit or publish the capability statement.
- Make the open-core boundary visible in the public README.
- Keep internal deployment secrets, customer boards, private evidence, and hosted operations out of
  the public package.
