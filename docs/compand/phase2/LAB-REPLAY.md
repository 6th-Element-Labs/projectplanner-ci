# Compand Technique Lab replay wire

This is the development harness for replaying one content-addressed fixture through one
technique version in one Python process. It implements ADAPTER-41; it does not authorize
CES-1 confirmatory traffic, certification, production promotion, or a savings claim.

Run the exact `line-rle-v1` reference technique with:

```bash
python3 scripts/compand_lab.py replay path/to/fixture.txt \
  --output-root /tmp/compand-lab \
  --technique line-rle-v1 \
  --arm E1 \
  --model-id offline-model-fixture \
  --config-id line-rle-default
```

The command prints a content-free JSON result containing the new `run_id`, input and output
hashes, disposition, and run directory. Each invocation creates a new run ID. The output
root contains:

```text
objects/sha256/<prefix>/<digest>  # immutable fixture and derived bytes
runs/<run_id>/run_manifest.json   # created once; never replaced
runs/<run_id>/events.jsonl        # appended in monotonic sequence
```

Every event binds the arm, technique and version, candidate, input and output hashes,
parent event, model/config fingerprints, stage, status, typed reason, and elapsed time.
Events and manifests contain no fixture text or credential values. The manifest declares
the volatile run/timing fields excluded when comparing semantic determinism; semantic event
IDs and content hashes remain stable for the same fixture and configuration.

`B0` retains the byte-identical baseline. `S1` performs detection and estimation without
writing transformed bytes. `E1` applies exactly one technique and verifies recovery. A
plugin exception is recorded as a red event without retrying or modifying the baseline.

## Technique selection

The CLI accepts exactly the 30 IDs frozen in [`technique-catalog.json`](technique-catalog.json).
The 14 entries marked `cloud_gateway_enforceable` resolve to isolated plugin packages under
`src/switchboard/domain/compand/techniques/`. Each package implements only the shared
`Technique` contract and does not import another plugin.

Recovery is technique-owned. Codecs decode their transformed bytes, deltas apply their
patch, and reference or projection techniques validate transformed scope and hash metadata
against the retained content-addressed artifact. The shared harness checks the returned
bytes and records hashes; it never manufactures recovery by copying the original input.

The other 16 IDs resolve to structured `unsupported` records containing the frozen version,
eligibility, guarantee, host dependency, and reason. They never run through the replay wire
as fake passthrough transforms. An eligible plugin that sees an inapplicable fixture instead
produces `declined` with `reason_code=no_candidate`; this is intentionally distinct from
`status=unsupported`.

This registry implements individual `E1` development replay only. It does not compose
techniques or authorize the `C1` arm, confirmatory traffic, certification, production
promotion, or cumulative savings claims.
