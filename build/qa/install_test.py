#!/usr/bin/env python3
"""build/qa/install_test.py - CI INSTALL test for the built Lindos ISO (QEMU/KVM, real network).

Nobody can test an installation on the Windows dev host, and the installer (Ubiquity in OEM mode + the
lindos-installer target-config hook + finalize.sh) can only be proven by running it.  This harness does:

  phase 0  preflight   the ISO carries what the flow needs (oem-config debs in /pool, the "Install Lindos"
                       boot entry, lindos-installer in filesystem.manifest-remove)
  phase 1  install     a blank sparse disk, the ISO as a CD-ROM, the ISO's own kernel/initrd booted
                       DIRECTLY (no GRUB, like build/qa/boot_test.py) with the words of the shipped
                       "Install Lindos" entry plus ``automatic-ubiquity noprompt``; user-mode networking so
                       the hook's network steps really run; Ubiquity powers the guest off when it is done
  phase 2  assert      the disk is mounted READ-ONLY and checked by build/qa/install_checks.py:
                       install-state.json, installer.log, oem-config armed, no autologin=oem, no leftovers,
                       dpkg clean, Chrome vs browser marker, boot loader, kernel; the installer's logs are
                       collected (secrets scrubbed) for the artifact
  phase 3  first boot  the INSTALLED disk is booted (its own GRUB through SeaBIOS, or -kernel from the disk)
                       and must come up in Ubiquity's oem-config wizard - not LightDM, not the oem desktop,
                       not the Lindos first-run wizard, no installs; serial log + screenshot as evidence

How the CI-only preseed reaches the ISO (the choice, and why)
  Ubiquity's automatic mode needs answers (partitioning, locale, the temporary account, poweroff).  The
  Mint ISO has no /preseed directory and the medium must not be modified.  casper's initramfs script
  24preseed loads a file called /preseed.cfg from the initramfs root, so the harness PREPENDS a small
  uncompressed newc cpio archive (preseed + observer files) to the ISO's own initrd and boots that with
  -initrd.  The kernel unpacks concatenated archives in order (the same mechanism as early microcode), so
  it works whatever compression the initrd uses, needs no second CD/9p/virtio-serial device and does not
  touch the ISO.  Only words WITHOUT spaces go on the kernel command line; the preseed file carries the
  rest.  The answers the image itself bakes (lindos.seed) and the ``ubiquity/success_command`` word of the
  boot entry are deliberately NOT repeated: the test proves the shipped ones reach Ubiquity.

Known limitations (a reproducible install log is the point; expect several rounds)
  * Ubiquity's automatic mode is lightly maintained.  If the CI preseed does not skip a page, the run stops
    at that page and the timeout ends it; the serial log, the periodic screenshots and the
    ``LINDOS_HOOK_LOG`` lines show where.  ``--ubiquity-mode noninteractive`` runs the installer without
    X (start-ubiquity-dm falls back to it by itself when X dies before Ubiquity starts).
  * A preseed cannot hide Ubiquity's temporary-account page in OEM mode; the harness answers it (user
    ``oem``, a random throw-away password that is scrubbed from every uploaded log).
  * The GTK front end is only exercised in automatic mode: nothing here judges how the window looks.
  * The install needs real internet: without it every network step is 'pending' - the assertions then
    accept that (``--expect-online auto`` probes the runner) and record what they saw.
  * Phase 3 patches the installed disk before booting it (a serial console on the kernel command line and a
    read-only observer unit, see ci-observer.sh); the read-only assertions of phase 2 run before that.
  * UEFI (``--firmware uefi``) is supported by the argument builders but has not been run.

Usage (needs root for the loop mounts; as a normal user it uses ``sudo -n``):
    sudo python3 build/qa/install_test.py --iso 'out/lindos-*.iso' --out-dir out/qemu-install-test
        [--disk-size 32G] [--ram 6144] [--cpus 4] [--install-timeout 5400] [--install-budget 2400]
        [--boot-timeout 900] [--ubiquity-mode automatic|noninteractive] [--firmware bios|uefi]
        [--network on|off] [--expect-online auto|yes|no] [--phase3-boot grub|kernel] [--skip-phase3]
        [--require-kernel-suffix=-lindos] [--allow-tcg] [--keep-disk]

Exit codes: 0 pass - 1 the install or an assertion failed - 2 usage/environment error.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
for _p in (str(HERE), str(HERE.parent / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import boot_test as bt  # noqa: E402  (same directory: QEMU monitor client, screenshots, kernel extraction)
import install_checks as ic  # noqa: E402

try:  # the boot menu parser of the build (single source of truth for the GRUB entries)
    import boot_menu  # noqa: E402
except ImportError:  # pragma: no cover - only when build/lib is missing
    boot_menu = None  # type: ignore[assignment]

CI_INSTALL_FLAG = "lindos.ci_install_test"
OBSERVER_SCRIPT = HERE / "ci-observer.sh"
OBSERVER_UNIT = "lindos-ci-observer.service"
OBSERVER_BIN = "/usr/local/sbin/lindos-ci-observer"

# The words of the shipped "Install Lindos" entry (build/overlay/boot/grub/grub.cfg), used only when the ISO's own
# grub.cfg cannot be read.  tests/test_install_test_qa.py checks it still equals the real entry.
FALLBACK_ENTRY_WORDS: Tuple[str, ...] = (
    "boot=casper", "only-ubiquity", "oem-config/enable=true",
    "ubiquity/success_command=/usr/libexec/lindos/installer/finalize.sh",
    "username=liveuser", "hostname=lindos",
)

# serial-log grammar of the guest observer (build/qa/ci-observer.sh) and of the kernel
INSTALL_FAILED_RE = re.compile(r"^LINDOS_INSTALL_FAILED\b(.*)$")
INSTALL_FINALIZED_RE = re.compile(r"^LINDOS_INSTALL_FINALIZED\b")
UBIQUITY_EXIT_RE = re.compile(r"^LINDOS_INSTALL_UBIQUITY_EXIT\b(.*)$")
OBSERVER_STARTED_RE = re.compile(r"^LINDOS_OBSERVER_STARTED mode=(\w+)")
HOOK_LOG_RE = re.compile(r"^LINDOS_HOOK_LOG (.*)$")
OEM_READY_RE = re.compile(r"^LINDOS_OEM_READY(?: fails=(\d+))?")
OEM_TIMEOUT_RE = re.compile(r"^LINDOS_OEM_TIMEOUT(?: fails=(\d+))?")
OEM_DIAG_RE = re.compile(r"^LINDOS_OEM_(?:DIAG|PS) (.*)$")


def log(msg: str) -> None:
    print("[install-test] %s" % msg, flush=True)


# ============================================================================================
#  the initramfs overlay: newc cpio archive with /preseed.cfg + the observer
# ============================================================================================
def _pad4(n: int) -> int:
    return (4 - n % 4) % 4


def newc_entry(name: str, data: bytes, mode: int, ino: int) -> bytes:
    """One record of a 'newc' (SVR4, no CRC) cpio archive."""
    raw_name = name.encode("utf-8") + b"\0"
    fields = (ino, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(raw_name), 0)
    header = b"070701" + b"".join(b"%08X" % v for v in fields)
    out = header + raw_name
    out += b"\0" * _pad4(len(header) + len(raw_name))
    out += data + b"\0" * _pad4(len(data))
    return out


def build_newc_cpio(files: Sequence[Tuple[str, bytes, int]]) -> bytes:
    """A newc cpio archive of (path, data, permission bits) files; parent directories are added first.

    Padded with zeros to a multiple of 512 bytes like cpio(1) does (the kernel skips the zeros).
    """
    entries: List[bytes] = []
    seen_dirs = set()
    ino = 1
    for path, data, perm in files:
        parts = path.strip("/").split("/")
        for i in range(1, len(parts)):
            d = "/".join(parts[:i])
            if d not in seen_dirs:
                seen_dirs.add(d)
                entries.append(newc_entry(d, b"", 0o040755, ino))
                ino += 1
        entries.append(newc_entry("/".join(parts), data, 0o100000 | (perm & 0o7777), ino))
        ino += 1
    entries.append(newc_entry("TRAILER!!!", b"", 0, 0))
    blob = b"".join(entries)
    return blob + b"\0" * ((512 - len(blob) % 512) % 512)


def parse_newc_cpio(blob: bytes) -> List[Tuple[str, int, bytes]]:
    """Read a newc archive back: [(name, mode, data)] up to (not including) the trailer.  For the tests."""
    out: List[Tuple[str, int, bytes]] = []
    pos = 0
    while pos + 110 <= len(blob):
        if blob[pos:pos + 6] != b"070701":
            raise ValueError("bad cpio magic at %d" % pos)
        vals = [int(blob[pos + 6 + 8 * i:pos + 14 + 8 * i], 16) for i in range(13)]
        mode, size, namesz = vals[1], vals[6], vals[11]
        name = blob[pos + 110:pos + 110 + namesz - 1].decode("utf-8")
        pos += 110 + namesz
        pos += _pad4(110 + namesz)
        data = blob[pos:pos + size]
        pos += size + _pad4(size)
        if name == "TRAILER!!!":
            return out
        out.append((name, mode, data))
    raise ValueError("cpio archive has no trailer")


def write_combined_initrd(cpio: bytes, original: Path, dest: Path) -> Path:
    """dest = the overlay archive followed by the ISO's own initrd (the kernel unpacks both, in order)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as out:
        out.write(cpio)
        with open(original, "rb") as src:
            shutil.copyfileobj(src, out, 1 << 20)
    return dest


