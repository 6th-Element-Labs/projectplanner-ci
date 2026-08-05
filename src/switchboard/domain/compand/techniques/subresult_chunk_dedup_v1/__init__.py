"""FastCDC-style scoped chunk references for high exact overlap."""

import hashlib

from ..shared import (
    DetectionContext,
    IsolatedTechnique,
    Proposal,
    ReasonCode,
    Recovery,
    TechniqueCandidate,
    compact_json,
    decode_json_object,
    fixture_scenario,
)


_GEAR = tuple(
    int.from_bytes(hashlib.sha256(bytes([value])).digest()[:8], "big")
    for value in range(256)
)


def _chunks(data: bytes, minimum: int = 512, target: int = 2048, maximum: int = 8192) -> list[bytes]:
    chunks: list[bytes] = []
    start = 0
    mask = target - 1
    while start < len(data):
        cursor = start
        rolling = 0
        while cursor < len(data):
            rolling = ((rolling << 1) + _GEAR[data[cursor]]) & ((1 << 64) - 1)
            cursor += 1
            size = cursor - start
            if size >= minimum and ((rolling & mask) == 0 or size >= maximum):
                break
        chunks.append(data[start:cursor])
        start = cursor
    return chunks


class SubresultChunkDedupTechnique(IsolatedTechnique):
    technique_id = "subresult-chunk-dedup-v1"

    def propose(self, context: DetectionContext) -> Proposal | None:
        wrapped = fixture_scenario(context.original, family="exact_and_partial_overlap")
        if wrapped is None:
            return None
        scenario, _ = wrapped
        source = scenario.get("provider_visible_source")
        results = scenario.get("new_results")
        index = scenario.get("target_candidate_index")
        if (
            scenario.get("source_state") != "visible"
            or scenario.get("source_artifact_recoverable") is not True
            or scenario.get("source_scope") != scenario.get("requester_scope")
            or not isinstance(source, str)
            or not isinstance(results, list)
            or not isinstance(index, int)
            or not 0 <= index < len(results)
            or not isinstance(results[index], str)
        ):
            return None
        source_bytes = source.encode()
        target_bytes = results[index].encode()
        source_chunks = _chunks(source_bytes)
        target_chunks = _chunks(target_bytes)
        source_by_hash = {
            hashlib.sha256(chunk).hexdigest(): (position, chunk)
            for position, chunk in enumerate(source_chunks)
        }
        encoded: list[dict[str, object]] = []
        reconstructed = bytearray()
        referenced = 0
        for chunk in target_chunks:
            digest = hashlib.sha256(chunk).hexdigest()
            known = source_by_hash.get(digest)
            if known is None:
                encoded.append({"kind": "literal_utf8", "value": chunk.decode("utf-8")})
                reconstructed.extend(chunk)
            else:
                position, source_chunk = known
                encoded.append({"kind": "reference", "source_chunk_index": position, "sha256": digest})
                reconstructed.extend(source_chunk)
                referenced += len(source_chunk)
        overlap = referenced / len(target_bytes) if target_bytes else 0.0
        if overlap < 0.70 or bytes(reconstructed) != target_bytes:
            return None
        proposed = compact_json(
            {"version": self.technique_id, "scope": scenario["requester_scope"], "chunks": encoded}
        )
        return Proposal(proposed, {"chunk_count": len(encoded), "exact_overlap_ratio": overlap})

    def recover(self, candidate: TechniqueCandidate) -> Recovery:
        wrapped = fixture_scenario(candidate.original, family="exact_and_partial_overlap")
        encoded = decode_json_object(candidate.proposed)
        if wrapped is None or encoded is None:
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        scenario, _ = wrapped
        source = scenario.get("provider_visible_source")
        results = scenario.get("new_results")
        index = scenario.get("target_candidate_index")
        chunks = encoded.get("chunks")
        if (
            not isinstance(source, str)
            or not isinstance(results, list)
            or not isinstance(index, int)
            or not 0 <= index < len(results)
            or not isinstance(results[index], str)
            or encoded.get("version") != self.technique_id
            or encoded.get("scope") != scenario.get("requester_scope")
            or not isinstance(chunks, list)
        ):
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        source_chunks = _chunks(source.encode())
        recovered = bytearray()
        for item in chunks:
            if not isinstance(item, dict):
                raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
            if item.get("kind") == "literal_utf8" and isinstance(item.get("value"), str):
                recovered.extend(item["value"].encode())
                continue
            position = item.get("source_chunk_index")
            if item.get("kind") != "reference" or not isinstance(position, int):
                raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
            if not 0 <= position < len(source_chunks):
                raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
            source_chunk = source_chunks[position]
            if item.get("sha256") != hashlib.sha256(source_chunk).hexdigest():
                raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
            recovered.extend(source_chunk)
        if bytes(recovered) != results[index].encode():
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        return Recovery(
            candidate.original,
            {"recovery_kind": "chunk_reference_expansion", "source_artifact_retained": True},
        )


__all__ = ["SubresultChunkDedupTechnique"]
