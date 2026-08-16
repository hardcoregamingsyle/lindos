"""pytest bootstrap for lindos-winapps.

* Puts ``root/usr/lib/lindos-winapps`` (``import lindos_winapps``) and lindos-core's
  ``dist-packages`` (``import lindos``) on ``sys.path`` so the tests run from the source tree.
* ``home`` fixture: ``LINDOS_HOME`` / ``LINDOS_ROOT`` -> scratch directories (SPEC §4.1),
  and clears ``RDP_PASS`` so credential tests are deterministic.
* ``staged_catalog`` fixture: copies the shipped ``apps.json`` into the faked ``LINDOS_ROOT``
  tree so ``catalog_path()`` resolves to it (mirrors an installed system).

Everything works on Windows/macOS: no libvirt, no FreeRDP, no Linux calls.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pytest

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parent                                        # packages/lindos-winapps
ROOT = PKG_ROOT / "root"
BIN = ROOT / "usr" / "bin"
LIB = ROOT / "usr" / "lib" / "lindos-winapps"
SHARE = ROOT / "usr" / "share" / "lindos" / "winapps"
APPS = ROOT / "usr" / "share" / "applications"
DEBIAN = PKG_ROOT / "DEBIAN"
CATALOG = SHARE / "apps.json"
CORE_LIB = PKG_ROOT.parent / "lindos-core" / "root" / "usr" / "lib" / "python3" / "dist-packages"

for _p in (LIB, CORE_LIB):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Scratch ``LINDOS_HOME`` (and ``LINDOS_ROOT``); returns the home path."""
    h = tmp_path / "home"
    r = tmp_path / "root"
    h.mkdir()
    r.mkdir()
    monkeypatch.setenv("LINDOS_HOME", str(h))
    monkeypatch.setenv("LINDOS_ROOT", str(r))
    monkeypatch.delenv("RDP_PASS", raising=False)
    monkeypatch.delenv("LINDOS_DEBUG", raising=False)
    return h


@pytest.fixture()
def root_tree(tmp_path: Path) -> Path:
    return tmp_path / "root"


@pytest.fixture()
def staged_catalog(home: Path, root_tree: Path) -> Path:
    """Copy the shipped apps.json into ``$LINDOS_ROOT/usr/share/lindos/winapps/apps.json``."""
    dst = root_tree / "usr" / "share" / "lindos" / "winapps" / "apps.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CATALOG, dst)
    return dst


@pytest.fixture()
def fake_which() -> Callable[..., Callable[[str], Optional[str]]]:
    """Factory: ``fake_which("virsh", "xfreerdp3")`` -> a ``which`` that knows only those tools."""

    def factory(*names: str, prefix: str = "/usr/bin") -> Callable[[str], Optional[str]]:
        table = {n: f"{prefix}/{n}" for n in names}

        def which(name: str) -> Optional[str]:
            return table.get(name)

        return which

    return factory


class FakeRun:
    """Records ``run(argv, ...)`` calls; ``outputs``/``codes`` map a basename to (stdout, rc)."""

    def __init__(self) -> None:
        self.calls: List[List[str]] = []
        self.responses: Dict[str, tuple] = {}   # subcommand token -> (stdout, returncode)

    def set(self, key: str, stdout: str = "", rc: int = 0) -> None:
        self.responses[key] = (stdout, rc)

    def __call__(self, argv, *args, **kwargs):
        import types
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        stdout, rc = "", 0
        for token in argv:
            if token in self.responses:
                stdout, rc = self.responses[token]
                break
        return types.SimpleNamespace(returncode=rc, stdout=stdout, stderr="", args=argv)

    def has(self, *tokens: str) -> bool:
        import os
        def hit(argv, token):
            return any(a == token or os.path.basename(a) == token for a in argv)
        return any(all(hit(c, t) for t in tokens) for c in self.calls)


@pytest.fixture()
def fake_run() -> FakeRun:
    return FakeRun()