# ============================================================================================
#  the CI preseed
# ============================================================================================
@dataclass(frozen=True)
class SeedOptions:
    """Answers of the CI-only preseed.  The password is a throw-away random token (never a real credential)."""

    password: str
    disk: str = "/dev/vda"
    locale: str = "en_US.UTF-8"
    timezone: str = "UTC"
    layout: str = "us"
    hostname: str = "lindos-ci"
    username: str = "oem"
    fullname: str = "OEM Configuration (temporary user)"
    poweroff: bool = True


def early_command() -> str:
    """preseed/early_command, run by casper's 24preseed in the initramfs with the live root at /root: installs the
    observer as a throw-away systemd unit of the LIVE session (Ubiquity copies /rofs, so nothing of it reaches
    the installed system)."""
    return (
        "mkdir -p /root/usr/local/sbin /root/etc/systemd/system/multi-user.target.wants"
        " && cp /lindos-ci/ci-observer.sh /root%s"
        " && chmod 0755 /root%s"
        " && cp /lindos-ci/%s /root/etc/systemd/system/%s"
        " && ln -sf /etc/systemd/system/%s /root/etc/systemd/system/multi-user.target.wants/%s"
        % (OBSERVER_BIN, OBSERVER_BIN, OBSERVER_UNIT, OBSERVER_UNIT, OBSERVER_UNIT, OBSERVER_UNIT)
    )


def seed_lines(opts: SeedOptions, *, redact: bool = False) -> List[Tuple[str, str, str, str]]:
    """(owner, question, type, value) for every CI answer, in the order a reader follows the installer."""
    pw = "<redacted>" if redact else opts.password
    q: List[Tuple[str, str, str, str]] = [
        # language, keyboard, clock
        ("d-i", "debian-installer/locale", "string", opts.locale),
        ("d-i", "debian-installer/language", "string", opts.locale.split("_")[0]),
        ("d-i", "debian-installer/country", "string", opts.locale.split("_")[1].split(".")[0] if "_" in opts.locale else "US"),
        ("d-i", "console-setup/ask_detect", "boolean", "false"),
        ("d-i", "keyboard-configuration/layoutcode", "string", opts.layout),
        ("d-i", "keyboard-configuration/modelcode", "string", "pc105"),
        ("d-i", "keyboard-configuration/variantcode", "string", ""),
        ("d-i", "keyboard-configuration/xkb-keymap", "select", opts.layout),
        ("d-i", "time/zone", "string", opts.timezone),
        ("d-i", "clock-setup/utc", "boolean", "true"),
        # partitioning: method + disk make partman-auto partition without asking (display.d/initial_auto)
        ("d-i", "partman-auto/disk", "string", opts.disk),
        ("d-i", "partman-auto/method", "string", "regular"),
        ("d-i", "partman-auto/choose_recipe", "select", "atomic"),
        ("d-i", "partman/default_filesystem", "string", "ext4"),
        ("d-i", "partman-partitioning/confirm_new_label", "boolean", "true"),
        ("d-i", "partman/confirm_write_new_label", "boolean", "true"),
        ("d-i", "partman/choose_partition", "select", "finish"),
        ("d-i", "partman/confirm", "boolean", "true"),
        ("d-i", "partman/confirm_nooverwrite", "boolean", "true"),
        ("d-i", "partman-basicfilesystems/no_swap", "boolean", "false"),
        ("d-i", "grub-installer/bootdev", "string", opts.disk),
        # the OEM installer's temporary account (a preseed cannot hide its page; answering it is all that helps)
        ("d-i", "netcfg/get_hostname", "string", opts.hostname),
        ("d-i", "netcfg/get_domain", "string", ""),
        ("d-i", "passwd/user-fullname", "string", opts.fullname),
        ("d-i", "passwd/username", "string", opts.username),
        ("d-i", "passwd/user-password", "password", pw),
        ("d-i", "passwd/user-password-again", "password", pw),
        ("d-i", "user-setup/allow-password-weak", "boolean", "true"),
        ("d-i", "user-setup/encrypt-home", "boolean", "false"),
        # the installer's own switches: the hook does the updates/drivers, so no codecs page work and no pre-download
        ("ubiquity", "ubiquity/use_nonfree", "boolean", "false"),
        ("ubiquity", "ubiquity/download_updates", "boolean", "false"),
        ("ubiquity", "ubiquity/minimal_install", "boolean", "false"),
        ("ubiquity", "ubiquity/summary", "note", ""),
        ("ubiquity", "ubiquity/reboot", "boolean", "false"),
        ("ubiquity", "ubiquity/poweroff", "boolean", "true" if opts.poweroff else "false"),
        # runs in the initramfs (casper 24preseed): the observer into the live root
        ("d-i", "preseed/early_command", "string", early_command()),
    ]
    return q


def build_preseed(opts: SeedOptions, *, redact: bool = False) -> str:
    """The preseed file: '<owner> <question> <type> <value>' lines, LF only (casper-set-selections parses it)."""
    lines = ["# Lindos CI install test - generated by build/qa/install_test.py (not a shipped file)"]
    for owner, key, typ, val in seed_lines(opts, redact=redact):
        lines.append(("%s %s %s %s" % (owner, key, typ, val)).rstrip())
    return "\n".join(lines) + "\n"


def observer_unit(mode: str) -> str:
    """The systemd unit that runs ci-observer.sh: 'live' in the installer session, 'oem' on the installed disk."""
    if mode == "live":
        head = ("ConditionKernelCommandLine=%s\nAfter=basic.target\n" % CI_INSTALL_FLAG)
        wanted = "multi-user.target"
    else:
        head = "DefaultDependencies=no\n"
        wanted = "sysinit.target"
    return (
        "[Unit]\n"
        "Description=Lindos CI install-test observer (QA only; never on a real install)\n"
        "%s\n"
        "[Service]\n"
        "Type=simple\n"
        "ExecStart=%s %s\n"
        "Nice=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=%s\n" % (head, OBSERVER_BIN, mode, wanted)
    )


