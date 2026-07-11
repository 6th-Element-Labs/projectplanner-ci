# ADR-0007 — The application shell: hold the line (keep Caddy, ratchet the monoliths, redirect new growth, census the surfaces)

- **Status:** Proposed — accepted when the operator merges this PR. Board task **ARCH-20**.
- **Date:** 2026-07-11
- **Author:** application-shell audit session (Claude Code / Fable 5) — four parallel audit
  threads (app/MCP surface, store.py regrowth, frontend/polling, repo hygiene) plus a
  verification pass; all numbers measured on master @ `d83d59e`. **Amended 2026-07-11** after
  a second, independent deep review (run against the stale `4a97e71` branch) converged on the
  same downward-ratchet-plus-strangler direction and surfaced findings folded in below —
  its exact figures were already stale (it read a branch, not master), so the shape was kept
  and the digits re-measured, CONSOL-5 style.
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

**Keeping Caddy means finishing it, not just leaving it.** The current `Caddyfile` does TLS,
compression, caching, and proxying but ships **no security headers and no access log** — so
"keep Caddy" carries a small hardening rider (CONSOL-8): add HSTS, `X-Content-Type-Options:
nosniff`, a `frame-ancestors`/`X-Frame-Options` and `Referrer-Policy`, a tested CSP, and
structured access logging, all in the file we already own. This is the cheapest possible win
— edge hardening with zero application change — and it is the counterexample to "keeping a
component means ignoring it."

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

**Precondition — the gate must actually run (this is load-bearing, not hygiene).** The CI gate
is a *hand-maintained list* (`run_test <file>` lines in `switchboard_ci.sh`); it currently
enumerates **88 tests while the tree tracks 111 — ~23 are silently omitted** (the second
review flagged this against its branch; re-measured on master it is worse than reported).
Adding a size ratchet to a list that already drops a fifth of the suite means the ratchet can
drift out the same way. So CONSOL-6 first converts the gate to **pytest discovery with an
explicit, commented denylist** — every test runs unless a line says why it doesn't — and only
then is the ratchet meaningful. A gate that doesn't run every test can't enforce anything,
least of all itself.

Rules:

1. **Red gate → relief in this order:** (a) delete dead weight in the same file; (b) take the
   `runner` extraction (verbatim-move rules from ADR-0005; 445 lines of headroom, 2 external
   call sites — this is the live `runner_*` control cluster *inside* store.py, **not**
   `runner/service.py`, which Decision 7 deletes); (c) raise the constant **in the same PR**
   with a one-line justification — visible in diff review, answerable at the SESSION-12
   chokepoint.
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
| Web routes | an APIRouter module included by `app.py`, taking a **typed Pydantic DTO** (not `body: dict`) | `services/auth` |
| MCP tools | a tool module registering on the shared server instance | first such extraction establishes it |
| Frontend feature | its own `static/` JS file | `taikun-ui.js` |
| Tests | `tests/` (with a working path shim) | — |
| Root-level `.py` | nowhere — the root is frozen at its high-water mark | the ratchet |

Two hygiene rules ride with the redirect, both cheap and both enabling the static analysis a
ratchet depends on:

- **No new `import *`.** store.py has 11 star-imports today; they are why a stock Ruff run
  reports ~687 undefined-name findings and why "what does this file actually export" is
  unanswerable by tooling. New modules use explicit imports; the facade's existing
  re-exports are grandfathered, not extended.
- **New routes are typed.** 96 of app.py's routes take `body: dict` and hand-roll validation
  into 138 `HTTPException` sites. New routes take a Pydantic DTO so the contract lives in one
  place and OpenAPI/MCP parity is mechanical — this is the same move that lets REST and MCP
  become thin adapters over one typed service (Decision 4's duplication fix, Decision 7's
  `application/` layer).

Cross-cutting infrastructure (lock-retry shims, cache plumbing — the BUG-31/32 class of work)
is the recognized exception that may edit the monoliths in place, under the ratchet, using
the relief order. **No migration *program* and no *scheduled* module map** — Decision 7 names
a target *shape* for extractions to aim at, but nothing is scheduled and no bulk move is
authorized; the monoliths shrink by attrition or not at all, and either is acceptable.
ADR-0005's mistake was not the seams and not the destination — it was *scheduling* other
people's futures on the fleet's hottest file. A map you steer toward when you happen to be in
a domain is not a march everyone must complete.

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

