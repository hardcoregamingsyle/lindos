"""pytest bootstrap for lindos-core (runs on Windows/macOS/Linux, no GTK, no root).

* makes ``lindos`` importable from ``root/usr/lib/python3/dist-packages``;
* ``core_env`` fixture: scratch ``LINDOS_ROOT`` / ``LINDOS_HOME`` with the shipped mode
  definitions copied in, ``LINDOS_HELPER`` pointing at the in-tree helper and
  ``LINDOS_HELPER_DRYRUN=1`` so nothing privileged ever runs;
* ``run_cli`` fixture: run one of the ``root/usr/bin`` scripts (or the helper) through the
  current interpreter and return ``CompletedProcess``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pytest

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parent                                   # packages/lindos-core
ROOT = PKG_ROOT / "root"
LIB = ROOT / "usr" / "lib" / "python3" / "dist-packages"
BIN = ROOT / "usr" / "bin"
LIBEXEC = ROOT / "usr" / "libexec" / "lindos"
HELPER = LIBEXEC / "lindos-helper"
MODES = ROOT / "usr" / "share" / "lindos" / "modes"
DEBIAN = PKG_ROOT / "DEBIAN"
META_DEBIAN = PKG_ROOT.parent / "lindos-meta" / "DEBIAN"

if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

# never let a developer's real config leak into the tests
os.environ.setdefault("LINDOS_HELPER_DRYRUN", "1")


@pytest.fixture(scope="session")
def pkg_root() -> Path:
    return PKG_ROOT


@pytest.fixture(scope="session")
def root_dir() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def modes_dir() -> Path:
    return MODES


@pytest.fixture(scope="session")
def helper_path() -> Path:
    return HELPER


@pytest.fixture()
def core_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Path]:
    """Scratch LINDOS_ROOT/LINDOS_HOME with the shipped modes; helper in dry-run."""
    root = tmp_path / "root"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    dest = root / "usr" / "share" / "lindos" / "modes"
    shutil.copytree(MODES, dest)
    monkeypatch.setenv("LINDOS_ROOT", str(root))
    monkeypatch.setenv("LINDOS_HOME", str(home))
    monkeypatch.setenv("LINDOS_HELPER", str(HELPER))
    monkeypatch.setenv("LINDOS_HELPER_DRYRUN", "1")
    monkeypatch.setenv("LINDOS_FORCE_OFFLINE", "1")
    monkeypatch.delenv("LINDOS_HELPER_ONLINE", raising=False)
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    return {"root": root, "home": home, "modes": dest, "tmp": tmp_path}


@pytest.fixture()
def run_cli() -> Callable[..., subprocess.CompletedProcess]:
    """``run_cli("lindos-mode", "list", "--json", env=..., input=...)``."""

    def _run(name: str, *args: str, env: Optional[Dict[str, str]] = None, input: Optional[str] = None,
             timeout: float = 120) -> subprocess.CompletedProcess:
        script = HELPER if name == "lindos-helper" else BIN / name
        assert script.is_file(), script
        full_env = dict(os.environ)
        full_env["PYTHONIOENCODING"] = "utf-8"
        full_env["PYTHONPATH"] = str(LIB) + os.pathsep + full_env.get("PYTHONPATH", "")
        if env:
            full_env.update(env)
        cmd: List[str] = [sys.executable, str(script), *args]
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env=full_env, input=input, timeout=timeout, check=False)

    return _run


def bash_available() -> Optional[str]:
    return shutil.which("bash")
