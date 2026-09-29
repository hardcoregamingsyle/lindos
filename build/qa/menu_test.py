#!/usr/bin/env python3
"""build/qa/menu_test.py - boot the built Lindos ISO THROUGH ITS OWN BOOT LOADER under QEMU (SeaBIOS + ISOLINUX, or OVMF + GRUB).

build/qa/boot_test.py and build/qa/install_test.py start the ISO's kernel and initrd DIRECTLY (``-kernel``/``-initrd``, the
words copied from grub.cfg), so nothing there ever runs the shipped boot menus: a broken isolinux/live.cfg, a GRUB
script error, a wrong El Torito/EFI image or a menu whose default entry names a missing file would pass CI and show up
on real hardware only.  This harness boots the ISO like a machine does - ``-cdrom ISO`` and nothing else - and lets the
menu's own timeout start the DEFAULT entry (the first one: 'Install Lindos', only-ubiquity).

What it measures (no keystrokes, no serial console - a stock boot menu has neither):
  * screenshots (QEMU ``screendump``) of the first seconds: the boot loader drew something (a warning when it did not:
    a text-mode menu is a valid menu, and a black frame is only a hint);
  * the number of bytes the guest READ from the medium (monitor ``info blockstats``).  ISOLINUX/GRUB load the kernel
    and initrd (~100 MB) and casper then reads the root file system: a menu that waits for a key, a dangling default
    (a bare ``boot:`` prompt), a missing kernel/initrd or a kernel that never finds the medium stays far below
    ``--min-read-mb`` and FAILS; reaching it proves the default entry booted through casper;
  * a last screenshot after the boot (a warning when blank: whether the installer window draws is not this test's job).

What it cannot show: what the booted session runs (build/qa/ci-observer.sh judges that in the install test), that a
KEY press selects another entry, how the menus look, Secure Boot.  ``--firmware uefi`` runs OVMF (unproven, like the
install test's UEFI option).  Expect the first CI rounds to tune the thresholds: a red result here shows a screenshot.

Usage:
    python3 build/qa/menu_test.py --iso 'out/lindos-*.iso' --out-dir out/qemu-menu-test --firmware bios|uefi
        [--ram 4096] [--cpus 2] [--timeout 420] [--settle 25] [--min-read-mb 200] [--allow-tcg]

Exit codes: 0 pass - 1 a check failed - 2 usage/environment error.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import boot_test as bt  # noqa: E402  (QEMU monitor client, screendump, the blank-frame check)
import install_checks as ic  # noqa: E402  (Finding, ok/info/warn/fail)
import install_test as it  # noqa: E402  (OVMF lookup, stop_qemu)

DEFAULT_MIN_READ_MB = 200
_RD_BYTES = re.compile(r"\brd_bytes=(\d+)")


def log(msg: str) -> None:
    print("[menu-test] %s" % msg, flush=True)


# ============================================================================================
#  QEMU
# ============================================================================================
def build_argv(*, iso: Path, serial_log: Path, monitor_sock: Path, ram_mb: int, cpus: int, firmware: str = "bios",
               ovmf: Optional[Tuple[str, str]] = None) -> List[str]:
    """The ISO as the ONLY device that can boot: no -kernel, no -initrd, no -append.  ``-vga std`` (a VBE-capable VGA BIOS
    for ISOLINUX's graphical menu; GOP under OVMF), screendump works with ``-display none``."""
    argv = [
        "qemu-system-x86_64", "-name", "lindos-menu-test", "-machine", "q35", "-accel", "kvm", "-accel", "tcg", "-cpu", "max",
        "-m", str(ram_mb), "-smp", str(cpus), "-no-reboot",
        "-vga", "std", "-display", "none",
        "-serial", "file:%s" % serial_log,
        "-monitor", "unix:%s,server=on,wait=off" % monitor_sock,
        "-netdev", "user,id=n0", "-device", "virtio-net-pci,netdev=n0",
        "-audiodev", "none,id=snd0",
    ]
    if firmware == "uefi":
        if ovmf is None:
            raise ValueError("--firmware uefi needs OVMF (apt-get install ovmf)")
        argv += ["-drive", "if=pflash,format=raw,unit=0,readonly=on,file=%s" % ovmf[0],
                 "-drive", "if=pflash,format=raw,unit=1,file=%s" % ovmf[1]]
    argv += ["-drive", "if=none,id=cd0,media=cdrom,readonly=on,file=%s" % iso,
             "-device", "ide-cd,drive=cd0,bootindex=0"]
    return argv


def parse_blockstats(text: str) -> Optional[int]:
    """The bytes read from the boot medium, from the monitor's ``info blockstats`` (None when it shows no counter)."""
    values = [int(m) for m in _RD_BYTES.findall(text or "")]
    return max(values) if values else None


def read_blockstats(monitor_sock: Path) -> Optional[int]:
    try:
        mon = bt.MonitorClient(monitor_sock, connect_timeout=5.0)
    except RuntimeError:
        return None
    try:
        return parse_blockstats(mon.command("info blockstats", timeout=10.0).decode("utf-8", errors="replace"))
    except (OSError, RuntimeError):
        return None
    finally:
        mon.close()


# ============================================================================================
#  the run
# ============================================================================================
def _frame(shoot: Callable[[Path, Path], bool], monitor_sock: Path, png: Path, at: float) -> dict:
    took = shoot(monitor_sock, png)
    picture = next((p for p in (png, png.with_suffix(".ppm")) if p.exists()), None)
    if not took or picture is None:
        return {"name": png.name, "at": int(at), "has_content": None, "stats": "not captured"}
    has, stats = bt.screenshot_has_content(picture)
    return {"name": picture.name, "at": int(at), "has_content": has, "stats": stats}


def run_menu_boot(argv: Sequence[str], *, monitor_sock: Path, shots_dir: Path, timeout: float = 420.0, settle: float = 25.0,
                  min_read_bytes: int = DEFAULT_MIN_READ_MB << 20, menu_window: float = 48.0, shot_every: float = 6.0,
                  poll: float = 3.0, popen=subprocess.Popen, sleep=time.sleep, monotonic=time.monotonic,
                  shoot: Optional[Callable[[Path, Path], bool]] = None,
                  read_stats: Optional[Callable[[Path], Optional[int]]] = None) -> dict:
    """Start QEMU, watch the menu frames and the bytes read, stop QEMU.

    Returns {'outcome': booted|timeout|exited, 'seconds', 'read_bytes', 'stats_seen', 'reached_at', 'frames', 'final'}.
    'booted': the guest read at least *min_read_bytes* from the medium (and had *settle* more seconds to draw);
    'timeout': it did not within *timeout*; 'exited': QEMU quit by itself (a crash or a reboot, -no-reboot).
    """
    shoot = shoot or (lambda sock, png: bt.take_screenshot(sock, png, quit_after=False))
    read_stats = read_stats or read_blockstats
    shots_dir.mkdir(parents=True, exist_ok=True)
    proc = popen(list(argv))
    started = monotonic()
    frames: List[dict] = []
    read_bytes: Optional[int] = None
    reached_at: Optional[float] = None
    outcome = "running"
    final: Optional[dict] = None
    next_shot = shot_every
    try:
        while True:
            now = monotonic() - started
            if proc.poll() is not None:
                outcome = "exited"
                break
            if next_shot <= now <= menu_window:
                frames.append(_frame(shoot, monitor_sock, shots_dir / ("menu-%02d.png" % (len(frames) + 1)), now))
                next_shot = now + shot_every
            got = read_stats(monitor_sock)
            if got is not None:
                read_bytes = got if read_bytes is None else max(read_bytes, got)
                if reached_at is None and got >= min_read_bytes:
                    reached_at = now
            if reached_at is not None and now - reached_at >= settle:
                outcome = "booted"
                break
            if now >= timeout:
                outcome = "timeout"
                break
            sleep(poll)
        if outcome in ("booted", "timeout"):
            final = _frame(shoot, monitor_sock, shots_dir / "boot-final.png", monotonic() - started)
    finally:
        it.stop_qemu(proc)
    return {"outcome": outcome, "seconds": int(monotonic() - started), "read_bytes": read_bytes,
            "stats_seen": read_bytes is not None, "reached_at": None if reached_at is None else int(reached_at),
            "frames": frames, "final": final}


# ============================================================================================
#  judging
# ============================================================================================
def _mib(n: Optional[int]) -> str:
    return "%.0f MiB" % ((n or 0) / (1 << 20))


def judge_menu_boot(result: dict, *, firmware: str, min_read_mb: int = DEFAULT_MIN_READ_MB) -> List[ic.Finding]:
    """Findings about one boot of the ISO's own boot loader (see the module docstring for what each proves)."""
    tag = "menu-boot-" + firmware
    out: List[ic.Finding] = []
    frames = result.get("frames") or []
    rich = [f for f in frames if f.get("has_content") is True]
    if not frames:
        out.append(ic.info(tag + "-menu", "no menu frame was captured"))
    elif rich:
        out.append(ic.ok(tag + "-menu", "the boot loader drew a screen with content (%s at %ds: %s)" % (rich[0]["name"], rich[0]["at"], rich[0]["stats"])))
    elif all(f.get("has_content") is None for f in frames):
        out.append(ic.info(tag + "-menu", "the menu frames could not be judged (%s)" % frames[0].get("stats")))
    else:
        out.append(ic.warn(tag + "-menu", "no frame of the first seconds had content (%s): a text-only menu, or the boot loader "
                                          "did not draw" % frames[0].get("stats")))
    how = result.get("outcome")
    read = result.get("read_bytes")
    final = result.get("final") or {}
    if how == "booted":
        out.append(ic.ok(tag + "-default-boot", "the default entry booted: %s read from the medium after %ss (kernel, initrd and casper's "
                                                "root file system)" % (_mib(read), result.get("reached_at"))))
    elif how == "timeout" and result.get("stats_seen"):
        out.append(ic.fail(tag + "-default-boot", "only %s were read from the medium in %ds (need %d MiB): the default entry did not boot - a "
                                                  "menu waiting for a key, a dangling default (bare 'boot:' prompt), a missing kernel or "
                                                  "initrd, or a kernel that never found the medium; see the screenshots"
                           % (_mib(read), result.get("seconds", 0), min_read_mb)))
    elif how == "timeout":
        # no I/O counter (the monitor's format differs): the last screenshot is all there is
        if final.get("has_content") is True:
            out.append(ic.warn(tag + "-default-boot", "the I/O counter could not be read; the last screen has content but nothing proves "
                                                      "the default entry booted"))
        else:
            out.append(ic.fail(tag + "-default-boot", "nothing shows that the default entry booted (no I/O counter, last screen: %s)"
                               % final.get("stats", "not captured")))
    else:
        out.append(ic.fail(tag + "-default-boot", "QEMU quit after %ds (%s read): the guest crashed or rebooted" % (result.get("seconds", 0), _mib(read))))
    if how == "booted":
        if final.get("has_content") is False:
            out.append(ic.warn(tag + "-screen", "the screen is blank after the boot (%s): the session may not have drawn" % final.get("stats")))
        elif final.get("has_content") is True:
            out.append(ic.ok(tag + "-screen", "the screen shows something after the boot (%s)" % final.get("stats")))
    return out


# ============================================================================================
#  command line
# ============================================================================================
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--iso", required=True, help="path to the built ISO (glob allowed)")
    ap.add_argument("--out-dir", required=True, help="where the screenshots and the report go")
    ap.add_argument("--firmware", choices=("bios", "uefi"), default="bios", help="SeaBIOS (ISOLINUX menu) or OVMF (GRUB menu)")
    ap.add_argument("--ram", type=int, default=4096, help="guest RAM in MB")
    ap.add_argument("--cpus", type=int, default=2, help="guest vCPUs")
    ap.add_argument("--timeout", type=int, default=420, help="seconds the default entry gets to read the medium")
    ap.add_argument("--settle", type=int, default=25, help="seconds to let the screen draw once the medium was read")
    ap.add_argument("--min-read-mb", type=int, default=DEFAULT_MIN_READ_MB,
                    help="MiB the guest must read from the medium: kernel + initrd (~100) + casper's root file system")
    ap.add_argument("--allow-tcg", action="store_true", help="run without /dev/kvm (far slower: raise --timeout)")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    ns = build_parser().parse_args(argv)
    matches = sorted(glob.glob(ns.iso))
    if not matches:
        print("menu_test: no ISO matched %r" % ns.iso, file=sys.stderr)
        return 2
    if shutil.which("qemu-system-x86_64") is None:
        print("menu_test: qemu-system-x86_64 not found (apt-get install qemu-system-x86)", file=sys.stderr)
        return 2
    if not os.access("/dev/kvm", os.R_OK | os.W_OK) and not ns.allow_tcg:
        print("menu_test: /dev/kvm is not usable and a boot under TCG takes far too long; pass --allow-tcg to try anyway", file=sys.stderr)
        return 2
    ovmf = it.find_ovmf() if ns.firmware == "uefi" else None
    if ns.firmware == "uefi" and ovmf is None:
        print("menu_test: --firmware uefi needs OVMF (apt-get install ovmf)", file=sys.stderr)
        return 2
    iso = Path(matches[-1]).resolve()
    out_dir = Path(ns.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    serial, mon = out_dir / ("serial-%s.log" % ns.firmware), out_dir / ("monitor-%s.sock" % ns.firmware)
    for stale in (serial, mon):
        stale.unlink(missing_ok=True)
    if ovmf is not None:
        # the firmware's variable store is written to: never the system copy
        vars_copy = out_dir / ("OVMF_VARS-%s.fd" % ns.firmware)
        shutil.copyfile(ovmf[1], vars_copy)
        ovmf = (ovmf[0], str(vars_copy))
    argv_q = build_argv(iso=iso, serial_log=serial, monitor_sock=mon, ram_mb=ns.ram, cpus=ns.cpus, firmware=ns.firmware, ovmf=ovmf)
    log("ISO: %s; firmware %s; qemu: %s" % (iso.name, ns.firmware, " ".join(argv_q)))
    result = run_menu_boot(argv_q, monitor_sock=mon, shots_dir=out_dir / ns.firmware, timeout=ns.timeout, settle=ns.settle,
                           min_read_bytes=ns.min_read_mb << 20)
    findings = judge_menu_boot(result, firmware=ns.firmware, min_read_mb=ns.min_read_mb)
    lines = ["===== menu test (%s): %s =====" % (ns.firmware, iso.name),
             "outcome: %s after %ds; %s read from the medium" % (result["outcome"], result["seconds"], _mib(result.get("read_bytes")))]
    lines += [f.line() for f in findings]
    bad = ic.failures(findings)
    lines.append("===== %s =====" % ("FAIL" if bad else "PASS"))
    text = "\n".join(lines)
    print(text)
    (out_dir / ("report-%s.txt" % ns.firmware)).write_text(text + "\n", encoding="utf-8")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
