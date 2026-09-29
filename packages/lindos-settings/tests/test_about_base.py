"""About page "Based on" row and the Mint-free wording of Settings' user-visible text (Mint sweep).

On a Lindos system /etc/os-release says NAME="Lindos" but keeps ID=linuxmint, VERSION_ID and
UBUNTU_CODENAME on purpose (apt and Mint's tools rely on them).  The "Based on" row is built from those
identity fields, never from PRETTY_NAME - which would just repeat "Lindos".
"""
from __future__ import annotations

import os

from lindos_settings import model

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
    assert "model.base_description(osr)" in src
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
