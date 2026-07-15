"""Reusable service-cut skeleton (ARCH-MS-73).

A dormant FastAPI unit with ``/health``, a contracts/OpenAPI package boundary,
and matching deploy templates under ``deploy/skeleton/``. Copy this package
(rename ``_skeleton`` → the bounded-context name) when cutting Auth/Tasks into
their own uvicorn process. Not mounted into the live monolith.
"""
from __future__ import annotations

from .app import create_app
from .settings import SkeletonSettings

__all__ = ("SkeletonSettings", "create_app")
