"""Hermetic tests for build/qa/install_test.py - the CI INSTALL test harness (no QEMU, no root, no network).

What is covered: the initramfs overlay (newc cpio + concatenation with the ISO's initrd), the CI preseed and how
casper would parse it, the kernel command line derived from the SHIPPED boot menu (build/overlay/boot/grub/
grub.cfg), the ISO preflight (against a fake xorriso), the QEMU argument builders, the serial-log grammar, the
phase runners (fake QEMU process + fake clock), the loop-device wrapper (fake command runner), the CI-only disk
modifications, log collection with secret scrubbing, argument handling, and how .github/workflows/ci.yml wires the
job.  What cannot be tested here - that Ubiquity really accepts the preseed and installs - is exactly what the job
itself is for.
"""
from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
QA_DIR = REPO / "build" / "qa"
if str(QA_DIR) not in sys.path:
    sys.path.insert(0, str(QA_DIR))

import boot_test as bt  # noqa: E402
import install_checks as ic  # noqa: E402
import install_test as it  # noqa: E402

BASH = shutil.which("bash")
GRUB_CFG = REPO / "build" / "overlay" / "boot" / "grub" / "grub.cfg"
CI_YML = REPO / ".github" / "workflows" / "ci.yml"


def shipped_grub_text() -> str:
    """The real overlay menu with its build-time placeholders filled the way build-iso.sh fills them."""
    text = GRUB_CFG.read_text(encoding="utf-8")
    return text.replace("@PRESEED@", "").replace("@LINDOS_VERSION_SHORT@", "1.0")


OPTS = it.SeedOptions(password="0123456789abcdef01234567")


# ============================================================================================ cpio overlay
def test_cpio_roundtrip_dirs_modes_and_padding():
    blob = it.build_newc_cpio([("preseed.cfg", b"abc\n", 0o600), ("lindos-ci/ci-observer.sh", b"#!/bin/bash\n", 0o755),
                               ("lindos-ci/x.service", b"", 0o644)])
    assert len(blob) % 512 == 0                       # padded like cpio(1); the kernel skips the zeros
    got = it.parse_newc_cpio(blob)
    names = [n for n, _, _ in got]
    assert names == ["preseed.cfg", "lindos-ci", "lindos-ci/ci-observer.sh", "lindos-ci/x.service"]
    modes = {n: m for n, m, _ in got}
    assert modes["lindos-ci"] == 0o040755 and modes["preseed.cfg"] == 0o100600 and modes["lindos-ci/ci-observer.sh"] == 0o100755
    assert dict((n, d) for n, _, d in got)["preseed.cfg"] == b"abc\n"


def test_cpio_records_are_four_byte_aligned_and_end_with_the_trailer():
    blob = it.build_newc_cpio([("a", b"12345", 0o644), ("dir/bb", b"1", 0o644)])
    pos = 0
    seen = []
    while True:
        assert blob[pos:pos + 6] == b"070701" and pos % 4 == 0
        size = int(blob[pos + 54:pos + 62], 16)
        namesz = int(blob[pos + 94:pos + 102], 16)
        name = blob[pos + 110:pos + 110 + namesz - 1].decode()
        seen.append(name)
        assert blob[pos + 110 + namesz - 1] == 0                          # NUL-terminated name
        pos += 110 + namesz
        pos += (4 - pos % 4) % 4                                          # header+name padded
        pos += size + (4 - size % 4) % 4                                  # data padded
        if name == "TRAILER!!!":
            break
    assert seen == ["a", "dir", "dir/bb", "TRAILER!!!"]
    assert set(blob[pos:]) <= {0}                                         # only zero padding after the trailer


