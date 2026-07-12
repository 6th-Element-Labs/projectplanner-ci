# Switchboard CI Strategy — Provenance-Safe, Fleet-Universal CI Routing

- **Status:** Active (Phase 1 shipped). Engine is [`external_ci_mirror.py`](../external_ci_mirror.py) (REPO-1…4 / CI-MIRROR-2); spec in [`EXTERNAL-CI-MIRROR-SPEC.md`](EXTERNAL-CI-MIRROR-SPEC.md).
- **Scope:** How CI runs for every repo Switchboard coordinates — our own (`Helm`, `projectplanner`) and customer projects — as one uniform, declarative capability.
- **Relates to:** [ADR-0003 work-provenance](decisions/0003-work-provenance-and-reconciliation.md) · `repo_topology` in `store.py` · `external_ci_mirror.py`

---

## Decision (TL;DR)

**CI is a per-project *route*, declared in `repo_topology` — not a fixed pipeline.** One framework, one agent flow, one provenance model; three interchangeable routes chosen per repo by its constraints:

| Route | Runs where | Code stays private? | Cost | Handles macOS/heavy? | Default for |
|---|---|---|---|---|---|
| **A. Public CI mirror** (`external_ci_mirror`) | free GitHub-hosted runners on a **public** mirror repo | no (ephemeral, test-only branch) | **$0**, any account | **yes** (free hosted macOS) | small-budget / personal-account / expensive-CI / open-source repos → **incl. Helm** |
| **B. Self-hosted runner** | standard GitHub Actions on **our own dedicated runner box** | **yes** | $0 minutes (our compute) | Linux yes; macOS needs Mac hardware | **enterprise clients who refuse public code** |
| **C. Hosted on canonical** | GitHub-hosted runners on the private repo | **yes** | draws the account's included minutes | yes (billed) | orgs with ample allowance + cheap CI (e.g. `projectplanner`) |

The **invariant that makes all three safe** (below) is the actual product: *where tests run is decoupled from what is trusted.*

---

## Context — why no off-the-shelf CI fits us

Switchboard coordinates a **fleet of agent-driven repos across mixed GitHub accounts**, each with different cost/privacy constraints. A single fixed pipeline cannot serve all of them:

- **Personal accounts have tiny CI budgets** (`StevenRidder`, Pro ≈ 3,000 min/mo). `Helm` runs **macOS CI that bills at 10×** (a 120-min job = ~1,200 billed min) — **two runs exhausts the whole personal budget.** Only *public-repo* Actions (free, unlimited, incl. macOS) make Helm economical.
- **Org accounts have ample allowance** (`6th-Element-Labs`, enterprise, net $0/mo) — for them private-repo CI is essentially free.
- **Enterprise customers will refuse to put code in a public repo** at all — for them the public route is a non-starter.
- **Our production box is a 1 GB VM** that must never run CI (it melted down doing exactly that — see HARDEN-32).

Generic CI assumes one repo, one account, one budget. We need a layer that **adapts per project while keeping the agent experience and the trust model identical everywhere.** That layer is the edge.

---

## The core idea (and the market edge): authority-separated CI routing

`repo_topology` assigns every repo a **role with an authority**:

- `canonical` (private) → **the only** `["done", "merge_provenance", "code_truth"]` authority.
- `public_ci` / self-hosted / hosted → **`["verification_only"]`** — evidence, never truth.
- A fail-closed `code_repo_gate` refuses to satisfy "Done" if no canonical repo is configured.

**Because verification roles can *never* satisfy Done, tests can execute anywhere — even a public repo, even a customer's own runner — without that location ever becoming authoritative or trusted.** The canonical private repo remains the sole source of merge-provenance and completion.

For an **agent-fleet coordination platform**, this is the differentiator, not a footnote:

- **Onboard any customer repo** — free-tier personal, enterprise, or open-source — and give it *working, appropriately-priced CI* plus a *uniform agent workflow* from a single declarative contract.
- **Provenance integrity is guaranteed regardless of CI routing.** When AI agents mark work complete, "Done" is only ever stamped from the canonical repo's real merge — so nothing about *where* CI ran can forge completion. Competing agent tooling routes work without this guarantee; generic CI has no notion of it. This is CI routing as a **first-class, provenance-safe, fleet-adaptive capability of the coordination layer** — that is the leading edge.

---

## The engine: `external_ci_mirror` (do not reinvent it)

Route A is implemented by the first-class **`external_ci_mirror`** runner + the `external_ci_runs` store model (built by REPO-1…4 / CI-MIRROR-2). One call —
`external_ci_mirror.request_external_ci_mirror_run(request, source_path, project)` — resolves the source/mirror repos and status context from `repo_topology`, then **pushes the exact source SHA to a disposable `ci/…` branch, dispatches the workflow, polls to a terminal status, and writes an `external_ci_run` back to Switchboard** with a structured `failure_class` (`mirror_sync_failed` / `workflow_trigger_failed` / `workflow_poll_failed` / `workflow_failed`) and run-URL evidence. It shells out to `gh` (must be installed + `GH_TOKEN` present on the caller).

