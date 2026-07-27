# Switchboard CI Strategy — Provenance-Safe, Fleet-Universal CI Routing

- **Status:** Active. **projectplanner** mirrors exact SHAs to `projectplanner-ci` and dispatches one trusted default-branch workflow; **Helm** and other push-path repos use the same [`external_ci_mirror.py`](../external_ci_mirror.py) engine (REPO-1…4 / CI-MIRROR-2). Spec: [`EXTERNAL-CI-MIRROR-SPEC.md`](EXTERNAL-CI-MIRROR-SPEC.md).
- **Scope:** How CI runs for every repo Switchboard coordinates — our own (`Helm`, `projectplanner`) and customer projects — as one uniform, declarative capability.
- **Relates to:** [ADR-0003 work-provenance](decisions/0003-work-provenance-and-reconciliation.md) · [ADR-0010 CI concurrency (2026-07-12 post-mortem context)](decisions/0010-ci-concurrency.md) · `repo_topology` in `store.py` · `external_ci_mirror.py`

---

## Decision (TL;DR)

**CI is a per-project *route*, declared in `repo_topology` — not a fixed pipeline.** One framework, one agent flow, one provenance model; interchangeable routes chosen per repo by its constraints:

| Route | Runs where | Code stays private? | Cost | Handles macOS/heavy? | Default for |
|---|---|---|---|---|---|
| **A-push. Public CI scratchpad** (`external_ci_mirror`) | free GitHub-hosted runners on a **public** mirror repo | no (ephemeral, test-only branch) | **$0**, any account | **yes** (free hosted macOS) | small-budget / expensive-CI / open-source repos → **incl. projectplanner and Helm** |
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

The CI-6 pull model was a useful bridge: it moved the suite off the production VM and stabilized the required check. CI-10…CI-17 retain the public runner, exact-SHA contract, failure labels, and evidence model while deleting that bridge and replacing the unsafe trigger seam. The canonical webhook calls `external_ci_mirror.request_external_ci_mirror_run`, fetches the exact PR head, and pushes it to a disposable `refs/tags/ci/**` tag. It then dispatches `verify.yml` from `projectplanner-ci`'s trusted default branch. Tags cannot satisfy a legacy `branches: ci/**` push trigger, mirrored agent code never chooses the workflow, and the secret-free suite job never shares a runner with the App callback credential. There is no PAT fallback.

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

1. Canonical PR `opened` / `reopened` / `ready_for_review` / `synchronize` webhook → [`github_sync.py`](../github_sync.py) → `external_ci_mirror.request_external_ci_mirror_run`.
2. The runner fetches `refs/pull/<n>/head`, verifies the live GitHub head SHA, and pushes it to a deterministic disposable `refs/tags/ci/<task>/<sha>` tag on `6th-Element-Labs/projectplanner-ci`. The tag transport cannot trigger a branch-authored workflow.
3. The runner dispatches **`master:verify.yml`** with the scratchpad ref, exact SHA, and an audit-only purpose. The trusted workflow always runs the full canonical `scripts/switchboard_ci.sh` gate plus Playwright in a secret-free job; isolated App jobs post the one required context **`Switchboard CI / VM gate`**.
4. GitHub's merge queue creates a temporary merge-group SHA. The same route, workflow, script, suite, and context verify that distinct exact SHA before landing.
5. Claim, Work Session, review, remediation, and merge-authorization hygiene stay inside Switchboard as preconditions to arm auto-merge. They are not GitHub status contexts. Autopilot enqueues once and waits; it never owns a custom requeue cycle.

**Trigger decision (projectplanner):**

| Layer | Mechanism | Role |
|---|---|---|
| **Primary** | exact-SHA mirror plus trusted `workflow_dispatch` | Identical full verification for PR and merge-group SHAs |
| **Manual recovery** | the same exact-SHA mirror and trusted workflow with `purpose=ci_repair` | Require one green run, then audited administrator squash merge that bypasses only the queue |
| **Performance monitor** | scheduled, non-required public workflow | Wall-clock load ratchets and reports; never a PR status |
| **Heartbeat** | [`docs/UPTIME-MONITORING.md`](UPTIME-MONITORING.md) off-box probe (5-min) | Separate liveness probe for `plan.taikunai.com`; does not run the suite |

Failure legibility (2026-07-12 lesson): checkout/setup failures post `infra: …`; suite failures post `tests: …`.

