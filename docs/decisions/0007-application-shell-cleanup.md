# ADR-0007 — The application shell: hold the line (keep Caddy, ratchet the monoliths, redirect new growth, census the surfaces)

- **Status:** Proposed — accepted when the operator merges this PR. Board task **ARCH-20**.
- **Date:** 2026-07-11
- **Author:** application-shell audit session (Claude Code / Fable 5) — four parallel audit
  threads (app/MCP surface, store.py regrowth, frontend/polling, repo hygiene) plus a
  verification pass; all numbers measured on master @ `d83d59e`.
- **Relates to:** [ADR-0006](0006-control-plane-done-enough.md) (the discipline this extends;
  its Decision 2 pre-authorizes the ratchet) · [ADR-0005](0005-store-module-decomposition.md)
  (retired decomposition; its verbatim-move rules are reused for on-touch extraction) ·
  board task **ARCH-19** (SQLite stay; Postgres readiness gated on measured SLO failure) ·
  deliverable **`mcp-agent-path-performance`** (owns the perf SLOs and slim-read/lock-retry
  work) · HARDEN-36 (the TTL+ETag pattern Decision 5 reuses) · `deploy/Caddyfile`.

---

## Context — the numbers, then the diagnosis

The trigger question was small: *"does Switchboard need Nginx?"* The answer is no (Decision 1).
The audit that answered it found the real problem: **ADR-0006 froze the control plane's
mechanisms, but nothing is holding the line on its files** — and the application shell around
the control plane has no governing decision at all.

**The five surfaces, measured:**

| Surface | Size (master `d83d59e`) | Shape |
|---|---|---|
| `store.py` | **15,282 lines**, 491 top-level functions | god module; ADR-0006 froze it at 14,382 four days ago |
| `app.py` | 3,204 lines, **191 routes** (71 `/ixp/v1`, 19 `/api/deliverables`, …) | one `@app` object; only auth uses an APIRouter |
| `mcp_server.py` | 3,182 lines, **158 `@mcp.tool`s** | second monolith, same size as `app.py` |
| `static/app.js` | 5,945 lines (~330 KB raw) | one `TeepPlan` object, no build step, served raw |
| repo root | **159 flat `.py` files** (106 of them tests) | no `tests/`; tests run as scripts via `scripts/switchboard_ci.sh` |

**The moratorium is losing.** ADR-0006 (2026-07-08) imposed a *policy* moratorium on net-new
`store.py` growth. Four days later master is **+900 lines (+6.3%)**. A commit-level sample of
23 of those commits attributes +709 lines / 49 new top-level functions: BUG-32 lock-retry
wrappers (+102), BUG-31 board snapshot aggregators (+95), HARDEN-36 cache & mission builders
(+86), BUG-29 branch retirement (+75), UI-2 KPI listers (+56), ACCESS principal lookups (+25)…

**The violation is not misbehavior — it is routing.** Most of that code is exactly the
hardening ARCH-19 and the performance deliverable demanded. Every commit was locally
justifiable; the file grew because nothing says where else code may land. A policy without a
gate lost to the same equilibrium ADR-0006 named, in under a week. ADR-0006 Decision 2
anticipated this precisely: *"Promote to an automated check only if the policy is actually
violated."* It is. **This ADR is that promotion.**

**Parked mechanisms still ship their surfaces.** ADR-0006 parked RECON-8/9 and rejected
RECON-10, but their endpoints outlived the verdicts: `replay_verify` + `simulate_dispatch`
exist as MCP tools *and* `/ixp/v1` routes (mcp_server.py:1868,1881; app.py:2937,2950);
coordination receipts keep three MCP tools (`get_coordination_receipt`,
`list_coordination_receipts`, `project_task_receipts`); `evaluate_dbos_runtime` — an
evaluation whose recorded answer is *no* — is still callable on both surfaces.

**The frontend polls politely, but unevenly.** The board fetch is the good pattern
(HARDEN-36): lite payload, 5s server TTL, ETag/304. The mission cockpit polls
`mission_status` + `dependency_graph` every 5s with `cache: 'no-store'` and **no server
cache**; the 30s ack-bell poll ignores `document.hidden`. There is no SSE/WebSocket anywhere
(fine — see Decision 5). Dead HTML litters `static/`: `index-legacy.html`, `rebrand.html`,
`ocr.html` (`format.html` supersedes the latter two and stays — it is the
`format.taikunai.com` surface, Caddyfile:54).

**Corrections to the record** (so the plan-of-record stops lying, CONSOL-5 style):

- ADR-0006 named `side_effects` and `runner` as the two pre-authorized extraction clusters.
  Measured: **there is no `side_effects` function cluster** — zero functions; it is a table
  journaled from task-lifecycle code. `runner` is real and clean: **15 functions, 445 lines,
  2 external callers**.