def build_overlay_cpio(preseed_text: str, observer_script: bytes) -> bytes:
    return build_newc_cpio([
        ("preseed.cfg", preseed_text.encode("utf-8"), 0o600),
        ("lindos-ci/ci-observer.sh", observer_script, 0o755),
        ("lindos-ci/" + OBSERVER_UNIT, observer_unit("live").encode("utf-8"), 0o644),
    ])


# ============================================================================================
#  kernel command line
# ============================================================================================
def install_cmdline(entry_words: Sequence[str], *, mode: str, budget: int, extra: Sequence[str] = ()) -> str:
    """The shipped 'Install Lindos' words, made fit for a serial-console CI boot, plus the automatic-install words."""
    drop = {"quiet", "splash", "--"}
    words = [w for w in entry_words if w not in drop and not w.startswith("iso-scan/")]
    words += ["noninteractive" if mode == "noninteractive" else "automatic-ubiquity", "noprompt",
              "plymouth.enable=0", "console=ttyS0,115200n8", "debconf/priority=critical",
              "lindos.install_budget=%d" % budget, CI_INSTALL_FLAG]
    words += list(extra)
    seen: List[str] = []
    for w in words:
        if w not in seen:
            seen.append(w)
    return " ".join(seen) + " --"


def pick_install_entry(entries: Iterable) -> Optional[object]:
    """The first Install entry that does not need nomodeset (what a user picks by pressing Enter)."""
    for e in entries:
        args = list(getattr(e, "args", []))
        if "only-ubiquity" in args and "nomodeset" not in args:
            return e
    return None


def entry_words_from_grub(text: str) -> Optional[List[str]]:
    """The kernel words of the ISO's own Install entry, or None when there is none / no parser."""
    if boot_menu is None:
        return None
    entry = pick_install_entry(boot_menu.parse_grub_entries(text))
    if entry is None:
        return None
    return list(boot_menu.isolinux_words(entry))


# ============================================================================================
#  ISO preflight (xorriso, read-only)
# ============================================================================================
def iso_extract(iso: Path, iso_path: str, dest: Path, *, which=shutil.which, run=subprocess.run) -> bool:
    xorriso = which("xorriso")
    if not xorriso:
        return False
    dest.unlink(missing_ok=True)   # xorriso will not overwrite an existing file
    proc = run([xorriso, "-osirrox", "on", "-indev", str(iso), "-extract", iso_path, str(dest)],
               capture_output=True, text=True)
    return proc.returncode == 0 and dest.exists()


def iso_find(iso: Path, top: str, pattern: str, *, which=shutil.which, run=subprocess.run) -> List[str]:
    """Regular files under *top* of the ISO whose name matches *pattern* (xorriso -find prints them one per line)."""
    xorriso = which("xorriso")
    if not xorriso:
        return []
    proc = run([xorriso, "-indev", str(iso), "-find", top, "-type", "f", "-name", pattern],
               capture_output=True, text=True)
    return re.findall(r"(/[^\s'\"]+)", proc.stdout or "")


def preflight_iso(iso: Path, work: Path, *, which=shutil.which, run=subprocess.run) -> Tuple[List[ic.Finding], List[str]]:
    """Cheap facts about the ISO that decide whether an hour-long install can work; also returns the entry words."""
    out: List[ic.Finding] = []
    work.mkdir(parents=True, exist_ok=True)
    words: Optional[List[str]] = None
    grub = work / "grub.cfg"
    if iso_extract(iso, "/boot/grub/grub.cfg", grub, which=which, run=run):
        words = entry_words_from_grub(grub.read_text(encoding="utf-8", errors="replace"))
    if words is None:
        out.append(ic.warn("iso-boot-entry", "could not read an Install entry from the ISO's grub.cfg; using the built-in "
                                             "copy of its words"))
        words = list(FALLBACK_ENTRY_WORDS)
    else:
        missing = [w for w in ("boot=casper", "only-ubiquity", "oem-config/enable=true") if w not in words]
        if not any(w.startswith("ubiquity/success_command=") for w in words):
            missing.append("ubiquity/success_command=...")
        if missing:
            out.append(ic.fail("iso-boot-entry", "the ISO's Install entry lacks: " + ", ".join(missing)))
        else:
            out.append(ic.ok("iso-boot-entry", "Install entry: " + " ".join(words)))
    pool = iso_find(iso, "/pool", "oem-config*", which=which, run=run)
    bundled = iso_find(iso, "/lindos/oem-debs", "*.deb", which=which, run=run)
    if len(pool) >= 2 or bundled:
        out.append(ic.ok("iso-oem-pool", "oem-config is on the medium: %s" % ", ".join(sorted(set(pool + bundled))[:4])))
    else:
        out.append(ic.fail("iso-oem-pool", "neither /pool nor /lindos/oem-debs carries oem-config: Ubiquity would skip it "
                                           "silently and the first boot would land on the temporary desktop"))
    manifest = work / "filesystem.manifest-remove"
    if iso_extract(iso, "/casper/filesystem.manifest-remove", manifest, which=which, run=run):
        if "lindos-installer" in manifest.read_text(encoding="utf-8", errors="replace").split():
            out.append(ic.ok("iso-manifest-remove", "lindos-installer is in filesystem.manifest-remove"))
        else:
            out.append(ic.warn("iso-manifest-remove", "lindos-installer is not in filesystem.manifest-remove: it stays "
                                                      "on the installed system"))
    else:
        out.append(ic.warn("iso-manifest-remove", "the ISO has no /casper/filesystem.manifest-remove"))
    return out, words


# ============================================================================================
#  QEMU command lines
# ============================================================================================
OVMF_CANDIDATES: Tuple[Tuple[str, str], ...] = (
    ("/usr/share/OVMF/OVMF_CODE_4M.fd", "/usr/share/OVMF/OVMF_VARS_4M.fd"),
    ("/usr/share/OVMF/OVMF_CODE.fd", "/usr/share/OVMF/OVMF_VARS.fd"),
    ("/usr/share/ovmf/OVMF_CODE.fd", "/usr/share/ovmf/OVMF_VARS.fd"),
)


def find_ovmf(exists: Callable[[str], bool] = os.path.exists) -> Optional[Tuple[str, str]]:
    for code, variables in OVMF_CANDIDATES:
        if exists(code) and exists(variables):
            return code, variables
    return None


def _machine_args(*, name: str, ram_mb: int, cpus: int, serial_log: Path, monitor_sock: Path, network: str,
                  firmware: str, ovmf: Optional[Tuple[str, str]]) -> List[str]:
    argv = [
        "qemu-system-x86_64", "-name", name, "-machine", "q35", "-accel", "kvm", "-accel", "tcg", "-cpu", "max",
        "-m", str(ram_mb), "-smp", str(cpus), "-no-reboot",
        # virtio-gpu + screendump: the same display setup build/qa/boot_test.py proved for headless X
        "-vga", "none", "-device", "virtio-vga", "-display", "none",
        "-serial", "file:%s" % serial_log,
        "-monitor", "unix:%s,server=on,wait=off" % monitor_sock,
    ]
    if network == "off":
        argv += ["-nic", "none"]
    else:
        argv += ["-netdev", "user,id=n0", "-device", "virtio-net-pci,netdev=n0"]
    argv += ["-audiodev", "none,id=snd0"]
    if firmware == "uefi":
        if ovmf is None:
            raise ValueError("--firmware uefi needs OVMF (apt-get install ovmf)")
        argv += ["-drive", "if=pflash,format=raw,unit=0,readonly=on,file=%s" % ovmf[0],
                 "-drive", "if=pflash,format=raw,unit=1,file=%s" % ovmf[1]]
    return argv


