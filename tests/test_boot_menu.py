"""The installer boot menu: build/overlay/boot/grub/{grub,loopback}.cfg and the BIOS (isolinux) menu.

The installation flow (CONTINUATION.md, docs/BUILDING.md "Installer flow") starts here:

  1. "Install Lindos"  = boot=casper only-ubiquity oem-config/enable=true ...  the live session is the
     installer and nothing else (no desktop, no first-run wizard, no package installs, no prompts);
  2. the same with nomodeset;
  3. "Try Lindos (live session)" = boot=casper oem-config/enable=true ...  a desktop whose "Install Lindos"
     launcher follows the same OEM flow (no only-ubiquity);
  4. the same with nomodeset;  5. the integrity check;  6. boot from the first hard disk.

The old "OEM install (for manufacturers)" entry is gone: every install is an OEM-mode install whose
finalisation (ubiquity/success_command -> lindos-installer's finalize.sh) arms the first-boot wizard.

These tests pin that layout for both boot paths (UEFI: GRUB; BIOS: isolinux/live.cfg, generated from the
GRUB entries by build/lib/boot_menu.py) and keep grub.cfg and loopback.cfg in sync.  Pure stdlib, no Linux.
What they cannot show - that a machine really boots these entries - needs a QEMU/hardware run.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, NamedTuple

import pytest

REPO = Path(__file__).resolve().parent.parent
GRUB_DIR = REPO / "build" / "overlay" / "boot" / "grub"
BUILD_ISO = REPO / "build" / "build-iso.sh"
BOOT_MENU_PY = REPO / "build" / "lib" / "boot_menu.py"

SUCCESS_COMMAND = "ubiquity/success_command=/usr/libexec/lindos/installer/finalize.sh"


class Entry(NamedTuple):
    title: str
    classes: List[str]
    kernel: str
    initrd: str
    words: List[str]          # kernel command line words without the trailing "--" and the placeholder


def _text(path: Path) -> str:
    assert path.is_file(), path
    data = path.read_bytes()
    assert b"\r" not in data, "%s must use LF line endings" % path
    return data.decode("utf-8")


def _entries(text: str) -> List[Entry]:
    """Independent parse of the casper menu entries (deliberately not build/lib/boot_menu.py)."""
    out: List[Entry] = []
    for m in re.finditer(r'^menuentry\s+"([^"]+)"([^{]*)\{(.*?)^\}', text, re.M | re.S):
        body = m.group(3)
        linux = re.search(r"^\s*linux\s+(\S+)\s+(.*?)\s*$", body, re.M)
        if not linux:
            continue
        words = linux.group(2).split()
        assert words[-1] == "--", "the kernel command line must end with '--': %s" % m.group(1)
        initrd = re.search(r"^\s*initrd\s+(\S+)", body, re.M)
        assert initrd, "no initrd in %s" % m.group(1)
        words = [w.replace("@PRESEED@", "") for w in words[:-1]]
        words = [w for w in words if w]
        out.append(Entry(m.group(1), re.findall(r"--class\s+(\S+)", m.group(2)), linux.group(1), initrd.group(1), words))
    return out


@pytest.fixture(scope="module")
def grub() -> List[Entry]:
    return _entries(_text(GRUB_DIR / "grub.cfg"))


@pytest.fixture(scope="module")
def loopback() -> List[Entry]:
    return _entries(_text(GRUB_DIR / "loopback.cfg"))


@pytest.fixture(scope="module")
def boot_menu():
    spec = importlib.util.spec_from_file_location("lindos_boot_menu", BOOT_MENU_PY)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod        # typing.NamedTuple / dataclasses look the module up by name
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- the layout
def test_the_menu_offers_install_try_and_their_compatibility_variants_in_order(grub: List[Entry]) -> None:
    titles = [e.title for e in grub]
    assert titles == [
        "Install Lindos @LINDOS_VERSION_SHORT@",
        "Install Lindos @LINDOS_VERSION_SHORT@ (compatibility mode)",
        "Try Lindos @LINDOS_VERSION_SHORT@ (live session)",
        "Try Lindos @LINDOS_VERSION_SHORT@ (compatibility mode)",
        "Check the integrity of the medium",
    ], titles


def test_the_default_entry_is_the_first_one_the_installer(grub: List[Entry]) -> None:
    text = _text(GRUB_DIR / "grub.cfg")
    assert re.search(r"^set default=0$", text, re.M), "the installer entry must be the default"
    first = grub[0]
    assert "only-ubiquity" in first.words and "oem-config/enable=true" in first.words
    assert first.title.startswith("Install Lindos")


def test_no_oem_for_manufacturers_entry_remains() -> None:
    for name in ("grub.cfg", "loopback.cfg"):
        text = _text(GRUB_DIR / name).lower()
        assert "for manufacturers" not in text and "oem install" not in text, name
        assert not re.search(r'menuentry\s+"[^"]*oem', text), name


def test_only_the_install_entries_use_only_ubiquity(grub: List[Entry]) -> None:
    only = [e.title for e in grub if "only-ubiquity" in e.words]
    assert only == ["Install Lindos @LINDOS_VERSION_SHORT@", "Install Lindos @LINDOS_VERSION_SHORT@ (compatibility mode)"]


def test_the_try_entries_are_a_live_session_with_the_same_oem_flow(grub: List[Entry]) -> None:
    tries = [e for e in grub if e.title.startswith("Try Lindos")]
    assert len(tries) == 2
    for e in tries:
        assert "boot=casper" in e.words
        assert "only-ubiquity" not in e.words, "the Try entries boot the desktop"
        assert "oem-config/enable=true" in e.words, "the desktop 'Install Lindos' launcher must follow the OEM flow"
        assert SUCCESS_COMMAND in e.words


def test_every_entry_boots_casper_as_liveuser_on_lindos_and_arms_the_finalisation(grub: List[Entry]) -> None:
    assert len(grub) == 5
    for e in grub:
        assert e.kernel == "/casper/vmlinuz" and e.initrd == "/casper/initrd.lz", e.title
        assert "boot=casper" in e.words, e.title
        assert "username=liveuser" in e.words and "hostname=lindos" in e.words, e.title
        assert not any(w in ("username=mint", "hostname=mint") for w in e.words), e.title
        assert "oem-config/enable=true" in e.words, e.title
        assert SUCCESS_COMMAND in e.words, e.title
        assert "quiet" in e.words and "splash" in e.words, e.title
        assert "iso-scan/filename=${iso_path}" in e.words, "loop-booting loaders (Ventoy, grml) need it: " + e.title
        assert all(" " not in w for w in e.words), "kernel command line values cannot contain spaces"


def test_compatibility_entries_and_only_they_carry_nomodeset(grub: List[Entry]) -> None:
    for e in grub:
        assert ("nomodeset" in e.words) == ("compatibility mode" in e.title), e.title


def test_the_integrity_check_is_a_live_boot_with_the_check_flag(grub: List[Entry]) -> None:
    check = [e for e in grub if "integrity" in e.title.lower()]
    assert len(check) == 1 and "integrity-check" in check[0].words and "only-ubiquity" not in check[0].words


def test_no_entry_uses_an_unattended_installer_mode(grub: List[Entry]) -> None:
    for e in grub:
        assert not {"maybe-ubiquity", "automatic-ubiquity", "noninteractive"} & set(e.words), (
            "automatic-ubiquity falls back to an unattended install when the installer's X server fails: " + e.title)


def test_boot_from_the_first_hard_disk_and_memtest_are_still_offered() -> None:
    text = _text(GRUB_DIR / "grub.cfg")
    assert 'menuentry "Boot from the first hard disk"' in text
    assert "/boot/grub/memtest.cfg" in text


# --------------------------------------------------------------------------- grub.cfg and loopback.cfg agree
def test_grub_and_loopback_offer_the_same_entries(grub: List[Entry], loopback: List[Entry]) -> None:
    assert [(e.title, e.kernel, e.initrd, e.words) for e in grub] == \
        [(e.title, e.kernel, e.initrd, e.words) for e in loopback]


def test_placeholders_are_the_ones_build_iso_fills() -> None:
    build = _text(BUILD_ISO)
    filled = set(re.findall(r'-e "s\|(@[A-Z_]+@)\|', build))
    assert "@PRESEED@" in filled and "@LINDOS_VERSION_SHORT@" in filled
    for name in ("grub.cfg", "loopback.cfg"):
        used = set(re.findall(r"@[A-Z_]+@", "\n".join(ln for ln in _text(GRUB_DIR / name).splitlines()
                                                     if not ln.lstrip().startswith("#"))))
        assert used <= filled, (name, used - filled)


def test_the_preseed_placeholder_sits_right_before_boot_casper() -> None:
    """@PRESEED@ becomes 'file=/cdrom/preseed/x.seed ' or nothing; it must not swallow other words."""
    for name in ("grub.cfg", "loopback.cfg"):
        for line in _text(GRUB_DIR / name).splitlines():
            if re.match(r"^\s*linux\s", line):
                assert re.search(r"\s@PRESEED@boot=casper\s", line), (name, line)


@pytest.mark.parametrize("preseed", ["", "file=/cdrom/preseed/linuxmint.seed "])
def test_filled_in_the_way_build_iso_does_it_the_menu_stays_valid(preseed: str) -> None:
    for name in ("grub.cfg", "loopback.cfg"):
        text = _text(GRUB_DIR / name).replace("@PRESEED@", preseed).replace("@LINDOS_VERSION_SHORT@", "1.0")
        assert not re.search(r"@[A-Z_]+@", "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#")))
        for e in _entries(text):
            assert e.words[0] in ("boot=casper", "file=/cdrom/preseed/linuxmint.seed"), e.title


def test_build_iso_keeps_its_safety_checks_and_adds_the_installer_ones() -> None:
    build = _text(BUILD_ISO)
    assert "lost 'boot=casper'" in build                       # existing: refuse an unbootable ISO
    assert "unfilled @PLACEHOLDER@" in build                   # existing: no placeholder left
    assert "grub.cfg kernel" in build and "grub.cfg initrd" in build   # existing: kernel/initrd exist
    assert "only-ubiquity oem-config/enable=true" in build     # new: the first entry is the installer
    assert "username=mint|hostname=mint" in build              # new: no Mint live user left on any boot path
    assert "boot_menu.py" in build and "isolinux --grub" in build   # new: BIOS menu generated from the GRUB one


# --------------------------------------------------------------------------- the BIOS menu
MINT_LIVE_CFG = """\
label live
  menu label Start Linux Mint 22.2 Xfce 64-bit
  menu default
  kernel /casper/vmlinuz
  append  initrd=/casper/initrd.lz boot=casper username=mint hostname=mint quiet splash --