@pytest.mark.skipif(shutil.which("cpio") is None, reason="GNU cpio not available on this host")
def test_the_archive_is_readable_by_the_real_cpio(tmp_path):
    blob = it.build_newc_cpio([("preseed.cfg", b"x\n", 0o600), ("lindos-ci/ci-observer.sh", b"#!/bin/bash\n", 0o755)])
    proc = subprocess.run(["cpio", "-it"], input=blob, capture_output=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.decode().split() == ["preseed.cfg", "lindos-ci", "lindos-ci/ci-observer.sh"]


def test_combined_initrd_is_the_overlay_followed_by_the_isos_own_initrd(tmp_path):
    original = tmp_path / "initrd"
    original.write_bytes(b"\x02\x21\x4c\x18" + b"compressed-initrd" * 100)
    cpio = it.build_newc_cpio([("preseed.cfg", b"x\n", 0o600)])
    combined = it.write_combined_initrd(cpio, original, tmp_path / "out" / "initrd.overlay")
    data = combined.read_bytes()
    assert data.startswith(cpio) and data.endswith(original.read_bytes()) and len(data) == len(cpio) + original.stat().st_size
    assert it.parse_newc_cpio(data[:len(cpio)])[0][0] == "preseed.cfg"


def test_overlay_carries_the_preseed_the_observer_and_its_unit():
    script = it.OBSERVER_SCRIPT.read_bytes()
    entries = {n: (m, d) for n, m, d in it.parse_newc_cpio(it.build_overlay_cpio(it.build_preseed(OPTS), script))}
    assert set(entries) == {"preseed.cfg", "lindos-ci", "lindos-ci/ci-observer.sh", "lindos-ci/" + it.OBSERVER_UNIT}
    assert entries["lindos-ci/ci-observer.sh"] == (0o100755, script)
    assert b"partman-auto/disk" in entries["preseed.cfg"][1]
    assert b"ExecStart=/usr/local/sbin/lindos-ci-observer live" in entries["lindos-ci/" + it.OBSERVER_UNIT][1]


# ============================================================================================ preseed
def parse_like_casper(line: str):
    """casper-set-selections: owner, question and type are the first three fields, the rest of the line is the value."""
    parts = line.split(None, 3)
    return (parts + [""] * 4)[:4]


def test_preseed_lines_parse_like_casper_set_selections():
    text = it.build_preseed(OPTS)
    assert "\r" not in text and text.endswith("\n")
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    expected = it.seed_lines(OPTS)
    assert len(lines) == len(expected)
    for line, (owner, key, typ, val) in zip(lines, expected):
        assert parse_like_casper(line) == [owner, key, typ, val], line
        assert line == line.strip() and not line.endswith("\\")          # no continuation line, no stray blanks


def test_preseed_values_are_safe_for_the_initramfs_shell_loop():
    """casper-set-selections iterates `for line in $(...)`: an unquoted expansion, so a glob character in a value would be
    expanded against the initramfs root, and '$' or backticks are risky in the ash of the initramfs."""
    for owner, key, typ, val in it.seed_lines(OPTS):
        assert owner in ("d-i", "ubiquity")
        assert not re.search(r"[*?\[\]$`\\]", val), (key, val)
        assert typ in ("string", "boolean", "select", "password", "note", "multiselect")
        assert re.fullmatch(r"[a-z0-9-]+(/[A-Za-z0-9_.-]+)+", key), key


def test_preseed_answers_every_page_the_automatic_installer_would_stop_at():
    have = {k: v for _, k, _, v in it.seed_lines(OPTS)}
    assert have["partman-auto/disk"] == "/dev/vda" and have["partman-auto/method"] == "regular"
    assert have["partman-auto/choose_recipe"] == "atomic"
    assert have["partman/choose_partition"] == "finish" and have["partman/confirm"] == "true"
    assert have["partman/confirm_nooverwrite"] == "true" and have["partman-partitioning/confirm_new_label"] == "true"
    assert have["grub-installer/bootdev"] == "/dev/vda"
    assert have["ubiquity/poweroff"] == "true" and have["ubiquity/reboot"] == "false"
    assert have["passwd/username"] == "oem" and have["passwd/user-password"] == have["passwd/user-password-again"] == OPTS.password
    assert have["time/zone"] == "UTC" and have["debian-installer/locale"] == "en_US.UTF-8"
    assert have["debian-installer/language"] == "en" and have["debian-installer/country"] == "US"
    assert have["ubiquity/download_updates"] == "false"


def test_preseed_never_repeats_what_the_image_bakes():
    """The test proves the SHIPPED answers reach Ubiquity: success_command and oem-config/enable stay with the boot entry
    and lindos.seed, never in the CI file."""
    keys = {k for _, k, _, _ in it.seed_lines(OPTS)}
    assert "ubiquity/success_command" not in keys and "oem-config/enable" not in keys
    assert "user-setup/allow-password-empty" not in keys and "apt-setup/multiarch" not in keys


def test_redacted_preseed_hides_the_password_and_nothing_else():
    full, red = it.build_preseed(OPTS), it.build_preseed(OPTS, redact=True)
    assert OPTS.password in full and OPTS.password not in red and red.count("<redacted>") == 2
    assert full.replace(OPTS.password, "<redacted>") == red


def test_early_command_is_one_line_and_installs_the_observer_into_the_live_root():
    cmd = it.early_command()
    assert "\n" not in cmd and "/root" in cmd and "/lindos-ci/ci-observer.sh" in cmd
    assert it.OBSERVER_UNIT in cmd and "multi-user.target.wants" in cmd


@pytest.mark.skipif(BASH is None, reason="bash not available on this host")
def test_early_command_really_installs_the_observer_when_run(tmp_path):
    """Run the shipped command against a fake initramfs: '/root' and '/lindos-ci' are redirected into tmp_path."""
    ci = tmp_path / "ci"
    ci.mkdir()
    (ci / "ci-observer.sh").write_text("#!/bin/bash\necho hi\n", encoding="utf-8", newline="\n")
    (ci / it.OBSERVER_UNIT).write_text("[Unit]\n", encoding="utf-8", newline="\n")
    root = tmp_path / "root"
    # the link TARGET is a path of the booted system (/etc/...); here it must exist for a Windows 'ln -s', which copies
    cmd = it.early_command().replace("ln -sf /etc/", "ln -sf @ROOT@/etc/").replace("/lindos-ci/", ci.as_posix() + "/")
    cmd = re.sub(r"(?<![A-Za-z@])/root", "@ROOT@", cmd).replace("@ROOT@", root.as_posix())
    (root / "etc/systemd/system").mkdir(parents=True)
    proc = subprocess.run([BASH, "-c", cmd], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    assert (root / "usr/local/sbin/lindos-ci-observer").read_text(encoding="utf-8") == "#!/bin/bash\necho hi\n"
    assert (root / "etc/systemd/system" / it.OBSERVER_UNIT).exists()
    assert (root / "etc/systemd/system/multi-user.target.wants" / it.OBSERVER_UNIT).exists()      # a symlink (or a copy on Windows)


def test_observer_units():
    live, oem = it.observer_unit("live"), it.observer_unit("oem")
    assert "ConditionKernelCommandLine=lindos.ci_install_test" in live and "WantedBy=multi-user.target" in live
    assert "ExecStart=/usr/local/sbin/lindos-ci-observer live" in live
    assert "DefaultDependencies=no" in oem and "WantedBy=sysinit.target" in oem and "lindos-ci-observer oem" in oem
    assert "ConditionKernelCommandLine" not in oem          # the installed system is not started with the install flag
    for text in (live, oem):
        assert "\r" not in text and text.startswith("[Unit]\n")


# ============================================================================================ kernel command line
def test_command_line_is_built_from_the_shipped_install_entry():
    words = it.entry_words_from_grub(shipped_grub_text())
    assert words is not None
    cmd = it.install_cmdline(words, mode="automatic", budget=2400)
    parts = cmd.split()
    for w in ("boot=casper", "only-ubiquity", "oem-config/enable=true", "username=liveuser", "hostname=lindos",
              "ubiquity/success_command=/usr/libexec/lindos/installer/finalize.sh",
              "automatic-ubiquity", "noprompt", "plymouth.enable=0", "console=ttyS0,115200n8", "lindos.install_budget=2400",
              it.CI_INSTALL_FLAG, "debconf/priority=critical"):
        assert w in parts, w
    assert parts[-1] == "--" and parts.count("--") == 1
    for w in ("quiet", "splash", "nomodeset", "noninteractive"):
        assert w not in parts
    assert not any(w.startswith("iso-scan/") or "${" in w or "@" in w for w in parts)


def test_noninteractive_mode_and_extra_words_and_no_duplicates():
    words = list(it.FALLBACK_ENTRY_WORDS) + ["noprompt"]
    cmd = it.install_cmdline(words, mode="noninteractive", budget=60, extra=["foo=bar"]).split()
    assert "noninteractive" in cmd and "automatic-ubiquity" not in cmd and "foo=bar" in cmd
    assert cmd.count("noprompt") == 1


def test_the_fallback_words_are_the_shipped_install_entry():
    real = it.entry_words_from_grub(shipped_grub_text())
    assert [w for w in real if w not in ("quiet", "splash")] == list(it.FALLBACK_ENTRY_WORDS)


def test_install_entry_choice_skips_compat_and_try_entries():
    entries = boot_menu_entries()
    chosen = it.pick_install_entry(entries)
    assert chosen.title.startswith("Install Lindos") and "compatibility" not in chosen.title
    assert it.pick_install_entry([e for e in entries if "nomodeset" in e.args or "only-ubiquity" not in e.args]) is None


def boot_menu_entries():
    import boot_menu
    return boot_menu.parse_grub_entries(shipped_grub_text())


def test_entry_words_are_none_without_an_install_entry():
    assert it.entry_words_from_grub("menuentry \"Try\" {\n\tlinux /casper/vmlinuz boot=casper quiet --\n}\n") is None


# ============================================================================================ ISO preflight
def fake_xorriso(files=None, finds=None, missing_tool=False):
    files, finds = files or {}, finds or {}
    calls = []

    def which(name):
        return None if missing_tool else "/usr/bin/xorriso"

    def run(argv, **kw):
        calls.append(argv)
        if "-extract" in argv:
            src, dest = argv[argv.index("-extract") + 1], Path(argv[argv.index("-extract") + 2])
            if src in files:
                dest.write_text(files[src], encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 1, "", "no such file")
        top, pat = argv[argv.index("-find") + 1], argv[argv.index("-name") + 1]
        out = "\n".join("'%s'" % p for p in finds.get((top, pat), []))
        return subprocess.CompletedProcess(argv, 0, out, "")

    return which, run, calls


def test_preflight_passes_on_an_iso_that_carries_the_flow(tmp_path):
    which, run, _ = fake_xorriso(
        {"/boot/grub/grub.cfg": shipped_grub_text(), "/casper/filesystem.manifest-remove": "ubiquity\nlindos-installer\n"},
        {("/pool", "oem-config*"): ["/pool/main/u/ubiquity/oem-config_24.04.3+mint18_all.deb",
                                    "/pool/main/u/ubiquity/oem-config-gtk_24.04.3+mint18_all.deb"]})
    findings, words = it.preflight_iso(tmp_path / "x.iso", tmp_path / "work", which=which, run=run)
    assert not ic.failures(findings), ic.format_findings(findings)
    assert {f.name for f in findings} == {"iso-boot-entry", "iso-oem-pool", "iso-manifest-remove"}
    assert "only-ubiquity" in words and "quiet" in words                      # the raw entry words; install_cmdline drops quiet


def test_preflight_fails_when_no_oem_config_is_on_the_medium(tmp_path):
    which, run, _ = fake_xorriso({"/boot/grub/grub.cfg": shipped_grub_text()})
    findings, _ = it.preflight_iso(tmp_path / "x.iso", tmp_path / "work", which=which, run=run)
    assert "iso-oem-pool" in {f.name for f in ic.failures(findings)}


def test_preflight_accepts_the_bundled_fallback_debs(tmp_path):
    which, run, _ = fake_xorriso({"/boot/grub/grub.cfg": shipped_grub_text()},
                                 {("/lindos/oem-debs", "*.deb"): ["/lindos/oem-debs/oem-config_1_all.deb"]})
    findings, _ = it.preflight_iso(tmp_path / "x.iso", tmp_path / "work", which=which, run=run)
    assert "iso-oem-pool" not in {f.name for f in ic.failures(findings)}


def test_preflight_fails_when_the_install_entry_lost_its_success_command(tmp_path):
    grub = shipped_grub_text().replace(" ubiquity/success_command=/usr/libexec/lindos/installer/finalize.sh", "")
    which, run, _ = fake_xorriso({"/boot/grub/grub.cfg": grub},
                                 {("/pool", "oem-config*"): ["/pool/a/oem-config_1_all.deb", "/pool/a/oem-config-gtk_1_all.deb"]})
    findings, _ = it.preflight_iso(tmp_path / "x.iso", tmp_path / "work", which=which, run=run)
    assert "success_command" in {f.name: f.detail for f in ic.failures(findings)}["iso-boot-entry"]


def test_preflight_falls_back_to_the_builtin_words_when_grub_cfg_is_unreadable(tmp_path):
    which, run, _ = fake_xorriso({}, {("/pool", "oem-config*"): ["/pool/a/oem-config_1_all.deb", "/pool/a/oem-config-gtk_1_all.deb"]})
    findings, words = it.preflight_iso(tmp_path / "x.iso", tmp_path / "work", which=which, run=run)
    assert words == list(it.FALLBACK_ENTRY_WORDS)
    assert [f.level for f in findings if f.name == "iso-boot-entry"] == [ic.WARN]
    assert "iso-manifest-remove" in {f.name for f in ic.warnings(findings)}


def test_preflight_without_xorriso_cannot_confirm_anything(tmp_path):
    which, run, _ = fake_xorriso(missing_tool=True)
    findings, _ = it.preflight_iso(tmp_path / "x.iso", tmp_path / "work", which=which, run=run)
    assert "iso-oem-pool" in {f.name for f in ic.failures(findings)}


def test_stale_extraction_target_is_removed_first(tmp_path):
    which, run, calls = fake_xorriso({"/boot/grub/grub.cfg": "new"})
    dest = tmp_path / "grub.cfg"
    dest.write_text("stale", encoding="utf-8")
    assert it.iso_extract(tmp_path / "x.iso", "/boot/grub/grub.cfg", dest, which=which, run=run)
    assert dest.read_text(encoding="utf-8") == "new"


# ============================================================================================ QEMU argv
def _install_argv(tmp_path: Path, **kw):
    base = dict(vmlinuz=tmp_path / "k", initrd=tmp_path / "i", iso=tmp_path / "x.iso", disk=tmp_path / "d.raw",
                serial_log=tmp_path / "s.log", monitor_sock=tmp_path / "m.sock", append="boot=casper --", ram_mb=6144, cpus=4)
    base.update(kw)
    return it.build_install_argv(**base)


def test_install_argv_boots_the_iso_kernel_directly_with_a_blank_virtio_disk_and_a_cdrom(tmp_path):
    argv = _install_argv(tmp_path)
    assert argv[0] == "qemu-system-x86_64"
    accels = [argv[i + 1] for i, a in enumerate(argv) if a == "-accel"]
    assert accels == ["kvm", "tcg"]
    assert argv[argv.index("-kernel") + 1] == str(tmp_path / "k") and argv[argv.index("-initrd") + 1] == str(tmp_path / "i")
    assert argv[argv.index("-cdrom") + 1] == str(tmp_path / "x.iso")
    drive = argv[argv.index("-drive") + 1]
    assert drive.startswith("file=%s," % (tmp_path / "d.raw")) and "if=virtio" in drive and "format=raw" in drive
    assert argv[argv.index("-append") + 1] == "boot=casper --"
    assert "-no-reboot" in argv                                        # a poweroff (or reboot) ends QEMU: that is how the test ends
    assert argv[argv.index("-display") + 1] == "none" and "virtio-vga" in argv    # screendump needs a real display device
    assert argv[argv.index("-serial") + 1] == "file:%s" % (tmp_path / "s.log")
    assert argv[argv.index("-monitor") + 1].startswith("unix:%s," % (tmp_path / "m.sock"))
    assert argv[argv.index("-netdev") + 1] == "user,id=n0" and "virtio-net-pci,netdev=n0" in argv   # real (user-mode) networking
    assert argv[argv.index("-m") + 1] == "6144" and argv[argv.index("-smp") + 1] == "4"


def test_network_off_boots_without_a_nic(tmp_path):
    argv = _install_argv(tmp_path, network="off")
    assert "-netdev" not in argv and argv[argv.index("-nic") + 1] == "none"


def test_uefi_needs_ovmf_and_adds_the_pflash_drives(tmp_path):
    with pytest.raises(ValueError, match="OVMF"):
        _install_argv(tmp_path, firmware="uefi")
    argv = _install_argv(tmp_path, firmware="uefi", ovmf=("/c.fd", "/v.fd"))
    pflash = [argv[i + 1] for i, a in enumerate(argv) if a == "-drive" and "pflash" in argv[i + 1]]
    assert len(pflash) == 2 and "readonly=on,file=/c.fd" in pflash[0] and pflash[1].endswith("file=/v.fd")


def test_boot_argv_has_no_cdrom_and_boots_through_the_disk(tmp_path):
    common = dict(disk=tmp_path / "d.raw", serial_log=tmp_path / "s", monitor_sock=tmp_path / "m", ram_mb=4096, cpus=2)
    grub = it.build_boot_argv(**common)
    assert "-cdrom" not in grub and "-kernel" not in grub and "-initrd" not in grub
    assert grub[grub.index("-boot") + 1] == "c"
    direct = it.build_boot_argv(kernel=tmp_path / "vmlinuz", initrd=tmp_path / "initrd",
                                append=it.kernel_append_for_disk_boot("/dev/vda1"), **common)
    assert direct[direct.index("-kernel") + 1] == str(tmp_path / "vmlinuz") and "-cdrom" not in direct


def test_disk_boot_command_line_is_not_a_live_boot():
    words = it.kernel_append_for_disk_boot("/dev/vda1").split()
    assert "root=/dev/vda1" in words and "console=ttyS0,115200n8" in words and "plymouth.enable=0" in words
    assert "boot=casper" not in words and not any(w.startswith("only-ubiquity") for w in words)


def test_find_ovmf_takes_the_first_complete_pair():
    have = {"/usr/share/OVMF/OVMF_CODE.fd", "/usr/share/OVMF/OVMF_VARS.fd", "/usr/share/OVMF/OVMF_CODE_4M.fd"}
    assert it.find_ovmf(lambda p: p in have) == ("/usr/share/OVMF/OVMF_CODE.fd", "/usr/share/OVMF/OVMF_VARS.fd")
    assert it.find_ovmf(lambda p: False) is None


def test_no_vm_detection_evasion_in_the_harness():
    """Honesty rules (SPEC 0.1 / SPEC-VM 20): the guest is an ordinary, honest QEMU machine."""
    for path in (QA_DIR / "install_test.py", QA_DIR / "install_checks.py", QA_DIR / "ci-observer.sh"):
        text = path.read_text(encoding="utf-8").lower()
        for token in ("kvm=off", "hv-vendor-id", "hv_vendor_id", "-smbios", "acpitable", "hidden state"):
            assert token not in text, (path.name, token)


# ============================================================================================ serial parsing
INSTALL_SERIAL = """\
[    0.000000] Linux version 6.14.0-lindos
LINDOS_OBSERVER_STARTED mode=live uname=6.14.0-lindos
LINDOS_HOOK_LOG 2026-09-29 10:00:00 lindos-installer: start (version 1.0.0, target /target, budget 2400s)
LINDOS_HOOK_LOG 2026-09-29 10:05:00 lindos-installer: step browser: done - ok
LINDOS_CHECK live-only-ubiquity=OK
LINDOS_CHECK live-installer-up=OK
LINDOS_CHECK live-no-lightdm=OK
LINDOS_CHECK live-no-xfce-session=OK
LINDOS_CHECK live-no-lindos-setup=OK
LINDOS_CHECK live-no-pkexec=OK
LINDOS_CHECK live-inhibitor-active=OK
LINDOS_CHECK live-installer-theme=OK
LINDOS_INSTALL_SESSION_CHECKED fails=0
LINDOS_INSTALL_HEARTBEAT tick=12 ubiquity=active target=1024/9000KiB procs=9
LINDOS_INSTALL_UBIQUITY_EXIT result=success
[  900.000000] reboot: Power down
"""


def test_parse_install_serial():
    got = it.parse_install_serial(INSTALL_SERIAL)
    assert got["booted"] and got["observer"] and got["power_down"] and not got["restarting"] and not got["panic"]
    assert len(got["hook_lines"]) == 2 and got["hook_lines"][0].endswith("budget 2400s)")
    assert got["ubiquity_exit"] == "result=success" and got["failed"] == []
    failed = it.parse_install_serial("LINDOS_INSTALL_FAILED ubiquity.service result=exit-code state=failed\nKernel panic - not syncing\n")
    assert failed["failed"] == ["ubiquity.service result=exit-code state=failed"] and failed["panic"]
    assert it.parse_install_serial("reboot: Restarting system")["restarting"]


def test_a_finalized_but_not_powered_off_guest_is_a_warning_not_a_failure():
    findings = it.judge_install_phase({"outcome": "finalized-no-poweroff", "seconds": 900},
                                      it.parse_install_serial("Linux version x\nLINDOS_INSTALL_FINALIZED\n"))
    assert not ic.failures(findings)
    assert any(f.name == "install-poweroff" and f.level == ic.WARN and "ACPI" in f.detail for f in findings)
    assert it.parse_install_serial("LINDOS_INSTALL_FINALIZED\n")["finalized"] is True
    assert it.parse_install_serial("x\n")["finalized"] is False


def test_judging_the_install_phase():
    good = it.judge_install_phase({"outcome": "exited", "seconds": 1800}, it.parse_install_serial(INSTALL_SERIAL))
    assert not ic.failures(good) and any(f.name == "install-poweroff" and f.level == ic.OK for f in good)
    stuck = it.judge_install_phase({"outcome": "timeout", "seconds": 5400}, it.parse_install_serial("Linux version x\n"))
    assert "install-finished" in {f.name for f in ic.failures(stuck)}
    assert any(f.name == "install-observer" and f.level == ic.WARN for f in stuck)
    crashed = it.judge_install_phase({"outcome": "failed-marker", "seconds": 100}, it.parse_install_serial(
        "Linux version x\nLINDOS_INSTALL_FAILED ubiquity.service result=exit-code state=failed\n"))
    assert "result=exit-code" in {f.name: f.detail for f in ic.failures(crashed)}["install-finished"]
    dead = it.judge_install_phase({"outcome": "exited", "seconds": 5}, it.parse_install_serial(""))
    assert "install-kernel" in {f.name for f in ic.failures(dead)}
    rebooted = it.judge_install_phase({"outcome": "exited", "seconds": 5}, it.parse_install_serial("Linux version x\nreboot: Restarting system\n"))
    assert any(f.name == "install-poweroff" and f.level == ic.WARN for f in rebooted)


OEM_SERIAL = """\
Linux version 6.14.0-lindos
LINDOS_OBSERVER_STARTED mode=oem uname=6.14.0-lindos
LINDOS_CHECK oem-default-target=OK
LINDOS_CHECK oem-config-service=OK
LINDOS_CHECK oem-no-lightdm=FAIL rc=1
LINDOS_FAIL_LOG oem-no-lightdm: LightDM is running before the account exists
LINDOS_INFO oem_default_target=oem-config.target
LINDOS_OEM_PS 812 ubiquity-dm vt7 :0 oem
LINDOS_OEM_READY fails=1
"""


def test_parse_oem_serial_and_judging_the_first_boot():
    got = it.parse_oem_serial(OEM_SERIAL)
    assert got["observer"] and got["ready"] and not got["timeout"] and got["fails"] == 1
    assert got["checks"]["oem-no-lightdm"] == {"status": "FAIL", "rc": "1"} and got["info"]["oem_default_target"] == "oem-config.target"
    findings = it.judge_first_boot(got, screenshot_ok=True, screenshot_reason="ok")
    bad = {f.name: f.detail for f in ic.failures(findings)}
    assert list(bad) == ["first-boot-oem-no-lightdm"] and "LightDM is running" in bad["first-boot-oem-no-lightdm"]
    assert any(f.name == "boot-oem-config" and f.level == ic.OK for f in findings)


def test_first_boot_verdicts_for_the_failure_modes():
    silent = it.judge_first_boot(it.parse_oem_serial("GRUB error\n"), screenshot_ok=None)
    assert "boot-observer" in {f.name for f in ic.failures(silent)}
    timeout = it.judge_first_boot(it.parse_oem_serial(
        "LINDOS_OBSERVER_STARTED mode=oem uname=x\nLINDOS_OEM_DIAG status: failed\nLINDOS_OEM_TIMEOUT fails=3\n"), screenshot_ok=None)
    assert "boot-oem-config" in {f.name for f in ic.failures(timeout)}
    panic = it.judge_first_boot(it.parse_oem_serial("Kernel panic - not syncing\nLINDOS_OBSERVER_STARTED mode=oem uname=x\n"), screenshot_ok=None)
    assert "boot-kernel" in {f.name for f in ic.failures(panic)}
    blank = it.judge_first_boot(it.parse_oem_serial(OEM_SERIAL.replace("FAIL rc=1", "OK")), screenshot_ok=False, screenshot_reason="1 colour")
    assert {f.name for f in ic.failures(blank)} == {"boot-screenshot"}


def test_serial_grammar_is_the_one_ci_observer_prints():
    """The harness parses what the shipped observer prints: keep the two in step (ci-observer.sh is the source)."""
    text = it.OBSERVER_SCRIPT.read_text(encoding="utf-8")
    for token in ("LINDOS_OBSERVER_STARTED mode=", "LINDOS_HOOK_LOG", "LINDOS_INSTALL_FAILED", "LINDOS_INSTALL_UBIQUITY_EXIT",
                  "LINDOS_OEM_READY", "LINDOS_OEM_TIMEOUT", "LINDOS_CHECK", "LINDOS_FAIL_LOG"):
        assert token in text, token


# ============================================================================================ phase runners
class FakeProc:
    def __init__(self, exit_after=None, rc=0):
        self.polls = 0
        self.exit_after = exit_after
        self.returncode = None
        self._rc = rc
        self.terminated = False

    def poll(self):
        self.polls += 1
        if self.exit_after is not None and self.polls > self.exit_after:
            self.returncode = self._rc
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class Clock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, s):
        self.now += s

    def monotonic(self):
        return self.now


def _phase(tmp_path, proc, clock, *, serial="", timeout=100.0, shots_every=1000.0, shots=None, **kw):
    """Run run_install_phase against a fake QEMU process and a fake clock."""
    log = tmp_path / "serial.log"
    log.write_text(serial, encoding="utf-8")
    taken: List[str] = [] if shots is None else shots

    def shoot(sock, png):
        taken.append(png.name)
        return True

    out = it.run_install_phase(["qemu"], serial_log=log, monitor_sock=tmp_path / "m", shots_dir=tmp_path / "shots",
                               timeout=timeout, shots_every=shots_every, poll=10.0, popen=lambda argv: proc,
                               sleep=clock.sleep, monotonic=clock.monotonic, shoot=shoot, **kw)
    return out, taken


def test_install_phase_ends_when_the_guest_powers_off(tmp_path):
    clock = Clock()
    out, taken = _phase(tmp_path, FakeProc(exit_after=3, rc=0), clock)
    assert out["outcome"] == "exited" and out["rc"] == 0 and out["seconds"] == 30 and taken == []


def test_install_phase_times_out_and_stops_qemu(tmp_path):
    proc = FakeProc()
    out, taken = _phase(tmp_path, proc, Clock(), timeout=100.0)
    assert out["outcome"] == "timeout" and proc.terminated and out["seconds"] == 100 and taken == ["install-timeout.png"]


def test_install_phase_takes_progress_screenshots(tmp_path):
    out, taken = _phase(tmp_path, FakeProc(exit_after=12), Clock(), shots_every=30.0, timeout=1000.0)
    assert taken == ["install-progress-001.png", "install-progress-002.png", "install-progress-003.png"]     # t=30, 60, 90; t=120 it exited
    assert out["screenshots"] == taken and out["outcome"] == "exited"


def test_install_phase_shuts_down_a_finished_guest_that_does_not_power_off(tmp_path):
    """finalize.sh ran but Ubiquity ignored ubiquity/poweroff (it waits at its 'finished' dialog): after the grace the
    harness presses the power button, so the finished installation is judged now instead of at the install timeout."""
    proc, pressed = FakeProc(), []

    def powerdown(sock):
        pressed.append(sock)
        proc.exit_after = proc.polls + 2               # a clean ACPI shutdown takes a moment
        return True

    out, taken = _phase(tmp_path, proc, Clock(), timeout=5000.0, finalize_grace=100.0, powerdown=powerdown,
                        serial="LINDOS_INSTALL_FINALIZED\n")
    assert out["outcome"] == "finalized-no-poweroff" and len(pressed) == 1 and taken == ["install-finished.png"]
    assert 100 <= out["seconds"] <= 140 and proc.returncode == 0          # exited by itself after the power button


def test_install_phase_falls_back_to_killing_qemu_when_the_power_button_is_ignored(tmp_path):
    proc = FakeProc()
    out, _ = _phase(tmp_path, proc, Clock(), timeout=5000.0, finalize_grace=50.0, powerdown_wait=30.0,
                    powerdown=lambda sock: True, serial="LINDOS_INSTALL_FINALIZED\n")
    assert out["outcome"] == "finalized-no-poweroff" and proc.terminated


def test_a_guest_that_powers_off_by_itself_after_finalizing_is_a_plain_exit(tmp_path):
    out, taken = _phase(tmp_path, FakeProc(exit_after=3), Clock(), finalize_grace=100.0, serial="LINDOS_INSTALL_FINALIZED\n")
    assert out["outcome"] == "exited" and taken == []


def test_install_phase_stops_early_when_the_guest_reports_failure(tmp_path):
    proc = FakeProc()
    out, taken = _phase(tmp_path, proc, Clock(), timeout=5000.0, fail_grace=45.0,
                        serial="LINDOS_INSTALL_FAILED ubiquity.service result=exit-code state=failed\n")
    assert out["outcome"] == "failed-marker" and proc.terminated and 40 <= out["seconds"] <= 60 and taken == ["install-failed.png"]


def test_first_boot_phase_waits_for_the_observer_then_shoots_after_the_grace(tmp_path):
    proc, order = FakeProc(), []

    def wait_for(log, patterns, timeout):
        order.append(("wait", timeout, [p.pattern for p in patterns]))
        return "LINDOS_OEM_READY fails=0"

    out = it.run_first_boot_phase(["qemu"], serial_log=tmp_path / "s", monitor_sock=tmp_path / "m", screenshot=tmp_path / "s.png",
                                  timeout=900, grace=20, popen=lambda argv: proc, sleep=lambda s: order.append(("sleep", s)),
                                  wait_for=wait_for, shoot=lambda sock, png: order.append(("shot", png.name)) or True)
    assert out == {"line": "LINDOS_OEM_READY fails=0", "screenshot": True} and proc.terminated
    assert [o[0] for o in order] == ["wait", "sleep", "shot"] and order[1] == ("sleep", 20)
    assert any("LINDOS_OEM_READY" in p for p in order[0][2]) and any("LINDOS_OEM_TIMEOUT" in p for p in order[0][2])


def test_first_boot_phase_still_takes_a_picture_when_the_observer_never_speaks(tmp_path):
    slept, shots = [], []
    out = it.run_first_boot_phase(["qemu"], serial_log=tmp_path / "s", monitor_sock=tmp_path / "m", screenshot=tmp_path / "s.png",
                                  timeout=5, grace=20, popen=lambda argv: FakeProc(), sleep=slept.append,
                                  wait_for=lambda *a, **k: None, shoot=lambda sock, png: shots.append(png) or True)
    assert out["line"] is None and slept == [] and len(shots) == 1


def test_stop_qemu_kills_a_process_that_ignores_terminate():
    class Stubborn(FakeProc):
        def wait(self, timeout=None):
            if self.returncode is None:
                raise subprocess.TimeoutExpired("qemu", timeout)
            return self.returncode

        def terminate(self):
            self.terminated = True

    p = Stubborn()
    it.stop_qemu(p)
    assert p.terminated and p.returncode == -9


# ============================================================================================ loop devices
class FakeRunner:
    """Answers losetup/mount/umount like a Linux host would, and mounts by filling the target directory."""

    def __init__(self, layouts, fail_mounts=()):
        self.layouts = layouts            # device -> list of relative dirs to create when it is mounted
        self.fail_mounts = set(fail_mounts)
        self.calls: List[List[str]] = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        cmd = argv[argv.index("losetup") if "losetup" in argv else 0:]
        name = argv[0] if argv[0] != "sudo" else argv[2]
        if name == "losetup" and "--find" in argv:
            return subprocess.CompletedProcess(argv, 0, "/dev/loop9\n", "")
        if name == "mount":
            dev, target = argv[-2], Path(argv[-1])
            if dev in self.fail_mounts:
                return subprocess.CompletedProcess(argv, 32, "", "wrong fs type")
            for rel in self.layouts.get(dev, []):
                (target / rel).mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(argv, 0, "", "")
        del cmd
        return subprocess.CompletedProcess(argv, 0, "", "")


def _verbs(calls):
    return [(c[2] if c[0] == "sudo" else c[0]) for c in calls]


def test_loop_disk_finds_the_system_and_the_esp_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setattr(it, "_euid", lambda: 0)
    runner = FakeRunner({"/dev/loop9p1": ["EFI/ubuntu"], "/dev/loop9p2": ["etc", "var/lib/dpkg"], "/dev/loop9p3": ["x"]},
                        fail_mounts={"/dev/loop9p3"})
    with it.LoopDisk(tmp_path / "d.raw", tmp_path / "mnt", run=runner,
                     list_partitions=lambda loop: [loop + "p1", loop + "p2", loop + "p3"]) as disk:
        root, esp, dev = disk.find_system()
        assert root == tmp_path / "mnt" / "p2" and esp == tmp_path / "mnt" / "p1" and dev == "/dev/loop9p2"
    verbs = _verbs(runner.calls)
    assert verbs[0] == "losetup" and verbs[-1] == "losetup" and runner.calls[-1][:2] == ["losetup", "-d"]
    assert verbs.count("umount") == 2 and "sync" in verbs
    assert not disk.mounts and disk.loop == ""


def test_loop_disk_mounts_read_only_first_and_read_write_only_on_request(tmp_path, monkeypatch):
    monkeypatch.setattr(it, "_euid", lambda: 0)
    runner = FakeRunner({"/dev/loop9p1": ["etc", "var/lib/dpkg"]})
    with it.LoopDisk(tmp_path / "d.raw", tmp_path / "mnt", run=runner, list_partitions=lambda loop: [loop + "p1"]) as disk:
        disk.find_system()
    mount = [c for c in runner.calls if c[0] == "mount"][0]
    assert mount[:3] == ["mount", "-o", "ro,noload"]
    runner2 = FakeRunner({"/dev/loop9p1": ["etc", "var/lib/dpkg"]})
    with it.LoopDisk(tmp_path / "d2.raw", tmp_path / "mnt2", run=runner2, list_partitions=lambda loop: [loop + "p1"]) as disk:
        disk.find_system(rw=True)
    assert [c for c in runner2.calls if c[0] == "mount"][0][:3] == ["mount", "-o", "rw"]


def test_loop_disk_detaches_even_when_the_body_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(it, "_euid", lambda: 0)
    runner = FakeRunner({})
    with pytest.raises(RuntimeError, match="boom"):
        with it.LoopDisk(tmp_path / "d.raw", tmp_path / "mnt", run=runner, list_partitions=lambda loop: []):
            raise RuntimeError("boom")
    assert runner.calls[-1][:2] == ["losetup", "-d"]


def test_loop_disk_without_a_partition_table_uses_the_loop_device_itself(tmp_path, monkeypatch):
    monkeypatch.setattr(it, "_euid", lambda: 0)
    runner = FakeRunner({"/dev/loop9": ["etc", "var/lib/dpkg"]})
    with it.LoopDisk(tmp_path / "d.raw", tmp_path / "mnt", run=runner, list_partitions=lambda loop: [], sleep=lambda s: None) as disk:
        assert disk.find_system()[2] == "/dev/loop9"


def test_a_failed_losetup_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(it, "_euid", lambda: 0)
    with pytest.raises(RuntimeError, match="losetup"):
        with it.LoopDisk(tmp_path / "d.raw", tmp_path / "m", run=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "no loop")):
            pass


