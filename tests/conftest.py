"""Repository-level pytest bootstrap (SPEC §12).

* Puts the in-repo library directories on ``sys.path``:
  ``packages/lindos-core/root/usr/lib/python3/dist-packages``,
  ``packages/lindos-setup/root/usr/lib/lindos-setup``,
  ``packages/lindos-settings/root/usr/lib/lindos-settings``,
  ``packages/lindos-compat/root/usr/lib/lindos-compat``,
  ``packages/lindos-tune/root/usr/lib/lindos-tune`` (and any other
  ``packages/*/root/usr/lib/lindos-*``).
* Installs a stub ``gi`` (``gi.require_version``, ``gi.repository.Gtk/Gdk/
  GLib/Gio/Pango/GdkPixbuf`` … as permissive dummies) **only if the real
  PyGObject is missing**.

The heavy lifting lives in ``tests/lindos_testsupport.py`` so that
``tests/run.sh`` can also load it as a rootdir-independent plugin
(``-p lindos_testsupport``) for ``packages/*/tests`` and ``build/tests``.
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import lindos_testsupport  # noqa: E402  (needs the sys.path tweak above)

lindos_testsupport.install()

REPO_ROOT = lindos_testsupport.REPO_ROOT


@pytest.fixture(scope="session")
def repo_root() -> str:
    """Absolute path of the repository root."""
    return REPO_ROOT


@pytest.fixture(scope="session")
def docs_dir() -> str:
    return os.path.join(REPO_ROOT, "docs")


@pytest.fixture(scope="session")
def packages_dir() -> str:
    return os.path.join(REPO_ROOT, "packages")


@pytest.fixture(scope="session")
def gi_stub_active() -> bool:
    """True when tests run against the permissive ``gi`` stub."""
    return lindos_testsupport.gi_is_stub()


@pytest.fixture()
def lindos_env(tmp_path, monkeypatch):
    """Point lindos-core at a scratch tree (SPEC §4.1: LINDOS_ROOT/LINDOS_HOME).

    Yields ``(root, home)`` paths.  Safe on any OS.
    """
    root = tmp_path / "root"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    monkeypatch.setenv("LINDOS_ROOT", str(root))
    monkeypatch.setenv("LINDOS_HOME", str(home))
    return root, home
