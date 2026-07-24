# Switchboard CI Strategy — Provenance-Safe, Fleet-Universal CI Routing

- **Status:** Active. **projectplanner** verification is push-triggered scratchpad CI on `projectplanner-ci` (CI-10…CI-14); **Helm** and other push-path repos use the same [`external_ci_mirror.py`](../external_ci_mirror.py) engine (REPO-1…4 / CI-MIRROR-2). Spec: [`EXTERNAL-CI-MIRROR-SPEC.md`](EXTERNAL-CI-MIRROR-SPEC.md).
- **Scope:** How CI runs for every repo Switchboard coordinates — our own (`Helm`, `projectplanner`) and customer projects — as one uniform, declarative capability.
- **Relates to:** [ADR-0003 work-provenance](decisions/0003-work-provenance-and-reconciliation.md) · [ADR-0010 CI concurrency (2026-07-12 post-mortem context)](decisions/0010-ci-concurrency.md) · `repo_topology` in `store.py` · `external_ci_mirror.py`

---

## Decision (TL;DR)

**CI is a per-project *route*, declared in `repo_topology` — not a fixed pipeline.** One framework, one agent flow, one provenance model; interchangeable routes chosen per repo by its constraints:

| Route | Runs where | Code stays private? | Cost | Handles macOS/heavy? | Default for |
|---|---|---|---|---|---|
| **A-push. Public CI scratchpad** (`external_ci_mirror`) | free GitHub-hosted runners on a **public** mirror repo | no (ephemeral, test-only branch) | **$0**, any account | **yes** (free hosted macOS) | small-budget / expensive-CI / open-source repos → **incl. projectplanner and Helm** |
| **A-pull. Private checkout bridge** (`ci_verify_dispatch`) | free GitHub-hosted runners; workflow checks out a private canonical ref | **yes** | **$0**, any account | Linux today | manual rollback bridge only; not projectplanner's primary path |
| **B. Self-hosted runner** | standard GitHub Actions on **our own dedicated runner box** | **yes** | $0 minutes (our compute) | Linux yes; macOS needs Mac hardware | **enterprise clients who refuse public code** |
| **C. Hosted on canonical** | GitHub-hosted runners on the private repo | **yes** | draws the account's included minutes | yes (billed) | orgs with ample allowance + cheap CI |

The **invariant that makes all three safe** (below) is the actual product: *where tests run is decoupled from what is trusted.*

---

## Context — why no off-the-shelf CI fits us

Switchboard coordinates a **fleet of agent-driven repos across mixed GitHub accounts**, each with different cost/privacy constraints. A single fixed pipeline cannot serve all of them:

- **Personal accounts have tiny CI budgets** (`StevenRidder`, Pro ≈ 3,000 min/mo). `Helm` runs **macOS CI that bills at 10×** (a 120-min job = ~1,200 billed min) — **two runs exhausts the whole personal budget.** Only *public-repo* Actions (free, unlimited, incl. macOS) make Helm economical.
- **Org accounts have ample allowance** (`6th-Element-Labs`, enterprise, net $0/mo) — for them private-repo CI is essentially free.
- **Enterprise customers will refuse to put code in a public repo** at all — for them the public route is a non-starter.
- **Our production box is a 1 GB VM** that must never run CI (it melted down doing exactly that — see HARDEN-32).

Generic CI assumes one repo, one account, one budget. We need a layer that **adapts per project while keeping the agent experience and the trust model identical everywhere.** That layer is the edge.

### Why projectplanner uses the scratchpad push path (2026-07-13)

Before CI-6/CI-7, **projectplanner** used Route A-push like Helm: the Plan VM ran `external_ci_mirror` (or a local venv fallback) from a **bare mirror** under `/var/lib/projectplanner/ci-gate`, posting `Switchboard CI / VM gate` from the box. Under a parallel agent fleet that architecture failed in ways documented in **[ADR-0010 — CI concurrency (2026-07-12)](decisions/0010-ci-concurrency.md)**:

- A **single slow, contended box** serialized every PR gate (~15 min), widening the race where `master` moved before the merge ref existed ("no merge ref").
- The **bare mirror + git checkout on the prod VM** tied verification to disk, SSH/HTTPS auth, and cgroup contention on the same host that serves `plan.taikunai.com` — the failure class called out in [`ci_verify_dispatch.py`](../ci_verify_dispatch.py) as the **2026-07-12 bare-mirror outage**.
- **Push-path mirror sync** briefly published source to a public `ci/…` branch; acceptable for Helm economics, unnecessary for an org repo that can keep code private.

