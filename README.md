# ProjectPlanner

A tiny, standalone Asana-style project-board web app with a per-task **Ask Taikun**
agent (RAG over the plan docs + propose-to-confirm task edits). Extracted from the
ActionEngine `taikun-pm` satellite (ADR 0007) into its own repo so it is **not** part
of the core platform and never ships to a fresh ActionEngine install.

Runs as **two small processes on one cheap VM**:
- `app` — FastAPI on `127.0.0.1:8110` (board UI + task CRUD + live xlsx/MSPDI export + the agent)
- `gateway` — a bundled **LiteLLM** proxy on `127.0.0.1:8095` exposing `taikun-chat` / `taikun-embed`

The app talks only to the local gateway (so the OpenAI key lives in the gateway, not
the app, and models are swappable in `deploy/gateway/config.yaml`). Storage is a single
**SQLite** file — no database server. Caddy fronts it with auto-HTTPS at
`plan.taikunai.com`.

> Why no workflow engine? The agent is an interactive ReAct loop (a few tool calls) —
> the in-process / non-durable class. The durable workflow engine is core-coupled and
> unnecessary here. The shared *gateway* is the only platform piece worth reusing, and
> it's standalone, so it's bundled.

## Layout
```
app.py store.py export.py rag.py agent.py   # the service
static/                                       # board UI (index.html + app.js)
plan-docs/                                    # docs the agent RAGs over + project-plan.json (source)
seed_plan.json                                # dated slim plan seeded into SQLite on first run
build_plan_artifacts.py                       # regenerate seed_plan.json (rebase kickoff/timeline)
requirements.txt                              # app deps
deploy/
  gateway/config.yaml + requirements.txt      # the bundled LiteLLM gateway
  projectplanner.service / -gateway.service   # systemd units
  Caddyfile                                    # plan.taikunai.com -> :8110 (auto-HTTPS)
  PROVISION.md                                 # spin up the cheap VM end-to-end
```

## Run locally
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r deploy/gateway/requirements.txt
cp .env.example .env   # set OPENAI_API_KEY + a master key (PM_LLM_KEY == LLM_GATEWAY_MASTER_KEY)
litellm --config deploy/gateway/config.yaml --port 8095 &     # the gateway
uvicorn app:app --port 8110                                   # the app -> http://localhost:8110/
```

## Rebase the timeline
```bash
python3 build_plan_artifacts.py 2026-06-01    # any Monday kickoff -> regenerates seed_plan.json
```
On a live VM, update dates in-place with a dates-only SQLite UPDATE (don't wipe/reseed —
that drops user edits). See PROVISION.md.

## Deploy to a VM at plan.taikunai.com
See [deploy/PROVISION.md](deploy/PROVISION.md). Roughly: a t4g.micro (~$6/mo), one venv,
two systemd units, Caddy, and a Route 53 A record.