def test_priv_uses_sudo_only_when_not_root(monkeypatch):
    monkeypatch.setattr(it, "_euid", lambda: 0)
    assert it.priv(["mount", "x"]) == ["mount", "x"]
    monkeypatch.setattr(it, "_euid", lambda: 1000)
    assert it.priv(["mount", "x"]) == ["sudo", "-n", "mount", "x"]


# ============================================================================================ CI-only disk changes
def test_patch_grub_cfg_adds_a_serial_console_to_every_linux_line():
    text = ("set default=0\nmenuentry 'A' {\n\tlinux\t/boot/vmlinuz-1 root=UUID=x ro quiet splash $vt_handoff\n\tinitrd /boot/initrd.img-1\n}\n"
            "menuentry 'B' {\n  linux /boot/vmlinuz-2 root=UUID=x ro\n}\n")
    new, n = it.patch_grub_cfg(text)
    assert n == 2 and new.endswith("\n")
    linux = [ln for ln in new.splitlines() if ln.strip().startswith("linux")]
    for ln in linux:
        words = ln.split()
        assert "console=ttyS0,115200n8" in words and "plymouth.enable=0" in words and "systemd.show_status=1" in words
        assert "quiet" not in words and "splash" not in words and "root=UUID=x" in words
    assert "$vt_handoff" in linux[0] and "initrd /boot/initrd.img-1" in new
    again, n2 = it.patch_grub_cfg(new)
    assert again == new and n2 == 2                                   # idempotent


