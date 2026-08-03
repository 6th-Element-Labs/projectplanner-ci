# Compand Codex Responses wire contract

- **Status:** Frozen pilot contract v1
- **Task:** `PROTO-9`
- **Fixture root:** `fixtures/compand/openai-responses/codex-cli-0.144.5/`
- **Evidence ceiling:** protocol/mechanism evidence; this does not certify a transform
- **Recertification:** mandatory when any pinned tuple field below changes

## Outcome

Before Compand changes any model-visible bytes, the OpenAI Responses adapter must prove
that it can preserve the exact Codex request, response, continuation, retry, error, and
streaming surfaces represented by the fixture set. Unknown or unsupported input remains
transparent passthrough. This contract does not implement a transform or authorize one.

## Frozen certification tuple

| Field | Frozen value |
|---|---|
| Client | Codex CLI `0.144.5` |
| Binary SHA-256 | `d96ae1ca1ff6fc8587842fa04c92d3ee4d31651a811c2f89b65fcfd9c28473e2` |
| Observed user agent | `codex_exec/0.144.5 (Mac OS 26.3.0; arm64) dumb (codex_exec; 0.144.5)` |
| Model | `gpt-5.4` |
| Auth lane | Custom provider API key; no real credential was used or retained |
| Provider id | `compand_fixture` |
| Provider name | `Compand Fixture Capture` |
| API base URL | `http://127.0.0.1:18765/v1` |
| Credential environment variable | `COMPAND_FIXTURE_API_KEY` |
| Wire API | `responses` |
| Positive-capture retries | request `0`; stream `0` |
| Stream idle timeout | `5000` ms |
| Reasoning effort | `high` |
| Store | `false` |
| API surfaces | `GET /v1/models?client_version=0.144.5`, `POST /v1/responses`, `POST /v1/responses/input_tokens` |

The proof used trusted command-line configuration overrides with
`--ignore-user-config`. Codex treats provider configuration as machine/user trust
configuration: the equivalent persistent configuration belongs in user-level
`$CODEX_HOME/config.toml`, never project-local `.codex/config.toml`:

```toml
model = "gpt-5.4"
model_provider = "compand_fixture"
model_reasoning_effort = "high"

[model_providers.compand_fixture]
name = "Compand Fixture Capture"
base_url = "http://127.0.0.1:18765/v1"
env_key = "COMPAND_FIXTURE_API_KEY"
wire_api = "responses"
request_max_retries = 0
stream_max_retries = 0
stream_idle_timeout_ms = 5000
```

The positive capture invocation was:

```bash
COMPAND_FIXTURE_API_KEY=fixture-token \
codex exec --ephemeral --ignore-user-config --skip-git-repo-check \
  -C /tmp -m gpt-5.4 \
  -c 'model_provider="compand_fixture"' \
  -c 'model_providers.compand_fixture.name="Compand Fixture Capture"' \
  -c 'model_providers.compand_fixture.base_url="http://127.0.0.1:18765/v1"' \
  -c 'model_providers.compand_fixture.env_key="COMPAND_FIXTURE_API_KEY"' \
  -c 'model_providers.compand_fixture.wire_api="responses"' \
  -c 'model_providers.compand_fixture.request_max_retries=0' \
  -c 'model_providers.compand_fixture.stream_max_retries=0' \
  -c 'model_providers.compand_fixture.stream_idle_timeout_ms=5000' \
  -c 'model_reasoning_effort="high"' \
  'Reply only with fixture-ok'
```

No real API key is present; `fixture-token` is a published, non-secret dummy. A capture or
published fixture containing an actual authorization value, private prompt, absolute user
path, installation id, session id, or raw source excerpt is invalid.

## Capture provenance and limitation

The request shape and tool loop were black-box captured from the pinned binary on
2026-08-03 against a loopback listener. The listener retained method, path, selected
non-secret header presence, request byte counts and hashes, typed field structure, and
dummy tool output. Instructions, built-in context, identifiers, and non-dummy text were
replaced during capture with length-and-hash markers.

