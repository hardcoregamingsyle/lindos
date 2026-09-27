#!/usr/bin/env python3
"""build/qa/boot_test.py — headless CI boot test for the built Lindos ISO.

Extracts the live kernel/initrd from the ISO (so GRUB's menu timeout/theme
never has to be scripted), boots them directly under QEMU with the ISO
attached as a virtual CD-ROM (so casper's live-boot init finds the squashfs
on it exactly as it would on real hardware), appends
``systemd.run=/usr/libexec/lindos/qa/ci-boot-smoke-test.sh`` on the kernel
command line (systemd's kernel-command-line generator turns that into a
transient unit that runs *alongside* the normal boot — it never delays or
replaces the real desktop startup), and:

  1. tails the serial console for the smoke test's ``LINDOS_SMOKE_DONE``
     sentinel (with an overall timeout — a stuck/crashed boot fails the test
     instead of hanging CI forever);
  2. waits a further grace period for the live desktop (LightDM autologin +
     XFCE) to actually finish drawing;
  3. takes a screenshot via the QEMU monitor's ``screendump`` (works with
     ``-display none`` as long as a real VGA device — ``-vga std`` — is
     present, so no X server/VNC/framebuffer capture tooling is needed);
  4. quits QEMU and reports pass/fail from the serial log alone (never from
     the guest's own shutdown/exit code, which is unreliable to script).

Nothing here is guessed: every check the smoke-test script runs is itself
one of Lindos's own shipped CLIs, so a failure here is a failure of the real
system, not of this test harness.

Usage:
    python3 build/qa/boot_test.py --iso out/lindos-*.iso --out-dir out/qemu-boot-test
        [--timeout 600] [--grace 90] [--ram 4096] [--cpus 2]
        [--require-kernel-suffix -lindos]

Exit codes: 0 pass · 1 boot/smoke-test failure · 2 usage/environment error.
"""

from __future__ import annotations

import argparse
import glob
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

SMOKE_SCRIPT_PATH = "/usr/libexec/lindos/qa/ci-boot-smoke-test.sh"
DONE_RE = re.compile(r"^LINDOS_SMOKE_DONE rc=(\d+)")
CHECK_RE = re.compile(r"^LINDOS_CHECK (\S+)=(OK|FAIL)(?: rc=(-?\d+))?")
INFO_RE = re.compile(r"^LINDOS_INFO (\S+)=(.*)$")
FAILED_UNIT_RE = re.compile(r"^LINDOS_FAILED_UNIT (.+)$")
DOCTOR_FAIL_RE = re.compile(r"^LINDOS_DOCTOR_FAIL (.+)$")
FAIL_LOG_RE = re.compile(r"^LINDOS_FAIL_LOG (.+)$")
# ci-boot-smoke-test.sh's start_desktop_watch() prints one of these from a *detached* unit/
# process once the desktop is (or, on the watcher's own internal timeout, still isn't) up --
# see that function's docstring for why it must never run inline in the smoke-test unit itself.
DESKTOP_READY_RE = re.compile(r"^LINDOS_DESKTOP_READY$")
DESKTOP_READY_TIMEOUT_RE = re.compile(r"^LINDOS_DESKTOP_READY_TIMEOUT$")
MONITOR_PROMPT = b"(qemu) "


def log(msg: str) -> None:
    print(f"[boot-test] {msg}", flush=True)


class MonitorClient:
    """Tiny synchronous client for QEMU's human monitor protocol over a unix socket."""

    def __init__(self, sock_path: Path, *, connect_timeout: float = 30.0) -> None:
        self.sock_path = sock_path
        deadline = time.monotonic() + connect_timeout
        last_err: Optional[Exception] = None
        self.sock: Optional[socket.socket] = None
        while time.monotonic() < deadline:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(10.0)
                s.connect(str(sock_path))
                self.sock = s
                break
            except OSError as exc:  # socket not created yet
                last_err = exc
                time.sleep(0.5)
        if self.sock is None:
            raise RuntimeError(f"could not connect to QEMU monitor at {sock_path}: {last_err}")
        self._read_until_prompt()  # banner + first prompt

    def _read_until_prompt(self, timeout: float = 20.0) -> bytes:
        assert self.sock is not None
        self.sock.settimeout(timeout)
        buf = b""
        deadline = time.monotonic() + timeout
        while MONITOR_PROMPT not in buf and time.monotonic() < deadline:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
        return buf

    def command(self, cmd: str, *, timeout: float = 20.0) -> bytes:
        assert self.sock is not None
        self.sock.sendall((cmd + "\n").encode("ascii"))
        return self._read_until_prompt(timeout=timeout)

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass


