"""Audio/video transcription via the shared LLM gateway (OpenAI Whisper).

Mirrors rag.py: the app talks to the bundled LiteLLM gateway with the logical model
name `taikun-transcribe`, so it never holds the OpenAI key and the model can be swapped
in deploy/gateway/config.yaml without touching the app. The resulting transcript flows
straight into intake.ingest_and_triage -> rag.add_document, i.e. into the corpus.
"""
import logging
import os

import httpx

log = logging.getLogger("transcribe")

BASE = os.environ.get("PM_LLM_BASE_URL", "http://127.0.0.1:8095/v1")
KEY = os.environ.get("PM_LLM_KEY") or os.environ.get("LLM_GATEWAY_MASTER_KEY", "")
MODEL = os.environ.get("PM_TRANSCRIBE_MODEL", "taikun-transcribe")
MAX_BYTES = 25 * 1024 * 1024  # OpenAI per-file transcription limit

MEDIA_EXT = (".m4a", ".mp3", ".mp4", ".wav", ".webm", ".mov", ".m4v",
             ".aac", ".ogg", ".oga", ".flac", ".mpeg", ".mpga", ".amr")


def is_media(filename, content_type=None):
    fn = (filename or "").lower()
    ct = (content_type or "").lower()
    return fn.endswith(MEDIA_EXT) or ct.startswith("audio/") or ct.startswith("video/")


def transcribe(filename, data, content_type=None):
    """Return transcript text for an audio/video blob. Raises ValueError with a
    user-facing message on size limit; raises on gateway/HTTP errors."""
    mb = len(data) / (1024 * 1024)
    if len(data) > MAX_BYTES:
        raise ValueError(
            f"{filename or 'file'} is {mb:.0f} MB — OpenAI's transcription limit is 25 MB. "
            "Split or compress it (e.g. export audio-only / lower bitrate) and re-drop.")
    log.info("transcribe: %s (%.1f MB) via %s", filename, mb, MODEL)
    r = httpx.post(
        f"{BASE}/audio/transcriptions",
        headers={"Authorization": f"Bearer {KEY}"},
        data={"model": MODEL, "response_format": "text"},
        files={"file": (filename or "audio", data, content_type or "application/octet-stream")},
        timeout=600,
    )
    r.raise_for_status()
    txt = (r.text or "").strip()
    # response_format=text returns raw text; some gateways still wrap it in JSON.
    if txt.startswith("{"):
        try:
            txt = (r.json().get("text") or txt).strip()
        except Exception:
            pass
    return txt
