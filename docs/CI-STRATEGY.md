# Switchboard CI Strategy — Provenance-Safe, Fleet-Universal CI Routing

- **Status:** Active. **projectplanner** verification is pull-model on `projectplanner-ci` (CI-6…CI-7); **Helm** and other push-path repos still use [`external_ci_mirror.py`](../external_ci_mirror.py) (REPO-1…4 / CI-MIRROR-2). Spec: [`EXTERNAL-CI-MIRROR-SPEC.md`](EXTERNAL-CI-MIRROR-SPEC.md).
- **Scope:** How CI runs for every repo Switchboard coordinates — our own (`Helm`, `projectplanner`) and customer projects — as one uniform, declarative capability.
- **Relates to:** [ADR-0003 work-provenance](decisions/0003-work-provenance-and-reconciliation.md) · [ADR-0010 CI concurrency (2026-07-12 post-mortem context)](decisions/0010-ci-concurrency.md) · `repo_topology` in `store.py` · `ci_verify_dispatch.py` · `external_ci_mirror.py`

---

## Decision (TL;DR)

**CI is a per-project *route*, declared in `repo_topology` — not a fixed pipeline.** One framework, one agent flow, one provenance model; interchangeable routes chosen per repo by its constraints:

| Route | Runs where | Code stays private? | Cost | Handles macOS/heavy? | Default for |
|---|---|---|---|---|---|
| **A-push. Public CI mirror** (`external_ci_mirror`) | free GitHub-hosted runners on a **public** mirror repo | no (ephemeral, test-only branch) | **$0**, any account | **yes** (free hosted macOS) | small-budget / personal-account / expensive-CI / open-source repos → **incl. Helm** |
| **A-pull. Pull-model verify** (`verify.yml` on `public_ci`) | free GitHub-hosted runners; workflow **checks out** the private canonical merge ref | **yes** (no push to public; read-only token) | **$0**, any account | Linux today (macOS via hosted runners if needed) | **projectplanner** (org account, box must never run git/CI) |
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

### Why projectplanner left the push path (2026-07-12)

Before CI-6/CI-7, **projectplanner** used Route A-push like Helm: the Plan VM ran `external_ci_mirror` (or a local venv fallback) from a **bare mirror** under `/var/lib/projectplanner/ci-gate`, posting `Switchboard CI / VM gate` from the box. Under a parallel agent fleet that architecture failed in ways documented in **[ADR-0010 — CI concurrency (2026-07-12)](decisions/0010-ci-concurrency.md)**:

- A **single slow, contended box** serialized every PR gate (~15 min), widening the race where `master` moved before the merge ref existed ("no merge ref").
- The **bare mirror + git checkout on the prod VM** tied verification to disk, SSH/HTTPS auth, and cgroup contention on the same host that serves `plan.taikunai.com` — the failure class called out in [`ci_verify_dispatch.py`](../ci_verify_dispatch.py) as the **2026-07-12 bare-mirror outage**.
- **Push-path mirror sync** briefly published source to a public `ci/…` branch; acceptable for Helm economics, unnecessary for an org repo that can keep code private.

**Pull-model fix:** the Plan VM webhook fires a stateless `repository_dispatch` ([`ci_verify_dispatch.py`](../ci_verify_dispatch.py)); **`verify.yml` on `6th-Element-Labs/projectplanner-ci`** checks out `refs/pull/N/merge` from the private canonical repo with `PRIVATE_READ_TOKEN`, runs `scripts/switchboard_ci.sh`, and posts the required status — **zero git on the box**. Helm keeps Route A-push unchanged.

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

## Route A — two engines (push vs pull)

Route A is still "free GitHub-hosted runners on the public_ci sandbox," but **projectplanner and Helm use different mechanisms**:

### A-push — `external_ci_mirror` (Helm; unchanged)

Route A-push is implemented by the first-class **`external_ci_mirror`** runner + the `external_ci_runs` store model (REPO-1…4 / CI-MIRROR-2). One call —
`external_ci_mirror.request_external_ci_mirror_run(request, source_path, project)` — resolves the source/mirror repos and status context from `repo_topology`, then **pushes the exact source SHA to a disposable `ci/…` branch, dispatches the workflow, polls to a terminal status, and writes an `external_ci_run` back to Switchboard** with a structured `failure_class` (`mirror_sync_failed` / `workflow_trigger_failed` / `workflow_poll_failed` / `workflow_failed`) and run-URL evidence. It shells out to `gh` (must be installed + `GH_TOKEN` present on the caller).

**Do not build a second mirror path.** A prior iteration added an inline `run_sandbox_gate` and ported Helm's `ci-sandbox.sh`; both duplicated `external_ci_mirror` and were **retired** (ADR-0006 subtraction rule). Agents drive Route A-push via the `request_external_ci_mirror_run` MCP tool.

### A-pull — `verify.yml` on projectplanner-ci (projectplanner only)

**projectplanner** verification no longer uses `external_ci_mirror` or any on-box git. Flow:

1. Canonical PR webhook → [`github_sync.py`](../github_sync.py) → [`ci_verify_dispatch.dispatch_verify()`](../ci_verify_dispatch.py) (`SWITCHBOARD_CI_PULL_MODEL=1`) → `repository_dispatch` **`verify-pr`** to `6th-Element-Labs/projectplanner-ci`.
2. **`verify.yml`** (public hosted runners) checks out the **private** canonical repo at `refs/pull/<n>/merge` using secret `PRIVATE_READ_TOKEN`, runs `scripts/switchboard_ci.sh`, posts required context **`Switchboard CI / VM gate`** on the PR head SHA.
3. Plan VM **`switchboard_pr_gate.py` is claim-gate-only** (SESSION-12 `Switchboard / claim gate`); it never runs the suite or touches git (CI-7).

