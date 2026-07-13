# Switchboard — operator runbook (DOGFOOD-4)

How to actually run the autonomous coordination mesh, and **where each piece runs**. Written
from the dogfood (DOGFOOD-3) + the shipped pieces: `run_session` (driver, decision #4),
the Codex `supervisor.py` (ADAPTER-8), RECON-5 auto-provenance, the monitor sweep, and the
Agent Host wake contract in [`AGENT-HOST-SPEC.md`](AGENT-HOST-SPEC.md).

Naming note: Switchboard is the product name. The live VM still uses `projectplanner`
for the repo, `/opt` checkout, `/var/lib` data path, systemd units, and `PM_*` env vars.
Those are compatibility names until the staged rename in
[`SWITCHBOARD-RENAME-MIGRATION.md`](SWITCHBOARD-RENAME-MIGRATION.md) is complete.

## 1. Deployment topology — two distinct hosts (don't conflate them)

```
┌─────────────────────────────┐         ┌──────────────────────────────────────────┐
│  SUBSTRATE  (Plan VM)        │  MCP/   │  AGENT HOST(s)  (where work happens)        │
│  plan.taikunai.com           │◀──REST──▶│  repo checkout · API keys · compute        │
│  t4g.micro · 2 core · 911 MB │  /ixp/  │                                            │
│                              │         │  supervisor.py (ADAPTER-8)                 │
│  • board + web   :8110       │         │   └─spawns→ agent runtime (Claude/Codex…)  │
│  • MCP           :8111       │         │        └─runs→ adapter handshake +         │
│  • LLM gateway   :8095       │         │               switchboard_core.run_session │
│  • monitor sweep (every 1m)  │         │        (claim_next→work→complete→repeat)   │
│  COORDINATION ONLY           │         │  /var/lib/projectplanner/runner            │
│  • wake intents (durable)    │         │  host daemon polls wake intents            │
│  • message-only wake host    │         │                                            │
└─────────────────────────────┘         └──────────────────────────────────────────┘
```

**The Plan VM is the substrate, not the runner.** It holds the board, the protocol endpoints,
and the lightweight monitor sweep — near-zero load, correctly sized for a micro. It does **not**
run agents: agent sessions need repo + API keys + real compute (builds, model calls), which
don't belong on a 911 MB coordination box. The **supervisor and the agents it spawns run on an
agent host** (your dev machine, a CI runner, or a dedicated agent box) — one supervisor process
spawns/keeps-alive/kills each agent it launches.

Exception for P0 dogfood: the Plan VM may run `projectplanner-agent-host.service` as a
**message-only wake host**. It starts `run_agent.py --inbox-only` for lane-less handoff wakes so
delivery can be proven without a human manually running `agent_host.py`. It intentionally uses
`PM_HOST_LANES=__MESSAGE_ONLY__`, so it will not accept lane-scoped work-dispatch wakes or call
`claim_next`.

| Piece | Host | Why |
|---|---|---|
| board / MCP / gateway / monitor-sweep timer | **Plan VM** | coordination substrate; tiny + always-on |
| message-only `projectplanner-agent-host` | **Plan VM** | wake-delivery proof; no lane work, no `claim_next` |
| `supervisor.py` (spawn / keep-alive / T3 kill) | **agent host** | owns the agent process group; needs compute |
| Agent Host daemon / wake loop | **agent host** | keeps warm capacity and starts absent runtimes |
| agent runtime (Claude Code, Codex, …) + `run_session` | **agent host** | does the actual work; needs repo + keys |

## 1.1 Why durable state matters

Agent runtimes are not durable infrastructure. They can compact their context window, restart,
lose a terminal, move to another host, or be killed by a supervisor. That limit is imposed by
the runtime/model platform, not by Switchboard, and it will differ across Claude Code, Codex,
Cursor, LangGraph, and custom loops.

Switchboard's job is to make those discontinuities boring. The board, inbox, claims, leases,
decisions, monitors, wake intents, git evidence, and Tally records are the durable contract.
An agent's current chat memory is useful working state, but it is never authoritative.

Operator rule: if an agent says it lost context, compacted, restarted, or "hit a handoff
limit," do not treat that as a product failure by itself. Check Switchboard:

1. Is the agent registered or stale?
2. Does it hold an active claim or lease?
3. Did it leave branch, head SHA, PR, merged SHA, or other evidence?
4. Are there unacked messages or fired monitors?
5. Is there a wake intent or eligible Agent Host to restart the runtime?

If those answers are visible, Switchboard is doing its job: the runtime blinked, but the
coordination state survived.

## 2. Run the substrate (Plan VM) — already deployed
```bash
ssh plan-vm; cd /opt/projectplanner
git pull --ff-only
set -a; . ./.env; set +a       # REQUIRED: .env redirects the data dir to /var/lib/projectplanner.
                               # Without it, store resolves the empty /opt/*.db and migrates the WRONG file.
.venv/bin/python -c "import store;[store.init_db(p) for p in store.project_ids()]"
sudo systemctl restart projectplanner projectplanner-mcp
sudo systemctl enable --now projectplanner-monitors.timer   # durable ack/deadline sweep (every 1m)
```
> The live DBs and runner/session artifacts live in `/var/lib/projectplanner/` (env-redirected),
> not `/opt/projectplanner/`.
> The `*.db` files under `/opt` are empty placeholders — never point a tool at them.
> Keep `/opt/projectplanner` as a clean git checkout so repo preflight can distinguish code dirt
> from operational state.
> Do not move these paths directly during the rename. Add and validate Switchboard aliases first.

## 2.1 CI and deployment gates

`scripts/switchboard_ci.sh` is the shared gate for Switchboard core changes. It compiles the Python
surface, runs the P0 conformance/runtime smoke suite, checks adapter behavior (Codex, LangGraph,
Agent Host), verifies webhook/provenance lifecycle, activity payload compatibility, task
move/archive, Tally project surface, unattended proof helpers, and frontend JavaScript syntax.

Run it before claiming a code task is ready for review:

```bash
cd /path/to/projectplanner
scripts/switchboard_ci.sh
```

GitHub Actions runs the same script on every PR and every push to `master` with strict dependency
checks and Node.js syntax checks enabled. The workflow currently targets the repo-scoped
self-hosted runner labelled `switchboard-ci` on the Plan VM because GitHub-hosted jobs were
startup-failing before job creation in this org/repo. Keep the runner online until hosted Actions
produce normal queued jobs again. Strict mode requires Python 3.10+ because the MCP runtime
dependency requires it:

```bash
PYTHON=.venv/bin/python SWITCHBOARD_CI_PYTHON=.venv/bin/python \
  SWITCHBOARD_CI_STRICT=1 SWITCHBOARD_CI_REQUIRE_NODE=1 scripts/switchboard_ci.sh
```

Merge rule: a branch can move to `In Review` with branch/head/PR evidence, but it should not be
merged unless the PR's `Switchboard CI / VM gate` commit status is green or a human explicitly
records the risk. GitHub Actions is currently disabled because the hosted workflow records
`startup_failure` before job creation; the VM-backed status is the canonical merge gate. `Done`
comes only from GitHub/default-branch provenance or verifier-stamped offline evidence.

Offline/non-PR completion rule: an agent still completes its claim to `In Review`. A verifier or
operator may then call the offline-evidence completion path with review evidence, an artifact URL
and/or evidence hash, and a verifier identity. Reconcile accepts that explicit
`offline_evidence` provenance, and still flags naked `Done` task rows with neither git provenance
nor offline evidence.

Runner bootstrap exception: if `Switchboard CI / VM gate` is missing on a PR, check the
projectplanner-ci `verify` workflow run and the corresponding Switchboard `external_ci_run` —
do not treat a missing status as a pass. Re-open or synchronize the PR to request a fresh
exact-SHA scratchpad branch through the Plan VM webhook.

The Plan VM posts only the SESSION-12 **`Switchboard / claim gate`** via
`projectplanner-claim-gate.timer`:

```bash
/opt/projectplanner/.venv/bin/python /opt/projectplanner/jobs.py claim_gate_prs
```

Manual claim-gate for one PR:

```bash
PM_GITHUB_TOKEN=... scripts/switchboard_pr_gate.py --pr 18
```

### Scratchpad CI (CI-12) + box teardown (CI-7)

**Required VM verification** runs on `6th-Element-Labs/projectplanner-ci` (`verify.yml`), posting
`Switchboard CI / VM gate`. Canonical PR open/sync webhooks call `external_ci_mirror`, fetch the
exact `refs/pull/<n>/head` SHA, and push it to a disposable `ci/**` branch. That push triggers
the workflow. The Plan VM coordinates the mirror from the service-owned
`/var/lib/projectplanner/ci-source` clone but never runs the test suite. If mirroring fails,
verify that path is a Git checkout owned by `projectplanner` and that the service token can
fetch the canonical repo, push to `projectplanner-ci`, and poll Actions.

Confirm the service account can fetch the private canonical repo, push to projectplanner-ci,
and poll Actions with `gh`. Confirm `PRIVATE_READ_TOKEN` is installed on projectplanner-ci for
canonical commit-status writeback only; scratchpad checkout does not use it.

**Retire on-box VM CI** after scratchpad verification holds (operator script — reversible via
`deploy/retired/*.bak` for one week):

```bash
cd /opt/projectplanner && sudo bash deploy/ci7-teardown-box-ci.sh
```

This stops `projectplanner-ci-gate.{timer,service}` and `projectplanner-ci-gate-request.{path,service}`,
removes `/var/lib/projectplanner/ci-gate`, enables `projectplanner-claim-gate.timer`, and deletes
disabled merge-queue ruleset **18821466**.

Rollback (within one week): restore units from `deploy/retired/` or `/etc/systemd/system/*.bak-ci7`,
re-enable the old timers, and disable `projectplanner-claim-gate.timer`.

### Native merge queue

Merge-queue gating is not yet covered by the PR-head scratchpad trigger. If you enable GitHub's
native merge queue, add a mirror trigger for merge-group head SHAs and ensure `verify.yml` posts
`Switchboard CI / VM gate` to those SHAs. The disabled ruleset 18821466 was dead config removed
in CI-7.

Verifier resume rule: review/audit workflows that spawn skeptic verifier agents should write a
`switchboard.review_verifier_run.v1` checkpoint with one deterministic job per
finding/lens pair, using `review_verifier_runs.py`. Reruns must load the checkpoint and schedule
only missing or retryable jobs. Token-limit/rate-limit failures are recorded as structured job
states, final reports must include verifier completion ratios, and reports fail closed while a
load-bearing finding is missing any required verifier lens.

Claim evidence rule: task comments and completion evidence may describe deliverables, reports,
generated pages, server wiring, or artifacts, but narrative text is not proof. When a comment or
completion payload names those things, declare repo-accessible evidence with `evidence_paths`,
`evidence_urls`, or `evidence_refs`. Reconcile emits red/yellow `claim_without_evidence` or
`claim_evidence_missing` findings when claimed artifacts cannot be tied back to a repo path,
HTTP(S) URL, or reachable git ref; audit exports include the full claim-to-evidence report.

External artifact root rule: review/build workflows that consume external worktrees, generated
reports, temp roots, uploaded artifacts, URLs, or git refs should write and check a
`switchboard.external_artifact_roots.v1` manifest with `external_artifact_roots.py` before creating
green reports or derived artifacts. Required roots must exist and be repo-owned, versioned,
attached, URL-backed, reachable by git ref, or explicitly declared `non_reproducible` with a
reason. Missing required roots are red; non-reproducible roots stay yellow in final reports; finding
summaries should distinguish repo-state findings from external-temp/external-versioned findings.

External side-effect rule: any Switchboard workflow that may change state outside the Switchboard
database should claim an `external_side_effects` row before touching the provider. The effect key is
deterministic over project, effect type, target, resource, payload hash, and idempotency window. If a
replay sees an unverified effect, read back provider/host state before issuing again; if it sees a
verified effect, return the recorded proof. Wake intents and runner-control requests are wired into
this ledger now; GitHub writes, notifications, provider pulls, hosted dispatch, and audit exports
should use the generic `claim_external_effect` -> `mark_external_effect_issued` ->
`verify_external_effect`/`fail_external_effect` path as they adopt it.

GitHub Actions `startup_failure` rule: this private repo has produced Actions runs with
`conclusion=startup_failure`, `jobs=[]`, and `path=BuildFailed` before any checkout/setup step.
Treat those as CI-infra failures, not test results. Do not merge on a vague "red but probably
fine" claim: require the `Switchboard CI / VM gate` status or a recorded strict local/VM run, and
keep GitHub Actions workflows absent until a one-step hosted-runner probe can start successfully.

## 2.2 Fail-and-fix-early operating policy

Switchboard should make the weakest link visible quickly. Missing data, broken connections,
invalid inputs, stale branch state, absent credentials, failed tests, and malformed payloads should
be reported at the point they are detected. Do not replace them with placeholder values or hidden
defaults that let downstream work keep moving on false assumptions.

Use [`fail_fix_signal.v1`](FAIL-FIX-SIGNAL-SCHEMA.md) for any product-level or repeated failure.
The same taxonomy is emitted by BUG intake, reconcile findings, monitor timeouts, and visible
task-comment fallbacks.

Operationally, this means:

- ingestion, normalization, protocol adapters, CI gates, monitors, and workflow execution should
  fail closed when their required signal is missing or invalid;
- a visible fallback is acceptable only if it keeps the original failure visible, names the
  fallback path, and preserves a red/yellow signal such as a PR status, reconcile finding, monitor
  event, or task comment;
- when a test or deploy gate exposes a real bug, fix the bug before treating the task as complete,
  even if the bug is in the environment or process rather than the first code change;
- if the current agent cannot fix the issue safely, it must leave a precise blocker with the
  observed command, failing input, expected signal, and next action.
- if the issue is product-level or repeated, file it through `submit_bug` with a canonical
  `failure_class` instead of leaving it as chat-only noise.

This is why the CI fallback posts `Switchboard CI / VM gate` instead of silently replacing GitHub
Actions: GitHub Actions remains visibly broken, while PRs still get a concrete pass/fail signal.

## 2.3 Project hierarchy and repo roles

Switchboard models a **Project** as the repo/trust/policy/access/CI/model/budget/Done authority
boundary. Under each Project:

- **Board / Mission** ids are outcome cockpits (`project_boards`, deliverables).
- **Epic / workstream / task** rows are execution planning below that layer.

Repo roles live on the **Project**, not on a board/mission. Read them from MCP/REST instead of
chat memory:

| Question | Source | Rule |
|---|---|---|
| Which repo controls Done? | `repo_topology.roles.canonical` or `repo_role_guide.done_authority` | Only canonical merge provenance can mark code work Done |
| Which repo runs CI? | `repo_topology.roles.public_ci` or `repo_role_guide.ci_verification` | Verification evidence only; never code truth |
| Which repo is public/publish evidence? | `repo_topology.roles.public` or `repo_role_guide.publication_evidence` | Publication evidence only; never Done |

Agent surfaces:

```text
get_working_agreement(project="switchboard")
get_project_contract(project="helm", task_id="ENGINE-1")
get_task(task_id="REPO-5", project="switchboard")   # includes project_context
```

Operator surfaces:

```text
GET /api/projects/switchboard/context
GET /api/board?project=helm                          # includes project_context
```

Helm built-in topology:

- **canonical:** `StevenRidder/Helm` — private code truth and Done authority
- **public_ci:** `StevenRidder/helm-ci` — shared CI sandbox; verifies canonical SHAs only
- **public:** public mirror placeholder — publication evidence only via `publish-public-mirror.sh`

The cockpit UI shows the same role guide on Exec Summary, About, and task detail when
`project_context` is present.

## 3. Operator login

Production runs with `PM_AUTH_MODE=required`. In that mode, board/API/control reads and writes
require either:

- a human web session from `/api/auth/login`, backed by a password principal and the
  `switchboard_session` cookie; or
- an adapter/MCP bearer token such as `PM_MCP_TOKEN` or an explicit principal token.

`PM_MCP_TOKEN` and `PM_AUTH_TOKEN` are compatibility shared tokens. They authenticate a caller but
do not, by themselves, identify the agent or automation responsible for a task mutation. Public
task writes that use those tokens must bind identity before the mutation:

- agent work passes `agent_id` for a currently registered/heartbeat-active agent;
- deliberate automation passes `system_actor` plus `system_reason`;
- otherwise the write fails closed with `failure_class=unbound_identity` and no task row/comment
  is created.

Recommended adapter sequence: fetch the working agreement, `register_agent`, drain directed inbox
and ack-required messages, claim/confirm the task, then call write tools such as `add_comment`,
`update_task`, or `complete_claim` with the same `agent_id`. If more than one live agent is bound
to the task, the server requires `agent_id` instead of inferring an author.

First admin bootstrap is intentionally narrow:

```bash
export PM_BOOTSTRAP_ADMIN_LOGIN=admin
export PM_BOOTSTRAP_ADMIN_PASSWORD='<strong one-time password>'
sudo systemctl restart projectplanner
```

The bootstrap path creates the admin only if no password login exists for that project. After
confirming login, remove `PM_BOOTSTRAP_ADMIN_PASSWORD` from the environment and restart the app.
For manual bootstrap, POST `/api/auth/bootstrap` from localhost, or provide `PM_BOOTSTRAP_TOKEN`
and send it as `X-Switchboard-Bootstrap-Token`.

Operators with `write:system` can download a redacted enterprise evidence bundle:

```bash
curl -s "$PM_BASE/api/audit/export?project=switchboard" \
  -H "Authorization: Bearer $PM_MCP_TOKEN" > switchboard-audit-export.json
```

The export is `switchboard.audit_export.v1` JSON. It includes tasks, activity, claims, directed
messages, monitors, runner sessions/control requests, Git and offline provenance, Tally
spend/outcomes/KPIs, scoped principals, role grants, and archived task snapshots. Stored
credential material such as token hashes, password hashes, session hashes, and raw bearer/session
tokens is omitted.

Lifecycle cleanup is also a `write:system` operator path. It is dry-run first and preserves
provenance through `cleanup.*` activity plus archive snapshots instead of raw deletion:

```bash
curl -s "$PM_BASE/api/cleanup/candidates?project=switchboard" \
  -H "Authorization: Bearer $PM_MCP_TOKEN"

curl -s "$PM_BASE/api/cleanup/apply" \
  -H "Authorization: Bearer $PM_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"project":"switchboard","dry_run":true}'
```

The candidate list covers stale agent registrations, expired runner sessions, expired/orphaned
claims and leases, old wake intents, fired/orphan monitors, and old terminal proof/sentinel tasks.
To apply a bounded cleanup, pass `candidate_ids` and set `dry_run:false`; old proof/sentinel tasks
are archived with snapshots, runner sessions are marked `expired`, claims are abandoned with a
cleanup reason, wakes/monitors are cancelled or resolved, and stale agent presence rows are removed
from the live registry only after their snapshot is written to activity.

ACCESS role state lives in the central project registry: orgs, users, org memberships, project
ownership metadata, and project role grants. Inspect it with `GET /api/access/model?project=...`.
Admins can grant a project role with `POST /api/access/project_role?project=...`:

```json
{"subject_kind":"principal","subject_id":"user-viewer","role":"viewer"}
```

Built-in roles map to effective scopes at auth time: `viewer` can read, `contributor` can
read/write tasks and agent protocol state, and `admin`/`owner` can manage system settings.

Scoped bearer tokens are managed by `write:system` operators. Create a token with a role preset
or explicit scopes; the raw secret is returned once and later listings are redacted:

```bash
curl -s "$PM_BASE/api/access/tokens?project=switchboard" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"kind":"agent","display_name":"claude/WX","role":"contributor"}'
```

Audit active credentials without exposing secrets:

```bash
curl -s "$PM_BASE/api/access/tokens?project=switchboard" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

Revoke by principal id; revocation also kills live sessions for that principal:

```bash
curl -s -X POST "$PM_BASE/api/access/tokens/agent-abc123/revoke?project=switchboard" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

Project creation is also a system-scope operation. A successful create makes a physically separate
project DB, records purpose/boundary metadata, and grants the creator admin on that project so the
same session can switch into it through an explicit role grant:

```bash
curl -s "$PM_BASE/api/projects" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "project_id":"customer-alpha",
    "name":"Customer Alpha",
    "purpose":"Customer Alpha product work",
    "boundary":"Only Customer Alpha work belongs here."
  }'
```

Agents see that boundary in `list_projects`, `get_working_agreement`, `get_project_contract`, and
`prepare_agent_session`. Cross-project cleanup uses `move_task` or `archive_task`; both are audited
and refuse unknown projects or active claims/leases instead of silently editing shared state.

## 4. Run an autonomous agent (agent host)
```bash
export PM_BASE=https://plan.taikunai.com PM_PROJECT=switchboard PM_MCP_TOKEN=…  PM_AGENT_ID=claude/work-1
# the supervisor spawns the agent process group, injects the runner-session id, can hard-kill it:
python3 adapters/codex/supervisor.py start -- <your-agent-launch-cmd>
```
Inside the agent, the loop is `switchboard_core.run_session(work_fn=…)`:
`handshake → claim_next → work_fn(task) → complete_claim(evidence) → repeat`, stopping on
`no_unblocked_work` / error (claim abandoned) / `max_tasks`. `work_fn` is "run the model on this
task and return {branch, head_sha}" — supplied by the runtime.

For hands-off delivery, run an Agent Host daemon as well as one-off supervised sessions. The
daemon registers host capacity, polls wake intents, and starts/reuses a supervised runtime when
an ack timeout, operator request, or ready-work policy asks for one. Without that daemon, a
message to an absent Claude/Codex session is durable but not deliverable until a human or another
process starts the runtime.

Safety rule: message-only wakes do not have `selector.lane`, so the daemon must use the
inbox-only path and must not call `claim_next`. Work-dispatch wakes need an explicit lane.
Agent Hosts fail closed: `PM_AGENT_HOST_ALLOW_WORK` defaults to off, `PM_HOST_LANES` must name
the allowed work lanes, and `PM_AGENT_HOST_ALLOW_GLOBAL_CLAIM` stays off unless an operator is
intentionally allowing global dispatch.

Runner session control is host-owned. Switchboard keeps a central registry of supervised
`runner_session_id` records so operators can see host, runtime, task/claim, heartbeat,
control fidelity, last snapshot, and available actions. Snapshot/kill buttons create audited
runner-control requests; only the owning Agent Host claims them and calls the local supervisor.
Unmanaged or hostless sessions cannot advertise `runner_kill`, and kill/restart control never
marks task work complete.

Runner rows also expose an `environment` block for triage before intervention: `status`,
`uptime_seconds`, `failure_reason`, `last_command`, `last_result`, `log_tail`, and per-action
`capabilities`. Supported control actions are `snapshot`, `kill`, `restart`, `health`, `logs`,
and `open`; unsupported actions are recorded as refused control requests with
`reason=not_supported`. The Agent Host currently answers `health` from supervisor status and
`logs` from supervisor snapshots. `open` is an explicit future host capability, not assumed.

```bash
curl -s "$PM_BASE/ixp/v1/runner_sessions?project=switchboard&task_id=HARDEN-24&include_stale=true" \
  -H "Authorization: Bearer $PM_MCP_TOKEN"

curl -s "$PM_BASE/ixp/v1/request_runner_health" \
  -H "Authorization: Bearer $PM_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"project":"switchboard","runner_session_id":"run_...","reason":"operator triage"}'
```

### 3.1 Run the P0 message-only host on the Plan VM

```bash
ssh plan-vm; cd /opt/projectplanner
git pull --ff-only
.venv/bin/pip install -r requirements.txt
sudo cp deploy/projectplanner-agent-host.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now projectplanner-agent-host
sudo systemctl restart projectplanner-agent-host
systemctl is-active projectplanner-agent-host
journalctl -u projectplanner-agent-host -n 80 --no-pager
```

Expected behavior:

- host registers as `host/plan-vm-message-wake`;
- lane-less wake intents can be claimed and completed with `wake_mode=inbox_only`;
- child sessions run `adapters/run_agent.py --inbox-only`;
- no `task.claimed` activity is emitted by message-only wakes.

### 3.2 Run a work-capable Agent Host on an eligible worker

Do this on a machine that actually has the repo, runtime credentials, and compute budget to do
agent work. The Plan VM should stay message-only.

```bash
cd /path/to/projectplanner
export PM_BASE=https://plan.taikunai.com
export PM_PROJECT=switchboard
export PM_MCP_TOKEN=...
export PM_HOST_ID=host/my-worker-hardening
export PM_RUNTIME=codex
export PM_REPO_ROOT=$PWD
export PM_HOST_LANES=HARDEN,ADAPTER
export PM_AGENT_HOST_ALLOW_WORK=1
export PM_AGENT_HOST_ALLOW_GLOBAL_CLAIM=0
export PM_HOST_MAX_SESSIONS=1
export PM_AGENT_HOST_CLAIM_IDLE_SECONDS=6
python3 adapters/agent_host.py --once
```

For a dry proof, leave `PM_AGENT_WORK_MODULE` unset. A lane-scoped wake starts
`run_agent.py --lanes <lane> --dry`, which calls `claim_next` only for that explicit lane and
abandons any claim instead of fabricating completion. For real delivery, set
`PM_AGENT_WORK_MODULE=package.module:work_fn` after the runtime adapter can perform the work and
return branch/SHA/PR evidence.

A work-capable host should show:

- `register_host` inventory with `policy.mode=lane_scoped`;
- explicit `allowed_lanes`;
- lane-less handoff wakes still using `wake_mode=inbox_only`;
- lane-scoped wakes using `wake_mode=claim_next`;
- lane-less `policy.mode=claim_next` wakes left unclaimed unless `PM_AGENT_HOST_ALLOW_GLOBAL_CLAIM=1`.

### 3.3 HARDEN-4 unattended proof mode

For the bounded dogfood proof, create an isolated proof task and a downstream sentinel on the
`PROOF` lane, then start an eligible work host with the proof worker:

```bash
export PM_BASE=https://plan.taikunai.com
export PM_PROJECT=switchboard
export PM_MCP_TOKEN=...
export PM_HOST_ID=host/my-worker-proof
export PM_RUNTIME=codex
export PM_HOST_LANES=PROOF
export PM_AGENT_HOST_ALLOW_WORK=1
export PM_AGENT_HOST_ALLOW_GLOBAL_CLAIM=0
export PM_AGENT_WORK_MODULE=proof_work:run_task
python3 adapters/agent_host.py --once
```

The proof worker is deliberately narrow. It claims only via `claim_next(lanes="PROOF")`, writes
a generated markdown proof file under `docs/dispatches/`, pushes a task-scoped branch, opens a
PR containing `Closes PROOF-n`, and returns branch/head/PR evidence to `complete_claim`. After
merge provenance marks that proof task Done, a second `claim_next(lanes="PROOF")` should see the
downstream sentinel. That is the minimum live proof that wake -> host -> runtime -> handshake ->
inbox -> claim_next -> PR -> In Review -> merge-proven Done -> downstream dispatch works.

Before trusting the merge-provenance leg, verify the repository webhook is actually installed:

```bash
gh api repos/6th-Element-Labs/projectplanner/hooks \
  --jq '.[] | select(.config.url=="https://plan.taikunai.com/api/github/webhook") | {id,active,events,last_response}'
```

The live app should have `PM_GITHUB_WEBHOOK_SECRET` set in `/opt/projectplanner/.env`; the GitHub
hook should use the same secret, subscribe to `pull_request` and `push`, and show
`last_response.status=active` after `gh api --method POST repos/.../hooks/<id>/pings`. Without
that repo-level hook, PR merges can still be replayed through `/api/github/webhook`, but they are
not unattended.

## 5. The self-driving loop (what makes it hands-off)
```
supervisor keeps agent(s) alive
   → each agent run_session: claim_next → work → push → complete_claim(evidence)
      → RECON-5 auto-stamps direct-push provenance  (or PR-merge webhook, RECON-2)
      → task → Done  → unblocks downstream deps
      → claim_next hands out the next task … (loop)
   monitor-sweep (Plan VM) fires any unacked requires_ack handoff
      → optional wake intent asks an Agent Host to start/reuse a runtime
   any agent can stop/redirect another via a signal consumed at the tool boundary (FR-14)
```
**Human stays in the loop only where it should:** approve/kill via the supervisor, and review
the board. No human relay for handoffs; no human ignition once the supervisor is running.

## 6. Control fidelity / safety (PRD §10)
- **T1** advisory (any runtime): the working agreement + `evaluate_tool`.
- **T2** boundary-deny: runtimes with a pre-tool hook (Claude Code `PreToolUse`); Codex via a
  managed runner that honors deny.
- **T3** hard kill: only for processes the **supervisor launched** (`os.killpg` + pre-kill
  snapshot). This is why the supervisor must own the agent process group.

## 7. Honest limits
The substrate is live; the driver + supervisor + auto-provenance are built and unit/dogfood
tested. The Agent Host substrate adds host inventory, wake intents, optional
`on_ack_timeout=wake_target` escalation, a message-only systemd host for lane-less handoff
wakes, and a lane-scoped work-host policy for eligible worker machines. The Agent Host daemon
uses inbox-only mode for lane-less message wakes, and refuses global `claim_next` unless an
operator explicitly enables it. Still to prove after HARDEN-3: a long-running multi-agent
supervised session under real load.
