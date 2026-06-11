# plan.taikunai.com — Agent Roadmap

**From a project tool to an active AI assistant.**

This document is the build plan for evolving the `projectplanner` satellite
(plan.taikunai.com) from a standard PM board into a RAG-grounded, action-taking
assistant — plus a second front door over MCP. It is intentionally lean: everything
runs on the existing t4g.micro + bundled LiteLLM gateway, systemd, SQLite. **No
workflow engine** (per ADR 0007 — the satellite stays self-contained).

---

## What's live today (the agentic seed)

- **Per-task "Ask Taikun" agent** (`agent.py`): a slim ReAct loop on GPT‑5.5 via the
  bundled gateway. Tools: `doc_search` (RAG) + `propose_task_update`.
- **RAG** (`rag.py`): brute-force cosine over `plan-docs/*.md` using `taikun-embed`.
  Cited sources.
- **Propose-to-confirm**: nothing changes until the user approves; every applied
  change is audited as actor **"Maxwell (confirmed)"**.
- **Task store** (`store.py`): SQLite, full task CRUD, an **activity log that stamps
  every change with actor + timestamp** (this is the delta/audit substrate).
- **API** (`app.py`): `/api/board`, `/api/tasks` CRUD, `/api/tasks/{id}/chat`,
  `/api/people`, exports.

Verdict: reactive, single-task. The perfect seed — not yet a teammate.

---

## The reframe: ~7 primitives, then the features fall out

The 8 capabilities are thin compositions over a handful of shared primitives. Build
the substrate once; most features become a prompt + a small UI.

| Primitive | Lives in | Feeds capabilities |
|---|---|---|
| **Board-read tools** (`search_tasks`, `get_task`, `board_summary`) | agent.py | 1, 2, 3, 4, 6, 7, 8 |
| **Global agent session** (agent runs with or without a task; own chat store) | new endpoint + table | 1, 2, 5, 8 |
| **Bulk propose** (agent emits a *list*; confirm-all UI) | agent + UI | 2, 5, 6 |
| **Plan signals** (`compute_plan_signals`) | new module | 3, 4, 6 |
| **Notify** (`notify.send` → Slack + Gmail adapters) | new module | 3, 4, 5 |
| **Scheduler** (systemd timer — NOT the workflow engine) | timer + job | 3, 6 |
| **Risk classifier + autonomy policy** | new function | 7 (informs confirm UX everywhere) |
| **MCP server** (exposure layer wrapping all the above) | FastMCP mount | 8 (cross-cutting) |

Two are nearly free: **deltas** come straight from the existing activity log (no
snapshot infra), and the **autonomy pattern** is the same `auto_resolve` /
`needs_human` switch already proven in the ActionEngine sensing layer.

---

## The 8 capabilities

1. **Plan-wide chat** — one "Ask Taikun" that sees the whole plan + docs. "What's
   blocking SSO?", "What did Sahir commit to?", "Summarize this week's risks."
2. **Bulk / cross-task actions** — propose-to-confirm macros: "mark all SEN bootstrap
   done", "push every Bedrock task out a week."
3. **Proactive standing digests** — scheduled chief-of-staff brief: what changed,
   newly overdue/unblocked, critical paths slipping, decisions past due.
4. **Next-best-action** — each person's top 1–2 unblocked tasks; drafts the nudge.
5. **Draft from live state** — meeting transcript → tasks / stakeholder update / exec
   summary.
6. **Automated plan maintenance** — dependency-aware reschedule on slip, flag orphans,
   clean sloppy titles.
7. **Autonomy switch** — auto-apply low-risk changes (status flips) with audit;
   escalate high-impact. **Off by default.**
8. **MCP server** — expose the plan to Cursor / Claude Desktop / Claude Code as MCP
   tools over Streamable HTTP. A second front door over the same primitives.

---

## Decisions (locked)

- **Auth: public for now.** No login. Everything runs **shared-identity**; all agent
  actions audited as "Maxwell". No per-user identity yet (personal digests/nudges run
  in team/unattributed mode until auth). **MCP write tools get a shared token**; reads
  open. Revisit real auth before per-user features or a second customer. Because we're
  staying public, Phases 4 and 7 are **not** gated — they ship in shared-identity mode.
- **Notify channel: Slack + Gmail** (not SMTP/SES). `notify.send` gets two adapters:
  Slack (incoming webhook / bot token) and Gmail (API, `gmail.send` scope). This
  **deletes the SPF/DKIM/DMARC deliverability problem**. Build it now, wire it; heavy
  scheduled use lands in Phase 4. Setup = one Gmail OAuth scope + one Slack app/webhook.
- **Autonomy default: OFF.** #7 ships off; opt-in per deployment to auto-apply only
  low-risk status flips, with one-click undo.

