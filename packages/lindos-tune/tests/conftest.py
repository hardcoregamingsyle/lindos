"""pytest bootstrap for lindos-tune (SPEC §12).

* Makes ``lindos_tune`` importable straight from ``root/usr/lib/lindos-tune`` (and lindos-core's
  ``lindos`` from the sibling package when present) — nothing needs Linux, root or GTK.
* ``staging`` fixture: a scratch ``LINDOS_ROOT`` tree pre-populated with the files this package
  ships (whitelist, autostart-hide list, tune.d confs, preset, sysctl base, journald drop-in)
  so tests exercise the real data files.  Escalation is disabled and chroot mode forced.
* ``recorder`` / ``make_ctx``: an injectable runner that records every argv and answers from a
  table, so no real ``systemctl``/``sensors``/``nbfc`` is ever executed.

Helpers live in ``tune_testlib.py`` (unique module name; several ``packages/*/tests`` trees are
collected in one pytest run).
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import tune_testlib  # noqa: E402  (after the sys.path tweak on purpose)
from tune_testlib import ROOT, SHARE, SHIPPED_FILES, Recorder  # noqa: E402

from lindos_tune import common  # noqa: E402
from lindos_tune.common import Context  # noqa: E402


@pytest.fixture(scope="session")
def pkg_root() -> Path:
    return tune_testlib.PKG_ROOT


@pytest.fixture(scope="session")
def share_dir() -> Path:
    return SHARE


@pytest.fixture()
def staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Scratch ``LINDOS_ROOT`` with the shipped data files; escalation disabled; chroot forced."""
    root = tmp_path / "root"
    root.mkdir()
    for system_path in SHIPPED_FILES:
        src = ROOT / system_path.lstrip("/")
        if src.exists():
            dst = Path(common.path(system_path, str(root)))
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
    monkeypatch.setenv("LINDOS_ROOT", str(root))
    monkeypatch.setenv("LINDOS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LINDOS_CHROOT", "1")
    monkeypatch.setenv("LINDOS_TUNE_NO_ESCALATE", "1")
    monkeypatch.setenv("LINDOS_HELPER_DRYRUN", "1")
    return root


@pytest.fixture()
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture()
def make_ctx(staging: Path, recorder: Recorder) -> Callable[..., Context]:
    """``make_ctx(dry_run=False, offline=False, tools=("systemctl",), runner=None, **overrides)``"""

    def _make(dry_run: bool = False, offline: bool = False, tools: Sequence[str] = ("systemctl",),
              runner: Optional[Recorder] = None, **overrides: object) -> Context:
        tool_set = set(tools)
        ctx = Context(root_dir=str(staging), dry_run=dry_run, offline=offline, chroot=True,
                      runner=runner or recorder,
                      which_fn=lambda name: f"/usr/bin/{name}" if name in tool_set else None)
        ctx.overrides.update(overrides)
        return ctx

    return _make