def build_install_argv(*, vmlinuz: Path, initrd: Path, iso: Path, disk: Path, serial_log: Path, monitor_sock: Path,
                       append: str, ram_mb: int, cpus: int, network: str = "on", firmware: str = "bios",
                       ovmf: Optional[Tuple[str, str]] = None) -> List[str]:
    """Phase 1: blank disk (virtio: /dev/vda), the ISO as CD-ROM, direct kernel boot of the ISO's own kernel/initrd."""
    argv = _machine_args(name="lindos-install-test", ram_mb=ram_mb, cpus=cpus, serial_log=serial_log,
                         monitor_sock=monitor_sock, network=network, firmware=firmware, ovmf=ovmf)
    argv += [
        # cache=unsafe: the disk is disposable, and the install writes gigabytes
        "-drive", "file=%s,if=virtio,format=raw,cache=unsafe" % disk,
        "-cdrom", str(iso),
        "-kernel", str(vmlinuz), "-initrd", str(initrd), "-append", append,
    ]
    return argv


def build_boot_argv(*, disk: Path, serial_log: Path, monitor_sock: Path, ram_mb: int, cpus: int, network: str = "on",
                    firmware: str = "bios", ovmf: Optional[Tuple[str, str]] = None,
                    kernel: Optional[Path] = None, initrd: Optional[Path] = None, append: str = "") -> List[str]:
    """Phase 3: the installed disk alone (no CD-ROM).  With *kernel*/*initrd* the kernel from the disk is booted
    directly with *append* (no boot loader); without, the disk's own boot loader runs."""
    argv = _machine_args(name="lindos-first-boot-test", ram_mb=ram_mb, cpus=cpus, serial_log=serial_log,
                         monitor_sock=monitor_sock, network=network, firmware=firmware, ovmf=ovmf)
    argv += ["-drive", "file=%s,if=virtio,format=raw" % disk, "-boot", "c"]
    if kernel is not None and initrd is not None:
        argv += ["-kernel", str(kernel), "-initrd", str(initrd), "-append", append]
    return argv


def kernel_append_for_disk_boot(root_dev: str, *, extra: Sequence[str] = ()) -> str:
    """Command line for booting the installed system's kernel directly (phase 3, ``--phase3-boot kernel``)."""
    words = ["root=%s" % root_dev, "ro", "console=tty0", "console=ttyS0,115200n8", "plymouth.enable=0",
             "systemd.show_status=1", CI_INSTALL_FLAG] + list(extra)
    return " ".join(words)


# ============================================================================================
#  serial-log parsing
# ============================================================================================
def parse_install_serial(text: str) -> dict:
    """What the serial log of the install phase says about the run (observer lines and the kernel's own)."""
    res = {"booted": "Linux version" in text, "observer": False, "hook_lines": [], "failed": [], "ubiquity_exit": None,
           "power_down": "reboot: Power down" in text, "restarting": "reboot: Restarting system" in text,
           "panic": "Kernel panic" in text, "finalized": False}
    for raw in text.splitlines():
        line = raw.strip()
        if OBSERVER_STARTED_RE.match(line):
            res["observer"] = True
            continue
        if INSTALL_FINALIZED_RE.match(line):
            res["finalized"] = True
            continue
        m = HOOK_LOG_RE.match(line)
        if m:
            res["hook_lines"].append(m.group(1))
            continue
        m = INSTALL_FAILED_RE.match(line)
        if m:
            res["failed"].append(m.group(1).strip())
            continue
        m = UBIQUITY_EXIT_RE.match(line)
        if m:
            res["ubiquity_exit"] = m.group(1).strip()
    return res


def parse_oem_serial(text: str) -> dict:
    """What the first boot of the installed disk printed: observer checks, READY/TIMEOUT, diagnostics."""
    checks: Dict[str, dict] = {}
    fail_logs: List[str] = []
    diag: List[str] = []
    res = {"booted": "Linux version" in text, "observer": False, "ready": False, "timeout": False, "fails": None,
           "checks": checks, "fail_logs": fail_logs, "diag": diag, "panic": "Kernel panic" in text, "info": {}}
    for raw in text.splitlines():
        line = raw.strip()
        m = OBSERVER_STARTED_RE.match(line)
        if m and m.group(1) == "oem":
            res["observer"] = True
            continue
        m = bt.CHECK_RE.match(line)
        if m:
            checks[m.group(1)] = {"status": m.group(2), "rc": m.group(3)}
            continue
        m = bt.FAIL_LOG_RE.match(line)
        if m:
            fail_logs.append(m.group(1))
            continue
        m = bt.INFO_RE.match(line)
        if m:
            res["info"][m.group(1)] = m.group(2)
            continue
        m = OEM_READY_RE.match(line)
        if m:
            res["ready"] = True
            res["fails"] = int(m.group(1)) if m.group(1) else None
            continue
        m = OEM_TIMEOUT_RE.match(line)
        if m:
            res["timeout"] = True
            res["fails"] = int(m.group(1)) if m.group(1) else None
            continue
        m = OEM_DIAG_RE.match(line)
        if m:
            diag.append(m.group(1))
    return res


def judge_install_phase(outcome: dict, serial: dict) -> List[ic.Finding]:
    """Findings about the install phase itself (before any disk assertion)."""
    out: List[ic.Finding] = []
    how = outcome.get("outcome")
    if how == "exited":
        if serial.get("restarting"):
            out.append(ic.warn("install-poweroff", "the guest RESTARTED instead of powering off (crash dialog + reboot, or "
                                                   "ubiquity/reboot was honoured)"))
        elif serial.get("power_down"):
            out.append(ic.ok("install-poweroff", "Ubiquity finished and the guest powered off after %ds" % outcome.get("seconds", 0)))
        else:
            out.append(ic.warn("install-poweroff", "QEMU exited after %ds without a kernel 'Power down' line" % outcome.get("seconds", 0)))
    elif how == "finalized-no-poweroff":
        out.append(ic.warn("install-poweroff", "the installation finished (finalize.sh ran) but the guest did not power off by "
                                               "itself: ubiquity/poweroff was not honoured; it was shut down through ACPI"))
    elif how == "timeout":
        out.append(ic.fail("install-finished", "the install did not finish in %ds (Ubiquity stuck on a page? see the "
                                                "screenshots and LINDOS_HOOK_LOG lines)" % outcome.get("seconds", 0)))
    elif how == "failed-marker":
        out.append(ic.fail("install-finished", "Ubiquity failed: " + "; ".join(serial.get("failed", [])[:3])))
    else:
        out.append(ic.fail("install-finished", "the install phase ended as %r" % (how,)))
    if serial.get("panic"):
        out.append(ic.fail("install-kernel", "the live kernel panicked"))
    if not serial.get("booted"):
        out.append(ic.fail("install-kernel", "no kernel output on the serial console: the live system never booted"))
    if not serial.get("observer"):
        out.append(ic.warn("install-observer", "the guest observer never started (the preseed early_command did not run?): "
                                               "the serial log has no hook lines"))
    if serial.get("hook_lines"):
        out.append(ic.info("install-hook-log", "%d hook log lines on the serial console; last: %s"
                           % (len(serial["hook_lines"]), serial["hook_lines"][-1][:160])))
    return out