def extract_casper(iso: Path, dest: Path, *, which=shutil.which, run=subprocess.run) -> tuple[Path, Path]:
    xorriso = which("xorriso")
    if not xorriso:
        raise RuntimeError("xorriso not found — apt-get install xorriso")
    dest.mkdir(parents=True, exist_ok=True)
    vmlinuz = dest / "vmlinuz"
    run([xorriso, "-osirrox", "on", "-indev", str(iso), "-extract", "/casper/vmlinuz", str(vmlinuz)],
        check=True, capture_output=True, text=True)
    initrd = dest / "initrd"
    for name in ("/casper/initrd.lz", "/casper/initrd"):
        proc = run([xorriso, "-osirrox", "on", "-indev", str(iso), "-extract", name, str(initrd)],
                  capture_output=True, text=True)
        if proc.returncode == 0 and initrd.exists():
            break
    else:
        raise RuntimeError("neither /casper/initrd.lz nor /casper/initrd found in the ISO")
    if not vmlinuz.exists() or vmlinuz.stat().st_size == 0:
        raise RuntimeError("extracted vmlinuz is missing or empty")
    if not initrd.exists() or initrd.stat().st_size == 0:
        raise RuntimeError("extracted initrd is missing or empty")
    return vmlinuz, initrd


def build_qemu_argv(*, vmlinuz: Path, initrd: Path, iso: Path, serial_log: Path, monitor_sock: Path,
                    ram_mb: int, cpus: int) -> List[str]:
    append = (
        "boot=casper username=mint hostname=mint quiet splash "
        f"console=ttyS0,115200n8 systemd.run={SMOKE_SCRIPT_PATH} --"
    )
    return [
        "qemu-system-x86_64",
        "-name", "lindos-boot-test",
        "-machine", "q35",
        "-accel", "kvm",
        "-accel", "tcg",
        "-cpu", "max",
        "-m", str(ram_mb),
        "-smp", str(cpus),
        "-no-reboot",
        "-vga", "std",
        "-display", "none",
        "-serial", f"file:{serial_log}",
        "-monitor", f"unix:{monitor_sock},server=on,wait=off",
        "-netdev", "user,id=n0", "-device", "virtio-net-pci,netdev=n0",
        "-audiodev", "none,id=snd0",
        "-cdrom", str(iso),
        "-kernel", str(vmlinuz),
        "-initrd", str(initrd),
        "-append", append,
    ]


def _tail_for(serial_log: Path, patterns: List["re.Pattern[str]"], *, timeout: float) -> Optional[str]:
    """Poll serial_log for a line matching any of *patterns*; return it, or None on timeout."""
    deadline = time.monotonic() + timeout
    seen = 0
    while time.monotonic() < deadline:
        if serial_log.exists():
            text = serial_log.read_text(encoding="utf-8", errors="replace")
            if len(text) > seen:
                for line in text[seen:].splitlines():
                    stripped = line.strip()
                    if any(p.match(stripped) for p in patterns):
                        return stripped
                seen = len(text)
        time.sleep(2.0)
    return None


def tail_for_done(serial_log: Path, *, timeout: float) -> Optional[str]:
    """Poll serial_log for LINDOS_SMOKE_DONE; return the matched line or None on timeout."""
    return _tail_for(serial_log, [DONE_RE], timeout=timeout)


def tail_for_desktop_ready(serial_log: Path, *, timeout: float) -> Optional[str]:
    """Poll serial_log for LINDOS_DESKTOP_READY or LINDOS_DESKTOP_READY_TIMEOUT (the detached
    guest-side watcher's sentinels -- see ci-boot-smoke-test.sh's start_desktop_watch()); return
    the matched line, or None if even the watcher's own timeout sentinel never showed up (e.g.
    systemd-run/setsid themselves failed, or the guest never got far enough to run it at all)."""
    return _tail_for(serial_log, [DESKTOP_READY_RE, DESKTOP_READY_TIMEOUT_RE], timeout=timeout)


