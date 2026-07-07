# CEO-voice narrator — design contract (NARRATE-1)

**Status:** design note. No code in this task. Implementation is NARRATE-2 (task narrator),
NARRATE-3 (deliverable header), NARRATE-4 (UI), NARRATE-5 (backfill + cost proof).

This defines *what* the narrator is, *how it stays cheap*, and *how it avoids the stale-text
failures* the board already hit. It is the agreed contract the implementation tasks build to.

---

## 1. What it is

A **second, CEO-voiced narrator** that runs entirely **separate from the task-scoping agents**.
Agents keep calling `create_task` / `update_task` exactly as today — they are never pulled into
narration duty. A cheap background job reads the board *after* the fact and writes plain-English
prose for a human reader.

It is the same shape as the existing terse agent summarizer (`summarize.py` →
`task_summaries.rationale`), but a different audience and a separate store. The two never mix:

| | `rationale` (exists today) | `narration` (this feature) |
|---|---|---|
| Audience | scoping agents | the operator / a CEO |
| Voice | terse, factual | marketing manager briefing a CEO |
| Length | ≤ 50 words | 3–4 sentences |
| Store | `task_summaries` | `task_narrations` (new) + deliverable metadata |
| Job | `summarize_pending` | `narrate_pending` (new) |

**Derived, never source of truth.** Like `rationale_state` and `narrative_state` today,
narration is advisory. The structured fields (`status`, `dependency_state`, `provenance`,
`progress`, `blockers`) always win. This is a hard rule — see §4.

---

## 2. Voice spec

System-prompt intent (exact wording finalized in NARRATE-2). One short paragraph, **3–4
sentences**, plain English, no jargon, no bullet points, no headers.

**Task narration**
- If the task is **done**: what the feature *is*, and what was **delivered** — in the words a
  marketing manager would use to a CEO.
- If the task is **not done**: what the feature *is*, and what **will be delivered**.

**Deliverable header** (one paragraph answering, in order):
1. What this deliverable *is*.
2. How far along we are.
3. What's been done so far.
4. What's still to do.
5. What it gives us once shipped.

The deliverable narrator does **not** read raw board data. It rewrites the *already-assembled*
structured brief from `mission_narrative.build_mission_brief()` (sections: "What we are
building", "Why it matters", "Completed proof", "Active work", "Risks and blockers", "Next best
move") into CEO prose. Cheaper and more accurate than generating from scratch.

---

## 3. Cost policy (the OpenAI bill stays negligible)

Two structural guarantees, both already proven in `summarize.py`:

**a) Cheap model by default.** `gpt-4o-mini` via the existing gateway. One task narration ≈
1k tokens in / ~150 out ≈ **$0.0002**. A deliverable header ≈ **$0.0005**. A full 197-task
board backfill ≈ **a few cents**.

**b) Pay only for real change.** A source-state **fingerprint** + minimum re-run interval means
an idle re-run makes **zero API calls**. Steady-state cost tracks genuine status transitions,
not clock ticks or board size. Task-level reuses the `activity_cursor` guard from
`get_tasks_needing_summary`; deliverable-level reuses `brief_source_fingerprint(mission_status)`
so a burst of child-task changes collapses into **one** regeneration.

### Env-var contract

| Var | Default | Meaning |
|---|---|---|
| `PM_LLM_BASE_URL` | `http://127.0.0.1:8095/v1` | reused — existing gateway |
| `PM_LLM_KEY` | (gateway key) | reused |
| `PM_NARRATE_MODEL` | `taikun-summarize` (gpt-4o-mini) | narrator model; bump only for richer prose |
| `PM_NARRATE_INTERVAL` | `45` | min seconds between drain cycles / per-item re-narration |
| `PM_NARRATE_MAX_TOKENS` | `220` | output cap (~3–4 sentences) |
| `PM_NARRATE_MAX_TASKS` | `40` | hard per-run ceiling so a mass import can't spike the bill |
| `PM_NARRATE_TRIGGERS` | `create,In Review,Done,Blocked` | which transitions enqueue a narration |

> **Open decision folded into this task:** whether `PM_NARRATE_MODEL` points at the gateway's
> OpenAI alias (default, recommended — reuses cost controls) or a direct OpenAI client. Default
> is the gateway; no direct client unless a later task justifies it.

---

## 4. Stale-flag discipline (carries over BUG-13 / BUG-17 / HARDEN-30)

The board has already been burned by generated text going stale and contradicting reality
(BUG-13 added `dependency_state` + stale flags; BUG-17 suppressed stale rationale from task
APIs; HARDEN-30 made terminal provenance override derived state). The narrator inherits the
same rules — it must not reintroduce that failure mode.

1. **Fingerprint stamp.** Every narration is stored with the exact source fingerprint it was
   written from — `activity_cursor` for tasks, `brief_source_fingerprint` for deliverables.
2. **Stale, not wrong.** When current state moves past the stored fingerprint, the narration is
   flagged stale (mirroring `narrative_state`'s `flags` / `stale` / `message`) and the UI shows
   an "updating…" badge (NARRATE-4) rather than silently presenting outdated prose.
3. **Terminal provenance wins.** A `Done`-with-merge task's narration never overrides or
   contradicts its provenance; if prose and provenance disagree, provenance is truth and the
   prose is marked stale for regeneration.
4. **No new source of truth.** Narration is display text. Nothing in dispatch, claim, reconcile,
   or done-gating may read it as a signal.

---

## 5. Trigger policy

- **Not** a synchronous LLM call in the request path. `create_task` / `update_task`, *after
  commit*, enqueue a lightweight marker (`task_id` + new status) into a `pending_narrations`
  queue — only on the transitions in `PM_NARRATE_TRIGGERS` (default: create, In Review, Done,
  Blocked). Cosmetic field edits do **not** trigger.
- A `narrate_pending` drain job runs on a short systemd timer (~`PM_NARRATE_INTERVAL`), the same
  runner pattern as `summarize_pending`. This reads as near-real-time to the operator while the
  fingerprint guard keeps idle cycles free.
- Deliverable headers re-narrate off the same drain cycle whenever a linked task transition
  changes the deliverable's `brief_source_fingerprint`.

---

## 6. Definition of done for NARRATE-1

- This document merged.
- Env-var contract (§3) agreed.
- Stale discipline (§4) and trigger policy (§5) agreed as the spec NARRATE-2/3/4 build to.
- No change to the agent write path in this task.
