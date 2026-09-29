"""Hermetic tests for build/qa/menu_checks.py - the structural checks of the BOOT MENUS of a built ISO.

Neither CI job boots the ISO through its own boot loader, so a broken grub.cfg or isolinux/live.cfg would ship green.
These tests build the menus exactly the way build-iso.sh does (the shipped overlay grub.cfg with its placeholders filled,
the BIOS menu generated from it by build/lib/boot_menu.py over a Mint-style live.cfg) and prove that

  * the real, correct menus pass every check;
  * every way the menus can be wrong that the reviewers named - a GRUB script syntax error, a wrongly rewritten isolinux
    live.cfg (the base labels live/compat/oem left, two defaults, a dangling default), a missing kernel, a lost OEM word -
    is reported by name.

xorriso is faked (its command shapes are the ones build/qa/install_test.py already uses); what a real ISO looks like to
the real xorriso is what the CI step is for.  No Linux, no QEMU, no network.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
for _p in (REPO / "build" / "qa", REPO / "build" / "lib"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import boot_menu  # noqa: E402
import install_checks as ic  # noqa: E402
import menu_checks as mc  # noqa: E402

OVERLAY = REPO / "build" / "overlay" / "boot" / "grub"

# the base ISO's live.cfg and isolinux.cfg (the same shapes tests/test_boot_menu.py rewrites)
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

LISTING = {
    "/casper/vmlinuz", "/casper/initrd.lz", "/casper/memtest", "/casper/filesystem.squashfs",
    "/isolinux/isolinux.bin", "/isolinux/isolinux.cfg", "/isolinux/live.cfg", "/isolinux/menu.cfg",
    "/boot/grub/grub.cfg", "/boot/grub/loopback.cfg", "/.disk/info",
}

EL_TORITO = """\
El Torito catalog  : /boot.catalog  1
El Torito images   : N Pltf B Emul Ld_seg Hdpt Ldsiz     LBA
El Torito boot img :   1  BIOS  y   none  0x0000  0x00      4    1234
El Torito boot img :   2  UEFI  y   none  0x0000  0x00  10272    5678
El Torito img path :   1  /isolinux/isolinux.bin
"""


def fill(text: str) -> str:
    """What build-iso.sh apply_overlay does to the overlay's placeholders (a Mint 22.2 base ISO has no preseed seed)."""
    return text.replace("@PRESEED@", "").replace("@LINDOS_VERSION_SHORT@", "1.0")


GRUB = fill((OVERLAY / "grub.cfg").read_text(encoding="utf-8"))
LOOPBACK = fill((OVERLAY / "loopback.cfg").read_text(encoding="utf-8"))


def edit_linux(text: str, index: int, old: str, new: str) -> str:
    """Replace *old* by *new* in the *index*-th ``linux`` line of a GRUB menu (comments mention the same words)."""
    lines = text.split("\n")
    seen = -1
    for i, line in enumerate(lines):
        if re.match(r"^\s*linux\s", line):
            seen += 1
            if seen == index:
                assert old in line, (old, line)
                lines[i] = line.replace(old, new, 1)
                return "\n".join(lines)
    raise AssertionError("no linux line %d" % index)


def good_files() -> Dict[str, str]:
    """The menu files of a correctly built ISO."""
    entries = boot_menu.parse_grub_entries(GRUB)
    live = boot_menu.rewrite_live_cfg(MINT_LIVE_CFG, entries)
    iso_cfg = boot_menu.retarget_label_refs(MINT_ISOLINUX_CFG, boot_menu.casper_labels(MINT_LIVE_CFG), boot_menu.isolinux_label(entries[0], 0))
    return {"boot/grub/grub.cfg": GRUB, "boot/grub/loopback.cfg": LOOPBACK, "isolinux/live.cfg": live,
            "isolinux/isolinux.cfg": iso_cfg, "isolinux/menu.cfg": "menu title Lindos\n"}


def run(files=None, listing=None, **kw) -> List[ic.Finding]:
    kw.setdefault("el_torito", EL_TORITO)
    kw.setdefault("grub_script_check", {"boot/grub/grub.cfg": (0, ""), "boot/grub/loopback.cfg": (0, "")})
    return mc.check_menus(files if files is not None else good_files(), set(LISTING if listing is None else listing), **kw)