Operator runbook: [`SWITCHBOARD-RUNBOOK.md`](SWITCHBOARD-RUNBOOK.md). The old
suite runner, pull relay, duplicate backend/sharded workflows, claim-status
timer, and legacy merge coordinator remain retired; only exact-SHA mirror
coordination runs on the Plan VM. Merge authorization remains internal
Switchboard state, evaluated before Autopilot arms the PR, rather than a second
advisory GitHub lifecycle.

Merge authorization remains internal Switchboard state.
The pull relay and duplicate backend/sharded workflows are retired.

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
| **projectplanner** | `6th-Element-Labs` (org) | suite must never run on prod; scratchpad exposure accepted | **A-push — `verify.yml`** on `projectplanner-ci` (disposable `refs/tags/ci/**`) |
| **Enterprise customer** | their org | code must not go public | **B — self-hosted runner** (their compute, code private) |
| **Open-source project** | any | code already public | **A-push** (natural fit) |

Helm routing is **unchanged**. projectplanner now uses the same push mirror engine; the Plan VM coordinates the exact-SHA mirror but never executes the suite.

---

## What exists vs. what to build (honest gap)

**Built + shipped:**
- `repo_topology` schema — roles, authority, `required_status_contexts`, `claim_gate`; MCP tools (`set_project_repo_topology`, …); agent session-prompt guidance ("public_ci = verification evidence only").
- **`external_ci_mirror` engine** — push/dispatch/poll/record for Route A-push (Helm and MCP-driven mirrors).
- **Scratchpad verification:** exact-SHA mirror plus one trusted default-branch `verify.yml`; identical full suite execution for PR, queue, and repair purposes; one required status; no process-state status fan-out.
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
- **Phase 1c — Scratchpad route (CI-10…CI-17):** reuse the mirror engine, push exact projectplanner PR heads to non-triggering disposable tags, and keep the required status contract. Other mirror consumers retain their declared ref strategy.
- **Phase 1d — Trusted thin-queue route:** one App-authenticated verdict, trusted workflow authority, secret/job separation, identical exact-SHA verification, non-blocking scheduled timing monitors, and an audited administrator repair lane.
- **Phase 2 — Automate provisioning:** opt-in creates + wires a mirror or pull workflow from the topology.
- **Phase 3 — Route B:** dedicated/autoscaling self-hosted runner as the private fallback.
- **Phase 4 — UI:** project-settings strategy selector + status.

---

## Risks & honest caveats

- **Route A-push briefly exposes source on a public repo.** Mitigations: ephemeral `ci/…` branches, terminal cleanup, a secrets/history scan gate before first push, and **Route B/C for anyone who can't accept it.** This exposure is explicit and accepted for the projectplanner scratchpad route.
- **Route A-push needs authenticated source fetch and mirror push credentials on the caller.** For projectplanner the Plan VM performs only this coordination step; the suite still runs off-box.
- **projectplanner-ci uses a dedicated App for commit-status writeback.** `SWITCHBOARD_APP_ID` and `SWITCHBOARD_APP_PRIVATE_KEY` are mandatory and token minting fails closed; the retired `PRIVATE_READ_TOKEN` must not be restored. Only isolated announce/report jobs can read those secrets. The suite job checks out public scratchpad code with no credential.
- **Self-hosted (B) is standard GitHub Actions on a *separate* machine** — never the prod web box (that was the HARDEN-32 mistake).
- **Free macOS only exists on public runners**, so macOS-heavy private repos either accept Route A-push or pay for Mac hardware under B.

## Native merge queue

GitHub's native merge queue tests merge-group head SHAs, not PR heads. The canonical `merge_group/checks_requested` webhook sends that exact temporary SHA through the same mirror route, and **the same `verify.yml` / `scripts/switchboard_ci.sh` path posts only `Switchboard CI / VM gate`** after the full suite and Playwright. Autopilot enqueues once and waits. It does not requeue; a persistent GitHub/process failure uses the audited exact-SHA CI-repair administrator lane. See [`SWITCHBOARD-RUNBOOK.md`](SWITCHBOARD-RUNBOOK.md) → "Native merge queue".

## Non-goals

- Open-sourcing the products (Route A-push publishes test-only, ephemerally — not a release).
- Running CI on the production web box, ever.
- Letting any non-canonical repo speak for "Done."
- A second CI-mirror mechanism for projectplanner. **`external_ci_mirror` plus one trusted workflow is the complete route.**
