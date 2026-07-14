"""Thin FastAPI entrypoint (ARCH-MS-45). Implementation lives in app_impl.py."""
from __future__ import annotations

import app_impl as _impl

app = _impl.app


def __getattr__(name: str):
    return getattr(_impl, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_impl)))


if __name__ == "__main__":
    import os
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PM_PORT", "8110")))
