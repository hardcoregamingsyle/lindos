"""pytest bootstrap for lindos-gaming.

The gaming CLIs live in ``root/usr/bin`` without a ``.py`` suffix, so they are
loaded through ``importlib`` (see ``gaming_testlib.load_bin``) and exposed as
fixtures.  Nothing needs GTK or Linux: every module is importable on
Windows/macOS (SPEC §4/§12).

Also makes ``lindos`` (lindos-core) importable straight from the source tree
when it exists, mirroring the repository-level ``tests/conftest.py``, and puts
this directory on ``sys.path`` so test modules can ``import gaming_testlib``
whatever pytest's import mode is.
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from typing import Any, Dict

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.append(_HERE)   # appended, not prepended: never shadow other packages' `conftest`

from gaming_testlib import (  # noqa: E402  (after sys.path tweak on purpose)
    LIBEXEC,
    PKG_ROOT,
    ROOT,
    SHARE,
    load_bin,
    read_json,
)


# ----------------------------------------------------------------------------- #
# fixtures
# ----------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def pkg_root() -> Path:
    return PKG_ROOT


@pytest.fixture(scope="session")
def root_dir() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def proton_mod() -> types.ModuleType:
    return load_bin("lindos-proton")


@pytest.fixture(scope="session")
def drivers_mod() -> types.ModuleType:
    return load_bin("lindos-drivers")


@pytest.fixture(scope="session")
def game_mod() -> types.ModuleType:
    return load_bin("lindos-game")


@pytest.fixture(scope="session")
def mangohud_mod() -> types.ModuleType:
    return load_bin("lindos-mangohud")


@pytest.fixture(scope="session")
def compat_matrix() -> Dict[str, Any]:
    return read_json(SHARE / "compat-matrix.json")


@pytest.fixture(scope="session")
def launchers_catalogue() -> Dict[str, Any]:
    return read_json(SHARE / "gaming" / "launchers.json")


@pytest.fixture(scope="session")
def install_script_text() -> str:
    return (LIBEXEC / "install-gaming.sh").read_text(encoding="utf-8")


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated $HOME for lindos-proton / lindos-mangohud (LINDOS_HOME)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("LINDOS_HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("LINDOS_ROOT", str(tmp_path / "root"))
    return home