**Do not build a second mirror path.** A prior iteration added an inline `run_sandbox_gate` and ported Helm's `ci-sandbox.sh`; both duplicated `external_ci_mirror` and were **retired** (ADR-0006 subtraction rule). Agents drive Route A via the `request_external_ci_mirror_run` MCP tool; the PR gate drives it programmatically.

---

## The provenance invariant (non-negotiable)

1. `canonical` is the **only** repo that can mark a task Done / carry merge-provenance.
2. Every other route posts **verification evidence only** (a commit status / `external_ci_run`), never Done.
3. The merge webhook + reconcile stamp Done **only** from the canonical default-branch merge.
4. `external_ci_mirror` verifies the **exact source SHA** on the mirror — the tested code *is* the code that merges.

This is why Route A is safe for private code: the public mirror is a disposable test runner that can never speak for "Done."

---

## Fleet mapping (concrete)

| Repo | Account | Constraint | Route |
|---|---|---|---|
| **Helm** | `StevenRidder` (personal) | tiny budget + macOS 10× | **A — public mirror** (only economical option) |
| **projectplanner** | `6th-Element-Labs` (org) | ample allowance, cheap Linux CI | **A today** (fleet uniformity); C is the equally-valid simplification |
| **Enterprise customer** | their org | code must not go public | **B — self-hosted runner** (their compute, code private) |
| **Open-source project** | any | code already public | **A** (natural fit) |

We run **A across our own fleet** so the tooling, agent flow, provenance, and UI are identical for Helm and projectplanner.

---

## What exists vs. what to build (honest gap)

**Built + shipped:**
- `repo_topology` schema — roles, authority, `required_status_contexts`; MCP tools (`set_project_repo_topology`, …); agent session-prompt guidance ("public_ci = verification evidence only").
- **`external_ci_mirror` engine** — push/dispatch/poll/record, topology-resolved, structured failure classes + evidence + tests.
- **Phase 1 (this):** `switchboard` topology configured (`public_ci = 6th-Element-Labs/projectplanner-ci`); the on-box PR gate (`switchboard_pr_gate.py`) now calls `external_ci_mirror` when `public_ci` is configured instead of building a venv + running the suite locally; provenance preflight + SESSION-12 claim gate unchanged; the venv path remains as the fallback for repos without a `public_ci` role. The HARDEN-32 CPU hog (per-PR venv+suite) is retired.

**To build (turns the capability into a one-click product):**
1. **Provision-on-opt-in** — create/register the mirror repo, seed it, install the workflow, set branch protection, all from the topology (today it's manual).
2. **Route B stand-up** — a dedicated (not prod-box) or autoscaling self-hosted runner for the no-public case.
3. **UI** — a per-project CI-strategy selector + live `external_ci_runs` status; no hand-run commands.

---

## Rollout phases

- **Phase 0 — Proven:** Route A validated on projectplanner; live on Helm.
- **Phase 1 — Consolidate (DONE):** topology-driven gate on `external_ci_mirror`; on-box venv test-runner retired; duplicate `run_sandbox_gate` + `ci-sandbox.sh` removed.
- **Phase 2 — Automate provisioning:** opt-in creates + wires a mirror from the topology.
- **Phase 3 — Route B:** dedicated/autoscaling self-hosted runner as the private fallback.
- **Phase 4 — UI:** project-settings strategy selector + status.

---

## Risks & honest caveats

- **Route A briefly exposes source on a public repo.** Mitigations: ephemeral `ci/…` branches, a secrets/history scan gate before first push, and **Route B/C for anyone who can't accept it.** No credentials are committed (verified).
- **Route A needs `gh` + `GH_TOKEN` on the caller** (the box, or the agent's machine). Provisioned on the prod box in Phase 1.
- **Self-hosted (B) is standard GitHub Actions on a *separate* machine** — never the prod web box (that was the HARDEN-32 mistake).
- **Free macOS only exists on public runners**, so macOS-heavy private repos either accept Route A or pay for Mac hardware under B.

## Native merge queue (Lever 2 — HARDEN-70 / CI-3)

The native merge queue lets many agents land PRs without serializing on a hand-run merge: GitHub
batches queued PRs into a merge group, tests base+PRs together, and fast-forwards master when the
required checks pass. The catch: those checks run against the merge group's `gh-readonly-queue/*`
branch head, **not** the PR head. Our required check is an external commit status, so
`switchboard_pr_gate.py` must post `Switchboard CI / VM gate` to the merge-group head SHA or the
queue hangs forever. Pass 3 of the gate does exactly that (same external-mirror → local-fallback
policy as the PR pass; idempotent per immutable SHA). Enabling the queue is a **deploy-ordered
operator step** — flip master branch protection *after* the Pass-3 gate is live on the Plan VM, or
the queue hangs on day one. See `docs/SWITCHBOARD-RUNBOOK.md` → "Native merge queue".

## Non-goals

- Open-sourcing the products (Route A publishes test-only, ephemerally — not a release).
- Running CI on the production web box, ever.
- Letting any non-canonical repo speak for "Done."
- A second CI-mirror mechanism. `external_ci_mirror` is the one engine.
