# Open-core release and repository packaging plan

- **Status:** Approved packaging decision; external launch remains gated
- **Board anchor:** DOGFOOD-8
- **Decision date:** 2026-07-22
- **Owner:** Switchboard Operator
- **Related:** [`IXP-PUBLIC-PACKAGE.md`](IXP-PUBLIC-PACKAGE.md),
  [`RUNTIME-ADAPTERS-SPEC.md`](RUNTIME-ADAPTERS-SPEC.md),
  [`SWITCHBOARD-BACKEND-MOAT.md`](SWITCHBOARD-BACKEND-MOAT.md),
  [`SECURITY.md`](../SECURITY.md)

This document is the operational gate for an external Switchboard open-core launch. It fixes the
repository boundary, licensing, public positioning, security posture, adapter promise, namespace
checks, and objective go/no-go criteria. It does not relicense the private canonical repository.

## 1. Decision

Create a new public repository, provisionally `6th-Element-Labs/switchboard`, by allow-list
extraction from the private canonical repository. Do not convert the canonical repository to
public and do not publish it as a history-preserving mirror. The private canonical repository
remains the product's code-truth and merge authority; the public repository is the source of truth
only for the explicitly released protocol, SDK, adapter, and local-development packages.

Use a one-way, reviewed release manifest. Every public path must be named in that manifest, pass a
secret/private-data scan, and receive an independent diff review before publication. Public fixes
may be developed publicly, but product integration is an explicit upstream port rather than an
automatic reverse sync.

The product boundary is:

> Open the coordination contract and runtime on-ramp. Sell the governed workplace.

### Public repository

| Area | Initial public content | License |
|---|---|---|
| Protocol | IXP/TXP/OXP specifications, JSON schemas, compatibility notes, examples | Apache-2.0 |
| Conformance | Reference fixture, badge rules, capability-statement format | Apache-2.0 |
| Adapters | Claude Code, Codex, Cursor, LangGraph, raw OpenAI, and generic MCP/REST packs | Apache-2.0 |
| SDK | Small typed protocol clients and lifecycle helpers required by adapters | Apache-2.0 |
| Local development | Single-node reference server, synthetic fixtures, dev harness | Apache-2.0 |
| Project hygiene | Public README, CONTRIBUTING, CODE_OF_CONDUCT, SECURITY, changelog, notices | Apache-2.0 / CC0 where stated |

The public local server is a compatibility and evaluation runtime, not a feature-equivalent copy
of hosted Switchboard. Public schemas may describe hosted extensions so clients can interoperate;
that does not require publishing the hosted implementation.

### Private or hosted product

- production identity, RBAC, enterprise SSO, entitlements, and tenant administration;
- managed runners, wake routing, fleet supervision, snapshots, kill policy, and cloud execution;
- durable customer work/evidence graphs, retention, audit exports, replay, and simulation;
- hosted dispatch policy, coordinator recommendations, approval gates, and reliability learning;
- Tally provider reconciliation, budgets, economics dashboards, and cost/outcome intelligence;
- commercial integrations, operations, deployment automation, billing, and support systems;
- private board data, customer material, production configuration, credentials, and incident data.

When a module mixes public contract code with hosted policy, extract the contract behind a narrow
interface and keep the hosted implementation private. Never publish by deleting known-private
files from a full copy: build from the allow-list instead.

## 2. Repository and package layout

The public repository should begin with this stable shape:

```text
docs/                 protocol, compatibility, security, governance
schemas/              versioned wire schemas
sdk/python/           typed client and lifecycle helpers
adapters/<runtime>/   install bundle, configuration, quickstart, capability statement
conformance/          fixture, test vectors, badge verification
reference-server/     local/dev implementation and synthetic fixtures
examples/             secret-free minimal integrations
```

Version the protocol independently from SDKs and adapters. Publish Git tags and GitHub releases
from the public repository. Package names should be organization-scoped; do not depend on the
unscoped `switchboard` name. The first proposed package is
`@6th-element-labs/switchboard-adapter`, with runtime entry points or separately scoped runtime
packages added only when their install experience requires it.

