"""Shared test setup: allow imports without deployment secrets.

`constant` raises OSError when required env vars are absent; stub the module
so database/monitor imports work in collection/probe environments.
"""

import sys
from types import ModuleType

try:
    import constant  # noqa: F401
except OSError:
    _stub = ModuleType("constant")
    _stub.CHECK_INTERVAL = 1
    _stub.DB_CONFIG = {}
    _stub.DEVICE = "unused"
    _stub.LINE_11 = {}
    _stub.LINE_12 = {}
    _stub.LINE_TESTING = {}
    sys.modules["constant"] = _stub
