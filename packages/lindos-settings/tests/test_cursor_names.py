"""The cursor themes Lindos ships are Lindos-Cursors / Lindos-Cursors-Dark everywhere Settings and lindos.theme
write them; the upstream "Fluent" names only survive as hidden alias themes and are mapped when read."""
from __future__ import annotations

import os

from lindos import theme
from lindos_settings import model
from lindos_settings.backend import Backend

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.normpath(os.path.join(HERE, "..", "root", "usr", "lib", "lindos-settings", "lindos_settings"))


def _source(*parts: str) -> str:
    with open(os.path.join(LIB, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_settings_and_lindos_theme_agree_on_the_shipped_cursor_names() -> None:
    assert (model.CURSOR_DARK, model.CURSOR_LIGHT) == (theme.CURSOR_DARK, theme.CURSOR_LIGHT)
    assert (theme.CURSOR_DARK, theme.CURSOR_LIGHT) == ("Lindos-Cursors-Dark", "Lindos-Cursors")


def test_a_stored_fluent_name_is_shown_as_the_lindos_theme_it_aliases() -> None:
    assert model.canonical_cursor_theme("Fluent-dark-cursors") == "Lindos-Cursors-Dark"
    assert model.canonical_cursor_theme("Fluent-cursors") == "Lindos-Cursors"
    assert model.canonical_cursor_theme("  Lindos-Cursors ") == "Lindos-Cursors"
    assert model.canonical_cursor_theme("Adwaita") == "Adwaita"
    assert model.canonical_cursor_theme("") == ""


def test_the_backend_reports_the_canonical_cursor_theme(monkeypatch) -> None:
    b = Backend()
    monkeypatch.setattr(b, "xfconf_get", lambda channel, prop: "Fluent-dark-cursors")
    assert b.cursor_theme() == "Lindos-Cursors-Dark"
    monkeypatch.setattr(b, "xfconf_get", lambda channel, prop: None)
    assert b.cursor_theme() == ""


def test_the_fallback_theme_switch_writes_the_lindos_cursor_names(monkeypatch) -> None:
    """Without lindos.theme the backend sets the theme itself - it must not bring the Fluent name back."""
    b = Backend()
    written = {}
    monkeypatch.setattr(b, "_theme", lambda fn, *args: None)
    monkeypatch.setattr(b, "xfconf_set", lambda channel, prop, value, *a, **k: written.__setitem__((channel, prop), value) or True)
    monkeypatch.setattr(b, "config_set", lambda key, value: True)
    b.set_dark(True)
    assert written[("xsettings", "/Gtk/CursorThemeName")] == "Lindos-Cursors-Dark"
    b.set_dark(False)
    assert written[("xsettings", "/Gtk/CursorThemeName")] == "Lindos-Cursors"


def test_no_settings_text_or_code_still_asks_for_the_fluent_cursor_names() -> None:
    for name in ("backend.py", os.path.join("pages", "personalization.py")):
        text = _source(name)
        assert "Fluent-dark-cursors" not in text and "Fluent-cursors" not in text, name
    model_text = _source("model.py")
    assert model_text.count("Fluent-cursors") == 1 and model_text.count("Fluent-dark-cursors") == 1   # the alias map only
