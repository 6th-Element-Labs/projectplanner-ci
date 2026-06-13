# Agent-as-Operator — feature roadmap & live-meeting agent

**The thesis:** the AI *operates* the plan; the software is the **observation deck**.

Linear, Jira-AI, and Atlassian Rovo are copilots bolted onto a system-of-record — the
human is the operator, the AI summarizes / suggests / drafts. We invert it: the agent
**creates, runs, manages, and does the work**, and the UI exists so humans can *watch,
approve, and redirect* — not so they can do data entry. The human's job shrinks to
**supervise → approve → redirect**.

Everything below builds on the seed already documented in [`AGENT_ROADMAP.md`](AGENT_ROADMAP.md)
(per-task + plan-wide Ask agent, RAG over `plan-docs/*.md`, propose-to-confirm, the
activity-log audit substrate, the MCP front door, `dispatch.py` → Claude Code → PR, the
email Inbox triage, the Pulse digest). This doc is the *operator-level* layer on top.

---

## Feature roadmap, by operator verb

Effort key: **S** = prompt + small UI over existing primitives · **M** = one new
module/table · **L** = new subsystem. All writes stay behind propose-to-confirm.

### Creates — the plan authors and re-authors itself
| Feature | What it does | Builds on | Effort |
|---|---|---|---|
| **Continuous re-planner** | When reality changes (slip, flipped decision, new Inbox info) it re-sequences downstream tasks, shifts dates, reassigns — and posts a *diff* to approve. The plan is never stale because a human forgot to drag a card. | `signals.py`, dep graph, bulk-propose | M |
| **Goal → plan synthesis** | Drop a PRD / transcript / deck → agent emits the full workstream+task graph with deps, owners, estimates. | `intake.py`, `build_plan_artifacts.py` | M |
| **Dependency & gap inference** | Reads task text, proposes missing deps, detects cycles, flags blocking-but-unowned tasks. (The manual audit, made standing.) | board-read tools | S |

### Does — the part no incumbent has
Our moat: `dispatch.py` already ships code via Claude Code → PR. Extend it from one-click
to a loop, and beyond code.
| Feature | What it does | Builds on | Effort |
|---|---|---|---|
| **Autonomous delivery loop** *(flagship)* | Agent picks the next *ready, unblocked, Taikun-owned* task, dispatches it, watches the PR, and on merge marks it Done → unblocks dependents → dispatches the next. The plan executes itself; you watch it drain. | `dispatch.py`, dep graph, GitHub status | M |
| **Ops actions (not just code)** | Most pilot tasks aren't code — "email Sahir the Entra app-reg request," "file the S3 bucket ask," "draft the data-sharing memo." Agent drafts + sends via the gmail/notify stack and logs it. | `gmail_source.py`, `notify.py` | M |
| **Chase agent** | Overdue human/Total-owned task → auto-drafts a grounded nudge to the owner ("Sahir, SSO-2 is the SSO go-live gate, due 6/2 — ETA?") → propose-to-send. | signals + notify | S |

### Manages — it keeps itself honest
| Feature | What it does | Builds on | Effort |
|---|---|---|---|
| **Status-drift detector** | Reconciles *claimed* status vs *evidence*: Done tasks whose PR never merged, or whose dependents report it isn't actually working. Flags the lie. PM tools trust the checkbox; we shouldn't. | activity log, dispatch records, GitHub | M |
| **Outcome tracker** *(the big one)* | Don't just track task completion — track whether the *project is winning*. Wire the agent to the pilot's six success criteria (≥95% ingest, MTTA ≤15m, ≥40% auto-close…) against live data: "on track to pass 4/6, at risk on auto-close." A plan that knows if it's succeeding. | DATA workstream KPIs, live pilot data | L |
| **Standing critic / auditor** | Periodically runs the scope/staleness/contradiction/overclaim audit and proposes cleanups. | board-read + RAG | S |

