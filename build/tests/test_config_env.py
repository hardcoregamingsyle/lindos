"""build/config.env + the chroot hooks that consume it (SPEC §8):

  * laptop hardware-enablement packages (``LAPTOP_ESSENTIALS``, consumed by ``20-base.sh``);
  * Bluetooth stays enabled by default — ``DEBLOAT_DISABLE_SERVICES`` (``10-debloat.sh``) and the
    systemd preset it feeds into no longer disable ``bluetooth.service`` unconditionally; only
    Lite mode's own ``tune.d``/``mode.json`` turns it off (see ``packages/lindos-tune``'s own
    tests for the preset side of this).

Stdlib-only text/regex assertions so this runs on the bare Windows pytest job too — no bash/apt
needed, this only checks the *wiring* (the lists + which hook consumes them), not a real build.
"""
from __future__ import annotations

import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent                        # build/tests -> repo root
CONFIG_ENV = REPO_ROOT / "build" / "config.env"
REPOS_HOOK = REPO_ROOT / "build" / "chroot" / "00-repos.sh"
DEBLOAT_HOOK = REPO_ROOT / "build" / "chroot" / "10-debloat.sh"
BASE_HOOK = REPO_ROOT / "build" / "chroot" / "20-base.sh"
DEBS_HOOK = REPO_ROOT / "build" / "chroot" / "30-lindos-debs.sh"


def _text(path: Path) -> str:
    assert path.is_file(), path
    return path.read_text(encoding="utf-8")


def _default_of(text: str, var: str) -> str:
    """Extract the default value from a ``: "${VAR:=default value}"`` line."""
    m = re.search(r"\$\{" + re.escape(var) + r":=([^}]*)\}", text)
    assert m, f"{var} default not found in the given text"
    return m.group(1)


def test_config_env_and_hooks_exist() -> None:
    assert CONFIG_ENV.is_file() and DEBLOAT_HOOK.is_file() and BASE_HOOK.is_file()


# --- bluetooth stays enabled by default (SPEC §8, §11; docs/RAM-BUDGET.md) -----------------------
def test_debloat_disable_services_no_longer_disables_bluetooth() -> None:
    units = _default_of(_text(CONFIG_ENV), "DEBLOAT_DISABLE_SERVICES").split()
    assert "bluetooth.service" not in units
    # every other historically-disabled service is still there — this is a removal of exactly
    # one entry, not a rewrite of the debloat policy
    for unit in ("ModemManager.service", "apport.service", "whoopsie.service", "kerneloops.service",
                 "brltty.service", "speech-dispatcher.service", "NetworkManager-wait-online.service"):
        assert unit in units, unit


def test_debloat_hook_local_default_matches_config_env() -> None:
    config_default = _default_of(_text(CONFIG_ENV), "DEBLOAT_DISABLE_SERVICES")
    hook_default = _default_of(_text(DEBLOAT_HOOK), "DEBLOAT_DISABLE_SERVICES")
    assert config_default.split() == hook_default.split()
    assert "bluetooth.service" not in hook_default


def test_debloat_hook_never_disables_bluetooth_directly() -> None:
    text = _text(DEBLOAT_HOOK)
    assert "svc_disable bluetooth" not in text
    assert "DEBLOAT_DISABLE_SERVICES:=bluetooth" not in text
    # the explanatory comment is still expected (documents *why* it's absent)
    assert "bluetooth" in text.lower()


# --- laptop hardware-enablement packages (SPEC §8) ------------------------------------------------
EXPECTED_LAPTOP_PACKAGES = (
    "linux-firmware", "firmware-sof-signed", "alsa-ucm-conf",
    "pipewire-audio", "wireplumber", "pipewire-pulse",
    "bluez", "blueman",
    "intel-microcode", "amd64-microcode",
    "ubuntu-drivers-common", "fwupd",
    "printer-driver-gutenprint", "ipp-usb",
)


