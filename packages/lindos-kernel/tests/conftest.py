"""pytest bootstrap for lindos-kernel (runs on Windows/macOS/Linux, no root, no real kernel).

* makes ``lindos_kernel`` importable from ``root/usr/lib/lindos-kernel``;
* ``fake_root`` fixture: a scratch ``LINDOS_ROOT`` tree with the shipped ``manifest.json`` and
  ``lindos.config`` copied in, plus helpers to plant a faked ``/proc`` + ``/sys`` + ``/boot`` +
  ``/dev`` so the feature probe can be exercised off Linux;
* ``run_cli`` fixture: run ``root/usr/bin/lindos-kernel`` through the current interpreter.
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
PKG_ROOT = HERE.parent                                        # packages/lindos-kernel
ROOT = PKG_ROOT / "root"
LIB = ROOT / "usr" / "lib" / "lindos-kernel"
BIN = ROOT / "usr" / "bin"
SHARE = ROOT / "usr" / "share" / "lindos" / "kernel"
MANIFEST = SHARE / "manifest.json"
CONFIG = SHARE / "lindos.config"
GRUB_DROPIN = ROOT / "etc" / "default" / "grub.d" / "50-lindos.cfg"

if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))


@pytest.fixture(scope="session")
def pkg_root() -> Path:
    return PKG_ROOT


@pytest.fixture(scope="session")
def shipped_manifest() -> Path:
    return MANIFEST


@pytest.fixture(scope="session")
def shipped_config() -> Path:
    return CONFIG


@pytest.fixture(scope="session")
def shipped_dropin() -> Path:
    return GRUB_DROPIN


@pytest.fixture()
def fake_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, object]:
    """Scratch LINDOS_ROOT with the shipped data files; ``plant`` builds a faked probe tree."""
    root = tmp_path / "root"
    kern = root / "usr" / "share" / "lindos" / "kernel"
    kern.mkdir(parents=True)
    shutil.copy2(MANIFEST, kern / "manifest.json")
    shutil.copy2(CONFIG, kern / "lindos.config")
    monkeypatch.setenv("LINDOS_ROOT", str(root))
    monkeypatch.delenv("LINDOS_KERNEL_RELEASE", raising=False)

    def write(rel: str, text: str, mode: str = "w") -> Path:
        target = root / rel.lstrip("/\\")
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, mode, encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        return target

    def touch(rel: str) -> Path:
        target = root / rel.lstrip("/\\")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
        return target

    def mkdir(rel: str) -> Path:
        target = root / rel.lstrip("/\\")
        target.mkdir(parents=True, exist_ok=True)
        return target

    return {"root": root, "write": write, "touch": touch, "mkdir": mkdir, "tmp": tmp_path}


@pytest.fixture()
def run_cli() -> Callable[..., subprocess.CompletedProcess]:
    """``run_cli("status", "--json", env=...)`` → CompletedProcess for /usr/bin/lindos-kernel."""

    def _run(*args: str, env: Optional[Dict[str, str]] = None,
             timeout: float = 120) -> subprocess.CompletedProcess:
        script = BIN / "lindos-kernel"
        assert script.is_file(), script
        full_env = dict(os.environ)
        full_env["PYTHONIOENCODING"] = "utf-8"
        full_env["PYTHONPATH"] = str(LIB) + os.pathsep + full_env.get("PYTHONPATH", "")
        if env:
            full_env.update(env)
        cmd: List[str] = [sys.executable, str(script), *args]
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=full_env, timeout=timeout, check=False)

    return _run