### The supervisor's deck — make veto effortless
| Feature | What it does | Builds on | Effort |
|---|---|---|---|
| **Unified agent action queue** | Every agent intent (dispatch, send email, change a date, close a decision, reassign) lands in one approve / deny / "auto-approve-by-policy" inbox. The human is a reviewer, not a clicker. *This is the thesis made concrete.* | propose-to-confirm (`agent.py`) | M |
| **Per-person login briefing** | "Here's what I did, what's slipping, what needs you" — personal, on demand, not just the weekly Pulse. | `digest.py`, signals | S |
| **What-if simulation** | "What if Bedrock access slips two weeks?" → re-runs the schedule, shows the blast radius. | Gantt + dep graph | M |
| **Slack / voice control plane** | Run the plan from where humans already are: "@plan what's blocking go-live?" / "dispatch the next three ready tasks." | MCP tools | M (Slack) · L (voice — see below) |

### Flagship next three
1. **Autonomous delivery loop** — turns `dispatch.py` from a button into the headline demo: a plan that ships itself.
2. **Unified action queue** — the cleanest expression of "AI operates, human supervises"; makes every other agent action safe to ship.
3. **Outcome tracker** — makes us a *project-success* tool, not a task tracker. Hard to copy: it requires the plan to be wired to real outcome data, which our agent already touches.

---

## Live-meeting / voice agent

> "Add the agent to con-calls, ask it questions, have it act." Very doable. It's three
> independent layers — pick a vendor per layer.

### Layer 1 — meeting transport (get audio in & out of the call) — **use Recall.ai**
The voice API does **not** join Zoom/Meet/Teams by itself; a bot has to sit in the meeting,
capture per-speaker audio, and play audio back. We **rent this layer from Recall.ai** rather
than build it (rationale in *Build vs buy* below).
- **Recall.ai** *(chosen)* — one "meeting-bot-as-an-API" across Zoom / Google Meet / Teams /
  Webex. Hand it a join URL → it spins up a named participant ("Atlas (Taikun)"), waits in the
  lobby until admitted, streams **per-participant audio out** (diarized), and lets us **play
  audio back** into the room. Same headless-bot job Otter/Fireflies do — rented, not rebuilt.
- **Dial-in / phone bridge** — for SIP/PSTN concalls, Twilio Media Streams **or** OpenAI
  Realtime's **native SIP** (point a trunk straight at the session). Recall is for the video
  platforms.

### Meeting auto-join — invite `plan@taikunai.com` (the Otter pattern)
Two ways the agent learns about a meeting; ship the first, add the second for robustness.
1. **Invite-the-email *(ship first — reuses what we have)*** — add `plan@taikunai.com` as a
   guest → the calendar emails it an `.ics` → our existing mailbox + Atlas calendar-parsing
   agent extracts the **start time + Zoom/Meet/Teams join URL** → schedules a join job → at
   start time we call **Recall.ai** with the URL and the bot joins. The "detect" half is
   already built; the only net-new call is the Recall handoff.
2. **Connect-your-calendar (OAuth) *(robustness upgrade)*** — authorize Google/Microsoft
   calendar once; watch every event, read `conferenceData` for the link, auto-join all
   meetings. More reliable than the email route (the `.ics` link is sometimes absent for
   Zoom/Teams) — it's how Otter auto-joins everything.

Caveats: the join link must be in the invite (Meet auto-embeds; Zoom/Teams rely on the
organizer pasting it — the OAuth path fixes this); the bot lands in the **lobby** and a host
admits it; in two-party-consent regions it must **announce itself / be clearly named**.

### Build vs buy — why Recall now, in-source later
The calendar/`.ics` "detect" logic is **ours** (already built). The **transport** is a
commodity we rent until scale or enterprise data-residency justifies in-sourcing — building
it from zero on day one spends moat-budget on plumbing.
- **Why not DIY now** — three hostile integrations with nothing in common: Zoom (official
  Meeting SDK/RTMS, headless + marketplace review), Teams (Graph calling/media bots on Azure,
  cert-based, .NET-heavy), and **Google Meet — no general bot API, so headless-Chromium
  scraping that breaks on every UI change, a forever maintenance treadmill** — plus a
  bot-per-meeting fleet, audio normalization, virtual-mic playback, and reconnect logic.
  Recall is one API over all of it.
- **When to in-source (Phase 2)** — (a) **cost at scale**: Recall is per-bot-hour; thousands
  of concurrent hours make the official-SDK platforms cheaper to self-host; (b) **enterprise
  data-residency — the decisive one for TEEP/TotalEnergies**: a third-party bot in Darko's
  confidential ops calls, audio transiting Recall's infra, is a procurement/data-boundary
  problem. "The audio never leaves our cloud" beats "it goes to Recall.ai."