def fails(findings: List[ic.Finding]) -> Dict[str, str]:
    return {f.name: f.detail for f in findings if f.level == ic.FAIL}


def levels(findings: List[ic.Finding], name: str) -> List[str]:
    return [f.level for f in findings if f.name == name]


# ============================================================================================ the correct menus
def test_the_menus_of_a_correctly_built_iso_pass_every_check():
    findings = run()
    assert fails(findings) == {}, ic.format_findings(findings)
    for name in ("menu-grub-entries", "menu-grub-default", "menu-grub-syntax", "menu-grub-loopback", "menu-isolinux-labels",
                 "menu-isolinux-default", "menu-isolinux-files", "menu-el-torito", "menu-grub-script-check"):
        assert ic.OK in levels(findings, name), name
    assert not ic.warnings(findings), ic.format_findings(ic.warnings(findings))


def test_the_shipped_overlay_menus_pass_the_builtin_script_check():
    for name in ("grub.cfg", "loopback.cfg"):
        assert mc.script_structure_errors((OVERLAY / name).read_text(encoding="utf-8")) == [], name


def test_without_grub_script_check_the_builtin_check_is_said_to_be_all_there_was():
    findings = run(grub_script_check=None)
    assert fails(findings) == {} and levels(findings, "menu-script-check") == [ic.INFO]


@pytest.mark.skipif(shutil.which("grub-script-check") is None, reason="grub-script-check (grub-common) is not installed")
def test_the_real_grub_script_check_accepts_the_shipped_menus_and_rejects_a_broken_one(tmp_path):
    for name, text in (("grub.cfg", GRUB), ("loopback.cfg", LOOPBACK)):
        p = tmp_path / name
        p.write_text(text, encoding="utf-8", newline="\n")
        got = mc.run_grub_script_check({"boot/grub/" + name: p})
        assert got == {"boot/grub/" + name: (0, "")} or got["boot/grub/" + name][0] == 0, got
    broken = tmp_path / "broken.cfg"
    broken.write_text(GRUB.replace("\nfi\n", "\n", 1), encoding="utf-8", newline="\n")
    assert mc.run_grub_script_check({"x": broken})["x"][0] != 0


# ============================================================================================ GRUB: what can be wrong
def test_a_missing_closing_fi_is_a_syntax_failure():
    """The mutation of the finding: the closing 'fi' of the font block removed - the script no longer parses."""
    broken = GRUB.replace("\tterminal_output gfxterm\nfi\n", "\tterminal_output gfxterm\n", 1)
    assert broken != GRUB
    files = dict(good_files(), **{"boot/grub/grub.cfg": broken})
    got = fails(run(files))
    assert "menu-grub-syntax" in got and "'if' has no 'fi'" in got["menu-grub-syntax"]


@pytest.mark.parametrize("text, needle", [
    ('menuentry "x" {\n  linux /a\n', "'{' is never closed"),
    ('menuentry "x" {\n  linux /a\n}\n}\n', "'}' without '{'"),
    ("if true ; then\n  echo x\n", "'if' has no 'fi'"),
    ("echo x\nfi\n", "'fi' without 'if'"),
    ('echo "unterminated\n', "unterminated double quote"),
    ("for i in a b ; do\n echo $i\n", "loop has no 'done'"),
])
def test_script_structure_errors(text, needle):
    assert any(needle in e for e in mc.script_structure_errors(text)), mc.script_structure_errors(text)


@pytest.mark.parametrize("text", [
    'menuentry "a {b}" {\n  linux /x root=${iso_path} --\n}\n',            # braces inside quotes and ${variables} are not blocks
    "# a comment with { and if\nset x=1  # trailing { comment\n",
    "if [ x\"$a\" = xy ] ; then\n  echo fi\nfi\n",                          # 'fi' as an argument is not a keyword
])
def test_script_structure_ok(text):
    assert mc.script_structure_errors(text) == []