---

## Phased build

Each phase deploys a usable increment to plan.taikunai.com on its own.

| Phase | Ships | Notes |
|---|---|---|
| **0** | Agent foundation: board-read tools, global/per-task loop | keystone; upgrades existing per-task chat too |
| **1** | #1 Plan-wide chat (web "Ask Taikun" tab) | front door; whole board (~10K tokens) fits in context |
| **1.5** | #8 **MCP server v1** (read + CRUD + doc_search, token-gated writes) | parity win; thin transport wrapper |
| **2** | #2 Bulk actions (confirm-all UI) | + `ask_plan` tool added to MCP |
| **3** | Plan signals + #4 next-best-action | + `get_plan_signals` to MCP |
| **3.5** | #3 digest, **in-app post first** (no delivery) | value before notify lands |
| **4** | `notify.send` (Slack + Gmail) + systemd scheduler → digests/nudges delivered | "feels alive" moment; the long pole |
| **5** | #5 draft-from-transcript → bulk-create + Slack/Gmail update | reuses 2 + 4 |
| **6** | #6 automated maintenance (dependency-aware reschedule, orphan flags, cleanup) | port the offline scheduler from `build_plan_artifacts.py` |
| **7** | #7 autonomy switch (**off by default**, low-risk only, undo) | the differentiator, last |

---

## MCP server design (#8)

- **Transport:** remote MCP over **Streamable HTTP** at `https://plan.taikunai.com/mcp`.
  Caddy terminates TLS; FastMCP mounts on the existing FastAPI app, so tools call
  `store` / `agent` / `rag` **in-process**.
- **Tools:** `search_tasks`, `get_task`, `board_summary`, `doc_search`, `create_task`,
  `update_task`, `add_comment`, and the killer **`ask_plan(question)`** — the whole
  RAG-+board-grounded agent as one tool (the differentiator vs CRUD-only MCP).
- **Auth:** shared MCP token for write tools (reads open) while public; proper OAuth
  when login lands. Rate-limit `ask_plan` (it runs the gateway → cost).
- **Grows as a track:** every new primitive gets a ~10-line MCP wrapper next to its web
  surface. Two doors, one engine.

---

## Cross-cutting

- Stays on the t4g.micro + bundled gateway; scheduler = **systemd timer**, not DBOS.
- SQLite in **WAL mode**, short transactions (scheduled agent + human edits won't collide).
- **Board-read is a queryable tool**, not a context dump — so a 1,000-task plan or a
  large doc corpus doesn't blow the window or the brute-force RAG.
- All agent writes audited in the activity log; the **one-click undo** for autonomy
  builds on it.

---

## Status

- [x] **Phase 0 — agent foundation** (board-read tools `search_tasks`/`get_task`, global/per-task loop, whole-board summary in the prompt, `task_id` on proposals) — `agent.py`
- [x] **Phase 1 — plan-wide chat tab** (`POST /api/chat` + `/api/chat/history`, persisted `chat` table, "Ask Taikun" tab with RAG-cited answers + propose-to-confirm cards) — live on plan.taikunai.com
- [x] **Phase 1.5 — MCP server v1** (`mcp_server.py`: 8 tools — search_tasks/get_task/board_summary/doc_search/ask_plan + create/update/add_comment; Streamable HTTP at `https://plan.taikunai.com/mcp` via a `projectplanner-mcp` systemd unit + Caddy `/mcp` route; reads open, writes gated by `PM_MCP_TOKEN`). See `docs/MCP.md`.
- [x] **Phase 2 — bulk / cross-task actions** (`propose_bulk_update` same-change-to-many + `propose_date_shift` server-computed shifts; `/api/chat` returns `proposals[]`; Ask-tab **Confirm-all** card with per-row drop). Verified: 3-task status change in one click, then reverted.
- [x] **Phase 3 — plan signals + next-best-action** (`signals.py` `compute_plan_signals`: overdue/due-soon/blocked/ready/critical-slip/past-due-decisions + each owner's next-best 1-2; surfaced via `GET /api/signals`, the `plan_signals` agent tool, the `get_plan_signals` MCP tool, and a "Next up" line per owner in the By-person tab). Verified live.
- [x] **Phase 3.5 — in-app digest** (`digest.py` `generate_digest` = signals + activity-log deltas since last digest → one LLM chief-of-staff brief; `digests` table; `POST /api/digest` + `GET /api/digests`; MCP `generate_digest`; **Pulse tab** with Generate + latest + collapsible history). Verified: brief reads real activity deltas, renders in the UI, MCP works.
- [ ] Phase 4 — notify (Slack + Gmail) + scheduler
- [ ] Phase 5 — draft from live state
- [ ] Phase 6 — automated maintenance
- [ ] Phase 7 — autonomy switch
