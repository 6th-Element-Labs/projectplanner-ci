"""HS256 JWT via PyJWT — matches ActionEngine's taikun_session token shape.

PyJWT is already a dependency (pulled in by ``mcp``), so signing/verification is
delegated to it rather than hand-rolled. The public contract is unchanged:
``encode`` returns a compact token; ``decode`` returns ``(payload, "")`` on success
or ``(None, reason)`` on any failure and never raises, so callers can treat every
failure as an unauthenticated request. Expiry is enforced on decode.
"""
from __future__ import annotations

import time
import warnings
from typing import Any, Dict, Optional, Tuple

import jwt
from jwt.warnings import InsecureKeyLengthWarning

_ALG = "HS256"

# PyJWT nudges when the HMAC secret is < 32 bytes. Whether the deployment's
# PM_JWT_SECRET / PM_AUTH_TOKEN is long enough is an ops concern surfaced once at
# provisioning, not something to re-log on every per-request encode/decode — so
# silence this advisory at the token boundary to avoid flooding prod logs.
warnings.filterwarnings("ignore", category=InsecureKeyLengthWarning)


def encode(payload: Dict[str, Any], secret: str) -> str:
    """Sign a payload. Caller sets iat/exp (seconds since epoch)."""
    return jwt.encode(payload, secret, algorithm=_ALG)


def decode(token: str, secret: str, *, now: Optional[float] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    """Verify signature + expiry. Returns (payload, "") on success or (None, reason).

    Never raises — invalid/expired tokens come back as (None, reason) so callers
    can treat every failure as an unauthenticated request. Signature is verified by
    PyJWT; expiry is enforced here so an explicit ``now`` (tests) and the legacy
    "exp is optional" behavior are both preserved.
    """
    if not token:
        return None, "malformed token"
    try:
        payload = jwt.decode(token, secret, algorithms=[_ALG],
                             options={"verify_exp": False})
    except jwt.InvalidSignatureError:
        return None, "bad signature"
    except jwt.DecodeError:
        return None, "malformed token"
    except jwt.InvalidTokenError:
        return None, "invalid token"
    if not isinstance(payload, dict):
        return None, "malformed payload"
    exp = payload.get("exp")
    if exp is not None:
        try:
            if (now if now is not None else time.time()) >= float(exp):
                return None, "expired"
        except (TypeError, ValueError):
            return None, "malformed payload"
    return payload, ""
