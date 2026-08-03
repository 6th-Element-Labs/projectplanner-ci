# Compand Scan evidence

- **Task:** `DOGFOOD-32`
- **Authority:** [ADR-0026](decisions/0026-compand-benchmark-publication.md) and
  [CES-1](COMPAND-BENCHMARK-STANDARD.md)
- **Wire baseline:** [Compand Codex Responses contract](COMPAND-CODEX-RESPONSES-WIRE-CONTRACT.md)

Compand Scan observes eligible `line-rle-v1` candidates while the gateway sends the
authorized original request upstream byte-for-byte. Scan evidence contains aggregate
counts, timings, provider token counts, dated prices, hashes, and decisions. It must not
contain prompt text, tool-output text, authorization values, or credential hashes.

## Evidence boundary

`compile_gateway_coverage_receipt` joins two independently supplied, content-free event
streams by correlation ID:

1. the gateway hook says which certified tuple and endpoint it handled;
2. the process-level observer classifies each provider request as `captured`, `bypassed`,
   `excluded`, or `unknown`.

Missing or contradictory observations become `unknown`. Any bypass, unknown tuple,
parity failure, snapshot mismatch, or absent process-level observation blocks mutation.
Coverage with zero captured inference requests is also blocking: an `advance` decision
requires a reconciled, certified `POST /v1/responses` capture, so `/v1/models` controls or
`/v1/responses/input_tokens` count calls alone never provide promotion authority.
Loopback fixture hooks deliberately compile to `low_coverage_hold`; they prove receipt
mechanics, not live insertion.

The shadow economics path accepts only a trusted `compand.command_result.v1` receipt for
a successful, complete UTF-8 command result. The eligibility boundary separately requires
the expected newly appended Responses `function_call_output`, matches its exact `call_id`
to the receipt, and hashes that item's output bytes. It builds the candidate in memory,
sends the original bytes on the authorized gateway path, and persists only aggregate
span/byte counts, whole-artifact hashes, provider count results, cache fields, latency,
retries, task outcome, and dated projected input cost. Published costs, savings, cache
exposure, and qualification are validated and regenerated from those primitives; provider
and model identity must match the run snapshot. A cheaper local token count is not enough:
`advance` requires exposed cache fields and a lower cache-adjusted projected provider input
cost for at least one completed task.

Every decision has `mutation_authorized=false`. `advance` means only that the next
separately gated experiment may be considered.

## Reproduction

The checked-in fixture bundle is an honest negative control. It uses the frozen Codex
`0.144.5` loopback exchanges and therefore must remain held:

```bash
tmp_dir="$(mktemp -d)"
python3 scripts/compand_scan_evidence.py \
  fixtures/compand/dogfood-32/fixture-loopback-input.json \
  "$tmp_dir/fixture-loopback-evidence.json"
python3 -m json.tool "$tmp_dir/fixture-loopback-evidence.json" >/dev/null
```

For a real run, freeze the input file before traffic, use a declared process observer and
time window, record every in-scope provider request, perform original and candidate
counts through `/v1/responses/input_tokens`, and compile to a new output path. The
compiler refuses to overwrite an existing evidence file. Corrections create a new input,
output, and checksum rather than modifying a prior bundle.
