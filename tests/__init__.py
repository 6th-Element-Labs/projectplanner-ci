"""Switchboard's script-style tests and their shared import bootstrap.

CI executes files directly, where ``tests/`` is the import root. The alias also
keeps those same files importable as ``tests.test_*`` package modules.
"""
from __future__ import annotations

import sys

from . import path_setup as _path_setup


sys.modules.setdefault("path_setup", _path_setup)
