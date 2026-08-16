"""Catalog loading and .desktop generation (SPEC-VM §22, §26)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lindos_winapps import apps as apps_mod
from lindos_winapps import catalog_path, desktop_path

# Computed locally (not imported from conftest) so this module loads under
# pytest's --import-mode=importlib (mirrors packages/lindos-compat/tests).
CATALOG = Path(__file__).resolve().parent.parent / "root" / "usr" / "share" / "lindos" / "winapps" / "apps.json"

REQUIRED_IDS = {"photoshop", "illustrator", "premiere", "office-word", "office-excel",
                "office-powerpoint", "office-outlook", "explorer"}


def test_shipped_catalog_has_required_apps():
    cat = apps_mod.load_catalog(CATALOG)
    assert REQUIRED_IDS <= set(cat), sorted(REQUIRED_IDS - set(cat))
    for app in cat.values():
        assert app.name and app.rdp_path
        assert app.rdp_path.startswith("C:\\")           # a real Windows path
        assert app.note                                   # honesty note present


def test_catalog_json_valid_and_uniqueids():
    raw = json.loads(CATALOG.read_text(encoding="utf-8"))
    ids = [a["id"] for a in raw["apps"]]
    assert len(ids) == len(set(ids))                      # no duplicate ids


def test_catalog_path_honours_lindos_root(home: Path, staged_catalog: Path):
    resolved = catalog_path()
    assert Path(resolved) == staged_catalog
    cat = apps_mod.load_catalog()                          # no-arg -> uses catalog_path()
    assert REQUIRED_IDS <= set(cat)


def test_desktop_content_shape():
    app = apps_mod.load_catalog(CATALOG)["photoshop"]
    text = apps_mod.desktop_content(app)
    assert text.startswith("[Desktop Entry]\n")
    fields = dict(l.split("=", 1) for l in text.splitlines() if "=" in l and not l.startswith("["))
    assert fields["Exec"] == "lindos-winapps run photoshop"
    assert fields["TryExec"] == "lindos-winapps"
    assert fields["Name"] == "Adobe Photoshop"
    assert fields["Type"] == "Application"
    assert fields["Icon"]
    assert "X-Lindos" in fields["Categories"]
    assert fields["X-Lindos-WinApp-Id"] == "photoshop"


def test_install_and_remove_roundtrip(home: Path):
    app = apps_mod.load_catalog(CATALOG)["office-word"]
    assert not apps_mod.is_installed("office-word")
    path = apps_mod.install_app(app)
    assert path == desktop_path("office-word")
    assert path.is_file()
    assert path.parent == home / ".local" / "share" / "applications"
    assert apps_mod.is_installed("office-word")
    assert "office-word" in apps_mod.installed_ids()
    # idempotent overwrite
    again = apps_mod.install_app(app)
    assert again == path
    assert apps_mod.remove_app("office-word") is True
    assert not apps_mod.is_installed("office-word")
    assert apps_mod.remove_app("office-word") is False    # already gone


def test_app_rows_marks_installed(home: Path):
    cat = apps_mod.load_catalog(CATALOG)
    apps_mod.install_app(cat["explorer"])
    rows = {r["id"]: r for r in apps_mod.app_rows(cat)}
    assert rows["explorer"]["installed"] is True
    assert rows["photoshop"]["installed"] is False
    assert rows["explorer"]["rdp_path"].endswith("explorer.exe")


def test_get_app_unknown_raises():
    cat = apps_mod.load_catalog(CATALOG)
    with pytest.raises(apps_mod.CatalogError):
        apps_mod.get_app("nope", cat)


def test_missing_catalog_raises(tmp_path: Path):
    with pytest.raises(apps_mod.CatalogError):
        apps_mod.load_catalog(tmp_path / "does-not-exist.json")


def test_malformed_catalog_raises(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    with pytest.raises(apps_mod.CatalogError):
        apps_mod.load_catalog(bad)
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"apps": [{"id": "", "name": "", "rdp_path": ""}]}), encoding="utf-8")
    with pytest.raises(apps_mod.CatalogError):
        apps_mod.load_catalog(empty)