## Decision 7 — Direction of travel: a target shape to aim at, not a march to complete

> **Updated 2026-07-11 (operator decision, board decision #7):** the compass was upgraded to a
> **committed program.** The operator reviewed the maintainability gap — the ratchet *holds*
> store.py at ~15k but never *shrinks* it, so compass-only meant "maintainable" never arrives —
> and chose to scope the staged rearchitecture as the tracked deliverable
> **`modular-monolith-rearchitecture`** (5 vertical-slice milestones, Slice 0 = the auth cutover,
> seed task ACCESS-16). This does **not** reinstate the ADR-0005 failure: what killed ADR-0005
> was the *schedule* (a 17-step horizontal reorder on the hot file), not the destination. The
> program's guardrails keep it safe — **vertical slices behind a green facade; each slice
> subtracts what it replaces; domain extraction (Slice 3) is on-touch, highest-change-first,
> with no global reorder; and the Decision 2 ratchet is the progress meter.** The target shape
> below is now that program's destination, not merely a compass. The "Restart the decomposition"
> rejection in *Alternatives* stands only for a *scheduled horizontal reorder*; a
> vertical-slice, subtract-as-you-go program is the sanctioned form.

ADR-0006 refused a target tree because ADR-0005 turned one into a 17-step *schedule* that the
dependency graph invalidated twice. The failure was the schedule, not the map. A second
independent review re-derived essentially the same modular-monolith target as the leaf-store
pattern already implies — so the destination is not controversial; only *mandating a route to
it* is. This ADR therefore records the shape as **the place an extraction lands when the fleet
is already in that domain**, and nothing more. The ratchet (Decision 2) is the only thing that
is enforced; this section is never a red gate and never a backlog.

```
src/switchboard/
  main.py                 app factory + lifespan only
  settings.py             typed settings
  api/routers/            auth · projects · tasks · deliverables · coordination · tally · webhooks
  mcp/tools/              board · tasks · coordination · deliverables   (thin adapters)
  application/            commands/ · queries/ · contracts/   (one typed service per op; REST + MCP call it)
  domain/                 access · board · coordination · deliverables · provenance
  storage/                connection · migrations/ · repositories/   (SQL lives only here)
  integrations/           github · llm · gmail · notifications
  jobs/
static/js/                api · state · board · mission · coordination   (ES modules)
tests/                    unit/ · integration/ · browser/
```

Three load-bearing invariants this shape encodes — these are the *why*, and they hold whether
or not the tree is ever fully realized:

1. **REST and MCP are adapters over one `application/` service.** Every duplicate pair
   Decision 4 named (create_task, update_task, claim, record_outcome, send_message) collapses
   to one typed command/query with two thin callers. This is the subtraction that pays for the
   layer.
2. **SQL lives only in `storage/repositories/`.** The 88 `subprocess`/`urllib` calls and raw
   SQL tangled into store.py are what make it un-unit-testable; the repository seam is where a
   domain becomes testable in isolation.
3. **Migrations are numbered and run once** (see the related-work note on the
   `except Exception: pass` startup loop) — the `storage/migrations/` directory is where that
   safety fix lands when it is done.

**How this composes with Decision 3:** when a lane genuinely needs to touch, say, the
deliverables domain, the verbatim extraction it does *anyway* aims at `domain/deliverables/`
+ `storage/repositories/` + an `application/` service, instead of a fresh ad-hoc seam. Same
work, a shared destination. No lane is ever assigned "go build `src/switchboard/`."

## Related work this ADR does *not* address (shape ≠ security ≠ reproducibility)

The deep review surfaced findings that are real and, in one case, **live** — but they are not
code-*shape* decisions and must not be laundered through a cleanup ADR, where they would be
invisible. Each is its own track; this list exists so the reader knows the ratchet does not
fix them:

- **P0 — MCP reads are unauthenticated (LIVE).** Verified 2026-07-11: an anonymous
  `tools/call` for `list_projects` against the public `/mcp` endpoint returns the full
  project/board list with no bearer token; only writes call `_require_write`
  (mcp_server.py, docs/MCP.md:50, Caddyfile exposes `/mcp`). Unless every board is
  intentionally public this is a data exposure. **Filed separately and urgently** — it is not
  gated on this ADR.
- **Schema migrations swallow every error** (`except Exception: pass`, db/schema.py:748, run
  at import by both web and MCP): a disk/permission/corruption/lock failure is indistinguishable
  from "column exists." Numbered, transactional, run-once migrations; catch only duplicate-column.
- **Non-reproducible builds:** lower-bound-only `requirements.txt`; adopt `pyproject.toml`,
  pin Python 3.12, commit a lockfile.
- **Runtime writability / service identity:** services run as a general account with the code
  tree writable; dedicated service users, read-only code, declarative systemd hardening.
- **`/health/deep` is public and leaks project identifiers, and startup can skip a failed
  project yet report healthy** (app.py:648, :72): a real readiness endpoint that checks
  schema/DB without exposing project data.
- **Executable frontend tests:** several "UI proof" tests assert a function *name appears in
  source text* (e.g. test_mission_page.py:215) rather than exercising behavior; Vitest for
  helpers + Playwright for the core flows. (The ES-module split that enables this is the
  Decision 7 frontend shape; the tests themselves are a quality track.)
- **Finish the two-auth cutover:** `PM_GLOBAL_AUTH` still gates a second live auth system
  beside the legacy one — a migration state, not an architecture (already tracked as the
  auth-strangler).

---

## Execution — four cuts, then it's culture

| Task | Cut | What it subtracts |
|---|---|---|
| **CONSOL-6** | `test_size_ratchet.py` with ceilings measured at merge | the policy-only moratorium |
| **CONSOL-7** | delete `gmail_source.py`, the retired `runner/` push-bridge (`service.py` + `run_task.sh` + README — dispatch.py's own header says the wake substrate replaced it; **not** store.py's live `runner_*` cluster), `index-legacy.html`, `rebrand.html`, `ocr.html`; add the superseded banners ADR-0006 promised to `SWITCHBOARD-STORE-DECOMPOSITION.md` / `SWITCHBOARD-STORE-ENDSTATE.md`; move `Maxwell-Pitch-Deck.pptx` out of the code root | ~1,700 lines of dead surface + two docs that contradict the plan of record |
| **CONSOL-8** | Caddy security headers + access log; mission pollers → TTL+ETag parity with the board; ack poll gains a visibility guard | two uncached hot paths + an unhardened edge |
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
- Decision 7 names a decomposition *destination* but schedules nothing. The risk it carries:
  a named target tempts someone to treat it as a backlog. The mitigation is written into the
  decision (never a red gate, never assigned) and into the culture ADR-0006 set — but if the
  fleet starts building `src/switchboard/` speculatively instead of on-touch, that is the
  ADR-0005 failure returning, and the subtraction rule should kill it. **The map is a compass,
  not a schedule; the moment it becomes a schedule it is wrong.**
- The related-work list is deliberately *outside* this ADR's authority. Its items — led by the
  live MCP-read exposure — are filed as their own tasks so a docs-cleanup merge never gets
  mistaken for a security or reproducibility fix.

## Alternatives rejected

- **Swap Caddy for Nginx.** No measured edge bottleneck; `server-timing` places latency
  in-process. Operational churn for zero perceptible win.
- **Restart the decomposition (ARCH-6…17), or a routers/-migration program, or a frontend
  framework rewrite.** ADR-0006 retired the program for reasons regrowth does not refute —
  the regrowth refutes only the *policy-only enforcement*. Bulk moves on the fleet's hottest
  files remain maximum conflict surface for a win invisible until fully done. Decision 7
  keeps the map and drops the march: same rejection of a *scheduled* program, while giving
  on-touch extractions a shared destination instead of ad-hoc seams. **(Superseded 2026-07-11
  — see the Decision 7 update: the operator committed to a *vertical-slice* program, deliverable
  `modular-monolith-rearchitecture`. What stays rejected is the *horizontal, big-bang,
  globally-scheduled* reorder — not a subtract-as-you-go slice program.)**
- **Fold the security/ops/reproducibility findings into this ADR.** Rejected on purpose. A
  cleanup ADR that also "fixes" a live auth hole buries the urgent thing behind the tidy
  thing; the reader skims a docs-shape decision and misses that `/mcp` is open. Separate
  tracks keep each finding at its true severity — the MCP-read exposure especially, which is
  filed and worked independently of whether this PR ever merges.
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
