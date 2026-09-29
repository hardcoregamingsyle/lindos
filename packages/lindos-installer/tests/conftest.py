"""pytest bootstrap for lindos-installer: puts the test harness on sys.path (pytest runs with
--import-mode=importlib, which does not) and provides the ``sandbox`` fixture."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.append(_HERE)   # appended, not prepended: never shadow other packages' modules

from installer_testlib import Sandbox  # noqa: E402  (after the sys.path tweak on purpose)


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)