**Trigger decision (projectplanner):**

| Layer | Mechanism | Role |
|---|---|---|
| **Primary** | `repository_dispatch` (`verify-pr`) from Plan VM on PR open/update | Instant verification when the webhook fires |
| **Cron backstop** | `verify.yml` schedule (`:07/:22/:37/:52` each hour) | Re-verify the oldest open PR head missing a recent green required check if dispatch was missed or delayed |
| **Heartbeat** | Same cron tick + [`docs/UPTIME-MONITORING.md`](UPTIME-MONITORING.md) off-box probe (5‑min) on `projectplanner-ci` | Verification backstop for open PRs; separate liveness probe for `plan.taikunai.com` (does not run the suite) |

Manual **`workflow_dispatch`** on `verify.yml` remains for acceptance drills. Failure legibility (2026-07-12 lesson): checkout/auth failures post `infra: …`; suite failures post `tests: …`.

Operator runbook: [`SWITCHBOARD-RUNBOOK.md`](SWITCHBOARD-RUNBOOK.md) (pull-model + CI-7 teardown). Box retirement: [`deploy/ci7-teardown-box-ci.sh`](../deploy/ci7-teardown-box-ci.sh).

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
| **projectplanner** | `6th-Element-Labs` (org) | prod box must never run git/CI; private code | **A-pull — `verify.yml`** on `projectplanner-ci` (dispatch + cron backstop) |
| **Enterprise customer** | their org | code must not go public | **B — self-hosted runner** (their compute, code private) |
| **Open-source project** | any | code already public | **A-push** (natural fit) |

Helm routing is **unchanged** by the pull-model redesign. projectplanner uses A-pull so the Plan VM stays stateless for verification.

---

## What exists vs. what to build (honest gap)

**Built + shipped:**
- `repo_topology` schema — roles, authority, `required_status_contexts`, `claim_gate`; MCP tools (`set_project_repo_topology`, …); agent session-prompt guidance ("public_ci = verification evidence only").
- **`external_ci_mirror` engine** — push/dispatch/poll/record for Route A-push (Helm and MCP-driven mirrors).
- **Pull-model verification (CI-6…CI-7, DONE):** `verify.yml` on `projectplanner-ci`; webhook relay [`ci_verify_dispatch.py`](../ci_verify_dispatch.py); Plan VM claim-gate-only [`switchboard_pr_gate.py`](../scripts/switchboard_pr_gate.py); retired on-box bare mirror, `ci-gate` systemd units, and push-path gate for projectplanner.
- **Off-box uptime probe (HARDEN-44):** [`UPTIME-MONITORING.md`](UPTIME-MONITORING.md) on `projectplanner-ci`.

**To build (turns the capability into a one-click product):**
1. **Provision-on-opt-in** — create/register the mirror repo, seed it, install the workflow, set branch protection, all from the topology (today it's manual).
2. **Route B stand-up** — a dedicated (not prod-box) or autoscaling self-hosted runner for the no-public case.
3. **UI** — a per-project CI-strategy selector + live verification status; no hand-run commands.

---

## Rollout phases

- **Phase 0 — Proven:** Route A validated on projectplanner; live on Helm.
- **Phase 1 — Consolidate (DONE):** topology-driven verification; on-box venv test-runner retired; duplicate `run_sandbox_gate` + `ci-sandbox.sh` removed.
- **Phase 1b — Pull model (DONE, CI-6…CI-9):** projectplanner VM gate moved to `verify.yml`; box git/mirror retired; strategy doc records push vs pull.
- **Phase 2 — Automate provisioning:** opt-in creates + wires a mirror or pull workflow from the topology.
- **Phase 3 — Route B:** dedicated/autoscaling self-hosted runner as the private fallback.
- **Phase 4 — UI:** project-settings strategy selector + status.

---

## Risks & honest caveats

- **Route A-push briefly exposes source on a public repo.** Mitigations: ephemeral `ci/…` branches, a secrets/history scan gate before first push, and **Route B/C for anyone who can't accept it.** Not used for projectplanner after pull-model (A-pull keeps code private).
- **Route A-push needs `gh` + `GH_TOKEN` on the caller** (agent machine or a dedicated runner — **not** the prod Plan VM for projectplanner verification).
- **Route A-pull needs `PRIVATE_READ_TOKEN`** on `projectplanner-ci` (read private canonical contents + write commit statuses). Plan VM needs dispatch token only — no git.
- **Self-hosted (B) is standard GitHub Actions on a *separate* machine** — never the prod web box (that was the HARDEN-32 mistake).
- **Free macOS only exists on public runners**, so macOS-heavy private repos either accept Route A-push or pay for Mac hardware under B.

## Native merge queue

GitHub's native merge queue tests merge-group head SHAs, not PR heads. **projectplanner** no longer posts VM-gate statuses from the Plan VM (CI-7). Before enabling merge queue on the canonical repo, ensure **`verify.yml` (or a follow-on workflow) posts `Switchboard CI / VM gate` to merge-group head SHAs** — otherwise the queue hangs. See [`SWITCHBOARD-RUNBOOK.md`](SWITCHBOARD-RUNBOOK.md) → "Native merge queue".

## Non-goals

- Open-sourcing the products (Route A-push publishes test-only, ephemerally — not a release; A-pull never publishes source).
- Running CI on the production web box, ever.
- Letting any non-canonical repo speak for "Done."
- A second CI-mirror mechanism for projectplanner. **`external_ci_mirror` remains the one push engine; pull-model is the separate read-only path for projectplanner.**
