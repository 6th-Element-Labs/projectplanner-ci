# Switchboard — software end-state after `store.py` decomposition

> **⚠️ SUPERSEDED (ADR-0006, 2026-07-08).** This end-state describes the completed ARCH-5…17
> `store.py` decomposition, a program [ADR-0006](decisions/0006-control-plane-done-enough.md)
> retired after the foundation cuts (ARCH-1…5). The enforced discipline is now the
> `test_size_ratchet.py` line-count ceiling ([ADR-0007](decisions/0007-application-shell-cleanup.md),
> Decision 2), and Decision 7 there records the modular-monolith shape as a compass to aim at
> on-touch — never a scheduled march. Kept for historical reference only.

- **Status:** Target end-state
- **Date:** 2026-07-07
- **Companion to:** [ADR-0005](decisions/0005-store-module-decomposition.md) ·
  [`SWITCHBOARD-TARGET-ARCHITECTURE.md`](SWITCHBOARD-TARGET-ARCHITECTURE.md) ·
  [`SWITCHBOARD-STORE-DECOMPOSITION.md`](SWITCHBOARD-STORE-DECOMPOSITION.md)
- **Tracks as:** board workstream `ARCH` — done when **ARCH-16** (cutover) and **ARCH-17**
  (guard) are `Done`.

This is the definition of "finished": what the tree looks like, what `store.py` becomes, the
invariants that must hold at *every* step in between, and how we prove we got there.

---

## Before → after

| | Before (today) | After (ARCH-17 done) |
|---|---|---|
| Biggest module | `store.py` — **15,817 lines** | largest domain module **≤ ~900 lines** |
| `store.py` | 513 fns, 0 classes, 32% of all Python | thin curated façade **(≤ ~200 lines)** or deleted |
| Layers | none — schema + DAL + logic fused | 6 layers, one import direction, CI-enforced |
| Merge conflicts | every lane collides on one file | conflicts scoped to the touched domain module |
| Unit-testing `merge_gate` | impossible without whole schema | import `merge_gate` alone |
| Agent context to grok a change | must page through 16k lines | read one ~500-line module |
| Per-project DB isolation | one SQLite file per project | **unchanged** |
| Runtime behavior | — | **byte-for-byte identical** |

## What `store.py` becomes

One of two outcomes, decided during ARCH-16 by counting callers that genuinely want an
aggregate import:

- **(A) Thin curated façade — preferred if many callers import broadly.** `store.py` shrinks
  to an explicit, documented public-API surface: a hand-written list of `from tasks_store
  import create_task, get_task, …` re-exports representing the *intended* public API — not a
  `import *` dumping ground. New code is expected to import the domain module directly; the
  façade exists for stable ergonomics, and the CI guard forbids it from growing logic.
- **(B) Deleted — preferred if callers are already domain-specific.** All ~84 importers point
  at real modules; `store.py` is removed. Cleanest end-state; reached iff the cutover shows the
  façade has no real consumers left.

Either way, **no business logic and no SQL lives in `store.py` at the end.**

## Invariants (hold at every commit, not just the end)

These are what make the strangler safe — each ARCH PR must satisfy all of them:

1. **Behavior is invariant.** Function bodies move verbatim. No behavior change rides along in
   a move PR. The proof is the existing suite green *unchanged* (`test_merge_gate`,
   `test_deliverables_model`, `test_access_auth_sessions`, `test_work_session_model`, …).
2. **Schema is invariant.** The DDL move (ARCH-4) is verified by schema-hash / `sqlite_master`
   equality: a DB built from `db/schema.py` is identical to one built from `master`.
3. **The suite is green after every PR.** Never a "half-migrated, tests later" commit. The
   façade re-exports keep every importer working mid-flight.
4. **Import direction only ever points down.** Enforced by review until ARCH-17, by CI after.
5. **Per-project isolation is untouched.** No PR merges project DBs or adds a cross-project
   table. The `_resolve`-per-file boundary is load-bearing and stays.
6. **One PR = one domain (or one leaf batch).** Small, reviewable, revertible. The whole point
   is to *stop* shipping 16k-line diffs.

## The durable guard (ARCH-17 — why this isn't a one-time cleanup)

A decomposition with no ratchet silently rebuilds the monolith. The end-state includes a CI
check wired into `scripts/switchboard_ci.sh` + the PR gate that fails when:

- **any module exceeds the size ceiling** — a ratchet: it starts at the largest post-split
  module and only ever tightens (a PR may lower it, never raise it); and
- **any import points upward** — expressed as an import-graph contract
  (`db < identity < board < execution < provenance < product`), with test modules exempt from
  direction but not from a generous ceiling.

Landing this is what converts "we cleaned it up once" into "it stays clean."

## Definition of done (the whole ARCH workstream)

- [ ] ARCH-1 — ADR + target-arch + end-state + per-function map merged (this set).
- [ ] ARCH-2…4 — `constants` / `db/core` / `db/schema` extracted; schema-hash equal.
- [ ] ARCH-5…15 — every domain in its own module behind the façade; suite green each PR.
- [ ] ARCH-16 — all importers migrated; `store.py` is façade-only (≤ ~200 lines) or deleted.
- [ ] ARCH-17 — size + layering guard active; a deliberate violation fixture fails CI; clean
      tree passes.
- [ ] No module > the agreed ceiling; no upward imports; `grep -c 'CREATE TABLE' store.py` → 0;
      `subprocess`/`urllib` no longer imported by the façade.

## Explicit non-goals

- **Not** a behavior change, bug fix, or performance rewrite. Those are separate tasks filed
  after, against the now-testable modules.
- **Not** a database re-architecture. Per-project SQLite files stay; no ORM, no Postgres, no
  per-module DBs.
- **Not** a service split. Modules in one process, not microservices (see ADR-0005).
- **Not** a public-API redesign. The MCP/REST surface (`mcp_server.py`, `app.py`) is unchanged;
  only where those files *import from* changes.
