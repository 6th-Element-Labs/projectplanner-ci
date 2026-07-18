# Codex execution conformance

CO-16 uses one fail-closed evidence contract for two explicitly selected execution
connections:

| Row | Connection kind | Billing | Required execution proof |
|---|---|---|---|
| Personal | `personal_subscription` | ChatGPT/Codex subscription; never API billed | Native Codex on an ephemeral worker and a persistent user-owned Agent Host, exclusive auth, scoped MCP, cross-scope denial, purge, and post-revoke denial |
| API | `direct_api` | Explicit user-owned OpenAI Platform billing and budget | Native Codex with the selected API credential, scoped MCP, cross-scope denial, purge, post-revoke denial, and a positive cost receipt |

The rows must have different `execution_connection_id` values. The same value must be
present on the UI, scheduler, runner, credential audit, capacity, and error receipts for
that row. A failed personal execution never selects the API row, and a failed API execution
never selects the personal row.

## Evidence boundary

`scripts/co16_codex_conformance.py` accepts a redacted JSON bundle and emits
`switchboard.codex_execution_conformance.v1`. It rejects secret-shaped fields anywhere in
the bundle. The bundle carries the exact 40-character `source_sha`, and must contain exactly
one row of each connection kind. Native execution receipts must include the exact task, claim, Work Session,
runner, host, wake, source SHA, and execution connection binding, plus:

- `native_cli: true` and a nonempty CLI version;
- successful Switchboard MCP registration and scoped read/action proof;
- cross-scope denial;
- runtime residue purge; and
- denial after the selected connection is revoked.

The API cost receipt must carry the same `execution_connection_id`, a redacted billing
account fingerprint, budget id, and a positive `cost_usd`. Personal evidence must contain
neither an API cost receipt nor an API-key fallback.

This evaluator does not turn a simulated CLI or host-only connectivity check into passing
proof. It validates receipts from the real provider, runner, host, credential, and Tally
paths. Store raw transcripts outside completion evidence; include only redacted hashes,
sizes, versions, and typed outcomes.

## Operator command

```bash
PYTHONPATH=src:. python3 scripts/co16_codex_conformance.py \
  /path/to/redacted-evidence.json --output /path/to/conformance-matrix.json
```

Exit status is zero only when both rows and every negative proof pass.
