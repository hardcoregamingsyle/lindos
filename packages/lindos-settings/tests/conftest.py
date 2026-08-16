"""pytest bootstrap for lindos-settings: make ``lindos_settings`` importable.

The repository-level ``tests/conftest.py`` does the same (plus a ``gi`` stub); this file keeps
the package tests self-contained when run on their own, e.g.
``pytest -q packages/lindos-settings/tests``.  ``lindos_settings.model`` and
``lindos_settings.backend`` need neither ``gi`` nor ``lindos``.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB = os.path.normpath(os.path.join(_HERE, "..", "root", "usr", "lib", "lindos-settings"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

_CORE = os.path.normpath(os.path.join(_HERE, "..", "..", "lindos-core", "root", "usr", "lib", "python3", "dist-packages"))
if os.path.isdir(_CORE) and _CORE not in sys.path:
    sys.path.insert(0, _CORE)

_REPO_TESTS = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "tests"))
if os.path.isdir(_REPO_TESTS) and _REPO_TESTS not in sys.path:
    sys.path.insert(0, _REPO_TESTS)
try:  # gi stub for the widget/page import smoke tests (only when real gi is missing)
    import lindos_testsupport  # noqa: F401
except Exception:  # pragma: no cover - repo tests dir missing
    pass