def parse_report(serial_log: Path) -> dict:
    text = serial_log.read_text(encoding="utf-8", errors="replace") if serial_log.exists() else ""
    checks: dict[str, dict] = {}
    info: dict[str, str] = {}
    failed_units: List[str] = []
    doctor_fails: List[str] = []
    fail_logs: List[str] = []
    smoke_rc: Optional[int] = None
    desktop_ready_line: Optional[str] = None
    for raw in text.splitlines():
        line = raw.strip()
        m = CHECK_RE.match(line)
        if m:
            checks[m.group(1)] = {"status": m.group(2), "rc": m.group(3)}
            continue
        m = INFO_RE.match(line)
        if m:
            info[m.group(1)] = m.group(2)
            continue
        m = FAILED_UNIT_RE.match(line)
        if m:
            failed_units.append(m.group(1))
            continue
        m = DOCTOR_FAIL_RE.match(line)
        if m:
            doctor_fails.append(m.group(1))
            continue
        m = FAIL_LOG_RE.match(line)
        if m:
            fail_logs.append(m.group(1))
            continue
        m = DONE_RE.match(line)
        if m:
            smoke_rc = int(m.group(1))
            continue
        if DESKTOP_READY_RE.match(line) or DESKTOP_READY_TIMEOUT_RE.match(line):
            desktop_ready_line = line
    return {"checks": checks, "info": info, "failed_units": failed_units, "smoke_rc": smoke_rc,
            "booted": "LINDOS_SMOKE_START" in text, "doctor_fails": doctor_fails,
            "fail_logs": fail_logs, "desktop_ready_line": desktop_ready_line}


