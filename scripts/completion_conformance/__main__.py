"""Entry point for ``python -m scripts.completion_conformance``."""
from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