The positive captures observed a 34,164-byte normal request, a 34,440-byte tool-call
request, and a 34,770-byte follow-up containing `function_call` plus
`function_call_output`. The mock `/v1/models` response was deliberately minimal and did
not satisfy Codex's model-catalog schema; Codex logged that decode failure and continued
to the explicitly selected model. Therefore the fixture proves that discovery is called,
not that model discovery is conformant. The failing signal remains visible here rather
than being converted into green evidence.

Provider responses and error bodies are deterministic sanitized OpenAI Responses shapes.
They are protocol fixtures, not claims that the loopback service called an upstream model.

### Exact capture coverage versus provider-contract fixtures

Only the normal streamed request, tool-call turn, and tool-output follow-up are observed
Codex `0.144.5` traffic for the frozen tuple. The manifest records their original body
byte counts and SHA-256 hashes plus the observed client outcome. Sanitized bodies are not
presented as the original bytes.

The remaining required shapes are deliberately classified below the exact-Codex evidence
ceiling:

| Surface | Evidence class | Why it is not exact Codex coverage |
|---|---|---|
| Manual history replay | Provider contract only | The capture observed only the narrower tool-loop continuation |
| `previous_response_id` | Provider contract only | The pinned client used `store=false` and emitted no such traffic |
| Durable `conversation` | Provider contract only | The pinned client used `store=false` and emitted no such traffic |
| Retry | Provider contract only | Request and stream retries were pinned to zero |
| Cancellation | Provider contract only | The positive capture did not disconnect mid-stream |
| 400/401/429/500 | Provider contract only | These are deterministic sanitized provider error bodies |
| Input-token count | Provider contract only | The pinned client did not call `/v1/responses/input_tokens` |
| Unsupported/adversarial shapes | Unsupported | These are synthetic fail-open test inputs by design |

`manifest.json` is the queryable authority for these classifications, bounded structural
metadata, observed outcome, original hash where one exists, and the explicit unobserved
reason where it does not. `wire/exchanges.json` records the replay method, path/query,
selected headers, status, and exact request/response body file for every fixture claimed
by the no-transform conformance test.

## Byte and ordering contract

For every fixture classified as passthrough:

1. Preserve the request method, path, query string, status, and header values that are not
   explicitly hop-by-hop. Do not normalize provider fields for convenience.
2. Preserve the request and response body bytes exactly. JSON key order, duplicate-safe
   retry bodies, unknown fields, `null`, empty arrays, and number/string distinctions are
   observable wire data.
3. Preserve every SSE frame as bytes and preserve frame order. Do not parse and
   reserialize an event on the transparent path.
4. A cancelled stream remains a partial stream. Do not synthesize
   `response.completed`, usage, or a green terminal response.
5. Preserve representative 4xx/5xx status and error bodies. Gateway attempt identity is
   out-of-band; it must not mutate a retried provider request.

`tests/test_proto9_compand_codex_responses_contract.py` starts a loopback upstream and a
separate no-transform HTTP proxy, replays every declared wire exchange through both, and
compares method, path/query, selected headers, status, request bytes, response bytes, and
raw SSE frame order at ingress and egress. It also freezes the SHA-256 of each fixture.
An in-process byte copy or semantic JSON comparison is insufficient.

## Continuation and dual ledgers

Compand maintains two ledgers per tenant/principal/session/context epoch:

- **Client-visible ledger:** canonical raw items as supplied or resent by Codex.
- **Provider-visible ledger:** the exact bytes Compand previously sent upstream.

A new request must match the complete client-visible high-water prefix. Once matched,
Compand replays the paired provider-visible prefix byte-for-byte and considers only newly
appended eligible raw suffix items. It may not compare new client bytes to previously
transformed provider bytes. Edited history creates a fork/context epoch and conservative
passthrough; ambiguity never guesses.

The same rule applies to all supported state mechanisms:

