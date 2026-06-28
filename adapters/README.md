# Switchboard Runtime Adapters

Adapter packs make the Switchboard lifecycle automatic inside each runtime: handshake,
presence, inbox, leases, dispatch, completion evidence, Tally, reconcile, and truthful control
fidelity.

## P0 Conformance

Run the shared P0 smoke against an isolated throwaway board:

```bash
python3 adapters/conformance.py
```

The command prints pass/fail checks plus a capability statement:

```bash
python3 adapters/conformance.py --json
```

Current reference transport:

- `local-store`: direct `store.py` calls against temporary SQLite files.

Runtime-specific packs should reuse `run_p0_conformance(...)` with their own REST, MCP, or SDK
client instead of inventing new smoke semantics per adapter.
