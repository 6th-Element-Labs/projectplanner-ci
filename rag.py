"""Tiny self-contained RAG over the TEEP plan docs (ADR 0007).

Embeds docs/customers/teep-barnett/*.md once via the shared LLM gateway, caches
to disk, and does brute-force cosine retrieval in memory. No vector DB; nothing
written to the platform's shared embeddings table.
"""
import glob
import hashlib
import json
import os

import httpx
import numpy as np

import store

_HERE = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.environ.get("PM_DOCS_DIR", os.path.join(_HERE, "plan-docs"))
CACHE = os.environ.get("PM_RAG_CACHE", os.path.join(_HERE, "rag_cache.json"))
BASE = os.environ.get("PM_LLM_BASE_URL", "http://127.0.0.1:8095/v1")
KEY = os.environ.get("PM_LLM_KEY") or os.environ.get("LLM_GATEWAY_MASTER_KEY", "")
EMBED_MODEL = os.environ.get("PM_LLM_EMBED_MODEL", "taikun-embed")
PACK_VERSION = 1  # bump when the contextual-packing format changes -> re-embed the corpus

_index = None  # static plan-docs (Maxwell plan): list of {text, file, embedding}
_dyn = {}      # project -> dynamic ingested artifacts (emails/transcripts/docs) from that project's rag_docs
_dyn_ver = {}  # project -> rag_docs max id we last loaded — cheap per-project freshness check across processes


def _chunks(text, size=1100, overlap=120):
    # Split on blank-line paragraphs, then HARD-WRAP any block longer than `size`. Markdown
    # transcripts/notes often have no blank lines between speaker turns -> one giant block that
    # embeds as a single blurry vector and never retrieves. Wrap on line, then char, boundaries;
    # finally merge small blocks up to `size` with overlap between emitted chunks.
    blocks = []
    for para in (text or "").split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if len(para) <= size:
            blocks.append(para)
            continue
        cur = ""
        for line in para.split("\n"):
            while len(line) > size:                 # one pathologically long line
                blocks.append(line[:size])
                line = line[size:]
            if cur and len(cur) + len(line) + 1 > size:
                blocks.append(cur)
                cur = line
            else:
                cur = (cur + "\n" + line) if cur else line
        if cur:
            blocks.append(cur)
    out, cur = [], ""
    for b in blocks:
        if cur and len(cur) + len(b) > size:
            out.append(cur)
            cur = (cur[-overlap:] + "\n" + b) if overlap else b
        else:
            cur = (cur + "\n\n" + b) if cur else b
    if cur:
        out.append(cur)
    return out


def _embed(texts):
    r = httpx.post(f"{BASE}/embeddings", headers={"Authorization": f"Bearer {KEY}"},
                   json={"model": EMBED_MODEL, "input": texts}, timeout=90)
    r.raise_for_status()
    return [d["embedding"] for d in r.json()["data"]]


def _pack(label, text):
    """Contextual embedding (Anthropic 'contextual retrieval', lite deterministic form):
    prefix each chunk with its source label so the topic/source lives INSIDE the vector.
    The label (e.g. 'email: Total Energy Con Call', 'project-plan.md') is the context.
    We store the RAW chunk but embed the packed form, so snippets stay clean."""
    return ("[%s]\n%s" % (label, text)) if label else (text or "")


def _pack_query(query):
    """Query is left RAW. Packing it with a constant project header (ActionEngine packs the
    per-query resolved asset; we have no such resolution) over-steered retrieval toward the
    plan docs whose labels resemble that header — A/B-tested. Chunk-side context is enough."""
    return query or ""


def _signature():
    files = sorted(glob.glob(os.path.join(DOCS_DIR, "*.md")))
    raw = "".join(f"{os.path.basename(f)}:{int(os.path.getmtime(f))}" for f in files) + f"|pack={PACK_VERSION}"
    sig = hashlib.md5(raw.encode()).hexdigest()
    return sig, files


