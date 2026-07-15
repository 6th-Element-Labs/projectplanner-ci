"""Versioned wire contracts for a cut-out service (ARCH-MS-73).

Own OpenAPI/Pydantic schemas live here — not in the monolith
``switchboard.contracts`` tree — so a process cut keeps a clear package
boundary. Clone this package when extracting Auth/Tasks.
"""
from . import openapi, v1
from .openapi import build_openapi_document

__all__ = ("build_openapi_document", "openapi", "v1")