- **The plan** — **Phase 1**: Recall.ai everywhere, agent-in-meetings in days. **Phase 2
  (security-sensitive accounts)**: bring **Zoom + Teams in-house via their official SDKs in
  our own cloud**; rent Meet (the one nobody wants to maintain), or don't support Meet there.

### Layer 2 — voice I/O (ears + mouth)
OpenAI gives you two shapes:
- **Realtime API** (`gpt-realtime`) — true **speech-to-speech** over WebRTC/WebSocket.
  Low latency (~300–800 ms voice-to-voice), built-in **server-side VAD**, **barge-in /
  interruption**, selectable voices, **function calling**, **native SIP**. Audio:
  `pcm16` 24 kHz, or `g711` for telephony. This is the one to use for a live meeting.
- **STT/TTS pipeline** — `gpt-4o-transcribe` / `gpt-4o-mini-transcribe` for streaming
  speech-to-text, `gpt-4o-mini-tts` for steerable speech-out. More moving parts and
  latency, but you control each stage.

### Layer 3 — the brain + tools (this is where we stay differentiated)
Three options:
- **A — OpenAI Realtime as the brain (GPT reasons).** Simplest, lowest latency. But the
  reasoning is GPT, not our tuned, RAG-grounded plan agent.
- **B — Claude as the brain, OpenAI for voice.** STT → our `agent.py` plan agent (RAG +
  MCP tools) → TTS. Keeps our agent's logic and grounding; higher latency.
- **C — Hybrid *(recommended)*.** Realtime runs the *conversation* (turn-taking, VAD,
  voice), but for anything substantive it **calls a tool that hits our plan agent**
  (`ask_plan` / the MCP tools) and reads the answer back. Snappy voice front, **Claude/our
  plan agent as the brain**, zero duplicated logic. While the tool call runs (a RAG answer
  is ~1–5 s) the agent says a filler — "let me check the plan…" — the standard pattern.

### Wiring it to the plan (so it answers *and* acts)
Point the Realtime session's **function-calling** at our existing MCP tools:
`search_tasks`, `get_task`, `ask_plan`, `board_summary`, `get_plan_signals` (read) and
`update_task`, `create_task`, `add_comment`, `dispatch_to_claude_code` (act). Then in a
call it can field "what's blocking SSO?" *and* "mark SEN-6 done" / "dispatch the next
ready task" live.

### Guardrails & UX (so it's a teammate, not a heckler)
- **Speak only when addressed** — wake word ("Atlas…"), host @-mention, or push-to-talk.
  Muted by default; it listens, speaks when called.
- **Writes go through the action queue** — no unconfirmed plan mutation mid-call. It can
  *propose* ("want me to mark SEN-6 done?") and apply after a yes, or queue for later.
- **Passive value even when silent** — pipe the call transcript into the existing
  `intake.py` / Inbox triage so the meeting updates the plan afterward regardless. Even a
  mute attendee earns its seat.
- **Diarization** — Recall's per-speaker streams (or Realtime + speaker labels) so updates
  attribute to the right person.
- **Cost / latency budget** — Realtime audio is priced per audio-minute; keep it muted
  until addressed to control cost; pre-warm the plan-agent tool path.

### Minimal POC (a few days)
Invite `plan@taikunai.com` → `.ics` parsed for the join URL → at start time call **Recall.ai**
with the URL → bot joins → audio ↔ **OpenAI Realtime** (tools = our plan MCP, hybrid brain via
`ask_plan`) → audio played back through Recall. Wake word "Atlas." Writes queued, not
auto-applied. Transcript → Inbox triage on hang-up.

---

## Principles (non-negotiable)
- **Propose-to-confirm for every write.** The agent never silently mutates outward-facing
  or shared state — it proposes; a human approves in one click (or sets a policy to
  auto-approve a class of low-risk actions).
- **The UI is the window, not the workplace.** If a human *has* to open the board to keep
  the plan correct, we've failed — the agent keeps it correct; the board is for watching.
- **No overclaim.** The agent reports what's true with evidence — including "I'm not sure"
  and "this task says Done but its PR isn't merged."