def judge_first_boot(serial: dict, *, screenshot_ok: Optional[bool], screenshot_reason: str = "") -> List[ic.Finding]:
    """Findings about phase 3: the installed disk must boot into oem-config."""
    out: List[ic.Finding] = []
    if serial.get("panic"):
        out.append(ic.fail("boot-kernel", "the installed system's kernel panicked"))
    if not serial.get("observer"):
        out.append(ic.fail("boot-observer", "the installed system never started the observer: it did not boot far enough "
                                            "(GRUB failed? kernel panic? see serial.log and the screenshot)"))
        return out
    if serial.get("ready"):
        out.append(ic.ok("boot-oem-config", "the wizard is up: oem-config started instead of a login/oem desktop"))
    else:
        out.append(ic.fail("boot-oem-config", "oem-config never came up (LINDOS_OEM_TIMEOUT): " +
                           "; ".join(serial.get("diag", [])[:3])))
    for name, val in sorted(serial.get("checks", {}).items()):
        if val["status"] != "OK":
            why = [ln for ln in serial.get("fail_logs", []) if ln.startswith(name + ":")]
            out.append(ic.fail("first-boot-" + name, why[0] if why else "the observer reported FAIL rc=%s" % val.get("rc")))
        else:
            out.append(ic.ok("first-boot-" + name))
    if screenshot_ok is False:
        out.append(ic.fail("boot-screenshot", "the first-boot screenshot is blank (%s)" % screenshot_reason))
    elif screenshot_ok is None:
        out.append(ic.info("boot-screenshot", "no screenshot check (%s)" % (screenshot_reason or "unavailable")))
    else:
        out.append(ic.ok("boot-screenshot", "the screenshot shows something (%s)" % screenshot_reason))
    return out


# ============================================================================================
#  the disk: loop devices, mounts, CI-only modifications
# ============================================================================================
def _euid() -> int:
    return getattr(os, "geteuid", lambda: 1)()


def priv(argv: Sequence[str]) -> List[str]:
    """Prefix sudo when not root (the CI step runs the whole script under sudo; this makes a plain user work too)."""
    return list(argv) if _euid() == 0 else ["sudo", "-n"] + list(argv)


class LoopDisk:
    """A raw disk image attached to a loop device (with partition scan); partitions are mounted on demand.

    Thin on purpose: every action is one command through *run*, so the sequence (and the clean-up on failure)
    can be unit-tested with a fake runner.  Needs root (or passwordless sudo) on a Linux host.
    """

    def __init__(self, disk: Path, work: Path, *, run=subprocess.run,
                 list_partitions: Optional[Callable[[str], List[str]]] = None, sleep=time.sleep) -> None:
        self.disk = Path(disk)
        self.work = Path(work)
        self.run = run
        self._list = list_partitions or self._glob_partitions
        self.sleep = sleep
        self.loop = ""
        self.mounts: List[Path] = []

    @staticmethod
    def _glob_partitions(loop: str) -> List[str]:
        return sorted(glob.glob(loop + "p[0-9]*"), key=lambda p: int(re.sub(r"\D", "", p.rsplit("p", 1)[-1]) or 0))

    def _do(self, argv: Sequence[str], *, check: bool = True):
        proc = self.run(priv(argv), capture_output=True, text=True)
        if check and getattr(proc, "returncode", 0) != 0:
            raise RuntimeError("%s failed (%s): %s" % (" ".join(argv[:2]), proc.returncode, (proc.stderr or "").strip()[:300]))
        return proc

    def __enter__(self) -> "LoopDisk":
        proc = self._do(["losetup", "--find", "--show", "--partscan", str(self.disk)])
        self.loop = (proc.stdout or "").strip().splitlines()[-1]
        self._do(["udevadm", "settle", "--timeout=10"], check=False)
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def partitions(self) -> List[str]:
        parts = self._list(self.loop)
        for _ in range(10):
            if parts:
                break
            self.sleep(0.5)
            parts = self._list(self.loop)
        return parts or [self.loop]

    def mount(self, dev: str, name: str, *, rw: bool = False) -> Optional[Path]:
        target = self.work / name
        target.mkdir(parents=True, exist_ok=True)
        attempts = [["-o", "rw"]] if rw else [["-o", "ro,noload"], ["-o", "ro"]]
        for opts in attempts:
            proc = self._do(["mount", *opts, dev, str(target)], check=False)
            if getattr(proc, "returncode", 1) == 0:
                self.mounts.append(target)
                return target
        return None

    def unmount(self, target: Path) -> None:
        proc = self._do(["umount", str(target)], check=False)
        if getattr(proc, "returncode", 0) != 0:
            self._do(["umount", "-l", str(target)], check=False)
        if target in self.mounts:
            self.mounts.remove(target)

    def close(self) -> None:
        for target in reversed(list(self.mounts)):
            self.unmount(target)
        if self.loop:
            self._do(["sync"], check=False)
            self._do(["losetup", "-d", self.loop], check=False)
            self.loop = ""

    def find_system(self, *, rw: bool = False) -> Tuple[Optional[Path], Optional[Path], Optional[str]]:
        """(root mount, EFI system partition mount, root device): the first partition that holds /etc and
        /var/lib/dpkg is the system; a vfat partition with /EFI is the ESP; everything else is unmounted again."""
        root: Optional[Path] = None
        root_dev: Optional[str] = None
        esp: Optional[Path] = None
        for i, dev in enumerate(self.partitions(), 1):
            mnt = self.mount(dev, "p%d" % i, rw=rw)
            if mnt is None:
                continue
            if (mnt / "etc").is_dir() and (mnt / "var" / "lib" / "dpkg").is_dir() and root is None:
                root, root_dev = mnt, dev
            elif (mnt / "EFI").is_dir() and esp is None:
                esp = mnt
            else:
                self.unmount(mnt)
        return root, esp, root_dev


def read_mbr(disk: Path) -> Optional[bytes]:
    try:
        with open(disk, "rb") as fh:
            return fh.read(512)
    except OSError:
        return None


def patch_grub_cfg(text: str, extra: Sequence[str] = ("console=tty0", "console=ttyS0,115200n8", "plymouth.enable=0",
                                                     "systemd.show_status=1", CI_INSTALL_FLAG),
                   drop: Sequence[str] = ("quiet", "splash")) -> Tuple[str, int]:
    """The installed /boot/grub/grub.cfg with a serial console on every linux line (CI-only); returns (text, lines patched)."""
    out: List[str] = []
    patched = 0
    for line in text.splitlines():
        m = re.match(r"^(\s*)(linux|linuxefi)(\s+.*)$", line)
        if m:
            words = [w for w in m.group(3).split() if w not in drop]
            words += [w for w in extra if w not in words]
            line = "%s%s %s" % (m.group(1), m.group(2), " ".join(words))
            patched += 1
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else ""), patched


def inject_observer(root: Path, script: bytes, *, symlink: Callable[[str, str], None] = os.symlink) -> List[str]:
    """Write the observer + its unit into the installed system (CI-only; after the read-only assertions)."""
    written: List[str] = []
    sbin = root / "usr" / "local" / "sbin"
    sbin.mkdir(parents=True, exist_ok=True)
    (sbin / "lindos-ci-observer").write_bytes(script)
    (sbin / "lindos-ci-observer").chmod(0o755)
    written.append(OBSERVER_BIN)
    unit_dir = root / "etc" / "systemd" / "system"
    (unit_dir / "sysinit.target.wants").mkdir(parents=True, exist_ok=True)
    (unit_dir / OBSERVER_UNIT).write_text(observer_unit("oem"), encoding="utf-8")
    written.append("/etc/systemd/system/" + OBSERVER_UNIT)
    link = unit_dir / "sysinit.target.wants" / OBSERVER_UNIT
    if os.path.lexists(link):
        link.unlink()
    symlink("/etc/systemd/system/" + OBSERVER_UNIT, str(link))
    written.append("/etc/systemd/system/sysinit.target.wants/" + OBSERVER_UNIT)
    return written


def patch_grub_on_disk(root: Path) -> int:
    cfg = root / "boot" / "grub" / "grub.cfg"
    if not cfg.is_file():
        return 0
    new, n = patch_grub_cfg(cfg.read_text(encoding="utf-8", errors="replace"))
    if n:
        cfg.write_text(new, encoding="utf-8")
    return n


