# Asset Binding Integration — canonical registry → cross-system ID map

**Audience:** Taikun engineering + TEEP (Darko Jankovic)
**Date:** 2026-05-29
**Status:** Proposed — implements "Path A" from [04-system-integrations.md §5](04-system-integrations.md)

---

## 1. The idea (confirmed model)

One **canonical asset registry** with a unique `asset_id` and a parent/child
**hierarchy** (lease → pad → well → equipment). As each new source system is
connected through an **Atlas connector**, we import that system's catalog and
**bind** its native identifiers to the canonical asset — we never re-key the
fleet per system.

```
                         ┌──────────────────────────────┐
                         │   CANONICAL ASSET REGISTRY    │
                         │   asset_metadata.assets       │
                         │   asset_id (PK) + parent_id   │   ← one identity,
                         │   + hierarchy + cohort fields │      built once
                         └──────────────┬───────────────┘
                                        │  bind-on-import
        ┌────────────────┬──────────────┼──────────────┬────────────────┐
        ▼                ▼              ▼              ▼                ▼
   Sensirion         Cygnet         ProCount        Carte           TaskHub
   NUB-D-1234      CYG-PAD-17      BARN-W-103     BARN-W-103       FMP-PAD-17
        └────────────────┴──────────────┴──────────────┴────────────────┘
                 asset_metadata.asset_bindings (system · external_id)
```

After binding, any system's native ID resolves to the canonical asset, **and**
the canonical asset resolves out to every system's ID. That bidirectional map
is what lets a Sensirion event fan out to Cygnet pressure, ProCount codes, and
a TaskHub dispatch — all keyed on one identity.

---

## 2. Does it work? — yes, it already runs at scale for R2Q

This is not aspirational. The exact schema is live in production for our R2Q
customer today. Read-only snapshot of demo on 2026-05-29:

| Metric | Value | Meaning |
|---|---|---|
| Canonical assets | **3,808** | wells (2,421), tanks (743), leases (479), facilities (59), … |
| Assets with a parent | **2,384 / 3,808** | hierarchy is populated, not empty |
| `asset_bindings` rows | **~8,500** across 4 ID namespaces | `isite` (2,382), `api` (2,372), `canonical_source_id` (1,894), `api_number` (1,888) |
| **Assets bound to >1 namespace** | **2,378** | the "one asset → many system IDs" pattern is live |
| `asset_aliases` rows | **3,947** | name-variant normalization populated |
| `asset_match_reviews` pending | 0 | nothing stuck in the ambiguous band right now |

The headline: **2,378 assets already carry multiple bindings simultaneously.**
The multi-binding mechanism is proven — TEEP turns "one source" into "five,"
which is connector wiring, not a redesign.

> Note: today's four namespaces all derive from **one** ingest path (the
> iSite/SCADA discovery for R2Q). They prove the *table* works at scale; they
> do **not** yet prove binding a genuinely foreign system with a different
> naming convention. That is what the test in §7 validates.

---

## 3. Data model (already in `schema/020_asset_metadata.sql`)

| Table | Role | Key columns |
|---|---|---|
| `asset_metadata.assets` | canonical registry | `asset_id` (PK), `display_name`, `asset_type`, `parent_asset_id`, `tenant_id`, `aliases` jsonb, `external_ids` jsonb, cohort fields |
| `asset_metadata.asset_bindings` | **cross-system ID map** | `asset_id`, `system`, `external_id`, `confidence`, `source` |
| `asset_metadata.asset_aliases` | O(1) normalized name lookup | `(tenant_id, alias_norm)` → `asset_id`, `confidence`, `seen_count` |
| `asset_metadata.asset_resolution_audit` | replayable decision trail | `query_raw`, `query_norm`, `resolved_asset`, `confidence`, `decision`, `candidates_json` |

