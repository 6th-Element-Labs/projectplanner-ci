# Switchboard documentation index

This is the canonical documentation front door. It separates current authority from
explanation, execution history, and evidence so old material can retain fidelity without
looking like current instruction.

## Start here

| Need | Read |
|---|---|
| Contribute code or operate as an agent | [`AGENTS.md`](../AGENTS.md) |
| Understand the system | [Current architecture](#current-architecture) below |
| Understand lifecycle authority | [Three-plane architecture packet](#three-plane-architecture-packet) |
| Understand Compand evidence and certification | [`ADR-0026 — Compand benchmark publication`](decisions/0026-compand-benchmark-publication.md) and [`CES-1`](COMPAND-BENCHMARK-STANDARD.md) |
| Build or grade the Compand Phase 2 Technique Lab | [`Phase 2 frozen benchmark contract`](compand/phase2/BENCHMARK-CARD.md) and its [machine-readable files](compand/phase2/) |
| Find an architectural decision | [`decisions/INDEX.md`](decisions/INDEX.md) |
| Follow fleet workflow and Done rules | [`WORKING-AGREEMENT.md`](WORKING-AGREEMENT.md), unless the live agreement is available |
| Operate production | [`SWITCHBOARD-RUNBOOK.md`](SWITCHBOARD-RUNBOOK.md) |
| Use the MCP surface | [`MCP.md`](MCP.md) |

## Current architecture

Switchboard is a Python 3.12 coordination control plane fronted by Caddy. SQLite remains the
canonical project-partitioned store. The live HTTP surface is being peeled by bounded context,
not replaced in one rewrite:

| Surface | Current process |
|---|---|
| Web app and compatibility routes | `:8110` |
| MCP | `:8111` |
| Auth | `:8121` |
| Tasks and exact claim routes | `:8122` |
| Selected coordination reads | `:8123` |
| Selected deliverables reads | `:8124` |
| Selected ingest/intake routes | `:8126` |
| LiteLLM gateway | `:8095` |

[`deploy/Caddyfile`](../deploy/Caddyfile) is route truth.
[ADR-0025](decisions/0025-bounded-context-service-extraction.md) explains the reusable
independence and process-cut policy; Caddy, service units, tests, and deployment evidence
show the current as-built routing.

New product code belongs in `src/switchboard/`. Root `app.py`, `mcp_server.py`, and other
root modules are compatibility entrypoints or grandfathered implementation surfaces.

## Three-plane architecture packet

This is the highest-value architecture material in the repository and must remain intact:

1. **Normative authority:** [`ADR-0008 — Three-plane separation`](decisions/0008-three-plane-separation.md)
2. **Visual and operational explanation:** [`COMPLETION-LIFECYCLE-PIPELINE.md`](COMPLETION-LIFECYCLE-PIPELINE.md)
3. **Coordinator rules:** [`COORDINATOR-CONTRACT.md`](COORDINATOR-CONTRACT.md)
4. **Capacity clock:** [`EXECUTION-LEASE-POLICY.md`](EXECUTION-LEASE-POLICY.md)

The Mermaid diagrams live in the lifecycle explainer. The ADR carries the binding C1-C3,
M1-M3, and W1-W4 rules. Neither document replaces the other.

## Document classes

| Class | Purpose | Authority and lifecycle |
|---|---|---|
| ADR | Why a durable, consequential choice was made | Binding only when accepted; preserved after supersession |
| Current guide or policy | How contributors and operators act now | Must point to underlying ADRs and executable truth |
| Spec or contract | Required product/protocol behavior | Current while implemented or explicitly planned; must have status |
| Runbook | Operational procedure | Current only while matching deployed topology |
| Execution tracker or plan | Sequence and evidence for a bounded program | Historical after the program closes; not a permanent coding guide |
| Evidence, audit, dispatch, design exploration | Proof or context | Preserved for fidelity but not normative |

Age does not make a document legacy. A document becomes legacy when it is explicitly
superseded or retired, its implementation is gone, and no current contract depends on it.
Legacy documents are retained with a visible banner or moved out of the active reading path.

## Current architecture and operating documents

- [`decisions/0006-control-plane-done-enough.md`](decisions/0006-control-plane-done-enough.md):
  provenance model, subtraction rule, and control-plane stop condition.
- [`decisions/0003-work-provenance-and-reconciliation.md`](decisions/0003-work-provenance-and-reconciliation.md):
  Git/default-branch-proven Done, evidence-backed completion, and reconciliation.
- [Three-plane architecture packet](#three-plane-architecture-packet): lifecycle authority.
- [`decisions/0019-repo-constitution.md`](decisions/0019-repo-constitution.md):
  checkout layout and front doors, distinct from repository topology.
- [`decisions/0020-merge-gates-observe-not-enforce.md`](decisions/0020-merge-gates-observe-not-enforce.md):
  dispatch enforcement, advisory merge observation, and GitHub landing authority.
- [`decisions/0023-thin-merge-queue-ci.md`](decisions/0023-thin-merge-queue-ci.md) and
  [`decisions/0024-merge-queue-admission-and-docs-lanes.md`](decisions/0024-merge-queue-admission-and-docs-lanes.md):
  one trusted CI/status/queue contract with bounded admission and fail-closed lane selection.
- [`decisions/0025-bounded-context-service-extraction.md`](decisions/0025-bounded-context-service-extraction.md):
  one reusable independence, No-Go, cutover, and rollback policy for process boundaries.
- [`decisions/0026-compand-benchmark-publication.md`](decisions/0026-compand-benchmark-publication.md):
  Compand evidence authority, whole-task economics, bounded claims, and Value Index gates.
- [`CI-STRATEGY.md`](CI-STRATEGY.md): current CI routing and provenance expectations.
- [`PROVIDER-AUTH-POLICY.md`](PROVIDER-AUTH-POLICY.md): provider credential and identity boundary.
- [`BACKUP-RESTORE-RUNBOOK.md`](BACKUP-RESTORE-RUNBOOK.md): data recovery procedure.

## Programs and as-built records

These retain important rationale and boundary detail, but should not be the first stop for
ordinary coding work:

- [`ARCH-MS-EXECUTION.md`](ARCH-MS-EXECUTION.md) and ADRs 0009, 0011-0016:
  completed modernization programs and process-cut history, superseded as current
  policy by ADR-0025.
- [`DATA-PORT-EXECUTION.md`](DATA-PORT-EXECUTION.md) and
  [`ADR-0018/storage-ports`](decisions/0018-storage-ports-backend-agnostic-data-layer.md):
  retained unaccepted storage-abstraction proposal; not current architecture.
- `phase2/`, `phase3/`, and `runbooks/`: cutover evidence and rollback procedures.
- `evidence/`, `dispatches/`, and dated audits: proof and history.
- `superpowers/plans/` and `superpowers/specs/`: working designs and implementation plans;
  they do not override accepted ADRs.

## Research and product exploration

These documents are evidence and design exploration, not accepted architecture:

- [`TOKEN-OPTIMIZATION-OVERVIEW.md`](TOKEN-OPTIMIZATION-OVERVIEW.md):
  executive summary, table of contents, current product contract, and reading path.
- [`TOKEN-OPTIMIZATION-CLOUD-RESEARCH.md`](TOKEN-OPTIMIZATION-CLOUD-RESEARCH.md):
  context-efficiency techniques, lineage, risks, and staged thesis.
- [`TOKEN-OPTIMIZATION-MARKET-ANALYSIS.md`](TOKEN-OPTIMIZATION-MARKET-ANALYSIS.md):
  dated market signals, threats, hypotheses, and evidence quality.
- [`TOKEN-OPTIMIZER-TECH-DEEP-DIVE.md`](TOKEN-OPTIMIZER-TECH-DEEP-DIVE.md):
  standalone protocol, state, transform, evidence, and trust design.
- [`COMPAND-CODEX-RESPONSES-WIRE-CONTRACT.md`](COMPAND-CODEX-RESPONSES-WIRE-CONTRACT.md):
  frozen Codex Responses fixture tuple, byte/order passthrough rules, typed tool-output
  eligibility, and dual-ledger continuation contract for the Compand pilot.
- [`COMPAND-SCAN-EVIDENCE.md`](COMPAND-SCAN-EVIDENCE.md):
  content-free coverage reconciliation, shadow `line-rle-v1` economics, bounded
  decisions, and immutable evidence reproduction for DOGFOOD-32.
- [`TOKEN-OPTIMIZER-FEASIBILITY-DEEP-DIVE.md`](TOKEN-OPTIMIZER-FEASIBILITY-DEEP-DIVE.md):
  observed insertion evidence, coverage boundaries, and E1–E5 falsification plan.
- [`WHY-NOT-FORK-ROUTELLM.md`](WHY-NOT-FORK-ROUTELLM.md):
  RouteLLM build-versus-adopt analysis.
- [`COMPAND-BENCHMARK-STANDARD.md`](COMPAND-BENCHMARK-STANDARD.md):
  normative CES-1 experiment, grading, publication, and reproduction procedure
  governed by ADR-0026.
- [`compand/phase2/BENCHMARK-CARD.md`](compand/phase2/BENCHMARK-CARD.md):
  frozen Phase 2 arms, technique inventory, estimands, statistical rules, hard gates,
  grades, KPI bindings, and machine-readable publication contract.
- [`compand/phase2/LAB-REPLAY.md`](compand/phase2/LAB-REPLAY.md):
  one-process, one-technique development replay command and immutable evidence layout.
- [`TOKEN-OPTIMIZATION-BUSINESS-MODEL.md`](TOKEN-OPTIMIZATION-BUSINESS-MODEL.md)
  and `TOKEN-OPTIMIZATION-BUSINESS-MODEL.xlsx`: assumption-driven market, revenue,
  and unit-economics scenarios; not an investment-grade forecast.

## Maintenance rules

- Add a new ADR only for a durable choice with meaningful alternatives and consequences.
- Put coding rules in `AGENTS.md`; link to the ADR that explains why.
- Put temporary sequencing on the board or in a clearly labelled execution tracker.
- Every ADR reference with a duplicated number must use a filename link or canonical key.
- Never delete an accepted ADR to make the corpus tidy. Supersede it and preserve the record.
- When code and docs diverge, fix or explicitly reclassify the document. Do not leave a stale
  document looking current.
