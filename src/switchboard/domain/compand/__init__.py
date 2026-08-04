"""Compand gateway domain primitives."""

from .gateway import (
    ClientCredentialRegistry,
    CredentialAuthentication,
    GatewayMode,
    GatewaySecurityError,
    validate_upstream_origin,
)
from .scan import (
    LineRleCandidate,
    ScanEligibilityError,
    build_line_rle_candidate,
    decode_line_rle,
    encode_line_rle,
)

__all__ = [
    "ClientCredentialRegistry",
    "CredentialAuthentication",
    "GatewayMode",
    "GatewaySecurityError",
    "validate_upstream_origin",
    "LineRleCandidate",
    "ScanEligibilityError",
    "build_line_rle_candidate",
    "decode_line_rle",
    "encode_line_rle",
]
