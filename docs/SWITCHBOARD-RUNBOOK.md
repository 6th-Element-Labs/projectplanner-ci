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
│  COORDINATION ONLY           │         │  .switchboard/runner/  (session records)   │
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
> The live DBs live in `/var/lib/projectplanner/` (env-redirected), not `/opt/projectplanner/`.
> The `*.db` files under `/opt` are empty placeholders — never point a tool at them.
> Do not move these paths directly during the rename. Add and validate Switchboard aliases first.

## 3. Run an autonomous agent (agent host)
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

## 4. The self-driving loop (what makes it hands-off)
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

## 5. Control fidelity / safety (PRD §10)
- **T1** advisory (any runtime): the working agreement + `evaluate_tool`.
- **T2** boundary-deny: runtimes with a pre-tool hook (Claude Code `PreToolUse`); Codex via a
  managed runner that honors deny.
- **T3** hard kill: only for processes the **supervisor launched** (`os.killpg` + pre-kill
  snapshot). This is why the supervisor must own the agent process group.

## 6. Honest limits
The substrate is live; the driver + supervisor + auto-provenance are built and unit/dogfood
tested. The Agent Host substrate adds host inventory, wake intents, optional
`on_ack_timeout=wake_target` escalation, a message-only systemd host for lane-less handoff
wakes, and a lane-scoped work-host policy for eligible worker machines. The Agent Host daemon
uses inbox-only mode for lane-less message wakes, and refuses global `claim_next` unless an
operator explicitly enables it. Still to prove after HARDEN-3: a long-running multi-agent
supervised session under real load.