def test_grub_script_check_failures_are_reported_with_its_output():
    got = fails(run(grub_script_check={"boot/grub/grub.cfg": (1, "error: syntax error, unexpected end of file\n"), "boot/grub/loopback.cfg": (0, "")}))
    assert "unexpected end of file" in got["menu-grub-script-check"] and "menu-loopback-script-check" not in got


def test_the_installer_must_be_the_first_and_default_entry():
    entries = boot_menu.parse_grub_entries(GRUB)
    swapped = GRUB.replace('menuentry "Install Lindos 1.0"', 'menuentry "Try TMP"', 1).replace('menuentry "Try Lindos 1.0 (live session)"', 'menuentry "Install Lindos 1.0"', 1)
    assert swapped != GRUB and entries
    assert "menu-grub-install-first" in fails(run(dict(good_files(), **{"boot/grub/grub.cfg": swapped})))
    assert "menu-grub-default" in fails(run(dict(good_files(), **{"boot/grub/grub.cfg": GRUB.replace("set default=0", "set default=2")})))
    findings = run(dict(good_files(), **{"boot/grub/grub.cfg": GRUB.replace("set timeout=10", "set timeout=0")}))
    assert "menu-grub-timeout" not in fails(findings) and levels(findings, "menu-grub-timeout") == [ic.WARN]


def test_the_compatibility_install_entry_must_carry_nomodeset():
    text = GRUB.replace("nomodeset ", "", 1)
    got = fails(run(dict(good_files(), **{"boot/grub/grub.cfg": text})))
    assert "menu-grub-install-entries" in got and "compatibility" in got["menu-grub-install-entries"]


def test_an_unfilled_placeholder_fails_but_a_commented_one_does_not():
    files = dict(good_files(), **{"boot/grub/grub.cfg": GRUB.replace("Install Lindos 1.0\"", "Install Lindos @LINDOS_VERSION_SHORT@\"", 1)})
    assert "@LINDOS_VERSION_SHORT@" in fails(run(files))["menu-grub-placeholder"]
    assert "menu-grub-placeholder" not in fails(run(dict(good_files(), **{"boot/grub/grub.cfg": GRUB + "# @PRESEED@ in a comment\n"})))


@pytest.mark.parametrize("word, name", [
    ("boot=casper ", "boot=casper"),
    ("oem-config/enable=true ", "oem-config/enable"),
    ("ubiquity/success_command=/usr/libexec/lindos/installer/finalize.sh ", "success_command"),
    ("username=liveuser ", "liveuser"),
])
def test_every_entry_keeps_the_words_the_install_flow_needs(word, name):
    got = fails(run(dict(good_files(), **{"boot/grub/grub.cfg": edit_linux(GRUB, 3, word, "")})))       # the 'Try ... (compatibility mode)' entry
    assert "menu-grub-entries" in got and name in got["menu-grub-entries"], got


def test_the_old_live_user_or_an_unattended_installer_mode_fails():
    got = fails(run(dict(good_files(), **{"boot/grub/grub.cfg": edit_linux(GRUB, 0, "username=liveuser", "username=mint")})))
    assert "username=mint" in got["menu-grub-entries"]
    got = fails(run(dict(good_files(), **{"boot/grub/grub.cfg": edit_linux(GRUB, 0, "only-ubiquity", "only-ubiquity automatic-ubiquity")})))
    assert "unattended" in got["menu-grub-entries"]


def test_a_kernel_or_initrd_that_is_not_on_the_medium_fails():
    got = fails(run(listing=LISTING - {"/casper/initrd.lz"}))
    assert "/casper/initrd.lz" in got["menu-grub-entries"] and "/casper/initrd.lz" in got["menu-loopback-entries"]
    assert "menu-isolinux-files" in got


def test_every_linux_line_must_end_with_the_double_dash():
    got = fails(run(dict(good_files(), **{"boot/grub/grub.cfg": edit_linux(GRUB, 1, "quiet splash --", "quiet splash")})))
    assert "menu-grub-terminator" in got


def test_loopback_cfg_must_offer_the_same_entries_as_grub_cfg():
    changed = LOOPBACK.replace("hostname=lindos", "hostname=lindos2", 1)
    files = dict(good_files(), **{"boot/grub/loopback.cfg": changed})
    assert "menu-grub-loopback" in fails(run(files))
    files = good_files()
    del files["boot/grub/loopback.cfg"]
    findings = run(files)
    assert "menu-grub-loopback" not in fails(findings) and levels(findings, "menu-grub-loopback") == [ic.WARN]


