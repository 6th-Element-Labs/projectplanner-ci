"""IXP protocol domain — version negotiation and field aliases (ARCH-MS-43)."""
from switchboard.domain.ixp.protocol import (
    PROTOCOL_ENVELOPE,
    apply_field_aliases,
    check_protocol_compatibility,
    field_aliases_for,
    negotiate_protocol,
    normalize_send_ack_deadline,
    protocol_envelope,
    render_protocol_envelope_json,
)

__all__ = [
    "PROTOCOL_ENVELOPE",
    "apply_field_aliases",
    "check_protocol_compatibility",
    "field_aliases_for",
    "negotiate_protocol",
    "normalize_send_ack_deadline",
    "protocol_envelope",
    "render_protocol_envelope_json",
]
