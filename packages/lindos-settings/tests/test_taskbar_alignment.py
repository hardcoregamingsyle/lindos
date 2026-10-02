"""Settings only offers what the taskbar can do: without xfce4-docklike-plugin (not packaged for Ubuntu 24.04) the
task list that stands in for it fills the bar, so "centre" is not available and the pages say so."""
from __future__ import annotations

import os

import pytest

from lindos_settings import model
from lindos_settings.backend import Backend

HERE = os.path.dirname(os.path.abspath(__file__))
PAGES = os.path.normpath(os.path.join(HERE, "..", "root", "usr", "lib", "lindos-settings", "lindos_settings", "pages"))


def _root_with_docklike(tmp_path, present: bool):
    plugins = tmp_path / "usr" / "share" / "xfce4" / "panel" / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    if present:
        (plugins / "docklike.desktop").write_text("[Xfce Panel]\nName=Docklike Taskbar\n", encoding="utf-8")
    return tmp_path


def test_centring_is_offered_only_when_docklike_is_installed(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    b = Backend()
    monkeypatch.setattr(b, "config_get", lambda key, default=None: "center")
    monkeypatch.setenv("LINDOS_ROOT", str(_root_with_docklike(tmp_path / "with", True)))
    assert b.taskbar_can_centre() is True and b.taskbar_alignment() == "center"
    monkeypatch.setenv("LINDOS_ROOT", str(_root_with_docklike(tmp_path / "without", False)))
    assert b.taskbar_can_centre() is False
    assert b.taskbar_alignment() == "left"          # what the bar really is, whatever the stored preference says


def test_the_alignment_hint_is_honest_about_what_this_system_can_do() -> None:
    assert "Centre" in model.taskbar_alignment_hint(True) and "Docklike" not in model.taskbar_alignment_hint(True)
    without = model.taskbar_alignment_hint(False)
    assert "Docklike" in without and without.startswith("Left")


def test_both_pages_use_the_hint_instead_of_a_fixed_promise() -> None:
    for name in ("home.py", "personalization.py"):
        with open(os.path.join(PAGES, name), encoding="utf-8") as fh:
            text = fh.read()
        assert "taskbar_alignment_hint(b.taskbar_can_centre())" in text, name
