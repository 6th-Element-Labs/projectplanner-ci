# Switchboard — Codex adapter (ADAPTER-2)

Tier-2 adapter for the **Codex** runtime (ADR-0004). The coordination logic is **already
built and proven** in [`../switchboard_core.py`](../switchboard_core.py) — this directory is
the thin Codex-specific wiring. Authored as a scaffold by `claude-code` per decision #2
(claude owns the adapter core; **Codex fills the two runtime hooks below**).

## What's done vs. what Codex fills
| Piece | State |
|---|---|
| Handshake (working_agreement → register → inbox) | ✅ in `switchboard_core.handshake()` |
| Enforce: FR-14 interrupt-consume, self-Done deny, lease-conflict deny | ✅ in `switchboard_core.evaluate_tool()` (live-verified via the Claude adapter) |
| Wire handshake to Codex session start | 🔲 `TODO(codex)` in `codex_adapter.on_session_start` |
| Wire `evaluate_tool` to Codex's pre-tool hook + map deny | 🔲 `TODO(codex)` in `codex_adapter.on_pre_tool` |

Only the two `TODO(codex)` blocks need Codex-runtime knowledge (its hook lifecycle + how a
pre-tool hook receives the pending call and signals a block) — which is exactly what
PRD §10 lists as "TBD: verify hook surface" for Codex.

## The adapter contract (ADR-0004 — same for every runtime)
1. **Session start:** surface the working agreement as first-turn context + `register_agent`.
2. **Per tool call:** call `evaluate_tool(...)`; on `deny` block the tool and surface the reason
   so the model self-corrects/halts; on `allow` permit (a non-empty reason is a soft reminder).
3. **Advertise fidelity:** `handshake(..., control={...})` tells the board how strongly this
   runtime is governed (discover / pre-tool-deny / runner-kill).

## Fidelity (be honest — PRD §10)
Codex's per-tool-call interrupt fidelity is **TBD** (verify whether Codex exposes a pre-tool
hook that can block). If it does, Codex reaches full Tier-2 (deny + interrupt-consume). If not,
Codex falls back to Tier-1 (advisory: the MCP `instructions` handshake) + runner-kill (NMI).
Set `control` accordingly so the board reflects reality.

## Config
`PM_BASE` (default `https://plan.taikunai.com`), `PM_PROJECT` (`switchboard`), `PM_MCP_TOKEN`,
`PM_AGENT_ID` (use the IXP `<runtime>/<scope>` convention, e.g. `codex/ADAPTER-2`).

> Note (agent_id drift, found live): the Claude adapter currently registers as `claude-code`
> and Codex as `codex/current`; IXP §2 wants `<runtime>/<scope>`. Align both when convenient —
> mismatched ids are why an early cross-agent IM missed its inbox.

## Smoke
```bash
PM_PROJECT=switchboard python3 adapters/codex/codex_adapter.py   # prints agreement + a sample deny verdict
```