- manual item replay, including all prior output and opaque/encrypted reasoning items;
- `previous_response_id` chains;
- durable `conversation` references;
- an identical retry of a frozen provider request.

A retry reuses the already frozen provider representation. Re-running an optimizer over
the old prefix, even if deterministic today, is prohibited because codec/profile changes
would drift model-visible history. `ledgers/continuation.json` contains a positive replay,
an identical retry, and a rejected recompressed-prefix example.

## Typed tool-output eligibility

Codex `0.144.5` sends tool output as a string-valued
`function_call_output.output`. In the captured command example that text includes
`Process exited with code 0`, but text is not lifecycle or eligibility authority. The raw
Responses item is therefore passthrough and ineligible by itself.

A command output becomes eligible only when a trusted adapter supplies a matching typed
receipt with all of these fields:

- schema `compand.command_result.v1`;
- exact `call_id` and output SHA-256 binding;
- `source_kind = "command_result"` and `trusted_adapter = true`;
- integer (not boolean or string) `exit_status = 0`;
- `content_type = "text/plain"` and `encoding = "utf-8"`;
- `truncated = false`, `signed = false`, and byte count at or below 1,048,576;
- the item is newly appended after the frozen prefix.

Failure output, missing/ambiguous exit status, binary or unknown encoding, signed/opaque
content, arbitrary JSON, truncated data, and oversized artifacts are ineligible. User or
assistant prose can never create, override, or repair this receipt. Ineligibility means
the original bytes pass through unchanged.

## Token count and usage surface

The provider-authoritative count request is `POST /v1/responses/input_tokens` with the
same model, input, instructions, tool, and conversation shapes used by Responses. The
response object is `response.input_tokens` with integer `input_tokens`. Local estimates
may inform diagnostics but do not replace this provider result when the endpoint is
available.

Responses usage fields are forwarded without schema narrowing. The fixtures cover
`input_tokens`, `input_tokens_details.cached_tokens`, the newer optional
`input_tokens_details.cache_write_tokens`, `output_tokens`,
`output_tokens_details.reasoning_tokens`, and `total_tokens`. Missing optional fields
remain missing; the gateway must not invent zero values.

## Unsupported shapes and fail behavior

The following are transparent unchanged passthrough, with an auditable reason:

- unknown item/event types or an unrecognized Codex/build/provider tuple;
- binary bodies, unknown encodings, image/file bodies, or non-text command output;
- signed, encrypted, opaque, or integrity-protected fields;
- arbitrary JSON not bound to a trusted typed command receipt;
- command output larger than the configured limit or marked truncated;
- failed commands, errors, warnings, stack traces, and cancellation fragments;
- any request whose frozen client prefix, provider prefix, or reference chain is missing,
  ambiguous, or inconsistent.

Optimization uncertainty fails open to the original authorized route. Authentication,
tenant, retention, DLP, budget, and egress-policy failures still fail closed; passthrough
is not an authority bypass.

## Recertification gate

Changing any client version/build, binary hash, OS/architecture profile, model/provider
snapshot, auth lane, provider/base URL configuration, wire API, request/stream retry
settings, stream timeout, Responses schema, tool surface, sanitizer, or fixture bytes
sets this tuple to `suspended`. Restore it only by:

1. capturing the same feature matrix with dummy/sanitized content;
2. reviewing every fixture diff and unsupported-shape decision;
3. rerunning direct conformance and the canonical repository gate;
4. publishing a new immutable fixture version and checksums rather than replacing v1.

## Official protocol references

- [Streaming Responses](https://developers.openai.com/api/docs/guides/streaming-responses)
- [Conversation state](https://developers.openai.com/api/docs/guides/conversation-state)
- [Counting tokens](https://developers.openai.com/api/docs/guides/token-counting)
- [Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)
- [Codex configuration reference](https://developers.openai.com/codex/config-reference)

This contract is subordinate to ADR-0026/CES-1 for evidence and claim boundaries and to
ADR-0008 for Switchboard lifecycle authority.
