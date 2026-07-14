# Provider credential vault

CO-6 adds the fail-closed BYOA credential boundary for personal OpenAI/Codex,
Anthropic/Claude, and Cursor subscriptions. Switchboard stores an encrypted provider identity and
issues a short-lived, exact-binding lease; it does not pool customer accounts or substitute one
customer's account for another.

## Security contract

- `PM_PROVIDER_VAULT_KEY` must be the URL-safe base64 encoding of exactly 32 random bytes.
  The key is read only from the process environment and is never stored in SQLite. Enrollment,
  rotation, and materialization return `503` when the key is absent, malformed, or has the wrong
  `PM_PROVIDER_VAULT_KEY_ID`; there is no development fallback.
- Provider capsules are encrypted with AES-256-GCM. Associated data binds the ciphertext to its
  credential reference, tenant, user, provider, provider account, and credential version, so a
  copied or modified row cannot be decrypted under a different identity.
- Public REST, MCP, repository metadata, lease receipts, events, and launch receipts are
  allowlists. They never include ciphertext, nonce, a raw credential, or an auth capsule.
- GitHub repository authorization is a separate trust boundary. `github_app` and other GitHub auth
  types are rejected by this vault.
- Revocation and deletion cryptographically erase ciphertext and nonce, fence every live lease,
  and preserve a non-secret audit tombstone. Rotation fences every lease on the previous version.

Generate one key for the deployment and make the same value available to the web, MCP, and trusted
runner bridge processes:

```bash
python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
```

Set the output as `PM_PROVIDER_VAULT_KEY` and set a non-secret version label such as
`PM_PROVIDER_VAULT_KEY_ID=env:v1`. The current implementation intentionally supports one active
master-key version. Changing the key or key id without re-encrypting enrolled rows makes them
unusable and fences a lease on the first failed materialization attempt.

## Identity and dispatch binding

An enrolled connection records:

- tenant and user;
- normalized provider and provider account id;
- auth type, expiry/refresh/revocation state, and project allowlist;
- exclusive or bounded concurrency policy;
- credential version, key id, and non-secret audit provenance.

Lease acquisition validates the selected task, healthy active Work Session, live Agent Host, and
explicitly runnable runner session before persisting this exact tuple:

```text
user_id + provider + provider_account_id + credential_reference
+ project + task_id + claim_id + agent_id + host_id + runner_session_id + work_session_id
+ authenticated principal_id + principal_kind + scopes
```

Every lease is single-use: `issued -> materializing -> active -> released`, with `fenced` and
`expired` terminal failure states. Materialization atomically consumes `issued` before decrypting;
duplicate and concurrent attempts cannot reuse it. The trusted `start_with_provider_credential`
application bridge repeats runtime validation, checks every lease field and credential version,
decrypts only in process memory, and only then invokes the process starter. A failed start purges
runtime material and fences the lease. That bridge is deliberately not registered as REST or MCP.
Wrong-provider, cross-project, cross-tenant, terminal/stale runner, cross-claim, cross-agent,
cross-host, cross-principal, expired, revoked, deleted, corrupted, or incomplete bindings fail
before the process starter runs.

## Surfaces and authorization

REST routes live below `/api/projects/{project}/provider-connections`; MCP exposes matching
metadata and lifecycle tools. Required scopes are:

| Operation | Scope |
|---|---|
| List/read metadata and audit events | `read:credentials` |
| Enroll, rotate, revoke, delete | `write:credentials` |
| Acquire or release a launch lease | `use:credentials` |

Human principals may act only for a matching `user_id` unless they are administrators. Agent,
host, and system principals with `use:credentials` may acquire a fully validated dispatch lease on
behalf of that user. The acquiring principal is stored structurally, not collapsed to a service
boolean. Release is restricted to the credential owner, the exact acquiring agent/host, an
explicitly scoped system dispatcher, or an administrator. A human cannot turn `use:credentials`
into cross-user impersonation, and a different service cannot release another service's lease.

The executable proof is `tests/test_co6_provider_credential_vault.py`. It covers lifecycle,
concurrency/replay, exact claim/agent/host/principal binding, terminal runners, scope separation,
service release symmetry, process-start recovery, expiry while materializing, missing/wrong key
behavior, authenticated-ciphertext failure, pre-launch denial, and scans API/MCP responses,
activity, SQLite, WAL, and cache artifacts for generated credential canaries.
