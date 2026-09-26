"""Package-level sanity for ``lindos-transfer``: ``DEBIAN/control`` matches what the package
actually ships (SPEC-WINDOWS §29.1).

``gui.py`` does a plain, unguarded ``import gi`` at module scope (its own docstring says so
deliberately -- see ``root/usr/bin/lindos-transfer-gui``'s ImportError handler, which names
``python3-gi`` to the user).  On real Debian/Ubuntu, ``gir1.2-gtk-3.0`` only ships the
GObject-Introspection typelib data; the ``gi`` Python module itself comes from the separate
``python3-gi`` package.  This file is a permanent regression test for that gap: it fails the build
again if ``python3-gi`` is ever dropped from ``DEBIAN/control`` while ``gui.py`` still imports
``gi`` unconditionally.
"""
from __future__ import annotations

from pathlib import Path

# Paths computed here (not imported from conftest) so this module also loads under
# pytest's --import-mode=importlib used by tests/run.sh.
PKG_ROOT = Path(__file__).resolve().parent.parent          # packages/lindos-transfer
ROOT = PKG_ROOT / "root"
LIB = ROOT / "usr" / "lib" / "lindos-transfer" / "lindos_transfer"
DEBIAN = PKG_ROOT / "DEBIAN"


def _control() -> dict:
    fields: dict = {}
    for line in (DEBIAN / "control").read_text(encoding="utf-8").splitlines():
        if line[:1].isspace() or not line.strip():
            continue
        k, v = line.split(":", 1)
        fields[k.strip()] = v.strip()
    return fields


def test_control_fields() -> None:
    c = _control()
    assert c["Package"] == "lindos-transfer"
    assert c["Architecture"] == "all"
    assert c["Maintainer"] == "Lindos Team <team@lindos.dev>"
    for dep in ("python3", "lindos-core"):
        assert dep in c["Depends"], dep
    for rec in ("udisks2", "ntfs-3g", "rsync", "zenity", "network-manager", "python3-gi",
               "gir1.2-gtk-3.0", "fontconfig", "xdg-user-dirs"):
        assert rec in c["Recommends"], rec
    assert not (DEBIAN / "conffiles").exists()


def test_gui_module_needs_python3_gi_and_control_declares_it() -> None:
    """``gui.py`` imports ``gi`` unconditionally; whatever package that needs must be declared
    somewhere in ``DEBIAN/control`` (Depends or Recommends), not merely the typelib package."""
    gui_src = (LIB / "gui.py").read_text(encoding="utf-8")
    assert "\nimport gi\n" in gui_src, "gui.py no longer imports gi the way this test expects"
    c = _control()
    declared = f"{c.get('Depends', '')}, {c.get('Recommends', '')}, {c.get('Suggests', '')}"
    assert "python3-gi" in declared, (
        "gui.py imports gi, but DEBIAN/control never declares python3-gi (gir1.2-gtk-3.0 alone "
        "does not provide the Python 'gi' module on Debian/Ubuntu)"
    )
