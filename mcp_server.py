"""Thin MCP entrypoint (ARCH-MS-45). Implementation lives in mcp_server_impl.py."""
from __future__ import annotations

import mcp_server_impl as _impl

mcp = _impl.mcp


def __getattr__(name: str):
    return getattr(_impl, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_impl)))


if __name__ == "__main__":
    import runpy
    runpy.run_module("mcp_server_impl", run_name="__main__")