def test_a_missing_or_empty_grub_cfg_fails():
    files = good_files()
    del files["boot/grub/grub.cfg"]
    assert "menu-grub" in fails(run(files))
    assert "menu-grub" in fails(run(dict(good_files(), **{"boot/grub/grub.cfg": "set default=0\n"})))


# ============================================================================================ ISOLINUX: what can be wrong
def test_the_unrewritten_base_menu_is_reported_with_the_base_labels():
    """live.cfg as the base ISO ships it: boot_menu.py did not run (or its result was replaced)."""
    files = dict(good_files(), **{"isolinux/live.cfg": MINT_LIVE_CFG, "isolinux/isolinux.cfg": MINT_ISOLINUX_CFG})
    got = fails(run(files))
    assert "menu-isolinux-labels" in got and "live" in got["menu-isolinux-labels"]
    assert "live" in got["menu-isolinux-base"] and "oem" in got["menu-isolinux-base"]
    assert "menu-isolinux-user" in got                                          # username=mint


def test_a_default_that_names_a_replaced_label_is_a_dangling_default():
    """boot_menu.py retargets 'default live'; without that the user lands at a bare 'boot:' prompt."""
    files = dict(good_files(), **{"isolinux/isolinux.cfg": MINT_ISOLINUX_CFG})
    got = fails(run(files))
    assert "default live" in got["menu-isolinux-dangling"] and "ontimeout live" in got["menu-isolinux-dangling"]
    files = dict(good_files(), **{"isolinux/isolinux.cfg": "default vesamenu.c32\nprompt 0\ntimeout 100\ninclude menu.cfg\n"})
    assert "menu-isolinux-dangling" not in fails(run(files))                  # a module name is not a label


def test_exactly_one_menu_default_and_it_is_the_install_label():
    live = good_files()["isolinux/live.cfg"]
    two = live.replace("label try\n", "label try\n\tmenu default\n", 1)
    assert "install, try" in fails(run(dict(good_files(), **{"isolinux/live.cfg": two})))["menu-isolinux-default"]
    none = live.replace("\tmenu default\n", "")
    assert "no label" in fails(run(dict(good_files(), **{"isolinux/live.cfg": none})))["menu-isolinux-default"]


def test_the_bios_words_must_match_the_grub_entries_they_come_from():
    live = good_files()["isolinux/live.cfg"]
    edited = live.replace("only-ubiquity ", "", 1)                          # the install label lost only-ubiquity
    assert "install" in fails(run(dict(good_files(), **{"isolinux/live.cfg": edited})))["menu-isolinux-words"]
    retitled = live.replace("menu label Install Lindos 1.0\n", "menu label Start Lindos\n", 1)
    assert "menu-isolinux-labels" in fails(run(dict(good_files(), **{"isolinux/live.cfg": retitled})))


def test_isolinux_files_must_exist():
    assert "menu-isolinux-files" in fails(run(listing=LISTING - {"/isolinux/menu.cfg"}))
    findings = run(listing=LISTING - {"/casper/memtest"})                  # a non-casper entry of the base: not our business
    assert "menu-isolinux-files" not in fails(findings)
    # SYSLINUX resolves an include relative to the config's directory, an absolute one as it is
    absolute = dict(good_files(), **{"isolinux/isolinux.cfg": good_files()["isolinux/isolinux.cfg"].replace("include menu.cfg", "include /isolinux/menu.cfg")})
    assert "menu-isolinux-files" not in fails(run(absolute))
    assert "/isolinux/menu.cfg" in fails(run(absolute, LISTING - {"/isolinux/menu.cfg"}))["menu-isolinux-files"]


def test_the_isolinux_labels_are_the_five_the_install_flow_offers():
    labels = [lb.name for lb in mc.parse_syslinux(good_files()["isolinux/live.cfg"]).labels]
    assert labels[:5] == list(mc.LABELS_EXPECTED) and labels[5:] == ["memtest", "hd"]


