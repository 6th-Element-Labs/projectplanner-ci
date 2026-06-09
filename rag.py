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

_HERE = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.environ.get("PM_DOCS_DIR", os.path.join(_HERE, "plan-docs"))
CACHE = os.environ.get("PM_RAG_CACHE", os.path.join(_HERE, "rag_cache.json"))
BASE = os.environ.get("PM_LLM_BASE_URL", "http://127.0.0.1:8095/v1")
KEY = os.environ.get("PM_LLM_KEY") or os.environ.get("LLM_GATEWAY_MASTER_KEY", "")
EMBED_MODEL = os.environ.get("PM_LLM_EMBED_MODEL", "taikun-embed")

_index = None  # list of {text, file, embedding}


def _chunks(text, size=1100, overlap=120):
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    out, cur = [], ""
    for p in paras:
        if cur and len(cur) + len(p) > size:
            out.append(cur)
            cur = (cur[-overlap:] + "\n" + p) if overlap else p
        else:
            cur = (cur + "\n\n" + p) if cur else p
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


def search(query, top_k=5):
    global _index
    if _index is None:
        build_index()
    if not _index:
        return []
    qe = _embed([query])[0]
    scored = sorted(((_cos(qe, it["embedding"]), it) for it in _index), key=lambda x: -x[0])[:top_k]
    return [{"file": it["file"], "text": it["text"][:800], "score": round(s, 3)} for s, it in scored]