The CI-6 pull model was a useful bridge: it moved the suite off the production VM and stabilized the required check. CI-10…CI-14 retain that harness, public runner, status contract, claim gate, failure labels, and evidence model while replacing the trigger and checkout seams. The canonical webhook now calls `external_ci_mirror.request_external_ci_mirror_run`, fetches the exact PR head, and pushes it to a disposable `ci/**` branch. That push starts `verify.yml`; the workflow checks out the scratchpad directly and uses `PRIVATE_READ_TOKEN` only to post the required status back to the canonical SHA. `SWITCHBOARD_CI_PULL_MODEL` is no longer the primary route.

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

## Route A — one primary mirror engine

Route A is "free GitHub-hosted runners on the public_ci sandbox." projectplanner and Helm share the same mirror engine; workflow details may differ per repo.

### A-push — `external_ci_mirror` (Helm; unchanged)

Route A-push is implemented by the first-class **`external_ci_mirror`** runner + the `external_ci_runs` store model (REPO-1…4 / CI-MIRROR-2). One call —
`external_ci_mirror.request_external_ci_mirror_run(request, source_path, project)` — resolves the source/mirror repos and status context from `repo_topology`, then **pushes the exact source SHA to a disposable `ci/…` branch, triggers by push or explicit dispatch, polls to a terminal status, and writes an `external_ci_run` back to Switchboard** with a structured `failure_class` (`mirror_sync_failed` / `workflow_trigger_failed` / `workflow_poll_failed` / `workflow_failed`) and run-URL evidence. It shells out to `git` and `gh` (credentials must be present on the caller).

**Do not build a second mirror path.** A prior iteration added an inline `run_sandbox_gate` and ported Helm's `ci-sandbox.sh`; both duplicated `external_ci_mirror` and were **retired** (ADR-0006 subtraction rule). Agents drive Route A-push via the `request_external_ci_mirror_run` MCP tool.

### projectplanner scratchpad route — `verify.yml` on projectplanner-ci

Flow:

1. Canonical PR `opened` / `reopened` / `ready_for_review` / `synchronize` webhook → [`github_sync.py`](../github_sync.py) → `external_ci_mirror.request_external_ci_mirror_run(..., push_triggered=True)`.
2. The runner fetches `refs/pull/<n>/head`, verifies the exact webhook head SHA, and pushes that commit to a deterministic disposable `ci/<task>/<sha>` branch on `6th-Element-Labs/projectplanner-ci`.
3. The branch push starts **`verify.yml`**. It checks out the public scratchpad directly, runs `scripts/switchboard_ci.sh`, and posts required context **`Switchboard CI / VM gate`** on the identical canonical SHA. `PRIVATE_READ_TOKEN` is used only for that status callback, not checkout.
4. Plan VM **`switchboard_pr_gate.py` posts board-backed PR authorization statuses**: SESSION-12 `Switchboard / claim gate` plus `Switchboard / merge authorization`, the branch-protection projection of the exact-head merge gate. It never runs the suite or mirrors source.

**Trigger decision (projectplanner):**

| Layer | Mechanism | Role |
|---|---|---|
| **Primary** | exact-SHA push to `ci/**` from canonical PR webhook | Instant verification when the webhook fires |
| **Rollback bridge** | manual [`ci_verify_dispatch.py`](../ci_verify_dispatch.py) invocation | Temporary operator escape hatch; not webhook primary |
| **Heartbeat** | [`docs/UPTIME-MONITORING.md`](UPTIME-MONITORING.md) off-box probe (5-min) | Separate liveness probe for `plan.taikunai.com`; does not run the suite |

Failure legibility (2026-07-12 lesson): checkout/setup failures post `infra: …`; suite failures post `tests: …`.

Operator runbook: [`SWITCHBOARD-RUNBOOK.md`](SWITCHBOARD-RUNBOOK.md) (scratchpad route + claim gate). The old suite runner remains retired by [`deploy/ci7-teardown-box-ci.sh`](../deploy/ci7-teardown-box-ci.sh); only the mirror coordination step runs on the Plan VM.

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
| **Helm** | `StevenRidder` (personal) | tiny budget + macOS 10× | **A-push — public mirror** (`external_ci_mirror`; only economical option) |
| **projectplanner** | `6th-Element-Labs` (org) | suite must never run on prod; scratchpad exposure accepted | **A-push — `verify.yml`** on `projectplanner-ci` (`ci/**` push) |
| **Enterprise customer** | their org | code must not go public | **B — self-hosted runner** (their compute, code private) |
| **Open-source project** | any | code already public | **A-push** (natural fit) |