The public README must say, above the fold:

1. Switchboard is a model-agnostic agent coordination protocol and hosted control plane.
2. This repository contains the open protocol, adapters, conformance kit, and local reference
   runtime—not the hosted product.
3. A five-minute quickstart connects one supported runtime and runs conformance locally.
4. Self-hosted local use is supported on a best-effort community basis; hosted governance,
   managed operations, enterprise controls, and support are commercial.
5. Conformance means protocol compatibility, not hosted feature parity or security certification.

## 3. License, contribution, and trademark policy

Use Apache License 2.0 for released code, specifications, schemas, and examples because it provides
a permissive grant with an explicit patent license. Each package must declare its license and the
public repository must include `LICENSE` and `NOTICE`. No file inherits a public license merely
because a similar private file exists in the canonical repository.

Require a Developer Certificate of Origin (`Signed-off-by`) for contributions initially. Do not
add a CLA until a concrete relicensing or enterprise contribution need justifies the friction.
Document maintainer authority, versioning, security handling, and conformance-mark approval in
`GOVERNANCE.md`.

“Switchboard,” its logos, and the phrases “Switchboard Certified” and “Switchboard-compatible” are
not granted by Apache-2.0. Publish a trademark policy before launch: nominative use is allowed to
describe compatibility; names, logos, or badge claims that imply sponsorship require written
permission. The conformance badge must identify the fixture version and verification date.

This is a product decision, not legal advice. Counsel must approve the license/NOTICE text,
contribution policy, and trademark policy before the launch gate can pass.

## 4. Availability checks

Preliminary checks made on 2026-07-22 establish direction but do not reserve a name:

| Surface | Result | Decision |
|---|---|---|
| GitHub `6th-Element-Labs/switchboard` | GitHub API returned 404 | Candidate appears unoccupied; create it privately first, then make it public at launch. |
| npm `switchboard` | Registry returned 200 | Unavailable; never rely on the unscoped name. |
| npm `@6th-element-labs/switchboard` | Registry returned 404 | Candidate appears unoccupied; verify organization ownership and reserve a scoped placeholder. |
| `switchboard.com`, `.io`, `.run` | Resolve to existing hosts | Treat as unavailable unless already controlled; do not make them launch dependencies. |
| `switchboard.dev` | No A record | Inconclusive, not availability; registrar/WHOIS and ownership checks are still required. |
| Trademark | Not cleared | Blocking: counsel must search relevant jurisdictions/classes and approve the chosen product and badge language. |

HTTP 404 and DNS results are not ownership guarantees. Immediately before announcement, record a
dated release receipt containing GitHub organization permission, npm scope ownership, registrar
status for the chosen domain, social/package namespaces actually used, and counsel's trademark
clearance. Avoid acquiring unrelated names merely to satisfy this checklist.

## 5. Security and privacy caveats

The public release must fail closed if any of these checks fail:

- scan the complete extracted tree and Git history being published for secrets, tokens, private
  keys, internal URLs, customer identifiers, board exports, logs, databases, and personal data;
- generate the public repository from a clean allow-list staging directory, never the developer's
  working tree or private `.git` history;
- replace production endpoints, tokens, identities, and examples with synthetic fixtures;
- pin or bound dependencies, generate an SBOM, run dependency/license review, and enable automated
  vulnerability reporting;
- publish a private security contact and coordinated-disclosure instructions before accepting
  issues; do not ask reporters to disclose vulnerabilities in public issues;
- threat-model the reference server and label it local/development-only until its auth, tenancy,
  storage, and deployment defaults pass a separate production-hardening review;
- state that adapters inherit the security boundary of their runtime and that bearer credentials
  must be least-privilege, project-scoped, redacted from logs, and never committed;
- prohibit the conformance badge from claiming security, availability, or hosted-feature parity.

The private repository's [`SECURITY.md`](../SECURITY.md) is input to the public policy, but the
public repository needs its own reachable disclosure instructions and supported-version table.