def test_patch_grub_cfg_leaves_a_config_without_linux_lines_alone():
    assert it.patch_grub_cfg("set default=0\n") == ("set default=0\n", 0)


def test_inject_observer_writes_script_unit_and_wants_link(tmp_path):
    links = []
    written = it.inject_observer(tmp_path, b"#!/bin/bash\n", symlink=lambda target, link: links.append((target, link)))
    assert (tmp_path / "usr/local/sbin/lindos-ci-observer").read_bytes() == b"#!/bin/bash\n"
    unit = (tmp_path / "etc/systemd/system" / it.OBSERVER_UNIT).read_text(encoding="utf-8")
    assert unit == it.observer_unit("oem")
    assert links == [("/etc/systemd/system/" + it.OBSERVER_UNIT,
                      str(tmp_path / "etc/systemd/system/sysinit.target.wants" / it.OBSERVER_UNIT))]
    assert it.OBSERVER_BIN in written and len(written) == 3


def test_patch_grub_on_disk(tmp_path):
    assert it.patch_grub_on_disk(tmp_path) == 0
    cfg = tmp_path / "boot/grub/grub.cfg"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("menuentry 'x' {\n linux /vmlinuz root=UUID=a ro quiet\n}\n", encoding="utf-8", newline="\n")
    assert it.patch_grub_on_disk(tmp_path) == 1 and "console=ttyS0" in cfg.read_text(encoding="utf-8")


