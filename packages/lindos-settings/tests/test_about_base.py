"""About page "Based on" row and the Mint-free wording of Settings' user-visible text (Mint sweep).

On a Lindos system /etc/os-release says NAME="Lindos" but keeps ID=linuxmint, VERSION_ID and
UBUNTU_CODENAME on purpose (apt and Mint's tools rely on them).  The "Based on" row is built from those
identity fields, never from PRETTY_NAME - which would just repeat "Lindos".
"""
from __future__ import annotations

import os

from lindos_settings import model
from lindos_settings.pages import about

HERE = os.path.dirname(os.path.abspath(__file__))
PAGES = os.path.normpath(os.path.join(HERE, "..", "root", "usr", "lib", "lindos-settings", "lindos_settings", "pages"))

LINDOS_OS_RELEASE = {
    "NAME": "Lindos", "PRETTY_NAME": "Lindos 1.0 (Aurora)", "ID": "linuxmint", "ID_LIKE": "ubuntu debian",
    "VERSION_ID": "22.2", "VERSION_CODENAME": "zara", "UBUNTU_CODENAME": "noble", "LINDOS_CODENAME": "Aurora",
}


def test_based_on_row_uses_the_identity_fields_not_the_pretty_name():
    text = model.base_description(LINDOS_OS_RELEASE)
    assert text == "Ubuntu 24.04 LTS (noble) · Linux Mint 22.2"
    assert "Lindos" not in text and "zara" not in text


def test_based_on_row_degrades_gracefully():
    assert model.base_description({"UBUNTU_CODENAME": "jammy"}) == "Ubuntu 22.04 LTS (jammy)"
    assert model.base_description({"UBUNTU_CODENAME": "future"}) == "Ubuntu future"
    assert model.base_description({"ID": "linuxmint", "VERSION_ID": "23"}) == "Linux Mint 23"
    assert model.base_description({"ID": "debian", "VERSION_ID": "12"}) == "Ubuntu / Debian packages"
    assert model.base_description({}) == "Ubuntu / Debian packages"


def _source(name: str) -> str:
    with open(os.path.join(PAGES, name), encoding="utf-8") as fh:
        return fh.read()


def test_about_hero_no_longer_calls_lindos_a_mint_remaster():
    src = _source("about.py")
    assert "Linux Mint XFCE remaster" not in src
    assert "based_on(osr)" in src and "model.base_description(osr)" not in src
    assert "A Windows-11-style desktop for Linux" in src


def test_update_and_apps_pages_do_not_name_mint_in_user_visible_text():
    assert "Mint's own Update Manager" not in _source("updates.py")
    assert "the system Update Manager" in _source("updates.py")
    assert "Mint's firefox" not in _source("apps.py")


def test_store_entry_is_described_as_apps_not_as_the_mint_software_manager():
    pages = {p.id: p for p in model.builtin_pages()}
    store = next(s for s in pages["apps"].subitems if s.id == "store")
    assert "Software Manager" not in store.description and "Flatpak" in store.description
    assert store.exec[0] == "mintinstall"          # the tool is unchanged; only the wording is


# --------------------------------------------------------------------------- the unrecognisable layer: About wording + notices
def test_the_based_on_row_names_only_the_ubuntu_release():
    assert about.based_on(LINDOS_OS_RELEASE) == "Ubuntu 24.04 LTS"
    assert about.based_on({"UBUNTU_CODENAME": "jammy"}) == "Ubuntu 22.04 LTS"
    assert about.based_on({"UBUNTU_CODENAME": "future"}) == "Ubuntu future"
    assert about.based_on({}) == "Ubuntu"
    text = about.based_on(LINDOS_OS_RELEASE)
    assert "Mint" not in text and "zara" not in text and "Lindos" not in text


def test_collect_specs_uses_that_row():
    class B:
        def cpu_info(self): return {"model": "cpu", "cores": 2, "threads": 4, "arch": "x86_64"}
        def gpu_info(self): return []
        def ram_info(self): return {"total": 1024, "used": 1, "available": 1}
        def os_release(self): return dict(LINDOS_OS_RELEASE)
        def disk_usage(self, _p): return None
        def hostname(self): return "lindos"
        def battery_present(self): return False
        def uptime_seconds(self): return 60
        def effective_mode(self): return "everyday"
        def lindos_release(self): return "Lindos 1.0"
        def kernel(self): return "6.14"
        def xfce_version(self): return "4.18"
        def effective_browser(self): return "chrome"
        def is_dark(self): return True
        def install_date(self): return "today"
    _device, rows = about.collect_specs(B(), {})
    assert dict(rows)["Based on"] == "Ubuntu 24.04 LTS"


def test_legal_notices_come_from_the_shipped_file_or_a_built_in_paragraph(tmp_path, monkeypatch):
    monkeypatch.setenv("LINDOS_ROOT", str(tmp_path))
    assert about.legal_notices_path() == str(tmp_path) + "/usr/share/lindos/legal/open-source-notices.txt"
    assert about.legal_notices_text() == about.LEGAL_FALLBACK, "no file: the built-in paragraph"
    f = tmp_path / "usr" / "share" / "lindos" / "legal" / "open-source-notices.txt"
    f.parent.mkdir(parents=True)
    f.write_text("   \n", encoding="utf-8")
    assert about.legal_notices_text() == about.LEGAL_FALLBACK, "an empty file is no notice"
    f.write_text("Lindos notices\nUbuntu, Debian\n", encoding="utf-8")
    assert about.legal_notices_text() == "Lindos notices\nUbuntu, Debian\n"
    for needle in ("GPL-3.0-or-later", "Ubuntu", "Debian", "Linux Mint", "not affiliated", "not Windows"):
        assert needle in about.LEGAL_FALLBACK, needle


def test_the_shipped_notices_are_the_default_source_and_the_page_offers_them():
    real = os.path.normpath(os.path.join(HERE, "..", "..", "lindos-desktop", "root", *about.LEGAL_NOTICES_PATH.strip("/").split("/")))
    assert os.path.isfile(real), real
    src = _source("about.py")
    assert '"Legal and open-source notices"' in src and "legal_notices_text()" in src