## 6. Adapter support matrix at launch

Support labels are promises, not a list of code that happens to exist.

| Runtime | Launch tier | Required proof |
|---|---|---|
| Claude Code | Supported | Install bundle, MCP auth setup, lifecycle/conformance pass, fresh-start and resume notes |
| Codex CLI | Supported | Install bundle, MCP auth setup, lifecycle/conformance pass, fresh-start and resume notes |
| Cursor | Preview | MCP setup, identity caveats, conformance pass for supported boundaries, explicit advisory-control limits |
| LangGraph | Preview | Package example, node-boundary hooks, conformance pass, checkpoint/resume example |
| Raw OpenAI agent loop | Reference | Minimal SDK example and conformance pass; no managed-runtime claim |
| Generic REST/MCP client | Reference | Protocol quickstart, auth example, and test vectors |

“Supported” requires CI on each advertised OS/runtime combination, a named maintainer, documented
upgrade and rollback paths, and a response target for security regressions. “Preview” may change
between minor versions and has no response-time promise. “Reference” demonstrates interoperability
only. Unsupported lifecycle or control features must be marked `false` in the adapter capability
statement rather than simulated or silently omitted.

## 7. Launch criteria

External launch is **No-Go** until every required item below has an owner and a dated evidence
link in the release receipt.

### Product and legal

- [ ] Counsel approves Apache-2.0/NOTICE, DCO, trademark policy, badge wording, and name clearance.
- [ ] GitHub repository, npm scope/package, and chosen domain/landing URL are owned and protected
  with organization MFA and least-privilege release access.
- [ ] Public README, governance, contributing, code of conduct, security policy, support tiers,
  and commercial boundary are reviewed together and contain no contradictory promise.

### Package integrity

- [ ] The allow-list manifest exactly matches the release tree; no deny-list-only export is used.
- [ ] Secret/privacy scanning passes on the tree and all history that will become public.
- [ ] License inventory and SBOM pass; every bundled dependency is redistributable.
- [ ] A release candidate can be rebuilt from a canonical source SHA, and its checksum, source
  SHA, extractor version, and reviewer approvals are recorded.

### Technical quality

- [ ] Protocol schemas, examples, and compatibility/versioning rules agree.
- [ ] `adapters/conformance.py --json` passes from a clean checkout against the local reference
  server and publishes a versioned capability statement.
- [ ] Claude Code and Codex supported-tier quickstarts pass on every advertised platform; Cursor
  and LangGraph preview limitations are visible before installation.
- [ ] The five-minute quickstart is reproduced by someone who did not author it.
- [ ] Security threat model, dependency scan, code tests, packaging tests, and clean-machine install
  tests have no unresolved critical/high findings.

### Operations and launch readiness

- [ ] Release signing, protected tags, changelog, rollback/yank procedure, and incident owner are
  tested in a dry run.
- [ ] Public issue templates route security reports privately and separate protocol, adapter,
  documentation, and hosted-product requests.
- [ ] At least two maintainers can cut and revoke a release; no launch-critical credential belongs
  only to one person's account.
- [ ] Announcement copy links to the exact tagged docs and does not imply production readiness of
  the local server, security certification, or hosted parity.

The release owner records **Go** only after all boxes pass. Any secret exposure, unclear ownership,
failed clean install, critical/high vulnerability, license conflict, or trademark objection resets
the decision to **No-Go** and requires a new release candidate.

## 8. Release receipt

For each release candidate, commit a machine-readable receipt in the public repository containing:

```json
{
  "schema": "switchboard.public-release.v1",
  "version": "0.1.0",
  "canonical_source_sha": "<private canonical SHA>",
  "public_commit_sha": "<public SHA>",
  "extractor_version": "<tag or SHA>",
  "conformance_fixture_version": "<version>",
  "checks": {},
  "approvals": [],
  "released_at": "<RFC3339 timestamp>"
}
```

The receipt proves what was reviewed and released without publishing private repository history or
private evidence. Public release evidence never changes the canonical repository's authority over
hosted product code or Switchboard task completion.