- `gmail_source.py`: zero importers, no `__main__`, no systemd unit — dead.
- **112 distinct `PM_*` env flags** exist, with no lifecycle and no owner list.

**What is already right (and constrains the fix):** handlers are thin — 85 of the 191 routes
are under 10 lines, none over 50; the fat lives in `store.py`, where ADR-0005 said it was.
Both surfaces already carry `server-timing` (app.py:189; `mcp_http_timing.py`), and
`mcp_observability.py` already samples per-tool latency and errors — **measurement exists;
use it, don't rebuild it.** Every redirect rule in Decision 3 has a shipped precedent: 7 leaf
stores over `db/` behind the `store.py` facade (ARCH-2/3), `services/auth` proving APIRouter
inclusion, `taikun-ui.js` proving the frontend grows fine in new files. This ADR adds no new
architecture; it makes the existing patterns the default instead of the exception.

---

## Decision 1 — Edge: keep Caddy; Nginx is rejected

`deploy/Caddyfile` already does the entire edge job: automatic TLS, `encode zstd gzip`
(line 23), 7-day immutable caching for versioned static assets (lines 28–29), `/mcp* → :8111`
and everything else `→ :8110` with bounded dial/response timeouts and retry-through-restart
(lines 5–11, 39–44), aggressive `/health` probing (lines 14–21). The `server-timing` headers
show latency is **in-process, behind the proxy** — a proxy swap changes the one layer that is
measurably not the problem, and adds config surface, a migration, and a second way to be
wrong about TLS. Revisit only with endpoint-level evidence that the edge itself is the
bottleneck.

## Decision 2 — The ratchet (the promotion ADR-0006 pre-authorized)

One plain test in the standard suite — `test_size_ratchet.py`, run by
`scripts/switchboard_ci.sh` and the sandbox full-suite like every other test — asserting
line-count ceilings on the four shell monoliths and a file-count ceiling on the repo root.
Ceilings are the high-water marks measured when the test merges; reference values today:

```
store.py        15,282      app.py           3,204
mcp_server.py    3,182      static/app.js    5,945
repo root       159 .py files
```

Rules:

1. **Red gate → relief in this order:** (a) delete dead weight in the same file; (b) take the
   `runner` extraction (verbatim-move rules from ADR-0005; 445 lines of headroom, 2 external
   call sites); (c) raise the constant **in the same PR** with a one-line justification —
   visible in diff review, answerable at the SESSION-12 chokepoint.
2. **The ratchet turns one way.** A PR that shrinks a file below its ceiling lowers the
   constant to the new size.
3. **Subtraction accounting:** one mechanism in (this test), one out (the policy-only
   moratorium it replaces). Net new mechanisms: zero — and it is the escalation ADR-0006
   explicitly reserved, not a new instinct.

## Decision 3 — Growth is redirected, not relocated

For **new** code only; existing code moves on-touch only, as verbatim moves:

| New thing | Lands in | Proven by |
|---|---|---|
| Domain state (tables + their CRUD) | `<domain>_store.py` over `db/`, re-exported through the `store.py` facade | the 7 leaf stores (ARCH-2/3) |
| Web routes | an APIRouter module included by `app.py` | `services/auth` |
| MCP tools | a tool module registering on the shared server instance | first such extraction establishes it |
| Frontend feature | its own `static/` JS file | `taikun-ui.js` |
| Tests | `tests/` (with a working path shim) | — |
| Root-level `.py` | nowhere — the root is frozen at its high-water mark | the ratchet |

Cross-cutting infrastructure (lock-retry shims, cache plumbing — the BUG-31/32 class of work)
is the recognized exception that may edit the monoliths in place, under the ratchet, using
the relief order. **No migration program. No module map. No target tree.** The monoliths
shrink by attrition or not at all; either is acceptable. ADR-0005's mistake was not the seams
— it was scheduling other people's futures on the fleet's hottest file.

## Decision 4 — The census (the second kill list, on ADR-0006's terms)

At the H2 post-Helm-sprint review — the judge ADR-0006 already appointed:

- `mcp_observability` gains per-tool **call counters** (it already samples latency and
  errors; counting is the existing mechanism finishing its sentence, not a new one). Web
  routes are already covered by the auth-middleware request log.
- Any of the 158 tools / 191 routes with **zero production calls in the sprint window and no
  named defender is deleted.** Seeded, per ADR-0006's own parked/rejected verdicts:
  `replay_verify`, `simulate_dispatch` (and their two `/ixp/v1` routes),
  `get_coordination_receipt`, `list_coordination_receipts`, `project_task_receipts`,
  `evaluate_dbos_runtime` (and its route).
- The same sweep deletes `PM_*` flags no code reads (112 distinct flags today).
- New tools and routes name their consumer in the PR description.

## Decision 5 — Frontend: parity, hygiene, and a deliberately boring stack