# ============================================================================================
#  collecting the installer's logs (secrets scrubbed)
# ============================================================================================
LOG_COLLECT: Tuple[str, ...] = (
    "var/lib/lindos/install-state.json", "var/log/lindos", "var/log/installer", "var/log/apt/history.log",
    "var/log/apt/term.log", "var/log/dpkg.log", "var/log/oem-config.log", "etc/fstab", "boot/grub/grub.cfg",
    "etc/lightdm/lightdm.conf", "etc/os-release", "etc/lindos-release",
)
LOG_CAP = 6 << 20


def collect_logs(tree: ic.Tree, dest: Path, secrets_: Iterable[str] = ()) -> List[str]:
    """Copy the installer's logs out of the installed tree, the tail of each capped, secrets scrubbed."""
    dest.mkdir(parents=True, exist_ok=True)
    secrets_ = list(secrets_)
    copied: List[str] = []

    def one(rel: str) -> None:
        data = tree.read_bytes(rel, LOG_CAP * 4)
        if data is None:
            return
        if len(data) > LOG_CAP:
            data = data[-LOG_CAP:]
        text = ic.redact_secrets(data.decode("utf-8", errors="replace"), secrets_)
        out = dest / rel.replace("/", "__")
        out.write_text(text, encoding="utf-8")
        copied.append(rel)

    for rel in LOG_COLLECT:
        if tree.is_dir(rel):
            for name in tree.listdir(rel):
                if tree.is_file(rel + "/" + name):
                    one(rel + "/" + name)
        elif tree.is_file(rel):
            one(rel)
    pk = tree.read_text("var/lib/dpkg/status", limit=64 << 20)
    if pk:
        pkgs = ic.parse_dpkg_status(pk)
        (dest / "dpkg-packages.txt").write_text(
            "\n".join("%s %s (%s %s %s)" % (p.name, p.version, p.want, p.flag, p.state) for p in sorted(pkgs.values())) + "\n",
            encoding="utf-8")
        copied.append("var/lib/dpkg/status (summary)")
    return copied


# ============================================================================================
#  screenshots
# ============================================================================================
def screenshot_verdict(path: Path) -> Tuple[Optional[bool], str]:
    """(False, why) for a blank/single-colour screenshot, (True, stats) for one with content, (None, why) if unjudged."""
    return bt.screenshot_has_content(path)


# ============================================================================================
#  host connectivity
# ============================================================================================
PROBE_URLS: Tuple[str, ...] = ("http://archive.ubuntu.com/ubuntu/dists/noble/InRelease",
                               "https://dl.google.com/linux/linux_signing_key.pub")


def probe_online(*, opener=urllib.request.urlopen, urls: Sequence[str] = PROBE_URLS, timeout: float = 8.0) -> bool:
    """True when this runner can reach the archive and Google's signing key (the two things the hook needs first)."""
    for url in urls:
        try:
            with opener(url, timeout=timeout) as resp:  # noqa: S310 - fixed, https/http vendor URLs
                resp.read(1)
        except Exception:  # noqa: BLE001 - any failure means "no usable internet"
            return False
    return True


# ============================================================================================
#  running the phases
# ============================================================================================
def _read_new(path: Path, offset: int) -> Tuple[str, int]:
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return "", offset
    return data.decode("utf-8", errors="replace"), offset + len(data)


def stop_qemu(proc) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=20)


def acpi_powerdown(monitor_sock: Path) -> bool:
    """Press the guest's power button through the QEMU monitor (a clean shutdown, unlike killing QEMU)."""
    try:
        mon = bt.MonitorClient(monitor_sock, connect_timeout=10.0)
    except RuntimeError:
        return False
    try:
        mon.command("system_powerdown")
        return True
    finally:
        mon.close()


def run_install_phase(argv: Sequence[str], *, serial_log: Path, monitor_sock: Path, shots_dir: Path, timeout: float,
                      shots_every: float = 600.0, poll: float = 3.0, fail_grace: float = 45.0,
                      finalize_grace: float = 300.0, powerdown_wait: float = 120.0,
                      popen=subprocess.Popen, sleep=time.sleep, monotonic=time.monotonic,
                      shoot: Optional[Callable[[Path, Path], bool]] = None,
                      powerdown: Optional[Callable[[Path], bool]] = None) -> dict:
    """Run the installer VM until it powers off, fails (LINDOS_INSTALL_FAILED) or the timeout ends it.

    Returns {'outcome': exited|finalized-no-poweroff|timeout|failed-marker, 'seconds', 'rc', 'screenshots'}.
    Progress screenshots (every *shots_every* s) show where an install is stuck.  When finalize.sh has run
    (LINDOS_INSTALL_FINALIZED) the guest has *finalize_grace* seconds to power off by itself; if it does not
    (Ubiquity ignored ubiquity/poweroff and waits at its 'finished' dialog) the harness presses the power button,
    so a completed installation is judged in minutes instead of hitting the install timeout.
    """
    shoot = shoot or (lambda sock, png: bt.take_screenshot(sock, png, quit_after=False))
    powerdown = powerdown or acpi_powerdown
    shots_dir.mkdir(parents=True, exist_ok=True)
    proc = popen(list(argv))
    started = monotonic()
    next_shot = started + shots_every
    offset = 0
    shots: List[str] = []
    outcome = "running"
    failed_at: Optional[float] = None
    finalized_at: Optional[float] = None
    try:
        while True:
            text, offset = _read_new(serial_log, offset)
            lines = [ln.strip() for ln in text.splitlines()]
            if failed_at is None and any(INSTALL_FAILED_RE.match(ln) for ln in lines):
                failed_at = monotonic()
                log("the guest reports a failed installation; collecting evidence for %ds" % fail_grace)
            if finalized_at is None and any(INSTALL_FINALIZED_RE.match(ln) for ln in lines):
                finalized_at = monotonic()
                log("finalize.sh has run; the guest has %ds to power off by itself" % finalize_grace)
            rc = proc.poll()
            now = monotonic()
            if rc is not None:
                outcome = "exited"
                break
            if finalized_at is not None and now - finalized_at >= finalize_grace:
                outcome = "finalized-no-poweroff"
                png = shots_dir / "install-finished.png"
                if shoot(monitor_sock, png):
                    shots.append(png.name)
                if powerdown(monitor_sock):
                    waited = 0.0
                    while proc.poll() is None and waited < powerdown_wait:
                        sleep(poll)
                        waited += poll
                break
            if failed_at is not None and now - failed_at >= fail_grace:
                outcome = "failed-marker"
                png = shots_dir / ("install-failed.png")
                if shoot(monitor_sock, png):
                    shots.append(png.name)
                break
            if now - started >= timeout:
                outcome = "timeout"
                png = shots_dir / "install-timeout.png"
                if shoot(monitor_sock, png):
                    shots.append(png.name)
                break
            if now >= next_shot:
                png = shots_dir / ("install-progress-%03d.png" % (len(shots) + 1))
                if shoot(monitor_sock, png):
                    shots.append(png.name)
                next_shot = now + shots_every
            sleep(poll)
    finally:
        stop_qemu(proc)
    return {"outcome": outcome, "seconds": int(monotonic() - started), "rc": proc.returncode, "screenshots": shots}