label compat
  menu label Start Linux Mint 22.2 Xfce 64-bit (compatibility mode)
  kernel /casper/vmlinuz
  append  initrd=/casper/initrd.lz boot=casper xforcevesa nomodeset username=mint hostname=mint noapic noacpi nosplash irqpoll --

label oem
  menu label OEM install (for manufacturers)
  kernel /casper/vmlinuz
  append  oem-config/enable=true only-ubiquity initrd=/casper/initrd.lz boot=casper username=mint hostname=mint quiet splash --

label memtest
  menu label Memory test
  kernel /casper/memtest
  append -

label hd
  menu label Boot from first hard disk
  localboot 0x80
  append -
"""

MINT_ISOLINUX_CFG = """\
# D-I config version 2.0
path
include menu.cfg
default live
prompt 0
timeout 100
ontimeout live
"""


@pytest.fixture()
def rendered_grub(tmp_path: Path) -> Path:
    text = _text(GRUB_DIR / "grub.cfg").replace("@PRESEED@", "").replace("@LINDOS_VERSION_SHORT@", "1.0")
    path = tmp_path / "grub.cfg"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def test_the_bios_menu_is_generated_from_the_grub_entries(boot_menu, rendered_grub: Path) -> None:
    entries = boot_menu.parse_grub_entries(rendered_grub.read_text(encoding="utf-8"))
    assert [e.title for e in entries][:2] == ["Install Lindos 1.0", "Install Lindos 1.0 (compatibility mode)"]
    text = boot_menu.rewrite_live_cfg(MINT_LIVE_CFG, entries)
    assert "username=mint" not in text and "hostname=mint" not in text and "manufacturers" not in text
    assert "Linux Mint" not in text.replace("Start Linux Mint", "")  # only the replaced Mint entries mentioned it
    labels = re.findall(r"^label\s+(\S+)", text, re.M)
    assert labels == ["install", "install-compat", "try", "try-compat", "check", "memtest", "hd"], labels
    assert text.count("menu default") == 1
    install = text.split("label install\n", 1)[1].split("\nlabel ", 1)[0]
    assert "menu default" in install and "kernel /casper/vmlinuz" in install
    assert "only-ubiquity" in install and "oem-config/enable=true" in install
    assert "username=liveuser" in install and "hostname=lindos" in install and install.rstrip().endswith("--")
    assert "iso-scan" not in text and "${" not in text, "GRUB-only words must not reach ISOLINUX"
    tries = text.split("label try\n", 1)[1].split("\nlabel ", 1)[0]
    assert "only-ubiquity" not in tries and "boot=casper" in tries and "oem-config/enable=true" in tries
    assert "Memory test" in text and "localboot 0x80" in text, "non-casper entries of the base stay"


def test_bios_and_uefi_entries_carry_the_same_kernel_words(boot_menu, rendered_grub: Path) -> None:
    grub_entries = boot_menu.parse_grub_entries(rendered_grub.read_text(encoding="utf-8"))
    text = boot_menu.rewrite_live_cfg(MINT_LIVE_CFG, grub_entries)
    appends = re.findall(r"^\s*append\s+initrd=(\S+)\s+(.*?)\s+--\s*$", text, re.M)
    assert len(appends) == len(grub_entries) == 5
    for (initrd, words), entry in zip(appends, grub_entries):
        expected = [w for w in entry.args if not w.startswith("iso-scan/")]
        assert initrd == entry.initrd and words.split() == expected, entry.title


def test_the_cli_rewrites_live_cfg_and_repoints_a_dangling_default(tmp_path: Path, rendered_grub: Path) -> None:
    iso = tmp_path / "isolinux"
    iso.mkdir()
    (iso / "live.cfg").write_text(MINT_LIVE_CFG, encoding="utf-8", newline="\n")
    (iso / "isolinux.cfg").write_text(MINT_ISOLINUX_CFG, encoding="utf-8", newline="\n")
    res = subprocess.run([sys.executable, str(BOOT_MENU_PY), "isolinux", "--grub", str(rendered_grub),
                          "--live", str(iso / "live.cfg"), "--write"],
                         capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert res.returncode == 0, res.stderr
    live = (iso / "live.cfg").read_text(encoding="utf-8")
    assert live.startswith("label install\n") and "label live" not in live and "username=mint" not in live
    cfg = (iso / "isolinux.cfg").read_text(encoding="utf-8")
    assert "default install" in cfg and "ontimeout install" in cfg and "label live" not in cfg
    assert "include menu.cfg" in cfg and "timeout 100" in cfg, "the rest of the base config is untouched"
    assert b"\r" not in (iso / "live.cfg").read_bytes()


def test_a_menu_module_default_is_not_mistaken_for_a_label(boot_menu) -> None:
    text = "default vesamenu.c32\nprompt 0\n"
    assert boot_menu.retarget_label_refs(text, ["live"], "install") == text


def test_a_missing_live_cfg_is_not_an_error_and_nothing_is_created(tmp_path: Path, rendered_grub: Path) -> None:
    res = subprocess.run([sys.executable, str(BOOT_MENU_PY), "isolinux", "--grub", str(rendered_grub),
                          "--live", str(tmp_path / "isolinux" / "live.cfg"), "--write"],
                         capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert res.returncode == 0 and not (tmp_path / "isolinux").exists()


def test_a_grub_cfg_without_casper_entries_fails_loudly(tmp_path: Path) -> None:
    empty = tmp_path / "grub.cfg"
    empty.write_text('menuentry "Boot from the first hard disk" {\n\texit\n}\n', encoding="utf-8", newline="\n")
    live = tmp_path / "live.cfg"
    live.write_text(MINT_LIVE_CFG, encoding="utf-8", newline="\n")
    res = subprocess.run([sys.executable, str(BOOT_MENU_PY), "isolinux", "--grub", str(empty), "--live", str(live), "--write"],
                         capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert res.returncode == 1, "an empty BIOS menu must fail the build, not ship"
    assert live.read_text(encoding="utf-8") == MINT_LIVE_CFG


def test_the_generated_bios_menu_survives_a_second_run(boot_menu, rendered_grub: Path) -> None:
    entries = boot_menu.parse_grub_entries(rendered_grub.read_text(encoding="utf-8"))
    once = boot_menu.rewrite_live_cfg(MINT_LIVE_CFG, entries)
    assert boot_menu.rewrite_live_cfg(once, entries) == once, "idempotent: a rebuild over a rewritten tree changes nothing"


def test_entry_titles_and_labels_are_unique_per_menu(boot_menu, rendered_grub: Path) -> None:
    entries = boot_menu.parse_grub_entries(rendered_grub.read_text(encoding="utf-8"))
    labels: Dict[str, int] = {}
    for i, e in enumerate(entries):
        labels[boot_menu.isolinux_label(e, i)] = labels.get(boot_menu.isolinux_label(e, i), 0) + 1
    assert all(n == 1 for n in labels.values()), labels