def test_laptop_essentials_variable_exists_and_exported() -> None:
    text = _text(CONFIG_ENV)
    units = _default_of(text, "LAPTOP_ESSENTIALS").split()
    for pkg in EXPECTED_LAPTOP_PACKAGES:
        assert pkg in units, pkg
    assert len(units) == len(set(units)), "duplicate package in LAPTOP_ESSENTIALS"
    assert re.search(r"^export .*\bLAPTOP_ESSENTIALS\b", text, re.M), "LAPTOP_ESSENTIALS not exported"
    # deliberately NOT included — see docs/RAM-BUDGET.md for the size trade-off
    assert "printer-driver-all" not in units and "hplip" not in units


def test_base_hook_consumes_laptop_essentials() -> None:
    text = _text(BASE_HOOK)
    assert "LAPTOP_ESSENTIALS" in text
    assert "apt_try_install" in text
    default = _default_of(text, "LAPTOP_ESSENTIALS")
    for pkg in EXPECTED_LAPTOP_PACKAGES:
        assert pkg in default.split(), pkg


def test_i386_mesa_vulkan_handled_by_gaming_hook_not_duplicated() -> None:
    """mesa-vulkan-drivers:i386 is already installed unconditionally by 70-gaming.sh (gated on
    i386 actually being enabled) — 20-base.sh must not repeat that (single source of truth)."""
    gaming_hook = REPO_ROOT / "build" / "chroot" / "70-gaming.sh"
    gtext = _text(gaming_hook)
    assert "mesa-vulkan-drivers:i386" in gtext and "i386" in gtext
    assert "mesa-vulkan-drivers:i386" not in _text(BASE_HOOK)


def test_base_hook_no_duplicate_essentials_in_nice_list() -> None:
    """blueman/ubuntu-drivers-common moved into LAPTOP_ESSENTIALS: no longer duplicated in NICE."""
    text = _text(BASE_HOOK)
    m = re.search(r"^NICE=\(\n(.*?)^\)\n", text, re.M | re.S)
    assert m, "NICE=( ... ) array not found"
    nice_block = m.group(1)
    assert "blueman" not in nice_block
    assert "ubuntu-drivers-common" not in nice_block
    # still gets mesa-vulkan-drivers (64-bit) from NICE — the i386 half is handled separately
    assert "mesa-vulkan-drivers" in nice_block


def test_base_hook_syntax_and_no_crlf() -> None:
    raw = BASE_HOOK.read_bytes()
    assert b"\r\n" not in raw
    text = raw.decode("utf-8")
    assert text.startswith("#!/bin/bash\n") and "set -Eeuo pipefail" in text


# --- Chrome's apt repo/key pre-staged on the ISO (SPEC §0.1, §8) — legal, never installed --------
def test_chrome_repo_staged_via_installer_script_not_duplicated() -> None:
    """30-lindos-debs.sh pre-stages Chrome's repo+key by calling lindos-core's own
    install-browser.sh --repo-only — it must never re-implement Chrome's key URL/repo line
    itself (single source of truth), and must never install the package at build time."""
    text = _text(DEBS_HOOK)
    assert "ADD_CHROME_REPO" in text
    assert "install-browser.sh" in text
    assert re.search(r"\bchrome\b\s+--repo-only\b", text), "expected a 'chrome --repo-only' invocation"
    # never duplicated: Chrome's key URL / repo host only ever appears in install-browser.sh
    # (and lindos/browsers.py, its Python mirror) — never re-typed in the build hooks
    for hook in (REPOS_HOOK, DEBS_HOOK, BASE_HOOK):
        assert "dl.google.com" not in _text(hook), hook


def test_add_chrome_repo_default_on_and_exported() -> None:
    text = _text(CONFIG_ENV)
    assert _default_of(text, "ADD_CHROME_REPO") == "1"
    assert re.search(r"^export .*\bADD_CHROME_REPO\b", text, re.M)


def test_debs_hook_syntax_and_no_crlf() -> None:
    raw = DEBS_HOOK.read_bytes()
    assert b"\r\n" not in raw
    text = raw.decode("utf-8")
    assert text.startswith("#!/bin/bash\n") and "set -Eeuo pipefail" in text