Helm routing is **unchanged**. projectplanner now uses the same push mirror engine; the Plan VM coordinates the exact-SHA mirror but never executes the suite.

---

## What exists vs. what to build (honest gap)

**Built + shipped:**
- `repo_topology` schema — roles, authority, `required_status_contexts`, `claim_gate`; MCP tools (`set_project_repo_topology`, …); agent session-prompt guidance ("public_ci = verification evidence only").
- **`external_ci_mirror` engine** — push/dispatch/poll/record for Route A-push (Helm and MCP-driven mirrors).
- **Scratchpad verification (CI-10…CI-14):** push-triggered `verify.yml` on `projectplanner-ci`; webhook routing through `external_ci_mirror`; exact-SHA status evidence; Plan VM claim and merge-authorization [`switchboard_pr_gate.py`](../scripts/switchboard_pr_gate.py). The CI-6 pull relay remains only as a manual rollback bridge.
- **Off-box uptime probe (HARDEN-44):** [`UPTIME-MONITORING.md`](UPTIME-MONITORING.md) on `projectplanner-ci`.

**To build (turns the capability into a one-click product):**
1. **Provision-on-opt-in** — create/register the mirror repo, seed it, install the workflow, set branch protection, all from the topology (today it's manual).
2. **Route B stand-up** — a dedicated (not prod-box) or autoscaling self-hosted runner for the no-public case.
3. **UI** — a per-project CI-strategy selector + live verification status; no hand-run commands.

---

## Rollout phases

- **Phase 0 — Proven:** Route A validated on projectplanner; live on Helm.
- **Phase 1 — Consolidate (DONE):** topology-driven verification; on-box venv test-runner retired; duplicate `run_sandbox_gate` + `ci-sandbox.sh` removed.
- **Phase 1b — Pull bridge (DONE, CI-6…CI-9):** projectplanner VM gate moved to `verify.yml`; suite and legacy bare-mirror units retired from the box.
- **Phase 1c — Scratchpad route (CI-10…CI-14):** reuse the mirror engine, push exact PR heads to disposable branches, and keep the required status contract.
- **Phase 2 — Automate provisioning:** opt-in creates + wires a mirror or pull workflow from the topology.
- **Phase 3 — Route B:** dedicated/autoscaling self-hosted runner as the private fallback.
- **Phase 4 — UI:** project-settings strategy selector + status.

---

## Risks & honest caveats

- **Route A-push briefly exposes source on a public repo.** Mitigations: ephemeral `ci/…` branches, terminal cleanup, a secrets/history scan gate before first push, and **Route B/C for anyone who can't accept it.** This exposure is explicit and accepted for the projectplanner scratchpad route.
- **Route A-push needs authenticated source fetch and mirror push credentials on the caller.** For projectplanner the Plan VM performs only this coordination step; the suite still runs off-box.
- **projectplanner-ci needs `PRIVATE_READ_TOKEN` only for commit-status writeback.** The scratchpad checkout is public and does not use the token.
- **Self-hosted (B) is standard GitHub Actions on a *separate* machine** — never the prod web box (that was the HARDEN-32 mistake).
- **Free macOS only exists on public runners**, so macOS-heavy private repos either accept Route A-push or pay for Mac hardware under B.

## Native merge queue

GitHub's native merge queue tests merge-group head SHAs, not PR heads. Before enabling merge queue on the canonical repo, add a mirror trigger for merge-group SHAs and ensure **`verify.yml` posts `Switchboard CI / VM gate` to those SHAs** — otherwise the queue hangs. See [`SWITCHBOARD-RUNBOOK.md`](SWITCHBOARD-RUNBOOK.md) → "Native merge queue".

## Non-goals

- Open-sourcing the products (Route A-push publishes test-only, ephemerally — not a release; the rollback A-pull bridge does not publish source).
- Running CI on the production web box, ever.
- Letting any non-canonical repo speak for "Done."
- A second CI-mirror mechanism for projectplanner. **`external_ci_mirror` is the one primary push engine; pull-model dispatch is rollback-only.**