def run_first_boot_phase(argv: Sequence[str], *, serial_log: Path, monitor_sock: Path, screenshot: Path, timeout: float,
                         grace: float, popen=subprocess.Popen, sleep=time.sleep,
                         wait_for: Optional[Callable[..., Optional[str]]] = None,
                         shoot: Optional[Callable[[Path, Path], bool]] = None) -> dict:
    """Boot the installed disk, wait for the observer's READY/TIMEOUT line, give the wizard a moment, take the picture."""
    wait_for = wait_for or bt._tail_for
    shoot = shoot or (lambda sock, png: bt.take_screenshot(sock, png, quit_after=False))
    proc = popen(list(argv))
    took = False
    line: Optional[str] = None
    try:
        line = wait_for(serial_log, [OEM_READY_RE, OEM_TIMEOUT_RE], timeout=timeout)
        if line is None:
            log("timeout waiting for the installed system's observer; taking the screenshot anyway")
        else:
            log("first boot: %s; waiting %ds for the wizard to draw" % (line, grace))
            sleep(grace)
        took = shoot(monitor_sock, screenshot)
    finally:
        stop_qemu(proc)
    return {"line": line, "screenshot": took}


# ============================================================================================
#  reporting
# ============================================================================================
def render_summary(verdict: str, sections: Mapping[str, Sequence[ic.Finding]], notes: Sequence[str]) -> str:
    """The Markdown that goes to $GITHUB_STEP_SUMMARY and out-dir/summary.md."""
    lines = ["# Lindos install test: %s" % verdict, ""]
    lines += ["- " + n for n in notes]
    for title, findings in sections.items():
        lines += ["", "## " + title, ""]
        lines += ["- `%s` **%s** %s" % (f.level.upper(), f.name, f.detail) for f in findings if f.level != ic.OK]
        n_ok = sum(1 for f in findings if f.level == ic.OK)
        lines.append("- %d check(s) passed" % n_ok)
    return "\n".join(lines) + "\n"


def findings_json(sections: Mapping[str, Sequence[ic.Finding]]) -> List[dict]:
    return [{"phase": title, "name": f.name, "level": f.level, "detail": f.detail}
            for title, findings in sections.items() for f in findings]


# ============================================================================================
#  command line
# ============================================================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--iso", required=True, help="path to the built ISO (glob allowed)")
    p.add_argument("--out-dir", required=True, help="where logs, screenshots, the disk image and the report go")
    p.add_argument("--disk-size", default="32G", help="size of the blank sparse disk (default 32G: the installer's "
                                                    "union of every Mode's extras does not fit the 20G a plain Mint needs)")
    p.add_argument("--ram", type=int, default=6144, help="guest RAM in MB")
    p.add_argument("--cpus", type=int, default=4, help="guest vCPUs")
    p.add_argument("--install-timeout", type=int, default=5400, help="seconds the installation may take in total")
    p.add_argument("--install-budget", type=int, default=2400,
                   help="seconds of wall clock the installer HOOK may use (kernel word lindos.install_budget)")
    p.add_argument("--poweroff-grace", type=int, default=300,
                   help="seconds the guest gets to power off by itself once finalize.sh has run; then the harness presses "
                        "the power button (Ubiquity ignored ubiquity/poweroff)")
    p.add_argument("--boot-timeout", type=int, default=900, help="seconds to wait for the first boot's oem-config")
    p.add_argument("--grace", type=int, default=20, help="seconds between 'wizard is up' and the screenshot")
    p.add_argument("--shots-every", type=int, default=600, help="seconds between progress screenshots of the install")
    p.add_argument("--ubiquity-mode", choices=("automatic", "noninteractive"), default="automatic",
                   help="automatic-ubiquity (GTK, needs X) or noninteractive (no window at all)")
    p.add_argument("--firmware", choices=("bios", "uefi"), default="bios")
    p.add_argument("--network", choices=("on", "off"), default="on", help="'off' boots without a NIC (offline install)")
    p.add_argument("--expect-online", choices=("auto", "yes", "no"), default="auto",
                   help="does the runner have internet? 'auto' probes it; decides whether Chrome MUST be installed")
    p.add_argument("--phase3-boot", choices=("grub", "kernel"), default="grub",
                   help="boot the installed disk through its own GRUB, or boot its kernel directly")
    p.add_argument("--skip-phase3", action="store_true", help="do not boot the installed disk")
    p.add_argument("--require-kernel-suffix", default=None, help="fail unless the installed kernel ends with this, e.g. -lindos")
    p.add_argument("--extra-cmdline", default="", help="extra words for the installer's kernel command line")
    p.add_argument("--allow-tcg", action="store_true", help="run without /dev/kvm (hours, not minutes)")
    p.add_argument("--keep-disk", action="store_true", help="keep the installed disk image in the out dir")
    p.add_argument("--skip-preflight", action="store_true", help="do not inspect the ISO with xorriso first")
    return p


def resolve_expect_online(choice: str, network: str, *, probe: Callable[[], bool] = probe_online) -> Optional[bool]:
    if network == "off":
        return False
    if choice == "yes":
        return True
    if choice == "no":
        return False
    return probe()


def _mib(n: int) -> str:
    return "%.1f MiB" % (n / (1 << 20))


