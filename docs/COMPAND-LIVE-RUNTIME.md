# Compand live Scan-to-Enforce runtime

- **Status:** executable corrective pilot
- **Task:** `ENFORCE-25`
- **Predecessors:** `PROTO-9`, `ADAPTER-39`, `DOGFOOD-32`
- **Production rollout:** not authorized by this document

## What this adds

The earlier tasks froze the Codex Responses wire tuple, built a no-transform gateway, and
compiled bounded Scan evidence. They did not connect a live candidate collector to provider
counting or implement mutation. This runtime closes that executable gap for the single
`line-rle-v1` mechanism.

`scan` validates a trusted `compand.command_result.v1` receipt, builds the candidate, calls
the provider `/v1/responses/input_tokens` endpoint for original and candidate payloads, and
still forwards the client's original bytes. `enforce` runs the same admission and changes the
new command-result suffix only when the provider count is strictly lower. Missing receipts,
failed commands, unsupported bodies, history drift, provider-count failure, and ties all keep
the request unchanged.

This does not retroactively make a DOGFOOD-32 decision authorize mutation. Scan evidence
continues to have `mutation_authorized=false`; `enforce` is a separately configured operator
mode with its own runtime safety boundary.

## State and recovery

The SQLite repository stores:

- a frozen client-request to provider-request mapping for exact retries;
- client and provider input ledgers so continuations reuse the previous provider view;
- content-free receipts containing hashes, byte counts, token counts, technique, and outcome;
- independently supplied process observations; and
- original transformed command-result bytes behind a random HMAC-bound capability.

Provider bodies, dual-ledger content, and recovery artifacts are AES-GCM encrypted at rest
with the operator-supplied capability secret. Persistent state refuses to start without that
secret.

Capabilities are bound to tenant and session. They expire at the configured retention limit;
zero retention disables recovery storage. The public recovery route requires the same tenant
credential and session ID. Client credentials, upstream credentials, prompt text, and tool
output never enter receipts or observation records.

`COMPAND_SESSION_RETENTION_SECONDS` separately bounds encrypted retry and continuation
state. Request handling and the purge endpoint remove expired artifact and session state.

## Executable dogfood

Run:

```bash
python3 scripts/dogfood32_live_compand.py
```

The runner starts a real loopback provider process and a real uvicorn gateway, executes
provider counts, enforce forwarding, retry, recovery, and cross-tenant denial, and prints one
content-free JSON result. Its provider-process capture is independent of the gateway hook.
It uses dummy credentials and makes no paid or production provider call.

Deployment and rollback are in
[`runbooks/compand-pilot-deploy-recovery.md`](runbooks/compand-pilot-deploy-recovery.md).
