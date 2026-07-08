# Switchboard CI Strategy — Provenance-Safe, Fleet-Universal CI Routing

- **Status:** Proposed (supersedes the ad-hoc setup in [`CI-SANDBOX.md`](CI-SANDBOX.md), which becomes the detail doc for one route)
- **Scope:** How CI runs for every repo Switchboard coordinates — our own (`Helm`, `projectplanner`) and customer projects — as one uniform, declarative capability.
- **Relates to:** [ADR-0003 work-provenance](decisions/0003-work-provenance-and-reconciliation.md) · `repo_topology` in `store.py` · `scripts/ci-sandbox.sh`

---

## Decision (TL;DR)

**CI is a per-project *routing* decision declared in `repo_topology`, not a fixed pipeline.** One framework, one agent flow, one provenance model — three interchangeable *routes* chosen per repo by its constraints:

| Route | Runs where | Code stays private? | Cost | Handles macOS/heavy? | Default for |
|---|---|---|---|---|---|
| **A. Public sandbox** (`ci-sandbox.sh`) | free GitHub-hosted runners on a **public** mirror repo | no (ephemeral, test-only) | **$0**, any account | **yes** (free hosted macOS) | small-budget / personal-account / expensive-CI / open-source repos → **incl. Helm** |
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

`repo_topology` already assigns every repo a **role with an authority**:

- `canonical` (private) → **the only** `["done", "merge_provenance", "code_truth"]` authority.
- `public_ci` / self-hosted / hosted → **`["verification_only"]`** — evidence, never truth.
- A fail-closed `code_repo_gate` refuses to satisfy "Done" if no canonical repo is configured.

**Because verification roles can *never* satisfy Done, tests can execute anywhere — even a public repo, even a customer's own runner — without that location ever becoming authoritative or trusted.** The canonical private repo remains the sole source of merge-provenance and completion.

For an **agent-fleet coordination platform**, this is the differentiator, not a footnote:

- **Onboard any customer repo** — free-tier personal, enterprise, or open-source — and give it *working, appropriately-priced CI* plus a *uniform agent workflow* from a single declarative contract.
- **Provenance integrity is guaranteed regardless of CI routing.** When AI agents mark work complete, "Done" is only ever stamped from the canonical repo's real merge — so nothing about *where* CI ran can forge completion. Competing agent tooling routes work without this guarantee; generic CI has no notion of it. This is CI routing as a **first-class, provenance-safe, fleet-adaptive capability of the coordination layer** — that is the leading edge.

---

## The provenance invariant (non-negotiable)

1. `canonical` is the **only** repo that can mark a task Done / carry merge-provenance.
2. Every other route posts **verification evidence only** (a required commit status), never Done.
3. The merge webhook + reconcile stamp Done **only** from the canonical default-branch merge.
4. `ci-sandbox.sh prove` requires the **exact SHA** that passed CI to be the SHA on canonical before it stamps the verification status — so the tested code *is* the merged code.

This is why Route A (public sandbox) is safe for private code: the public repo is a disposable test runner that can never speak for "Done."

---

## Fleet mapping (concrete)

| Repo | Account | Constraint | Route |
|---|---|---|---|
| **Helm** | `StevenRidder` (personal) | tiny budget + macOS 10× | **A — public sandbox** (only economical option) |
| **projectplanner** | `6th-Element-Labs` (org) | ample allowance, cheap Linux CI | **A today** (fleet uniformity); C is the equally-valid simplification |
| **Enterprise customer** | their org | code must not go public | **B — self-hosted runner** (their compute, code private) |
| **Open-source project** | any | code already public | **A** (natural fit) |

Uniformity note: we run **A across our own fleet** so the tooling, agent flow, provenance, and UI are identical for Helm and projectplanner. Per-repo micro-optimization (C for projectplanner) is *allowed by the same framework* but not worth splitting the toolchain for today.

---

## What exists vs. what to build (honest gap)

**Built (the hard, conceptual part):**
- `repo_topology` schema — roles, authority, `required_status_contexts`, `sync_scripts`; MCP tools (`set_project_repo_topology`, `get_project_repo_topology`, `repo_topology_role_guide`); agent session-prompt guidance ("public_ci = verification evidence only").
- `scripts/ci-sandbox.sh` — the Route-A executor (push → dispatch → wait → prove → open-PR).
- Validated green end-to-end on **projectplanner** and in daily use on **Helm**.

**To build (the automation that makes it a *feature*, not a playbook):**
1. **Topology-driven executor** — `ci-sandbox.sh` reads the project's `repo_topology` instead of env vars, so config lives in one place.
2. **Provision-on-opt-in** — create/register the sandbox repo, seed it, install the workflow, set branch protection, all from the topology (today it's ~10 manual steps).
3. **Topology-driven merge gate** — the gate reads the topology and routes (A/B/C), then **retires the bespoke on-box VM gate** (`switchboard_pr_gate.py`'s local venv+suite — the HARDEN-32 CPU hog). Keep only the provenance/claim check.
4. **Route B stand-up** — a dedicated (not prod-box) or autoscaling self-hosted runner for the no-public case.
5. **UI** — a per-project CI-strategy selector + live status; no more hand-run `gh` commands.

---

## Rollout phases

- **Phase 0 — Proven (done):** Route A validated on projectplanner; live on Helm. Tooling + schema in-repo.
- **Phase 1 — Consolidate:** topology-driven `ci-sandbox.sh`; retire the on-box VM test runner (keep provenance gate); branch-protect canonical to require the verification status.
- **Phase 2 — Automate provisioning:** opt-in creates + wires a sandbox from the topology.
- **Phase 3 — Route B:** dedicated/autoscaling self-hosted runner as the private fallback.
- **Phase 4 — UI:** project-settings strategy selector + status; ship as a Switchboard feature.

---

## Risks & honest caveats

- **Route A briefly exposes source on a public repo.** Mitigations: ephemeral feature branches (deleted post-merge), a secrets/history scan gate before first push, and **Route B/C for anyone who can't accept it.** No credentials are ever committed (verified).
- **Route A is a non-standard workaround.** It is contained to the one route that needs it; the framework, provenance, and Routes B/C are all standard GitHub mechanics.
- **Self-hosted (B) is not the on-box VM gate.** It is standard GitHub Actions on a *separate* machine — never the prod web box.
- **Free macOS only exists on public runners**, so macOS-heavy private repos (rare) either accept Route A or pay for Mac hardware under B.

## Non-goals

- Open-sourcing the products (Route A publishes test-only, ephemerally — not a release).
- Running CI on the production web box, ever.
- Letting any non-canonical repo speak for "Done."