def test_read_mbr(tmp_path):
    (tmp_path / "d").write_bytes(b"A" * 1024)
    assert it.read_mbr(tmp_path / "d") == b"A" * 512 and it.read_mbr(tmp_path / "absent") is None


# ============================================================================================ logs
def test_collect_logs_scrubs_the_password_and_caps_size(tmp_path):
    root = tmp_path / "root"
    (root / "var/log/installer").mkdir(parents=True)
    (root / "var/log/lindos").mkdir(parents=True)
    (root / "var/lib/lindos").mkdir(parents=True)
    (root / "var/log/installer/debug").write_text("db_set passwd/user-password s3cr3t-pw-value\nok\n", encoding="utf-8")
    (root / "var/log/installer/syslog").write_bytes(b"x" * ((6 << 20) + 100) + b"TAIL s3cr3t-pw-value")
    (root / "var/log/lindos/installer.log").write_text("hook\n", encoding="utf-8")
    (root / "var/lib/lindos/install-state.json").write_text("{}", encoding="utf-8")
    (root / "var/lib/dpkg").mkdir(parents=True)
    (root / "var/lib/dpkg/status").write_text(
        "Package: a\nStatus: install ok installed\nVersion: 1\nArchitecture: amd64\n\n", encoding="utf-8")
    dest = tmp_path / "out"
    copied = it.collect_logs(ic.Tree(root), dest, ["s3cr3t-pw-value"])
    assert {"var/log/installer/debug", "var/log/installer/syslog", "var/log/lindos/installer.log",
            "var/lib/lindos/install-state.json"} <= set(copied)
    debug = (dest / "var__log__installer__debug").read_text(encoding="utf-8")
    assert "s3cr3t" not in debug and "<redacted>" in debug and "ok" in debug
    syslog = (dest / "var__log__installer__syslog").read_text(encoding="utf-8")
    assert len(syslog) <= (6 << 20) + 32 and syslog.endswith("TAIL <redacted>")
    assert (dest / "dpkg-packages.txt").read_text(encoding="utf-8").strip() == "a 1 (install ok installed)"


