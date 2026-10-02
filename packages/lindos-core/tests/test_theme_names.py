"""lindos.theme writes the names of the themes Lindos actually ships (cursors, accents) - the same ones the
packaged defaults in lindos-desktop use - so finishing Setup or toggling dark/light never brings the upstream
"Fluent" or "Mint" names back into a user's configuration."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from lindos import theme

DESKTOP = Path(__file__).resolve().parents[2] / "lindos-desktop" / "root"


def _ini(path: Path, key: str) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise AssertionError("%s not found in %s" % (key, path))


def test_the_default_cursor_themes_are_the_lindos_ones() -> None:
    assert theme.CURSOR_DARK == "Lindos-Cursors-Dark" and theme.CURSOR_LIGHT == "Lindos-Cursors"
    assert "Fluent" not in theme.CURSOR_DARK + theme.CURSOR_LIGHT


def test_lindos_theme_and_the_packaged_defaults_name_the_same_dark_cursor_theme() -> None:
    xsettings = (DESKTOP / "etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml").read_text(encoding="utf-8")
    assert 'name="CursorThemeName" type="string" value="%s"' % theme.CURSOR_DARK in xsettings
    assert _ini(DESKTOP / "etc/xdg/gtk-3.0/settings.ini", "gtk-cursor-theme-name") == theme.CURSOR_DARK
    assert _ini(DESKTOP / "etc/lightdm/slick-greeter.conf", "cursor-theme-name") == theme.CURSOR_DARK


def test_set_dark_writes_the_lindos_cursor_names(core_env: Dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Tuple[str, str, str]] = []
    monkeypatch.setattr(theme.shutil, "which", lambda name, *a, **k: None)     # no gsettings on the runner
    monkeypatch.setattr(theme, "xfconf_available", lambda: True)
    monkeypatch.setattr(theme, "xfconf_set", lambda channel, prop, value, vtype="string": calls.append((channel, prop, value)) or True)
    ini = core_env["home"] / ".config" / "gtk-4.0" / "settings.ini"

    assert theme.set_dark(False) is True
    assert _ini(ini, "gtk-cursor-theme-name") == "Lindos-Cursors"
    assert ("xsettings", "/Gtk/CursorThemeName", "Lindos-Cursors") in calls
    assert theme.set_dark(True) is True
    assert _ini(ini, "gtk-cursor-theme-name") == "Lindos-Cursors-Dark"
    assert ("xsettings", "/Gtk/CursorThemeName", "Lindos-Cursors-Dark") in calls
    assert not [c for c in calls if "Fluent" in c[2]]


def test_no_accent_swatch_is_named_after_the_base_distribution() -> None:
    names = [name for name, _ in theme.ACCENTS]
    assert "Meadow Green" in names and not [n for n in names if "mint" in n.lower()]
    assert len({hx for _, hx in theme.ACCENTS}) == len(theme.ACCENTS)
