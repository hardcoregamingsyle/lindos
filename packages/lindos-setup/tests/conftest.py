"""pytest bootstrap for lindos-setup: make ``lindos_setup`` importable.

The repository-level ``tests/conftest.py`` does the same (plus a ``gi`` stub);
this file keeps the package tests self-contained when run on their own, e.g.
``pytest -q packages/lindos-setup/tests``.  ``lindos_setup.plan`` needs
neither ``gi`` nor ``lindos``.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB = os.path.normpath(os.path.join(_HERE, "..", "root", "usr", "lib", "lindos-setup"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

_CORE = os.path.normpath(os.path.join(_HERE, "..", "..", "lindos-core", "root", "usr", "lib",
                                      "python3", "dist-packages"))
if os.path.isdir(_CORE) and _CORE not in sys.path:
    sys.path.insert(0, _CORE)