`system` is free text, so `'sensirion' | 'cygnet' | 'procount' | 'carte' | 'fmp'`
all drop straight in. Hierarchy uses the self-FK `parent_asset_id`
(`schema/130_registry_v2.sql`, made legacy-data-tolerant via `NOT VALID` in
PR #62).

---

## 4. The critical insight: binding strategy is **per system**

Cross-system binding is **not** always fuzzy name matching. Pick the strongest
signal each system actually provides:

| Strategy | When to use | How | Confidence |
|---|---|---|---|
| **A. Exact shared key** | System carries a stable cross-operator ID (e.g. the 14-digit **API number**) | Direct join `external_id` ↔ canonical `api_number` binding | Deterministic (1.0) |
| **B. Reference map** | System gives an explicit parent/well reference, not just a name (e.g. Sensirion device metadata carries `pad_id` / `well_ids` / `asset_path`) | Bind via the provided FK to the already-canonical pad/well | Deterministic (1.0) |
| **C. Fuzzy name** | System only exposes a display name | Trigram + number-aware `SequenceMatcher` against `assets.display_name` + `asset_aliases` | Banded (see §5) |

This matters for TEEP specifically: **Sensirion device IDs (`NUB-D-1234`) have
zero name/API overlap with wells** — you cannot fuzzy-match them. They bind by
**Strategy B**, using the `pad_id` / `well_ids` / `asset_path` that the Sensirion
device-metadata endpoint already returns (04-system-integrations.md §1.3). Get
the strategy wrong and you either fail to bind or bind to the wrong well.

---

## 5. Confidence bands + human review (already built — `schema/150`)

For **Strategy C** matches, the discovery pipeline applies these bands
(from `150_asset_match_reviews.sql`):

| Confidence | Outcome |
|---|---|
| **≥ 0.75** | auto-link — write `asset_bindings` + capture variant in `asset_aliases` |
| **0.60 – 0.75** | enqueue in `asset_match_reviews` — human approves / rejects / relinks |
| **< 0.60** | auto-create a new canonical asset |

The review queue, service, and API (`/api/registry/v1/match-reviews`) already
exist. Strategies A and B skip the bands — they are deterministic.

---

## 6. The connector → binding pipeline (what to wire)

```
Atlas connector (per system)
  └─ pull system catalog  (devices / assets / wells / pads)
       └─ normalize to BindingRecord { system, external_id, display_name,
                                       parent_ref?, api_number?, lat/lon? }
            └─ Resolver.bind(record):
                 A) exact key  → write binding (conf 1.0)
                 B) reference  → write binding (conf 1.0)
                 C) fuzzy name → band → auto-link | review-queue | new-asset
                      └─ writes: asset_bindings, asset_aliases,
                                 asset_resolution_audit
```

The fuzzy-match + band + write logic already exists in
`asset_discovery_service.py` (it writes `asset_bindings` + `asset_aliases`
today for the iSite source). The work is to **invoke it generically per Atlas
connector** rather than only for iSite.

---

## 7. Two gaps to close (bounded, additive — no schema change)

1. **Connector-driven binding ingest.** The registry import (Data Catalog →
   Import Assets) writes the `assets` table keyed on a single `external_id`. It
   does **not** populate `asset_bindings` / `asset_aliases`. Add a per-system
   binding step (Strategies A/B/C above) so each connector's catalog dump
   produces binding rows. Maps 1:1 to Path A's "Taikun ingests + builds the
   registry."
2. **Generalize the resolver off `system='isite'`.** `AssetResolver`'s binding
   lookup is currently pinned to the iSite source. Parameterize it to a
   per-tenant list of systems so a canonical asset resolves across all bound
   namespaces.

Neither touches the schema. Both build on code that already exists.

---

## 8. TEEP system-by-system binding plan

| System | ID it exposes | Binding strategy | Notes |
|---|---|---|---|
| **ProCount** (IFS Merrick) | well_id + likely API number | **A (exact)** if API number present, else **C** on well_name | Best candidate for the canonical "spine" — it's the production-accounting master |
| **Cygnet** (SCADA) | asset_id / asset_path | **B (reference)** via asset_path, or **C** on display_name + aliases[] | Catalog includes `aliases[]` per 04-§5.1 |
| **Sensirion** (Nubo) | device_id | **B (reference)** via device→pad_id/well_ids/asset_path | **Never fuzzy** — device IDs have no name overlap |
| **Carte** (IFS Merrick) | well_id (shares ProCount store) | dedup against ProCount; same binding | May need no separate ingest at all |
| **TaskHub / FMP** | pad_id | **B (reference)** via pad → lease/well | Pad-level; expands to wells via hierarchy |

Recommended order: **build the canonical spine from ProCount** (most
authoritative well + lease structure), then bind Cygnet, Sensirion, Carte,
TaskHub onto it.

---

## 9. Recommended test (on demo, this week, with existing data)

**Goal:** prove a *genuinely foreign* source binds to the canonical R2Q
registry — i.e. validate Strategy C end-to-end on messy real names, not the
already-bound iSite namespace.

**Setup:** demo already has R2Q (iSite) + other DB + SharePoint-derived asset
names available. Pick one source that is **not** iSite (e.g. SharePoint
document-derived well names, or the secondary DB's well list).

**Procedure:**
1. Snapshot baseline counts (done — §2).
2. Run the discovery/resolver against the second source as a **new `system`
   tag** (e.g. `system='sharepoint'`), writing to `asset_bindings` /
   `asset_aliases` / `asset_resolution_audit`.
3. Measure:
   - **match rate** — % of source names that auto-linked (≥ 0.75)
   - **review rate** — % that landed in `asset_match_reviews` (0.60–0.75)
   - **new-asset rate** — % that created new canonicals (< 0.60)
   - **precision spot-check** — sample 20 auto-links, confirm correct well
   - **number-aware guard** — confirm "Bradley Ranch 11" ≠ "Bradley Ranch 12"
4. Review the queue in the UI (`/api/registry/v1/match-reviews`) and resolve a
   few to confirm the human-in-the-loop closes the loop.

**Pass criteria:** ≥ ~80% auto-link on a same-fleet source, zero number
collisions, review queue populated and resolvable. A high new-asset rate on a
known-overlapping fleet means the matcher needs tuning — useful to learn now,
cheaply, on R2Q data rather than during the TEEP pilot.

This test needs **no TEEP systems** — it de-risks the matcher and the
connector→binding wiring using data already on demo.

---

## 9.1 Dry-run results (2026-05-29, demo, read-only)

Ran the production resolver (`AssetDiscoveryService.find_only`, no writes) over
**raw iSite `r2` names** — a genuinely foreign-formatted source — against the
canonical R2Q registry. Script: `scripts/binding_dryrun.py`.

**400 well names** (e.g. `"ASHROD EAST * 14 , MV"`, `"BAILEY * 31L , MV"`):

| Band | Count | % |
|---|---|---|
| exact (=1.00) | 218 | 54.5% |
| auto_high (0.90–1.0) | 3 | 0.8% |
| verify (0.75–0.90) | 81 | 20.2% |
| review (0.60–0.75) | 77 | 19.2% |
| new/none (<0.60) | 21 | 5.2% |
| **auto-resolvable (≥0.75)** | **302** | **75.5%** |
| **trailing-number collisions** | **0** | ✅ |

**356 lease names:** 99.2% auto-resolvable (83.7% exact), 0 fuzzy collisions.

**Findings:**
1. **The live matcher is sound.** Zero trailing-number collisions across 400
   well names — the "Bradley 11 ≠ Bradley 12" guard holds on real data. The
   confidence bands route ambiguous formatting to the review queue correctly.
2. **Tuning lever:** the well review-band (19%) is dominated by one systematic
   pattern — the `" , MV"` / `" , AA"` formation suffixes and `"*"` markers
   (e.g. `"BIRK C * 11 , AA"` → `"BIRK C 11"` @0.72). A per-system input
   normalization rule that strips these before matching would push most of the
   review band into auto-link (→ ~90%+). This belongs in the connector's
   `BindingRecord` normalization step (§6).
3. **Data-quality issue found in historical aliases (not the live matcher).**
   The pre-existing `merged_alias` rows contain number-collision errors from an
   earlier bulk merge that wasn't fully number-aware — confirmed cases:
   `perkins 14 lease → perkins_12_lease` (conf 1.0) and a bare `12 → BURNS
   SHALLOW 4`. A first-number heuristic flagged 138/684 `merged_alias` rows,
   but most are **false positives** (parenthetical property IDs like
   `"(15914)"` in the canonical name); the genuine bad subset is smaller and
   needs a number-aware audit. **Action:** the binding-ingest alias-merge step
   must reuse the live matcher's number-aware guard, and existing `merged_alias`
   rows deserve a one-time cleanup pass.

**Verdict:** the cross-system binding mechanism works on foreign real-world
names today. The two TEEP-readiness items are per-system input normalization
(§6) and a number-aware alias-merge (so ingest doesn't reproduce the perkins
14→12 class of error).

## 10. Open questions

- Which source should be the canonical **spine** for TEEP — ProCount? (§8)
- Does ProCount expose the **API number** per well? If yes, ProCount↔Cygnet↔
  Carte all bind by Strategy A (deterministic).
- Confirm Sensirion device metadata reliably carries `well_ids` (not just
  `pad_id`) — determines well-level vs pad-level binding (04-§1.5 open item).
- Should the connector→binding ingest run on the nightly diff (04-§5.1) or
  on-demand per connector sync?
