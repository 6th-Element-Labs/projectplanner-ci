# Switchboard — operator runbook (DOGFOOD-4)

How to actually run the autonomous coordination mesh, and **where each piece runs**. Written
from the dogfood (DOGFOOD-3) + the shipped pieces: `run_session` (driver, decision #4),
the Codex `supervisor.py` (ADAPTER-8), RECON-5 auto-provenance, and the monitor sweep.

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
└─────────────────────────────┘         └──────────────────────────────────────────┘
```

**The Plan VM is the substrate, not the runner.** It holds the board, the protocol endpoints,
and the lightweight monitor sweep — near-zero load, correctly sized for a micro. It does **not**
run agents: agent sessions need repo + API keys + real compute (builds, model calls), which
don't belong on a 911 MB coordination box. The **supervisor and the agents it spawns run on an
agent host** (your dev machine, a CI runner, or a dedicated agent box) — one supervisor process
spawns/keeps-alive/kills each agent it launches.

| Piece | Host | Why |
|---|---|---|
| board / MCP / gateway / monitor-sweep timer | **Plan VM** | coordination substrate; tiny + always-on |
| `supervisor.py` (spawn / keep-alive / T3 kill) | **agent host** | owns the agent process group; needs compute |
| agent runtime (Claude Code, Codex, …) + `run_session` | **agent host** | does the actual work; needs repo + keys |

## 2. Run the substrate (Plan VM) — already deployed
```bash
ssh plan-vm; cd /opt/projectplanner
git pull --ff-only
set -a; . ./.env; set +a       # REQUIRED: .env redirects the data dir to /var/lib/projectplanner.
                               # Without it, store resolves the empty /opt/*.db and migrates the WRONG file.
.venv/bin/python -c "import store;[store.init_db(p) for p in store.PROJECTS]"
sudo systemctl restart projectplanner projectplanner-mcp
sudo systemctl enable --now projectplanner-monitors.timer   # durable ack/deadline sweep (every 1m)
```
> The live DBs live in `/var/lib/projectplanner/` (env-redirected), not `/opt/projectplanner/`.
> The `*.db` files under `/opt` are empty placeholders — never point a tool at them.

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

## 4. The self-driving loop (what makes it hands-off)
```
supervisor keeps agent(s) alive
   → each agent run_session: claim_next → work → push → complete_claim(evidence)
      → RECON-5 auto-stamps direct-push provenance  (or PR-merge webhook, RECON-2)
      → task → Done  → unblocks downstream deps
      → claim_next hands out the next task … (loop)
   monitor-sweep (Plan VM) fires any unacked requires_ack handoff
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
tested. Not yet proven: a long-running multi-agent supervised session under real load, and the
PR-merge webhook (RECON-2) as the primary Done path (RECON-5 covers direct-push today). Start
one supervised agent on an agent host, watch the board, then scale the fleet.
