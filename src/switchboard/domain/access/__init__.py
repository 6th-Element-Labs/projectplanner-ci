"""Access domain — identity binding policy without persistence."""
from .identity import (
    binding_for_principal,
    binding_for_registered_agent,
    binding_for_system_actor,
    is_unbound_system_actor,
    shared_token_binding_error,
    validate_system_actor_fields,
)

__all__ = [
    "binding_for_principal",
    "binding_for_registered_agent",
    "binding_for_system_actor",
    "is_unbound_system_actor",
    "shared_token_binding_error",
    "validate_system_actor_fields",
]