# ============================================================================================ screenshots / network
def test_screenshot_verdict_delegates_to_boot_test_content_check(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    blank = tmp_path / "blank.png"
    Image.new("RGB", (640, 480), (0, 0, 0)).save(blank)
    assert it.screenshot_verdict(blank)[0] is False
    busy = tmp_path / "busy.png"
    img = Image.new("RGB", (640, 480))
    img.putdata([((x * 7) % 256, (y * 3) % 256, (x + y) % 256) for y in range(480) for x in range(640)])
    img.save(busy)
    assert it.screenshot_verdict(busy)[0] is True


def test_probe_online_needs_every_url_to_answer():
    calls = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def good(url, timeout):
        calls.append(url)
        return Resp(b"x")

    assert it.probe_online(opener=good) is True and calls == list(it.PROBE_URLS)

    def half(url, timeout):
        if "google" in url:
            raise OSError("blocked")
        return Resp(b"x")

    assert it.probe_online(opener=half) is False


def test_resolve_expect_online():
    assert it.resolve_expect_online("auto", "off", probe=lambda: True) is False      # no NIC: offline whatever the runner has
    assert it.resolve_expect_online("yes", "on", probe=lambda: False) is True
    assert it.resolve_expect_online("no", "on", probe=lambda: True) is False
    assert it.resolve_expect_online("auto", "on", probe=lambda: True) is True


# ============================================================================================ reporting / arguments
def test_render_summary_lists_only_what_needs_reading():
    sections = {"install": [ic.ok("a"), ic.fail("install-finished", "timeout")], "disk": [ic.info("step-flatpaks", "pending"), ic.ok("b")]}
    md = it.render_summary("FAIL", sections, ["ISO: x.iso"])
    assert md.startswith("# Lindos install test: FAIL") and "ISO: x.iso" in md
    assert "`FAIL` **install-finished** timeout" in md and "`INFO` **step-flatpaks** pending" in md
    assert "**a**" not in md and "1 check(s) passed" in md
    rows = it.findings_json(sections)
    assert rows[1] == {"phase": "install", "name": "install-finished", "level": "fail", "detail": "timeout"}


def test_finish_writes_summary_and_report_and_maps_the_verdict(tmp_path, capsys):
    assert it._finish(tmp_path, {"x": [ic.ok("a")]}, ["n"], "PASS") == 0
    assert it._finish(tmp_path, {"x": [ic.fail("a", "b")]}, ["n"], "FAIL") == 1
    assert (tmp_path / "summary.md").read_text(encoding="utf-8").startswith("# Lindos install test: FAIL")
    assert json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))["verdict"] == "FAIL"
    assert "===== FAIL =====" in capsys.readouterr().out


def test_argument_defaults_and_choices():
    ns = it.build_parser().parse_args(["--iso", "x", "--out-dir", "y"])
    assert (ns.disk_size, ns.ram, ns.cpus, ns.install_timeout, ns.install_budget) == ("32G", 6144, 4, 5400, 2400)
    assert ns.poweroff_grace == 300
    assert (ns.ubiquity_mode, ns.firmware, ns.network, ns.expect_online, ns.phase3_boot) == ("automatic", "bios", "on", "auto", "grub")
    assert not ns.skip_phase3 and not ns.allow_tcg and not ns.keep_disk
    ns = it.build_parser().parse_args(["--iso", "x", "--out-dir", "y", "--require-kernel-suffix=-lindos", "--network", "off",
                                      "--ubiquity-mode", "noninteractive", "--firmware", "uefi", "--phase3-boot", "kernel"])
    assert ns.require_kernel_suffix == "-lindos" and ns.network == "off" and ns.firmware == "uefi"
    with pytest.raises(SystemExit):
        it.build_parser().parse_args(["--iso", "x", "--out-dir", "y", "--firmware", "ppc"])


def test_parse_size():
    assert it._parse_size("32G") == 32 << 30 and it._parse_size("512M") == 512 << 20 and it._parse_size("7") == 7