def take_screenshot(monitor_sock: Path, out_png: Path) -> bool:
    try:
        mon = MonitorClient(monitor_sock)
    except RuntimeError as exc:
        log(f"could not connect to monitor for screenshot: {exc}")
        return False
    try:
        ppm = out_png.with_suffix(".ppm")
        mon.command(f"screendump {ppm}")
        time.sleep(1.0)  # give QEMU a moment to finish writing the file
        if not ppm.exists() or ppm.stat().st_size == 0:
            log("screendump did not produce a file")
            return False
        try:
            from PIL import Image  # type: ignore[import-not-found]
            Image.open(ppm).save(out_png)
            ppm.unlink(missing_ok=True)
        except ImportError:
            log("Pillow not installed — leaving the screenshot as a raw .ppm")
            out_png = ppm
        log(f"screenshot saved: {out_png}")
        return True
    finally:
        try:
            mon.command("quit")
        except (RuntimeError, OSError):
            pass
        mon.close()


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--iso", required=True, help="path to the built ISO (glob allowed)")
    p.add_argument("--out-dir", required=True, help="where to put extracted kernel/initrd, logs, screenshot")
    p.add_argument("--timeout", type=int, default=600, help="seconds to wait for LINDOS_SMOKE_DONE")
    p.add_argument("--desktop-timeout", type=int, default=180,
                    help="seconds to wait for LINDOS_DESKTOP_READY (the detached guest-side "
                         "watcher) after the smoke test finishes, before giving up on it and "
                         "moving on to the grace period + screenshot anyway")
    p.add_argument("--grace", type=int, default=90, help="extra seconds after smoke-done before the screenshot")
    p.add_argument("--ram", type=int, default=4096, help="guest RAM in MB")
    p.add_argument("--cpus", type=int, default=2, help="guest vCPUs")
    p.add_argument("--require-kernel-suffix", default=None,
                    help="fail unless `uname -r` (LINDOS_INFO uname=...) ends with this suffix, e.g. -lindos")
    ns = p.parse_args(argv)

    matches = sorted(glob.glob(ns.iso))
    if not matches:
        print(f"boot_test: no ISO matched {ns.iso!r}", file=sys.stderr)
        return 2
    iso = Path(matches[-1]).resolve()
    out_dir = Path(ns.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    serial_log = out_dir / "serial.log"
    monitor_sock = out_dir / "monitor.sock"
    screenshot = out_dir / "desktop.png"
    if monitor_sock.exists():
        monitor_sock.unlink()

    log(f"ISO: {iso} ({iso.stat().st_size / (1 << 30):.2f} GiB)")
    try:
        vmlinuz, initrd = extract_casper(iso, out_dir / "extract")
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"boot_test: failed to extract casper kernel/initrd: {exc}", file=sys.stderr)
        return 2
    log(f"extracted kernel={vmlinuz} initrd={initrd}")

    argv_qemu = build_qemu_argv(vmlinuz=vmlinuz, initrd=initrd, iso=iso, serial_log=serial_log,
                                monitor_sock=monitor_sock, ram_mb=ns.ram, cpus=ns.cpus)
    log("qemu command: " + " ".join(argv_qemu))
    proc = subprocess.Popen(argv_qemu)
    try:
        log(f"waiting up to {ns.timeout}s for LINDOS_SMOKE_DONE on the serial console...")
        done_line = tail_for_done(serial_log, timeout=ns.timeout)
        if done_line is None:
            log("TIMEOUT waiting for the boot smoke test to finish")
        else:
            log(f"smoke test finished: {done_line}")
            log(f"waiting up to {ns.desktop_timeout}s for LINDOS_DESKTOP_READY "
                f"(the detached desktop-readiness watcher)...")
            ready_line = tail_for_desktop_ready(serial_log, timeout=ns.desktop_timeout)
            if ready_line is None:
                log("TIMEOUT waiting for LINDOS_DESKTOP_READY (or its own _TIMEOUT sentinel) — "
                    "screenshotting anyway for debugging")
            else:
                log(f"desktop watcher: {ready_line}")
            log(f"waiting {ns.grace}s for the live desktop to render before the screenshot...")
            time.sleep(ns.grace)
        took_shot = take_screenshot(monitor_sock, screenshot)
    finally:
        if proc.poll() is None:
            time.sleep(2.0)
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=15)

    report = parse_report(serial_log)
    print("\n===== boot-test report =====")
    if not report["booted"]:
        print("FAIL: the smoke test never started — the kernel/initrd did not boot far enough "
              "(see serial.log for a kernel panic or an early init failure)")
        return 1
    for name, val in sorted(report["checks"].items()):
        marker = "ok  " if val["status"] == "OK" else "FAIL"
        print(f"  [{marker}] {name}" + (f" (rc={val['rc']})" if val["status"] != "OK" else ""))
    for k, v in sorted(report["info"].items()):
        print(f"  info: {k}={v}")
    if report["failed_units"]:
        print(f"  failed systemd units ({len(report['failed_units'])}):")
        for u in report["failed_units"]:
            print(f"    - {u}")
    if report["doctor_fails"]:
        print("  lindos-compat doctor: failing required check(s):")
        for d in report["doctor_fails"]:
            print(f"    - {d}")
    if report["fail_logs"]:
        print("  tail of failing checks' own logs:")
        for line in report["fail_logs"]:
            print(f"    {line}")
    print(f"  desktop ready: {report['desktop_ready_line'] or 'never seen (timeout)'}")
    print(f"  screenshot: {'captured' if took_shot else 'NOT captured'}")

    ok = True
    if report["smoke_rc"] is None:
        print("FAIL: LINDOS_SMOKE_DONE sentinel never appeared (timeout or crash)")
        ok = False
    elif report["smoke_rc"] != 0:
        print(f"FAIL: smoke test reported rc={report['smoke_rc']}")
        ok = False
    if any(v["status"] != "OK" for v in report["checks"].values()):
        ok = False
    if ns.require_kernel_suffix:
        uname = report["info"].get("uname", "")
        if not uname.endswith(ns.require_kernel_suffix):
            print(f"FAIL: expected the booted kernel (uname -r={uname!r}) to end with "
                  f"{ns.require_kernel_suffix!r} (the Lindos-tuned kernel was requested but did not boot)")
            ok = False
    print("===== " + ("PASS" if ok else "FAIL") + " =====")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