def test_no_bios_menu_at_all_is_only_a_note_for_an_uefi_only_iso():
    files = {k: v for k, v in good_files().items() if not k.startswith("isolinux/")}
    listing = {p for p in LISTING if not p.startswith("/isolinux/")}
    findings = run(files, listing)
    assert fails(findings) == {} and levels(findings, "menu-isolinux") == [ic.INFO]
    assert "menu-isolinux" in fails(run(files))                              # isolinux/ exists but nothing could be read from it


def test_parse_syslinux_reads_labels_help_text_and_top_level_keys():
    cfg = mc.parse_syslinux("default vesamenu.c32\ninclude a.cfg\nlabel x\n  menu label X\n  menu default\n  text help\nlabel notalabel\n"
                            "  endtext\n  kernel /k\n  append initrd=/i a b --\nlabel y\n  com32 hdt.c32\n")
    assert cfg.top["default"] == "vesamenu.c32" and cfg.includes == ["a.cfg"]
    assert [(lb.name, lb.title, lb.default, lb.kernel) for lb in cfg.labels] == [("x", "X", True, "/k"), ("y", "", False, "hdt.c32")]
    assert mc._append_parts("initrd=/i a b --") == ("/i", ["a", "b"])


# ============================================================================================ the boot catalogue
def test_the_boot_catalogue_needs_a_bios_and_an_uefi_image():
    assert levels(mc.check_boot_catalog(EL_TORITO), "menu-el-torito") == [ic.OK]
    only_bios = "\n".join(ln for ln in EL_TORITO.splitlines() if "UEFI" not in ln)
    assert "menu-el-torito-uefi" in fails(mc.check_boot_catalog(only_bios))
    only_uefi = "\n".join(ln for ln in EL_TORITO.splitlines() if "BIOS" not in ln)
    assert "menu-el-torito-bios" in fails(mc.check_boot_catalog(only_uefi))


def test_an_unreadable_boot_catalogue_is_a_warning_never_a_failure():
    for report in ("", "xorriso : NOTE : nothing here\n", None):
        findings = mc.check_boot_catalog(report)
        assert [f.level for f in findings] == [ic.WARN]


# ============================================================================================ reading the ISO
class FakeXorriso:
    """A fake 'xorriso' with the command shapes menu_checks and install_test use."""

    def __init__(self, files: Dict[str, str], listing, report=EL_TORITO, list_rc=0):
        self.files, self.listing, self.report, self.list_rc = files, listing, report, list_rc
        self.calls: List[List[str]] = []

    def which(self, name):
        return "/usr/bin/xorriso" if name == "xorriso" else None

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if "-extract" in argv:
            src, dest = argv[argv.index("-extract") + 1], Path(argv[argv.index("-extract") + 2])
            rel = src.lstrip("/")
            if rel in self.files:
                dest.write_text(self.files[rel], encoding="utf-8", newline="\n")
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 32, "", "no such file")
        if "-report_el_torito" in argv:
            return subprocess.CompletedProcess(argv, 0, self.report, "xorriso : NOTE : x\n")
        if "-find" in argv:
            out = "\n".join("'%s'" % p for p in sorted(self.listing))
            return subprocess.CompletedProcess(argv, self.list_rc, out, "xorriso : NOTE : Loading ISO image tree\n")
        return subprocess.CompletedProcess(argv, 1, "", "unknown")


def test_iso_listing_reads_the_quoted_paths_of_one_xorriso_run():
    fake = FakeXorriso({}, LISTING | {"/boot/grub/i386-pc/eltorito.img"})
    got = mc.iso_listing(Path("x.iso"), which=fake.which, run=fake)
    assert got == LISTING | {"/boot/grub/i386-pc/eltorito.img"}
    assert len(fake.calls) == 1 and fake.calls[0].count("-find") == len(mc.LISTED_DIRS)       # one run for every directory
    assert mc.iso_listing(Path("x.iso"), which=lambda n: None, run=fake) is None
    assert mc.iso_listing(Path("x.iso"), which=fake.which, run=FakeXorriso({}, [], list_rc=32)) is None