def test_main_usage_errors(tmp_path, capsys, monkeypatch):
    assert it.main(["--iso", str(tmp_path / "no-such-*.iso"), "--out-dir", str(tmp_path / "o")]) == 2
    assert "no ISO matched" in capsys.readouterr().err
    iso = tmp_path / "x.iso"
    iso.write_bytes(b"x")
    assert it.main(["--iso", str(iso), "--out-dir", str(tmp_path / "o"), "--disk-size", "lots"]) == 2
    assert "--disk-size" in capsys.readouterr().err
    monkeypatch.setattr(it.shutil, "which", lambda name: None)
    assert it.main(["--iso", str(iso), "--out-dir", str(tmp_path / "o")]) == 2
    assert "not found" in capsys.readouterr().err
    monkeypatch.setattr(it.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(it.os, "access", lambda *a, **k: False)
    assert it.main(["--iso", str(iso), "--out-dir", str(tmp_path / "o")]) == 2
    assert "/dev/kvm" in capsys.readouterr().err
    monkeypatch.setattr(it, "find_ovmf", lambda: None)
    assert it.main(["--iso", str(iso), "--out-dir", str(tmp_path / "o"), "--allow-tcg", "--firmware", "uefi"]) == 2
    assert "OVMF" in capsys.readouterr().err


def test_main_stops_at_a_failed_preflight_without_starting_qemu(tmp_path, monkeypatch, capsys):
    iso = tmp_path / "x.iso"
    iso.write_bytes(b"x")
    monkeypatch.setattr(it.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(it.os, "access", lambda *a, **k: True)
    monkeypatch.setattr(it, "preflight_iso", lambda *a, **k: ([ic.fail("iso-oem-pool", "no oem-config")], list(it.FALLBACK_ENTRY_WORDS)))
    monkeypatch.setattr(it.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("QEMU must not start")))
    assert it.main(["--iso", str(iso), "--out-dir", str(tmp_path / "o")]) == 1
    assert "iso-oem-pool" in (tmp_path / "o" / "summary.md").read_text(encoding="utf-8")
    assert "===== FAIL =====" in capsys.readouterr().out


# ============================================================================================ ci.yml wiring
def _yaml():
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(CI_YML.read_text(encoding="utf-8"))


def _text() -> str:
    return CI_YML.read_text(encoding="utf-8")


def test_install_test_job_is_manual_opt_in_and_needs_only_the_iso_job():
    doc = _yaml()
    job = doc["jobs"]["install-test"]
    assert job["needs"] == "iso"
    cond = " ".join(str(job["if"]).split())
    for part in ("needs.iso.result == 'success'", "github.event_name == 'workflow_dispatch'", "inputs.build_iso", "inputs.install_test"):
        assert part in cond
    inputs = (doc.get(True) or doc.get("on"))["workflow_dispatch"]["inputs"]
    assert inputs["install_test"]["default"] is False and inputs["install_test"]["type"] == "boolean"
    assert inputs["install_budget"]["default"] == "2400"
    assert inputs["install_firmware"]["options"] == ["bios", "uefi"] and inputs["install_firmware"]["default"] == "bios"


def test_no_other_job_depends_on_the_install_test():
    doc = _yaml()
    for name, job in doc["jobs"].items():
        needs = job.get("needs") or []
        needs = [needs] if isinstance(needs, str) else needs
        assert "install-test" not in needs, name
    assert doc["jobs"]["install-test"]["continue-on-error"] is True       # a red bring-up job must not turn the run red


def test_install_test_job_runs_the_harness_as_root_and_uploads_evidence_always():
    job = _yaml()["jobs"]["install-test"]
    steps = job["steps"]
    run = "\n".join(str(s.get("run", "")) for s in steps)
    assert "sudo env" in run and "build/qa/install_test.py" in run and "--out-dir out/qemu-install-test" in run
    assert "--require-kernel-suffix=-lindos" in run                            # the --opt=value form (leading '-' value)
    assert "INSTALL_BUDGET" in run and "${{ inputs.install_budget }}" not in run    # inputs reach the shell through env, not inline
    download = [s for s in steps if str(s.get("uses", "")).startswith("actions/download-artifact")]
    assert download and download[0]["with"]["name"] == "lindos-iso"
    upload = [s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact")]
    assert upload and upload[0]["if"] == "always()" and upload[0]["with"]["name"] == "lindos-install-test"
    paths = upload[0]["with"]["path"]
    for need in ("serial-install.log", "serial-first-boot.log", "summary.md", "installed-logs/**", "*.png", "preseed.redacted.seed"):
        assert need in paths, need
    assert "disk.raw" not in paths and "work/**" not in paths and "initrd" not in paths      # gigabytes, nothing to read
    assert any("GITHUB_STEP_SUMMARY" in str(s.get("run", "")) for s in steps)
    assert job["timeout-minutes"] >= 120


def test_install_test_job_needs_kvm_and_the_tools_the_harness_uses():
    text = _text()
    seg = text[text.index("  install-test:"):]
    for tool in ("qemu-system-x86", "xorriso", "python3-pil", "ovmf"):
        assert tool in seg
    assert "/dev/kvm" in seg


def test_the_harness_flags_used_by_the_workflow_exist():
    seg = _text()[_text().index("  install-test:"):]
    used = set(re.findall(r"--([a-z0-9-]+)", seg.split("install_test.py", 1)[1].split("- name:")[0]))
    known = {a.option_strings[0].lstrip("-") for a in it.build_parser()._actions if a.option_strings}
    assert used <= known, used - known


# ============================================================================================ the Install session (serial grammar)
SESSION_NAMES = ("live-only-ubiquity", "live-installer-up", "live-no-lightdm", "live-no-xfce-session", "live-no-lindos-setup",
                 "live-no-pkexec", "live-inhibitor-active", "live-installer-theme")


def test_parse_install_serial_collects_the_session_checks():
    got = it.parse_install_serial(INSTALL_SERIAL)
    assert {k: v["status"] for k, v in got["checks"].items()} == {n: "OK" for n in SESSION_NAMES}
    assert got["session_checked"] and got["session_fails"] == 0 and got["fail_logs"] == [] and got["session_diag"] == []
    bad = it.parse_install_serial("LINDOS_OBSERVER_STARTED mode=live uname=x\nLINDOS_CHECK live-no-lightdm=FAIL rc=1\n"
                                  "LINDOS_FAIL_LOG live-no-lightdm: LightDM is running\nLINDOS_INSTALL_DIAG ps: 812 lightdm\n"
                                  "LINDOS_INSTALL_SESSION_CHECKED fails=1\n")
    assert bad["checks"]["live-no-lightdm"] == {"status": "FAIL", "rc": "1"} and bad["fail_logs"] == ["live-no-lightdm: LightDM is running"]
    assert bad["session_diag"] == ["ps: 812 lightdm"] and bad["session_fails"] == 1


def test_a_healthy_install_session_is_judged_ok_per_check():
    findings = it.judge_install_phase({"outcome": "exited", "seconds": 1800}, it.parse_install_serial(INSTALL_SERIAL))
    assert not ic.failures(findings)
    assert {f.name for f in findings if f.level == ic.OK} >= {"install-" + n for n in SESSION_NAMES}


def test_the_default_install_session_that_starts_lightdm_fails_the_install_test():
    text = INSTALL_SERIAL.replace("LINDOS_CHECK live-no-lightdm=OK", "LINDOS_CHECK live-no-lightdm=FAIL rc=1\n"
                                  "LINDOS_FAIL_LOG live-no-lightdm: LightDM is running\nLINDOS_INSTALL_DIAG ps: 812 lightdm")
    findings = it.judge_install_phase({"outcome": "exited", "seconds": 1800}, it.parse_install_serial(text))
    bad = {f.name: f.detail for f in ic.failures(findings)}
    assert list(bad) == ["install-live-no-lightdm"] and "LightDM is running" in bad["install-live-no-lightdm"]
    assert any(f.name == "install-session-diag" and "812 lightdm" in f.detail for f in findings)


def test_required_session_checks_are_the_ones_the_observer_prints():
    obs = it.OBSERVER_SCRIPT.read_text(encoding="utf-8")
    assert set(it.REQUIRED_SESSION_CHECKS) == set(SESSION_NAMES)
    assert all("verdict %s " % n in obs for n in it.REQUIRED_SESSION_CHECKS)


# ============================================================================================ seed effects: what the run expects
def test_expect_i386_choices_and_the_image_default(tmp_path):
    assert it.resolve_expect_i386("yes") is True and it.resolve_expect_i386("no") is False
    assert it.resolve_expect_i386("auto", env={}) is True                       # build/config.env: ENABLE_I386 defaults to 1
    assert it.resolve_expect_i386("auto", env={"ENABLE_I386": "0"}) is False
    assert it.resolve_expect_i386("auto", env={"ENABLE_I386": "1"}) is True
    cfg = tmp_path / "config.env"
    cfg.write_text(': "${ENABLE_I386:=0}"   # off\n', encoding="utf-8")
    assert it.resolve_expect_i386("auto", env={}, config_env=cfg) is False
    cfg.write_text("# nothing about it\n", encoding="utf-8")
    assert it.resolve_expect_i386("auto", env={}, config_env=cfg) is None       # unknown: the checks only prove dpkg is consistent
    assert it.resolve_expect_i386("auto", env={}, config_env=tmp_path / "missing.env") is None


def test_the_real_config_env_default_is_what_the_test_reads():
    assert re.search(r'^:\s+"\$\{ENABLE_I386:=1\}"', it.CONFIG_ENV.read_text(encoding="utf-8"), re.M)
    assert it.DEFAULT_EXTRAS.is_file() and ic.load_extras(it.DEFAULT_EXTRAS)["schema"] == 1


def _fake_run(tmp_path, monkeypatch, *, menus=None):
    """Everything main() needs, faked: a completed installation whose disk checks are captured, not run."""
    iso = tmp_path / "x.iso"
    iso.write_bytes(b"x")
    monkeypatch.delenv("ENABLE_I386", raising=False)
    monkeypatch.setattr(it.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(it.os, "access", lambda *a, **k: True)
    monkeypatch.setattr(it, "find_ovmf", lambda: None)
    monkeypatch.setattr(it, "preflight_iso", lambda *a, **k: ([ic.ok("iso-boot-entry")], list(it.FALLBACK_ENTRY_WORDS)))
    seen = {"menus": 0, "phase1": [], "checks": []}

    def fake_menus(iso_path, work, **k):
        seen["menus"] += 1
        return menus if menus is not None else [ic.ok("menu-grub-entries")]

    def fake_extract(iso_path, dest, **k):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "vmlinuz").write_bytes(b"k")
        (dest / "initrd").write_bytes(b"i")
        return dest / "vmlinuz", dest / "initrd"

    def fake_phase(argv, **k):
        seen["phase1"].append(list(argv))
        Path(k["serial_log"]).write_text(INSTALL_SERIAL, encoding="utf-8")       # a healthy install: only the menus can fail the run
        return {"outcome": "exited", "seconds": 5, "rc": 0, "screenshots": []}

    def fake_checks(tree, **k):
        seen["checks"].append(k)
        return [ic.ok("fake-disk-check")]

    class FakeLoop:
        def __init__(self, disk, work, **k):
            self.work = Path(work)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def find_system(self, rw=False):
            root = self.work / "sysroot"
            root.mkdir(parents=True, exist_ok=True)
            return root, None, "/dev/vda1"

    monkeypatch.setattr(it.mc, "check_iso_menus", fake_menus)
    monkeypatch.setattr(it.bt, "extract_casper", fake_extract)
    monkeypatch.setattr(it, "run_install_phase", fake_phase)
    monkeypatch.setattr(it.ic, "run_all_checks", fake_checks)
    monkeypatch.setattr(it, "LoopDisk", FakeLoop)
    return iso, seen


def _main(iso, tmp_path, *args):
    return it.main(["--iso", str(iso), "--out-dir", str(tmp_path / "out"), "--disk-size", "1M", "--skip-phase3", *args])


def test_the_disk_checks_get_the_extras_the_i386_expectation_and_the_online_state(tmp_path, monkeypatch):
    iso, seen = _fake_run(tmp_path, monkeypatch)
    assert _main(iso, tmp_path, "--expect-online", "yes") == 0
    kw = seen["checks"][0]
    assert kw["extras"] == ic.load_extras(it.DEFAULT_EXTRAS) and kw["expect_i386"] is True
    assert kw["strict_offline"] is False and kw["expect_online"] is True
    notes = (tmp_path / "out" / "summary.md").read_text(encoding="utf-8")
    assert "i386 expected in the image: yes" in notes and "OFFLINE run" not in notes


def test_a_network_off_run_is_the_strict_offline_run(tmp_path, monkeypatch):
    iso, seen = _fake_run(tmp_path, monkeypatch)
    _main(iso, tmp_path, "--network", "off")
    kw = seen["checks"][0]
    assert kw["strict_offline"] is True and kw["expect_online"] is False
    assert "OFFLINE run" in (tmp_path / "out" / "summary.md").read_text(encoding="utf-8")
    assert "-nic" in seen["phase1"][0] and "none" in seen["phase1"][0]


def test_i386_and_extras_options_reach_the_checks(tmp_path, monkeypatch):
    iso, seen = _fake_run(tmp_path, monkeypatch)
    _main(iso, tmp_path, "--expect-i386", "no", "--extras", str(tmp_path / "missing.json"))
    kw = seen["checks"][0]
    assert kw["expect_i386"] is False and kw["extras"] is None
    assert "NOT READABLE" in (tmp_path / "out" / "summary.md").read_text(encoding="utf-8")
    monkeypatch.setenv("ENABLE_I386", "0")
    _main(iso, tmp_path)
    assert seen["checks"][1]["expect_i386"] is False


def test_a_failed_menu_check_fails_the_verdict_but_the_install_still_runs(tmp_path, monkeypatch, capsys):
    """The structure of the ISO's own boot menus is judged in the preflight; the (expensive) install is still run for its evidence."""
    iso, seen = _fake_run(tmp_path, monkeypatch, menus=[ic.fail("menu-grub-syntax", "'if' has no 'fi'")])
    assert _main(iso, tmp_path) == 1
    assert seen["menus"] == 1 and len(seen["phase1"]) == 1
    summary = (tmp_path / "out" / "summary.md").read_text(encoding="utf-8")
    assert "## boot menus" in summary and "menu-grub-syntax" in summary and summary.startswith("# Lindos install test: FAIL")
    assert "[boot menus]" in capsys.readouterr().out


def test_skipping_the_preflight_skips_the_menu_check_too(tmp_path, monkeypatch):
    iso, seen = _fake_run(tmp_path, monkeypatch)
    _main(iso, tmp_path, "--skip-preflight")
    assert seen["menus"] == 0


# ============================================================================================ ci.yml: the new jobs and steps
def test_the_offline_install_job_is_opt_in_independent_and_offline():
    doc = _yaml()
    job = doc["jobs"]["install-test-offline"]
    assert job["needs"] == "iso" and job["continue-on-error"] is True
    cond = " ".join(str(job["if"]).split())
    for part in ("needs.iso.result == 'success'", "github.event_name == 'workflow_dispatch'", "inputs.build_iso", "inputs.install_test_offline"):
        assert part in cond
    inputs = (doc.get(True) or doc.get("on"))["workflow_dispatch"]["inputs"]
    assert inputs["install_test_offline"]["default"] is False and inputs["install_test_offline"]["type"] == "boolean"
    steps = job["steps"]
    run = "\n".join(str(s.get("run", "")) for s in steps)
    assert "sudo env" in run and "build/qa/install_test.py" in run and "--network off" in run and "--skip-phase3" in run
    assert "--out-dir out/qemu-install-test-offline" in run, "its own out dir: the online job's artifacts are never mixed with it"
    assert "--require-kernel-suffix=-lindos" in run and "${{ inputs.install_budget }}" not in run
    upload = [s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact")]
    assert upload and upload[0]["if"] == "always()" and upload[0]["with"]["name"] == "lindos-install-test-offline"
    for need in ("summary.md", "serial-install.log", "installed-logs/**", "*.png", "work/menus/*.cfg"):
        assert need in upload[0]["with"]["path"], need
    assert "disk.raw" not in upload[0]["with"]["path"] and "work/**" not in upload[0]["with"]["path"]
    assert any("GITHUB_STEP_SUMMARY" in str(s.get("run", "")) for s in steps)
    text = _text()
    seg = text[text.index("  install-test-offline:"):text.index("  menu-test:")]
    for tool in ("qemu-system-x86", "xorriso", "python3-pil", "grub-common", "/dev/kvm"):
        assert tool in seg, tool


def test_the_offline_job_flags_exist():
    text = _text()
    seg = text[text.index("  install-test-offline:"):text.index("  menu-test:")]
    used = set(re.findall(r"--([a-z0-9-]+)", seg.split("python3 build/qa/install_test.py", 1)[1].split("- name:")[0]))
    known = {a.option_strings[0].lstrip("-") for a in it.build_parser()._actions if a.option_strings}
    assert used and used <= known, used - known


def test_the_online_job_still_runs_the_real_network_install():
    """The online run is the one with internet: it must never grow the offline flags."""
    text = _text()
    seg = text[text.index("  install-test:"):text.index("  install-test-offline:")]
    assert "--network off" not in seg and "--skip-phase3" not in seg
    assert "grub-common" in seg and "work/menus/*.cfg" in seg


def test_the_boot_test_job_checks_the_boot_menus_even_when_the_boot_failed():
    doc = _yaml()
    steps = doc["jobs"]["boot-test"]["steps"]
    check = [s for s in steps if "build/qa/menu_checks.py" in str(s.get("run", ""))]
    assert len(check) == 1 and check[0]["if"] == "always()"
    assert "--iso 'out/lindos-*.iso'" in check[0]["run"] and "pipefail" in check[0]["run"], "a failing check must fail the step, not the tee"
    assert steps.index(check[0]) > steps.index([s for s in steps if "build/qa/boot_test.py" in str(s.get("run", ""))][0])
    upload = [s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact")][0]
    assert "menu-checks.txt" in upload["with"]["path"] and steps.index(upload) > steps.index(check[0])
    assert "grub-common" in " ".join(str(s.get("run", "")) for s in steps)


def test_the_lint_job_installs_grub_script_check_so_the_real_check_runs():
    run = " ".join(str(s.get("run", "")) for s in _yaml()["jobs"]["lint-test"]["steps"])
    assert "grub-common" in run
    assert "shellcheck" in run                                                # the tools that were there stay


# ============================================================================================ how the account wizard looks
def _ppm(path, width, height, pixel):
    """A binary PPM (what QEMU's screendump writes) of width x height, pixel(x, y) -> (r, g, b)."""
    body = b"".join(bytes(pixel(x, y)) for y in range(height) for x in range(width))
    path.write_bytes(b"P6\n# QEMU screendump\n%d %d\n255\n" % (width, height) + body)
    return path


def _window(inside):
    """A black X root with a 60x60 window in the middle whose pixels are inside(x, y)."""
    return lambda x, y: inside(x, y) if 20 <= x < 80 and 20 <= y < 80 else (0, 0, 0)


def test_read_ppm_parses_header_comments_and_refuses_anything_else(tmp_path):
    good = _ppm(tmp_path / "a.ppm", 3, 2, lambda x, y: (x, y, 9))
    w, h, buf = it.read_ppm(good)
    assert (w, h) == (3, 2) and buf[:3] == bytes([0, 0, 9]) and len(buf) == 18
    (tmp_path / "b.ppm").write_bytes(b"P5\n1 1\n255\n\0")
    (tmp_path / "c.ppm").write_bytes(b"P6\n4 4\n255\n\0\0")               # truncated pixel data
    for name in ("b.ppm", "c.ppm", "missing.ppm"):
        assert it.read_ppm(tmp_path / name) is None


def test_the_light_default_page_of_ubiquity_is_recognised_and_the_dark_skin_is_not(tmp_path):
    light = _ppm(tmp_path / "light.ppm", 100, 100, _window(lambda x, y: (246, 245, 244)))        # Adwaita's #f6f5f4 (first-boot.png)
    share, stats = it.screenshot_light_share(light, step=1)
    assert share is not None and share > 0.99 and "light" in stats
    dark = _ppm(tmp_path / "dark.ppm", 100, 100, _window(lambda x, y: (96, 205, 255) if x < 24 else (32, 32, 32)))   # #202020 + a strip of accent
    share, _stats = it.screenshot_light_share(dark, step=1)
    assert share is not None and share < 0.1, "a strip of accent colour (the Continue button) on a dark page is not a light page"
    black = _ppm(tmp_path / "black.ppm", 10, 10, lambda x, y: (0, 0, 0))
    assert it.screenshot_light_share(black)[0] is None and it.screenshot_light_share(tmp_path / "nothing.ppm")[0] is None


def test_a_light_first_boot_wizard_is_a_warning_a_dark_one_passes_and_an_unjudged_one_is_noted():
    serial = {"observer": True, "ready": True, "checks": {}, "fail_logs": [], "diag": [], "panic": False}

    def look(light):
        return {f.name: f for f in it.judge_first_boot(serial, screenshot_ok=True, screenshot_reason="x", light=light)}

    assert look((0.9, "1280x800, 90% of ...  are light"))["boot-wizard-look"].level == ic.WARN
    assert "Lindos-Setup" in look((0.9, "s"))["boot-wizard-look"].detail
    assert look((0.05, "s"))["boot-wizard-look"].level == ic.OK
    assert look((None, "nothing but black"))["boot-wizard-look"].level == ic.INFO
    assert "boot-wizard-look" not in look(None)
    assert not ic.failures(it.judge_first_boot(serial, screenshot_ok=True, light=(0.9, "s"))), "a heuristic never fails the run"
