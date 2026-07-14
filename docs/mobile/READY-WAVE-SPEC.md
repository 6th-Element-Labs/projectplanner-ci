# `ready_wave` — the thin read that makes "draw the work" real

- **Status:** proposed (v3 mobile) · thin add over shipped primitives
- **Product:** Switchboard · mobile Deliverables
- **Purpose:** turn `claim_next` from *one task at a time* into *"here is the parallelizable batch, and the keystone that unlocks the next one."* This is the single endpoint the mobile **Draw the work / Waves** screen needs to stop being a mock.

> `claim_next` answers "give me the one best task." `ready_wave` answers "show me everything an
> operator could dispatch **right now, in parallel**, and what merging the keystone unlocks." It
> claims nothing — it's the read behind the button.

---

## 1. Why it's a thin add, not a new subsystem

Everything it needs already exists:

| Needs | Already shipped |
|---|---|
| eligibility (ready status, deps complete, no active claim, not human-gated, risk/budget) | `claim_next` eligibility rules (`CLAIM-NEXT-SPEC.md §4`, `store.claim_next`) |
| per-task score + skip reasons | `dispatch_reason` (`score.v1`) |
| model/budget guidance | `recommendation` / `budget` on the claim response |
| dependency structure, keystones, blockers | `mission_graph.build_dependency_graph` (nodes, edges, `blocker` flag) |
| file-collision detection | `list_active_leases` / `claim_files` |
| free-runner capacity | agent presence + WIP limits (`claim_next §10`) |

`ready_wave` = run `claim_next`'s eligibility + scoring over the deliverable's linked tasks **without the atomic claim**, then group with `mission_graph`. It is `peek_next` (spec §7.1) widened from *one candidate* to *the batch + graph context*.

---

## 2. Operation

```text
peek_wave(project, deliverable_id?, board_id?, mission_id?, milestone_id?,
          lanes?, capabilities?, max_risk?, max_budget_usd?, free_runners?)
```
REST: `POST /txp/v1/peek_wave` · read-only, idempotent, no claim, no lease.

### Response
```json
{
  "schema": "switchboard.ready_wave.v1",
  "deliverable_id": "SSO-GO-LIVE",
  "capacity": { "free_runners": 3, "wip_limit_per_agent": 2 },
  "wave": {
    "index": 1,
    "ready_count": 6,
    "dispatchable_now": 3,
    "queued": 3,
    "lanes": [
      { "lane": "IDP", "tasks": [
        { "task_id": "SEN-7", "title": "SAML assertion mapping",
          "risk": "medium", "est_hours": 2, "score": 8420,
          "recommendation": { "tier": "balanced", "reason": "UI/backend integ w/ tests" },
          "budget": { "est_usd": 3.0, "status": "ok" },
          "collision": null }
      ]},
      { "lane": "DATA", "tasks": [
        { "task_id": "DATA-3", "title": "User attribute sync", "risk": "medium", "score": 8100,
          "collision": null },
        { "task_id": "DATA-4", "title": "Directory pagination",
          "collision": { "with": "DATA-3", "files": ["directory/sync.py"], "held_until": "DATA-3 release" } }
      ]}
    ]
  },
  "keystones": [
    { "task_id": "SEN-6", "state": "in_review",
      "unlocks_count": 5, "unlocks": ["SEN-8","SEN-9","INT-1","INT-2","QA-3"],
      "on_critical_path": true }
  ],
  "next_wave_preview": { "index": 2, "unlocked_by": "SEN-6", "task_count": 5 },
  "skipped": { "dependencies": 5, "human_approval": 1, "active_claim": 3,
               "capability_mismatch": 1, "risk": 0, "budget": 0, "lease": 1 },
  "dispatch_reason": { "policy": "score.v1", "candidate_count": 6 }
}
```

### Field derivation
- **`wave.lanes[].tasks`** — the eligible set from `claim_next` scoring, grouped by workstream (`mission_graph._workstream`), ordered by `score`.
- **`dispatchable_now` / `queued`** — `min(ready_count, free_runners × wip)` vs the remainder. Drives the capacity meter.
- **`collision`** — cross-check each ready task's likely files (`recommended_files`) against `list_active_leases` and against *other ready tasks in the batch*; hold the lower-scored one.
- **`keystones`** — nodes with the largest downstream `unlocks_count` among unfinished tasks (`mission_graph` edges + `blocker` flag). `unlocks_count` = size of the dependency closure that becomes ready when this task reaches terminal provenance.
- **`next_wave_preview`** — recompute eligibility assuming each keystone is `Done` → the delta is the next wave.
- **`skipped`** — passthrough of `dispatch_reason.skipped`, plus a new `lease` bucket for collision holds.

---

## 3. Dispatching a wave (the button)

`ready_wave` is the read. The write stays atomic and governed:

- **Option A (ship first):** the app fires **N parallel `claim_next` calls** — one per selected task — each atomic, each returning its own claim/lease/recommendation. Losers to a race get `claimed:false` and fall back to the next ready task. Zero new write primitive.
- **Option B (later):** `claim_wave(project, deliverable_id, task_ids[], agent_pool)` — a convenience that loops `claim_next` server-side in one request and returns the batch of claims. Same atomicity guarantees per task.

Every claim still flows through the action queue / `propose→confirm` when the operator has auto-approve off, and emits `task.claimed` activity. **No task is claimed twice; dispatch never bypasses governance.**

---

## 4. Why this is the keystone of v3

It converts three mocked mobile ideas into real ones with one read:
1. **The capacity meter** ("6 ready · 3 runners free → dispatch 3") — `capacity` + `dispatchable_now`.
2. **Parallel lanes** — `wave.lanes`.
3. **Keystone / "unlocks Wave 2"** — `keystones` + `next_wave_preview`.

And it stays honest: it exposes the scheduler's real reasoning (`score`, `skipped`, `recommendation`) instead of inventing a batch. The operator sees *why* these six are ready and those five aren't — which is the whole "AI operates, you supervise" thesis, made legible on a phone.

## 5. Conformance
1. A blocked / dep-incomplete / human-gated task never appears in `wave`.
2. `ready_count` == sum of `wave.lanes[].tasks`; `dispatchable_now + queued == ready_count`.
3. Two ready tasks sharing a held file → exactly one carries `collision`, the other is dispatchable.
4. `keystones[].unlocks_count` matches the dependency closure that `next_wave_preview` reveals.
5. `peek_wave` mutates nothing — no claim, no lease, no status change, no activity beyond a read audit.
6. Given identical inputs, output is deterministic (reuses `score.v1`).
