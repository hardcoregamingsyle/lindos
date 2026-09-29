"""pytest bootstrap for lindos-setup: make ``lindos_setup`` importable.

The repository-level ``tests/conftest.py`` does the same (plus a ``gi`` stub);
this file keeps the package tests self-contained when run on their own, e.g.
``pytest -q packages/lindos-setup/tests``.  ``lindos_setup.plan`` needs
neither ``gi`` nor ``lindos``.
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB = os.path.normpath(os.path.join(_HERE, "..", "root", "usr", "lib", "lindos-setup"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

_CORE = os.path.normpath(os.path.join(_HERE, "..", "..", "lindos-core", "root", "usr", "lib",
                                      "python3", "dist-packages"))
if os.path.isdir(_CORE) and _CORE not in sys.path:
    sys.path.insert(0, _CORE)


@pytest.fixture(autouse=True)
def _hermetic_host(monkeypatch, tmp_path):
    """No test may see the machine it runs on: an empty system root (so no real
    install-state.json), a scratch home, and a kernel command line that is not a live session."""
    cmdline = tmp_path / "cmdline"
    cmdline.write_text("BOOT_IMAGE=/vmlinuz root=/dev/sda1 ro quiet\n", encoding="utf-8")
    monkeypatch.setenv("LINDOS_TEST_CMDLINE", str(cmdline))
    monkeypatch.setenv("LINDOS_ROOT", str(tmp_path / "sysroot"))
    monkeypatch.setenv("LINDOS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LINDOS_INSTALLER", raising=False)