def test_check_iso_menus_reads_everything_it_needs_through_xorriso(tmp_path):
    fake = FakeXorriso(good_files(), LISTING)
    findings = mc.check_iso_menus(tmp_path / "x.iso", tmp_path / "work", which=fake.which, run=fake)
    assert fails(findings) == {}, ic.format_findings(findings)
    extracted = sorted(a[a.index("-extract") + 1] for a in fake.calls if "-extract" in a)
    assert extracted == ["/boot/grub/grub.cfg", "/boot/grub/loopback.cfg", "/isolinux/isolinux.cfg", "/isolinux/live.cfg", "/isolinux/menu.cfg"]
    assert levels(findings, "menu-script-check") == [ic.INFO]                # this fake host has no grub-script-check


def test_check_iso_menus_fails_when_the_menu_files_are_wrong_on_the_iso(tmp_path):
    files = good_files()
    files["isolinux/live.cfg"] = MINT_LIVE_CFG
    fake = FakeXorriso(files, LISTING)
    assert "menu-isolinux-base" in fails(mc.check_iso_menus(tmp_path / "x.iso", tmp_path / "work", which=fake.which, run=fake))


def test_check_iso_menus_without_xorriso_or_a_listing_fails(tmp_path):
    assert "menu-xorriso" in fails(mc.check_iso_menus(tmp_path / "x.iso", tmp_path, which=lambda n: None))
    fake = FakeXorriso({}, [], list_rc=32)
    assert "menu-listing" in fails(mc.check_iso_menus(tmp_path / "x.iso", tmp_path, which=fake.which, run=fake))


def test_run_grub_script_check_uses_the_tool_only_when_it_is_installed(tmp_path):
    p = tmp_path / "grub.cfg"
    p.write_text("set x=1\n", encoding="utf-8")
    assert mc.run_grub_script_check({"a": p}, which=lambda n: None) is None
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "syntax error")

    got = mc.run_grub_script_check({"a": p}, which=lambda n: "/usr/bin/grub-script-check", run=fake_run)
    assert got == {"a": (1, "syntax error")} and calls[0] == ["/usr/bin/grub-script-check", str(p)]


def test_main_usage_and_exit_codes(tmp_path, monkeypatch, capsys):
    assert mc.main(["--iso", str(tmp_path / "none-*.iso")]) == 2 and "no ISO matched" in capsys.readouterr().err
    iso = tmp_path / "x.iso"
    iso.write_bytes(b"x")
    monkeypatch.setattr(mc.shutil, "which", lambda n: None)
    assert mc.main(["--iso", str(iso)]) == 2 and "xorriso not found" in capsys.readouterr().err
    monkeypatch.setattr(mc.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(mc, "check_iso_menus", lambda *a, **k: [ic.ok("a"), ic.warn("b", "w")])
    assert mc.main(["--iso", str(iso)]) == 0 and "PASS (0 failed, 1 warnings)" in capsys.readouterr().out
    monkeypatch.setattr(mc, "check_iso_menus", lambda *a, **k: [ic.fail("menu-x", "broken")])
    assert mc.main(["--iso", str(iso)]) == 1 and "[FAIL] menu-x: broken" in capsys.readouterr().out


# ============================================================================================ contracts with the rest of the build
def test_the_constants_are_the_ones_the_shipped_menu_and_the_rewrite_use():
    text = GRUB
    assert mc.SUCCESS_COMMAND in text
    assert set(mc.LABELS_EXPECTED) == {boot_menu.isolinux_label(e, i) for i, e in enumerate(boot_menu.parse_grub_entries(GRUB))}
    live = boot_menu.casper_labels(MINT_LIVE_CFG)
    assert set(live) == set(mc.BASE_CASPER_LABELS)


def test_the_boot_test_job_runs_these_checks_on_the_built_iso():
    """The checks are only worth something when CI runs them on the finished ISO (ci.yml boot-test job)."""
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    seg = ci[ci.index("  boot-test:"):ci.index("  install-test:")]
    assert "build/qa/menu_checks.py" in seg and "grub-common" in seg and "--iso 'out/lindos-*.iso'" in seg
    assert re.search(r"if: always\(\)", seg[seg.index("menu_checks.py") - 400:seg.index("menu_checks.py")]), \
        "the menu checks must run even when the boot test step failed"