def main(argv: Optional[List[str]] = None) -> int:
    ns = build_parser().parse_args(argv)
    matches = sorted(glob.glob(ns.iso))
    if not matches:
        print("install_test: no ISO matched %r" % ns.iso, file=sys.stderr)
        return 2
    if not re.fullmatch(r"\d+[KMGT]?", ns.disk_size):
        print("install_test: --disk-size must look like 32G", file=sys.stderr)
        return 2
    for tool in ("qemu-system-x86_64", "xorriso", "losetup", "mount"):
        if shutil.which(tool) is None and not (tool in ("losetup", "mount") and _euid() != 0 and shutil.which("sudo")):
            print("install_test: %s not found (apt-get install qemu-system-x86 xorriso)" % tool, file=sys.stderr)
            return 2
    kvm_ok = os.access("/dev/kvm", os.R_OK | os.W_OK)
    if not kvm_ok and not ns.allow_tcg:
        print("install_test: /dev/kvm is not usable and an install under TCG takes hours; pass --allow-tcg to try anyway",
              file=sys.stderr)
        return 2
    ovmf = find_ovmf()
    if ns.firmware == "uefi" and ovmf is None:
        print("install_test: --firmware uefi needs OVMF (apt-get install ovmf)", file=sys.stderr)
        return 2

    iso = Path(matches[-1]).resolve()
    out_dir = Path(ns.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "work"
    work.mkdir(exist_ok=True)
    sections: Dict[str, List[ic.Finding]] = {}
    notes: List[str] = ["ISO: %s (%.2f GiB)" % (iso.name, iso.stat().st_size / (1 << 30)),
                        "guest: %d MB RAM, %d vCPUs, %s disk, firmware %s, network %s, %s ubiquity, KVM %s" % (
                            ns.ram, ns.cpus, ns.disk_size, ns.firmware, ns.network, ns.ubiquity_mode, "yes" if kvm_ok else "NO (TCG)")]
    log("ISO: %s" % iso)

    # ---- phase 0: preflight ------------------------------------------------------------------
    if ns.skip_preflight:
        words: List[str] = list(FALLBACK_ENTRY_WORDS)
        sections["preflight"] = [ic.info("preflight", "skipped; using the built-in entry words")]
    else:
        pre, words = preflight_iso(iso, work)
        sections["preflight"] = pre
    for f in sections["preflight"]:
        log(f.line())
    if ic.failures(sections["preflight"]):
        return _finish(out_dir, sections, notes, "FAIL")

    # ---- kernel/initrd + the overlay ---------------------------------------------------------
    try:
        vmlinuz, initrd = bt.extract_casper(iso, work / "extract")
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print("install_test: failed to extract casper kernel/initrd: %s" % exc, file=sys.stderr)
        return 2
    password = secrets.token_hex(12)
    opts = SeedOptions(password=password)
    preseed = build_preseed(opts)
    (out_dir / "preseed.redacted.seed").write_text(build_preseed(opts, redact=True), encoding="utf-8")
    observer = OBSERVER_SCRIPT.read_bytes()
    combined = write_combined_initrd(build_overlay_cpio(preseed, observer), initrd, work / "initrd.overlay")
    append = install_cmdline(words, mode=ns.ubiquity_mode, budget=ns.install_budget, extra=ns.extra_cmdline.split())
    (out_dir / "install-cmdline.txt").write_text(append + "\n", encoding="utf-8")
    log("kernel command line: " + append)

    expect_online = resolve_expect_online(ns.expect_online, ns.network)
    notes.append("the runner %s internet" % {True: "has", False: "has no", None: "may have"}[expect_online])
    disk = work / "disk.raw"
    with open(disk, "wb") as fh:
        fh.truncate(_parse_size(ns.disk_size))

    # ---- phase 1: install --------------------------------------------------------------------
    serial1 = out_dir / "serial-install.log"
    mon1 = out_dir / "monitor-install.sock"
    for p in (serial1, mon1):
        if p.exists():
            p.unlink()
    argv1 = build_install_argv(vmlinuz=vmlinuz, initrd=combined, iso=iso, disk=disk, serial_log=serial1, monitor_sock=mon1,
                               append=append, ram_mb=ns.ram, cpus=ns.cpus, network=ns.network, firmware=ns.firmware, ovmf=ovmf)
    log("phase 1 (install): " + " ".join(argv1))
    outcome = run_install_phase(argv1, serial_log=serial1, monitor_sock=mon1, shots_dir=out_dir, timeout=ns.install_timeout,
                                shots_every=ns.shots_every, finalize_grace=ns.poweroff_grace)
    serial_text = serial1.read_text(encoding="utf-8", errors="replace") if serial1.exists() else ""
    sections["install"] = judge_install_phase(outcome, parse_install_serial(serial_text))
    for f in sections["install"]:
        log(f.line())
    notes.append("install phase: %s after %ds" % (outcome["outcome"], outcome["seconds"]))

    # ---- phase 2: read-only assertions -------------------------------------------------------
    system_found = False
    boot_kernel: Optional[Tuple[Path, Path, str]] = None
    with LoopDisk(disk, work / "mnt") as loop:
        root, esp, root_dev = loop.find_system()
        if root is None:
            sections["disk"] = [ic.fail("disk-system", "no partition of the disk holds an installed system (the install "
                                                       "never reached the copy step, or partitioning failed)")]
        else:
            system_found = True
            tree = ic.Tree(root)
            sections["disk"] = ic.run_all_checks(tree, expect_online=expect_online, mbr=read_mbr(disk),
                                                 firmware=ns.firmware, esp=ic.Tree(esp) if esp else None)
            copied = collect_logs(tree, out_dir / "installed-logs", [password])
            notes.append("collected %d log file(s) from the installed disk" % len(copied))
            kernels = ic.list_kernels(tree)
            if ns.require_kernel_suffix and not any(k.version.endswith(ns.require_kernel_suffix) for k in kernels):
                sections["disk"].append(ic.fail("kernel-suffix", "no installed kernel ends with %r (have: %s)" % (
                    ns.require_kernel_suffix, ", ".join(k.version for k in kernels) or "none")))
            pick = ic.pick_kernel(kernels, ns.require_kernel_suffix)
            if pick and root_dev and ns.phase3_boot == "kernel":
                # the kernel files leave the read-only mount before it is unmounted
                kdir = work / "installed-kernel"
                if tree.copy_to(pick.vmlinuz, kdir / "vmlinuz") and tree.copy_to(pick.initrd, kdir / "initrd"):
                    boot_kernel = (kdir / "vmlinuz", kdir / "initrd", root_dev)
        for f in sections["disk"]:
            log(f.line())

    # ---- phase 3: first boot of the installed disk -------------------------------------------
    boot_findings: List[ic.Finding] = []
    if ns.skip_phase3:
        boot_findings.append(ic.info("first-boot", "skipped (--skip-phase3)"))
    elif not system_found or outcome["outcome"] not in ("exited", "finalized-no-poweroff"):
        boot_findings.append(ic.info("first-boot", "skipped: there is no completed installation to boot"))
    elif ns.phase3_boot == "kernel" and boot_kernel is None:
        boot_findings.append(ic.fail("first-boot", "no kernel/initrd pair could be taken from the installed disk"))
    else:
        with LoopDisk(disk, work / "mnt-rw") as loop:
            rw_root, _, _ = loop.find_system(rw=True)
            if rw_root is None:
                boot_findings.append(ic.fail("first-boot", "could not mount the installed system read-write to add the observer"))
            else:
                inject_observer(rw_root, observer)
                patched = patch_grub_on_disk(rw_root)
                log("phase 3 prepared: observer injected, %d grub linux line(s) patched for the serial console" % patched)
        if not ic.failures(boot_findings):
            boot_findings += _first_boot(ns, out_dir, disk, ovmf, boot_kernel)
    sections["first boot"] = boot_findings
    for f in boot_findings:
        log(f.line())

    if not ns.keep_disk:
        disk.unlink(missing_ok=True)
    bad = any(ic.failures(v) for v in sections.values())
    return _finish(out_dir, sections, notes, "FAIL" if bad else "PASS")


def _first_boot(ns: argparse.Namespace, out_dir: Path, disk: Path, ovmf: Optional[Tuple[str, str]],
                boot_kernel: Optional[Tuple[Path, Path, str]]) -> List[ic.Finding]:
    """Phase 3: boot the installed disk, wait for the observer, screenshot, judge."""
    serial3 = out_dir / "serial-first-boot.log"
    mon3 = out_dir / "monitor-first-boot.sock"
    for p in (serial3, mon3):
        p.unlink(missing_ok=True)
    common = dict(disk=disk, serial_log=serial3, monitor_sock=mon3, ram_mb=ns.ram, cpus=ns.cpus, network=ns.network,
                  firmware=ns.firmware, ovmf=ovmf)
    if ns.phase3_boot == "kernel" and boot_kernel is not None:
        kernel, initrd, dev = boot_kernel
        argv3 = build_boot_argv(kernel=kernel, initrd=initrd, append=kernel_append_for_disk_boot(dev), **common)
    else:
        argv3 = build_boot_argv(**common)
    log("phase 3 (first boot): " + " ".join(argv3))
    shot = out_dir / "first-boot.png"
    ran = run_first_boot_phase(argv3, serial_log=serial3, monitor_sock=mon3, screenshot=shot, timeout=ns.boot_timeout,
                               grace=ns.grace)
    text3 = serial3.read_text(encoding="utf-8", errors="replace") if serial3.exists() else ""
    picture = next((p for p in (shot, shot.with_suffix(".ppm")) if p.exists()), None)
    verdict_shot, why = screenshot_verdict(picture) if ran["screenshot"] and picture else (None, "not captured")
    return judge_first_boot(parse_oem_serial(text3), screenshot_ok=verdict_shot, screenshot_reason=why)


def _parse_size(text: str) -> int:
    m = re.fullmatch(r"(\d+)([KMGT]?)", text)
    assert m, text
    return int(m.group(1)) * {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}[m.group(2)]


def _finish(out_dir: Path, sections: Mapping[str, Sequence[ic.Finding]], notes: Sequence[str], verdict: str) -> int:
    summary = render_summary(verdict, sections, notes)
    (out_dir / "summary.md").write_text(summary, encoding="utf-8")
    (out_dir / "report.json").write_text(json.dumps({"verdict": verdict, "notes": list(notes),
                                                      "findings": findings_json(sections)}, indent=2), encoding="utf-8")
    print("\n===== install-test report =====")
    for title, findings in sections.items():
        print("[%s]" % title)
        for f in findings:
            if f.level != ic.OK:
                print("  " + f.line())
    print("===== %s =====" % verdict)
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
