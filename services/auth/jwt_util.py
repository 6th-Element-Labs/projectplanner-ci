"""Minimal HS256 JWT (stdlib only) — matches ActionEngine's taikun_session token shape.

We avoid a PyJWT dependency: a JWT is just base64url(header).base64url(payload).base64url(sig)
with an HMAC-SHA256 signature. Constant-time compare, exp enforced on decode.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional, Tuple

_HEADER = {"alg": "HS256", "typ": "JWT"}


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(seg: str) -> bytes:
    pad = "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg + pad)


def encode(payload: Dict[str, Any], secret: str) -> str:
    """Sign a payload. Caller sets iat/exp (seconds since epoch)."""
    header_seg = _b64u_encode(json.dumps(_HEADER, separators=(",", ":"), sort_keys=True).encode())
    payload_seg = _b64u_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signing_input = f"{header_seg}.{payload_seg}".encode("ascii")
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header_seg}.{payload_seg}.{_b64u_encode(sig)}"


def decode(token: str, secret: str, *, now: Optional[float] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    """Verify signature + expiry. Returns (payload, "") on success or (None, reason).

    Never raises — invalid/expired tokens come back as (None, reason) so callers
    can treat every failure as an unauthenticated request.
    """
    try:
        header_seg, payload_seg, sig_seg = (token or "").split(".")
    except ValueError:
        return None, "malformed token"
    signing_input = f"{header_seg}.{payload_seg}".encode("ascii")
    expected = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    try:
        actual = _b64u_decode(sig_seg)
    except Exception:
        return None, "malformed signature"
    if not hmac.compare_digest(expected, actual):
        return None, "bad signature"
    try:
        payload = json.loads(_b64u_decode(payload_seg))
    except Exception:
        return None, "malformed payload"
    exp = payload.get("exp")
    if exp is not None and (now or time.time()) >= float(exp):
        return None, "expired"
    return payload, ""