- The hot mission pollers (`mission_status` + `dependency_graph`, 5s × 2, uncached) get
  **exactly the board's HARDEN-36 treatment**: short-TTL server cache + ETag/304. Reuse, not
  invention.
- Every poller respects `document.hidden` (the 30s ack-bell poll currently doesn't).
- **SSE/WebSockets stay rejected** until post-parity measurement shows polling is still a
  material cost. A second delivery mechanism needs a corpse (subtraction rule); today it
  would run *alongside* polling, not instead of it.
- The stack stays boring on purpose: vanilla JS, no build step, no framework, no minifier —
  Caddy already compresses, and the supply chain stays empty. `app.js` sits under the
  ratchet; features land in their own files.
- Dead HTML is deleted: `index-legacy.html`, `rebrand.html`, `ocr.html`.

## Decision 6 — What this ADR does not reopen

- **SQLite vs Postgres:** ARCH-19 owns it; its triggers (zero client-visible
  `database is locked`, write p99 < 100 ms under 8 agents, or multi-tenant deployment) stand.
- **One Uvicorn worker:** stays. A second worker splits the in-process TTL cache and doubles
  concurrent SQLite writers — it makes the measured problem worse. Changing this takes the
  same evidence bar as ARCH-19.
- **Perf SLOs, slim reads, lock-retry:** deliverable `mcp-agent-path-performance` owns them.
- **The provenance model, the subtraction rule, fleet-default-Helm:** ADR-0006, unchanged.

---

## Execution — four cuts, then it's culture

| Task | Cut | What it subtracts |
|---|---|---|
| **CONSOL-6** | `test_size_ratchet.py` with ceilings measured at merge | the policy-only moratorium |
| **CONSOL-7** | delete `gmail_source.py`, `index-legacy.html`, `rebrand.html`, `ocr.html`; add the superseded banners ADR-0006 promised to `SWITCHBOARD-STORE-DECOMPOSITION.md` / `SWITCHBOARD-STORE-ENDSTATE.md`; move `Maxwell-Pitch-Deck.pptx` out of the code root | ~1,200 lines of dead surface + two docs that contradict the plan of record |
| **CONSOL-8** | mission pollers → TTL+ETag parity with the board; ack poll gains a visibility guard | two uncached hot paths |
| **CONSOL-9** | H2 census: call counts → delete zero-callers (seeded with the six RECON-8/9/10 surfaces) + unread `PM_*` flags | the parked mechanisms' surfaces |

Nothing else is authorized by this ADR. A fifth cut needs its own justification against the
subtraction rule.

## Consequences

- The ratchet will occasionally be the most annoying test in the suite. **That is it
  working.** The alternative was measured: +900 lines in four days against a written freeze.
- Legitimate infrastructure work will sometimes pay a deletion/extraction tax before it can
  land. The relief order and the justified-raise escape keep that tax visible and cheap
  instead of invisible and compounding.
- Tests become location-split (new in `tests/`, old at root until touched) until attrition
  finishes. Accepted: moving 106 files in one PR is a merge-conflict bomb across every open
  branch, for aesthetics.
- Deleted tools and flags resurrect from git history if a defender appears late. Parked ≠
  deleted was ADR-0006's rule; the census is where parked items finally find a defender or
  die.
- No fallback UI once `index-legacy.html` is gone.
- This document deliberately decides **nothing** about decomposition targets. If the fleet
  touches a domain and extracts it verbatim, good; if not, the ceiling still holds. Either
  way the trend line breaks.

## Alternatives rejected

- **Swap Caddy for Nginx.** No measured edge bottleneck; `server-timing` places latency
  in-process. Operational churn for zero perceptible win.
- **Restart the decomposition (ARCH-6…17), or a routers/-migration program, or a frontend
  framework rewrite.** ADR-0006 retired the program for reasons regrowth does not refute —
  the regrowth refutes only the *policy-only enforcement*. Bulk moves on the fleet's hottest
  files remain maximum conflict surface for a win invisible until fully done.
- **Multiple Uvicorn workers now.** Splits the read cache, multiplies writers against SQLite;
  see Decision 6.
- **SSE/WebSockets now.** A second delivery mechanism bolted alongside polling, before the
  cheap parity fix has been measured. Deferred, not banned.
- **import-linter / AST architecture cops.** A framework to do what a 30-line test does.
  Building a mechanism-prevention mechanism is still the joke writing itself.
- **A central flag-registry document.** A new artifact that goes stale by construction; the
  census deletes instead of cataloguing.
- **Move all 106 root tests in one PR.** See Consequences; freeze-plus-attrition wins.
- **Do nothing / "the numbers aren't that bad."** Individually, each surface is defensible.
  Together they are the exact accretion pattern ADR-0005 documented at 15.8k lines — being
  rebuilt at +225 lines/day while a freeze was nominally in force. The cheapest time to hold
  a line is while it still exists.
