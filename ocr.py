"""PDF OCR -> searchable PDF via the shared LLM gateway (OpenAI vision).

Drop a scanned / image-only PDF (e.g. anything "Print to PDF"); each page is
rendered to an image, sent to the gateway vision model for OCR, and the recognised
text is embedded back as an INVISIBLE layer over the original page. The result looks
pixel-for-pixel identical but is now selectable, searchable and copy-pasteable —
exactly what Acrobat's OCR does, but the OCR is done by the AI model.

Mirrors transcribe.py: the app never holds the OpenAI key; it talks to the bundled
LiteLLM gateway with a logical model name, swappable in deploy/gateway/config.yaml.
"""
import base64
import io
import logging
import os

import fitz  # PyMuPDF
import httpx

log = logging.getLogger("ocr")

BASE = os.environ.get("PM_LLM_BASE_URL", "http://127.0.0.1:8095/v1")
KEY = os.environ.get("PM_LLM_KEY") or os.environ.get("LLM_GATEWAY_MASTER_KEY", "")
# Dedicated vision model in the gateway (`taikun-ocr` -> openai/gpt-4o). Swap the
# concrete model in deploy/gateway/config.yaml without touching the app.
MODEL = os.environ.get("PM_OCR_MODEL", "taikun-ocr")
MAX_BYTES = 40 * 1024 * 1024   # protects the 1 GB VM
MAX_PAGES = 50
DPI = 200                       # render resolution for the page image sent to OCR

OCR_PROMPT = (
    "You are a precise OCR engine. Transcribe ALL text visible in this page image "
    "exactly, in natural reading order (top-to-bottom, left-to-right). Keep line "
    "breaks and the order of headings, paragraphs, list items, table cells and code. "
    "Do not translate, summarise, or add any commentary or markdown code fences — "
    "output only the transcribed text. If the page contains no text, output nothing."
)


def is_pdf(filename, content_type=None):
    fn = (filename or "").lower()
    ct = (content_type or "").lower()
    return fn.endswith(".pdf") or ct == "application/pdf"


def _ocr_image_png(png_bytes):
    """OCR a single page image (PNG bytes) -> plain text, via the gateway."""
    b64 = base64.b64encode(png_bytes).decode("ascii")
    body = {
        "model": MODEL,
        "temperature": 0,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": OCR_PROMPT},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        }],
    }
    r = httpx.post(
        f"{BASE}/chat/completions",
        headers={"Authorization": f"Bearer {KEY}"},
        json=body,
        timeout=300,
    )
    r.raise_for_status()
    txt = (r.json()["choices"][0]["message"]["content"] or "").strip()
    # Strip stray code fences if the model wrapped the page despite instructions.
    if txt.startswith("```"):
        lines = txt.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        txt = "\n".join(lines).strip()
    return txt


def _embed_invisible_text(page, text):
    """Lay `text` onto the page as an invisible (render_mode=3) layer so the page
    becomes searchable/selectable. Without per-word boxes we distribute the lines
    evenly down the page in reading order — search and copy work; the glyphs are
    never drawn, so the page still looks identical to the original image."""
    rect = page.rect
    lines = text.splitlines()
    n = max(len([ln for ln in lines if ln.strip()]), 1)
    margin = 36.0
    top, bottom = rect.y0 + margin, rect.y1 - margin
    step = max((bottom - top) / n, 6.0)
    fontsize = max(min(step * 0.85, 11.0), 4.0)
    x = rect.x0 + margin
    y = top
    for ln in lines:
        if ln.strip():
            try:
                page.insert_text((x, y), ln, fontname="helv", fontsize=fontsize,
                                 render_mode=3, color=(0, 0, 0))
            except Exception as e:  # never let one bad line kill the whole doc
                log.debug("insert_text skipped: %s", e)
            y += step
        else:
            y += step * 0.5
        if y > bottom:
            break


def ocr_pdf_bytes(data):
    """Take PDF bytes -> (searchable_pdf_bytes, full_text).

    Raises ValueError (user-facing message) on size/page limits or an unreadable
    PDF; other exceptions propagate for the route to map to a 502.
    """
    mb = len(data) / (1024 * 1024)
    if len(data) > MAX_BYTES:
        raise ValueError(
            f"PDF is {mb:.0f} MB — the limit is {MAX_BYTES // (1024*1024)} MB. "
            "Split it into smaller files and re-drop.")
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:
        raise ValueError(f"Could not open that PDF: {e}")
    if doc.page_count == 0:
        raise ValueError("That PDF has no pages.")
    if doc.page_count > MAX_PAGES:
        raise ValueError(
            f"PDF has {doc.page_count} pages — the limit is {MAX_PAGES}. "
            "Split it and re-drop.")

    log.info("ocr: %d page(s), %.1f MB via %s", doc.page_count, mb, MODEL)
    zoom = DPI / 72.0
    mat = fitz.Matrix(zoom, zoom)
    full = []
    for i, page in enumerate(doc):
        pix = page.get_pixmap(matrix=mat, alpha=False)
        text = _ocr_image_png(pix.tobytes("png"))
        full.append(text)
        if text:
            _embed_invisible_text(page, text)
        log.info("ocr: page %d/%d -> %d chars", i + 1, doc.page_count, len(text))

    out = doc.tobytes(deflate=True, garbage=4)
    doc.close()
    return out, "\n\n".join(full).strip()