def build_index(force=False):
    global _index
    sig, files = _signature()
    if not force and os.path.exists(CACHE):
        try:
            c = json.load(open(CACHE))
            if c.get("sig") == sig and c.get("items"):
                _index = c["items"]
                return len(_index)
        except Exception:
            pass
    items = []
    for f in files:
        name = os.path.basename(f)
        for ch in _chunks(open(f, encoding="utf-8").read()):
            items.append({"text": ch, "file": name})
    for i in range(0, len(items), 64):
        for it, e in zip(items[i:i + 64], _embed([_pack(x["file"], x["text"]) for x in items[i:i + 64]])):
            it["embedding"] = e
    _index = items
    try:
        json.dump({"sig": sig, "items": items}, open(CACHE, "w"))
    except Exception:
        pass
    return len(items)


def _cos(a, b):
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    return float(np.dot(a, b) / (na * nb)) if na and nb else 0.0


def _load_dyn(project=None):
    """Refresh ONE project's dynamic corpus from its rag_docs table, only when it changed
    (cheap per-project MAX(id) check), so the web and MCP processes both see newly-ingested
    artifacts without a restart. Each project's corpus lives in its own DB file, so the
    dynamic sets are segmented — a transcript ingested on project X never leaks into Y."""
    project = project or store.DEFAULT_PROJECT
    global _dyn, _dyn_ver
    ver = store.rag_docs_max_id(project)
    if project not in _dyn or ver != _dyn_ver.get(project):
        _dyn[project] = [{"file": r["label"], "text": r["text"], "embedding": r["embedding"]}
                         for r in store.all_rag_chunks(project)]
        _dyn_ver[project] = ver
    return _dyn[project]


def add_document(source_kind, label, text, project=None):
    """Ingest an artifact (email / transcript / document) into ONE project's persistent corpus.
    Chunks + embeds + stores; searchable immediately (next search reloads that project's set)."""
    project = project or store.DEFAULT_PROJECT
    chunks = _chunks(text or "")
    if not chunks:
        return 0
    embs = _embed([_pack(label, ch) for ch in chunks])   # embed packed, store raw
    for ch, e in zip(chunks, embs):
        store.add_rag_chunk(source_kind, label, ch, e, project=project)
    return len(chunks)


def reembed_dynamic(project=None):
    """Re-embed every dynamic rag_docs row for ONE project with the CURRENT packing (run once
    after a packing change). Static plan-docs re-embed on the next build_index via the bumped sig."""
    project = project or store.DEFAULT_PROJECT
    rows = store.all_rag_rows(project)  # [{id, label, text}]
    n = 0
    for i in range(0, len(rows), 64):
        batch = rows[i:i + 64]
        for r, e in zip(batch, _embed([_pack(r["label"], r["text"]) for r in batch])):
            store.update_rag_embedding(r["id"], e, project=project)
            n += 1
    global _dyn, _dyn_ver
    _dyn.pop(project, None)
    _dyn_ver.pop(project, None)
    try:
        store.set_meta("rag_pack_version", str(PACK_VERSION), project=project)
    except Exception:
        pass
    return n


def search(query, top_k=5, project=None):
    """Search ONE project's corpus. The static plan-docs index is Maxwell's plan (plan-docs/*.md),
    so it is mixed in ONLY for the default project; every other project searches just its own
    dynamic corpus (uploaded transcripts / emails / docs) — segmented, invisible across projects."""
    project = project or store.DEFAULT_PROJECT
    global _index
    static = []
    if project == store.DEFAULT_PROJECT:
        if _index is None:
            build_index()
        static = list(_index or [])
    pool = static + _load_dyn(project)
    if not pool:
        return []
    qe = np.asarray(_embed([_pack_query(query)])[0], dtype=np.float64)
    mat = np.asarray([it["embedding"] for it in pool], dtype=np.float64)
    # Vectorized cosine of the query against the whole pool in one matmul (was a
    # per-item pure-Python loop). Guard zero-norm rows so they score 0.0 rather
    # than divide-by-zero, matching the old _cos contract.
    denom = np.linalg.norm(mat, axis=1) * float(np.linalg.norm(qe))
    sims = np.zeros(len(pool), dtype=np.float64)
    nz = denom > 0
    sims[nz] = (mat[nz] @ qe) / denom[nz]
    # Stable descending sort keeps the original insertion order on ties, matching
    # Python's stable `sorted(..., key=-score)`.
    order = np.argsort(-sims, kind="stable")[:top_k]
    return [{"file": pool[i]["file"], "text": pool[i]["text"][:800],
             "score": round(float(sims[i]), 3)} for i in order]
