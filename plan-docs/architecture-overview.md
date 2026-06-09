# TEEP Barnett · Architecture Overview (one-pager)

The same architecture as [03-architecture.md §1.1](03-architecture.md) — redrawn so the 5-phase lifecycle reads in one glance. Trust boundaries, system colours, and read/write semantics are unchanged. The supporting tables below restate the chart in plain text for anyone who prefers prose.

---

## 1 · The architecture in one chart

```mermaid
flowchart TB
  classDef src   fill:#fef3c7,stroke:#92400e,stroke-width:2px,color:#1f2937
  classDef read  fill:#f1f5f9,stroke:#475569,stroke-dasharray:5 3,color:#1f2937
  classDef write fill:#dbeafe,stroke:#1e40af,stroke-width:2.5px,color:#1f2937
  classDef agent fill:#ffffff,stroke:#1e3a5f,stroke-width:3px,color:#1e3a5f
  classDef data  fill:#1e3a5f,stroke:#1e3a5f,color:#ffffff
  classDef ppl   fill:#ede9fe,stroke:#6b21a8,color:#1f2937

  subgraph TEEP[" 🏭  TEEP · TotalEnergies Barnett — OAuth2 gateway "]
    direction LR
    Nubo["Nubo Sensirion<br/>event source"]:::src
    Cygnet["Cygnet · SCADA"]:::read
    PC["ProCount"]:::read
    Carte["Carte"]:::read
    TH["TaskHub / FMP<br/><b>only write target</b>"]:::write
  end

  M{{"Maxwell Agent<br/>Taikun AWS · us-east-1"}}:::agent

  TAI[("emissions.alerts + dashboard<br/>22 Sierra columns")]:::data

  Consumers["Sierra · monthly Excel<br/>MRO + Clovis · live UI"]:::ppl

  %% Lifecycle arrows — numbered ①–⑤
  Nubo ==>|"①  kg/h webhook IN"| M
  M    -.->|"②  parallel GETs"| Cygnet
  M    -.-> PC
  M    -.-> Carte
  M    -.-> TH
  M    ==>|"③  POST dispatch task"| TH
  TH   ==>|"④  task.updated webhook IN"| M
  M    ==>|"⑤  PATCH task=closed"| TH

  M    -- "UPDATE every step" --> TAI
  TAI  --> Consumers
```

**Legend.**
- 🟨 yellow = event source (Nubo)
- ⬜ dashed grey = read-only API (Cygnet · ProCount · Carte)
- 🟦 blue solid = the one system we write to (TaskHub / FMP)
- ⚪ white-with-navy-outline = Maxwell agent
- ⬛ navy = Taikun-side store + dashboard
- 🟪 purple = downstream consumers

Thick arrows (`==>`) are **writes** or event-source webhooks. Thin dashed arrows (`-.->`) are **reads**. Numbered ①–⑤ trace one complete triage lifecycle.

---

## 2 · The 5 phases, in plain text

| # | Phase | Direction | What happens |
|---|---|---|---|
| ① | **Detect**     | Sensirion → Maxwell                | Sensirion `kg/h ≥ threshold` webhook arrives. Maxwell inserts a new row in `emissions.alerts` (status = Open). |
| ② | **Enrich**     | Maxwell → 4 systems (parallel)     | Maxwell fans out 4 GETs in parallel: Cygnet (pressures, sales, comp metrics), ProCount (down/up codes, operator comments), Carte (injection rate), TaskHub (LO notes around event). |
| ③ | **Dispatch**   | Maxwell → TaskHub                  | For *Unexpected* events, Maxwell POSTs a dispatch task to TaskHub with the full evidence pack. (Auto-close path skips this — UPDATE the alert and end.) |
| ④ | **Field work** | LO → TaskHub → Maxwell             | Lease operator works the task and updates TaskHub. TaskHub fires `task.updated` webhook back to Maxwell. |
| ⑤ | **Close loop** | Maxwell → TaskHub + alert store    | Maxwell PATCHes the TaskHub task closed and finalises the `emissions.alerts` row (`status = Closed`, full Sierra columns populated). |

After ⑤, the populated alert is what Sierra's monthly Excel and the MRO / Clovis dashboards read from. They never touch the live loop.

---

## 3 · Trust boundary — who calls what

| System              | Owner   | Maxwell access            | Purpose                                              |
|---------------------|---------|---------------------------|------------------------------------------------------|
| Nubo Sensirion      | TEEP    | read · webhook in         | event source                                         |
| Cygnet · SCADA      | TEEP    | read-only (GET)           | tubing / line / casing pressure · sales · comp metrics |
| ProCount            | TEEP    | read-only (GET)           | down / up codes · operator comments · work orders    |
| Carte               | TEEP    | read-only (GET)           | injection-rate drop                                  |
| TaskHub / FMP       | TEEP    | read + write (GET · POST · PATCH) | dispatch & close-the-loop                    |
| `emissions.alerts`  | Taikun  | full write                | event lifecycle store (Sierra's 22 columns)          |
| Dashboard / Excel   | Taikun  | read                      | MRO + Clovis (live UI) · Sierra (monthly export)     |

---

## 4 · Why this is safe

- **One write target.** Maxwell's only TEEP-side writes are POST + PATCH against TaskHub. Sensirion, Cygnet, ProCount, and Carte are GET-only.
- **OAuth2 gateway.** All TEEP API calls go through TEEP's OAuth2 client-credentials gateway with rotating tokens — no static keys, no IP allow-listing required.
- **No persisted data outside Taikun.** Raw event cache lives ≤ 7 days; aggregated `emissions.alerts` records are governed by the mutual retention policy.
- **Replayable.** Every Maxwell call writes an audit trace (`/v1/agent/traces/{event_id}`) — every GET, every LLM decision, every POST/PATCH is reconstructable for any past event.

---

*For the original LR flowchart and the sequence-view alternative, see [03-architecture.md §1.1 and §1.2](03-architecture.md). For the full decision tree (rules + actions), see §2.3. For per-system endpoint contracts, see [04-system-integrations.md](04-system-integrations.md).*
