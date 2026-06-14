"""Tiny self-contained RAG over the TEEP plan docs (ADR 0007).

Embeds docs/customers/teep-barnett/*.md once via the shared LLM gateway, caches
to disk, and does brute-force cosine retrieval in memory. No vector DB; nothing
written to the platform's shared embeddings table.
"""
import glob
import hashlib
import json
import math
import os

import httpx

import store

_HERE = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.environ.get("PM_DOCS_DIR", os.path.join(_HERE, "plan-docs"))
CACHE = os.environ.get("PM_RAG_CACHE", os.path.join(_HERE, "rag_cache.json"))
BASE = os.environ.get("PM_LLM_BASE_URL", "http://127.0.0.1:8095/v1")
KEY = os.environ.get("PM_LLM_KEY") or os.environ.get("LLM_GATEWAY_MASTER_KEY", "")
EMBED_MODEL = os.environ.get("PM_LLM_EMBED_MODEL", "taikun-embed")

_index = None  # static plan-docs: list of {text, file, embedding}
_dyn = None    # dynamic ingested artifacts (emails/transcripts/docs), from the rag_docs table
_dyn_ver = -1  # rag_docs max id we last loaded — cheap freshness check across processes


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


def _signature():
    files = sorted(glob.glob(os.path.join(DOCS_DIR, "*.md")))
    sig = hashlib.md5("".join(f"{os.path.basename(f)}:{int(os.path.getmtime(f))}" for f in files).encode()).hexdigest()
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
        for it, e in zip(items[i:i + 64], _embed([x["text"] for x in items[i:i + 64]])):
            it["embedding"] = e
    _index = items
    try:
        json.dump({"sig": sig, "items": items}, open(CACHE, "w"))
    except Exception:
        pass
    return len(items)


def _cos(a, b):
    s = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return s / (na * nb) if na and nb else 0.0


def _load_dyn():
    """Refresh the dynamic corpus from rag_docs only when it changed (cheap MAX(id) check),
    so the web and MCP processes both see newly-ingested artifacts without a restart."""
    global _dyn, _dyn_ver
    ver = store.rag_docs_max_id()
    if _dyn is None or ver != _dyn_ver:
        _dyn = [{"file": r["label"], "text": r["text"], "embedding": r["embedding"]}
                for r in store.all_rag_chunks()]
        _dyn_ver = ver
    return _dyn


def add_document(source_kind, label, text):
    """Ingest an artifact (email / transcript / document) into the persistent corpus.
    Chunks + embeds + stores; searchable immediately (next search reloads the dynamic set)."""
    chunks = _chunks(text or "")
    if not chunks:
        return 0
    embs = _embed(chunks)
    for ch, e in zip(chunks, embs):
        store.add_rag_chunk(source_kind, label, ch, e)
    return len(chunks)


def search(query, top_k=5):
    global _index
    if _index is None:
        build_index()
    pool = list(_index or []) + _load_dyn()
    if not pool:
        return []
    qe = _embed([query])[0]
    scored = sorted(((_cos(qe, it["embedding"]), it) for it in pool), key=lambda x: -x[0])[:top_k]
    return [{"file": it["file"], "text": it["text"][:800], "score": round(s, 3)} for s, it in scored]
