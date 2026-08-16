"""``lindos-compat doctor`` -- is this PC ready to run Windows programs?

Every check is a small probe with an injectable ``which``/``run``/``dpkg`` so the
logic is testable without Wine.  Output: one ✓/✗ line per check with the command
that fixes it, and a summary.  ``--json`` gives the same data structured.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import CoreMissing, core, get_logger, user_home
from .prefix import find_wine, prefixes_dir, wine_version
from .runner import BOTTLES_APP_ID, find_proton, is_bottles_installed

__all__ = ["Check", "DoctorReport", "run_doctor", "format_report", "dpkg_installed", "INSTALL_CMD"]

log = get_logger("lindos-compat.doctor")

INSTALL_CMD = "pkexec /usr/libexec/lindos/install-compat.sh   (or: Lindos Settings > Windows apps > Install)"
LEVELS = ("required", "recommended", "optional")


@dataclass
class Check:
    id: str
    label: str
    ok: bool
    level: str = "required"  # required | recommended | optional
    detail: str = ""
    fix: str = ""

    def as_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class DoctorReport:
    checks: List[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if c.level == "required")

    def summary(self) -> Dict[str, object]:
        out: Dict[str, object] = {"ok": self.ok}
        for level in LEVELS:
            subset = [c for c in self.checks if c.level == level]
            out[level] = {"total": len(subset), "passed": sum(1 for c in subset if c.ok)}
        return out

    def as_dict(self) -> Dict[str, object]:
        return {"ok": self.ok, "summary": self.summary(), "checks": [c.as_dict() for c in self.checks]}


# ---------------------------------------------------------------------------
# probes
# ---------------------------------------------------------------------------


def dpkg_installed(package: str, *, which: Callable[[str], Optional[str]] = shutil.which,
                   run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run) -> bool:
    """True when ``dpkg-query`` reports the package as installed (False when dpkg is absent)."""
    dq = which("dpkg-query")
    if not dq:
        return False
    try:
        proc = run([dq, "-W", "-f=${db:Status-Status}", package], capture_output=True, text=True,
                   timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and (proc.stdout or "").strip() == "installed"


def _tool_version(argv: List[str], run: Callable[..., "subprocess.CompletedProcess[str]"]) -> str:
    try:
        proc = run(argv, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    text = (proc.stdout or proc.stderr or "").strip()
    return text.splitlines()[0][:80] if text else ""


def run_doctor(
    *,
    which: Callable[[str], Optional[str]] = shutil.which,
    run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
    dpkg: Optional[Callable[[str], bool]] = None,
    env: Optional[Dict[str, str]] = None,
    home: Optional[Path] = None,
    isdir: Callable[[str], bool] = os.path.isdir,
    isfile: Callable[[str], bool] = os.path.isfile,
    exists: Callable[[str], bool] = os.path.exists,
) -> DoctorReport:
    env = dict(os.environ) if env is None else env
    home = home or user_home()
    root_prefix = (env.get("LINDOS_ROOT") or "").rstrip("/\\")
    if dpkg is None:
        dpkg = lambda pkg: dpkg_installed(pkg, which=which, run=run)  # noqa: E731
    rep = DoctorReport()

    # 1. lindos-core
    try:
        core()
        rep.checks.append(Check("core", "Lindos core module (lindos-core)", True, "required", "importable"))
    except CoreMissing:
        rep.checks.append(Check("core", "Lindos core module (lindos-core)", False, "required",
                                "python module 'lindos' not found", "apt install lindos-core"))

    # 2. wine
    wine = find_wine(which)
    if wine:
        ver = wine_version(run=run, which=which) or "version unknown"
        rep.checks.append(Check("wine", "Wine (runs Windows programs)", True, "required", f"{ver} at {wine}"))
    else:
        rep.checks.append(Check("wine", "Wine (runs Windows programs)", False, "required", "wine not found",
                                INSTALL_CMD))

    # 3. 32-bit wine
    wine32 = any(dpkg(p) for p in ("wine-staging-i386:i386", "wine-stable-i386:i386", "wine-devel-i386:i386",
                                   "wine32:i386", "wine32")) or isdir("/opt/wine-staging/lib/wine/i386-windows") \
        or isdir("/usr/lib/wine/i386-windows") or isdir("/usr/lib/i386-linux-gnu/wine")
    rep.checks.append(Check("wine32", "32-bit Wine (many installers are 32-bit)", bool(wine32), "recommended",
                            "present" if wine32 else "no 32-bit Wine libraries",
                            "" if wine32 else "dpkg --add-architecture i386 && " + INSTALL_CMD))

    # 4. winetricks
    wt = which("winetricks")
    rep.checks.append(Check("winetricks", "winetricks (installs runtimes/fonts into a C:\\ drive)", bool(wt),
                            "recommended", wt or "not found", "" if wt else "apt install winetricks"))

    # 5. umu-run
    umu = which("umu-run")
    rep.checks.append(Check("umu", "umu-launcher (Proton for games)", bool(umu), "recommended", umu or "not found",
                            "" if umu else "lindos-compat install-umu"))

    # 6. Proton-GE
    proton = find_proton(home)
    rep.checks.append(Check("proton-ge", "Proton-GE build (used by umu-run)", proton is not None, "optional",
                            str(proton) if proton else "none installed (umu-run downloads one on first use)",
                            "" if proton else "lindos-proton update"))

    # 7. vulkan tools
    vk = which("vulkaninfo") or which("vkcube")
    rep.checks.append(Check("vulkan-tools", "Vulkan tools (vulkaninfo/vkcube)", bool(vk), "recommended",
                            vk or "not found", "" if vk else "apt install vulkan-tools"))

    # 8. 64-bit vulkan driver
    icds = glob.glob("/usr/share/vulkan/icd.d/*.json") + glob.glob("/etc/vulkan/icd.d/*.json")
    vk64 = bool(icds) or (dpkg("libvulkan1") and dpkg("mesa-vulkan-drivers"))
    rep.checks.append(Check("vulkan64", "Vulkan driver (64-bit, needed for DXVK/games)", vk64, "recommended",
                            f"{len(icds)} ICD file(s)" if icds else ("via dpkg" if vk64 else "no Vulkan ICD found"),
                            "" if vk64 else "apt install libvulkan1 mesa-vulkan-drivers  (NVIDIA: lindos-drivers install)"))

    # 9. 32-bit vulkan libs
    vk32 = dpkg("libvulkan1:i386") and dpkg("mesa-vulkan-drivers:i386")
    rep.checks.append(Check("vulkan32", "32-bit Vulkan libraries (32-bit games)", bool(vk32), "recommended",
                            "libvulkan1:i386 + mesa-vulkan-drivers:i386" if vk32 else "missing",
                            "" if vk32 else "dpkg --add-architecture i386 && apt install libvulkan1:i386 mesa-vulkan-drivers:i386"))

    # 10. fonts
    lib_fonts = dpkg("fonts-liberation") or isfile("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf")
    rep.checks.append(Check("fonts", "Fonts (fonts-liberation, metric-compatible with Arial/Times)", bool(lib_fonts),
                            "recommended", "installed" if lib_fonts else "missing",
                            "" if lib_fonts else "apt install fonts-liberation"))
    ms_fonts = dpkg("ttf-mscorefonts-installer") or bool(glob.glob("/usr/share/fonts/truetype/msttcorefonts/*.ttf")) \
        or bool(glob.glob(str(home / ".local/share/fonts/*rial*.ttf")))
    rep.checks.append(Check("corefonts", "Microsoft core fonts (Arial, Times...) for Office/Adobe", bool(ms_fonts),
                            "optional", "installed" if ms_fonts else "not installed (EULA must be accepted)",
                            "" if ms_fonts else "apt install ttf-mscorefonts-installer   (per C:\\ drive: winetricks corefonts)"))

    # 11. helpers for installers
    cab = which("cabextract")
    rep.checks.append(Check("cabextract", "cabextract (unpacks Windows .cab files, used by winetricks)", bool(cab),
                            "recommended", cab or "not found", "" if cab else "apt install cabextract"))
    winbind = dpkg("winbind") or which("winbindd") or which("ntlm_auth")
    rep.checks.append(Check("winbind", "winbind (NTLM auth used by some installers/logins)", bool(winbind),
                            "recommended", "installed" if winbind else "missing", "" if winbind else "apt install winbind"))

    # 12. game mode / mangohud
    gm = which("gamemoderun")
    rep.checks.append(Check("gamemode", "Feral GameMode (CPU governor/priority boost for games)", bool(gm), "optional",
                            gm or "not found", "" if gm else "apt install gamemode"))
    mh = which("mangohud")
    rep.checks.append(Check("mangohud", "MangoHud (FPS overlay)", bool(mh), "optional", mh or "not found",
                            "" if mh else "apt install mangohud"))

    # 13. flatpak & bottles
    fp = which("flatpak")
    bottles = is_bottles_installed(which=which, run=run, home=home) if fp else False
    rep.checks.append(Check("bottles", f"Bottles (Flatpak {BOTTLES_APP_ID}) - optional creator/gaming manager", bool(bottles),
                            "optional", "installed" if bottles else ("flatpak present, Bottles not installed" if fp else "flatpak not installed"),
                            "" if bottles else "lindos-compat install-bottles"))

    # 14. icons + dialogs
    ico = which("wrestool") and which("icotool")
    rep.checks.append(Check("icoutils", "icoutils (icons for Start Menu entries)", bool(ico), "optional",
                            "installed" if ico else "not found", "" if ico else "apt install icoutils"))
    zen = which("zenity") or which("yad")
    rep.checks.append(Check("zenity", "zenity/yad (progress & error dialogs from the file manager)", bool(zen),
                            "optional", zen or "not found", "" if zen else "apt install zenity"))

    # 15. prefixes dir writable
    pdir = prefixes_dir()
    writable = False
    detail = str(pdir)
    try:
        pdir.mkdir(parents=True, exist_ok=True)
        writable = os.access(pdir, os.W_OK)
    except OSError as exc:
        detail = f"{pdir}: {exc}"
    rep.checks.append(Check("prefixes-dir", "C:\\ drives folder writable", writable, "required", detail,
                            "" if writable else f"mkdir -p '{pdir}' && chmod u+rwx '{pdir}'"))

    # 16. display
    disp = bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))
    rep.checks.append(Check("display", "Graphical session (DISPLAY)", disp, "required",
                            env.get("DISPLAY") or env.get("WAYLAND_DISPLAY") or "no DISPLAY - Windows programs need a desktop",
                            "" if disp else "run this from the desktop session (or set DISPLAY)"))

    # 17. vm.max_map_count (some games need a large value)
    try:
        with open("/proc/sys/vm/max_map_count", "r", encoding="ascii") as fh:
            mmc = int(fh.read().strip() or 0)
    except (OSError, ValueError):
        mmc = -1
    if mmc >= 0:
        good = mmc >= 1048576
        rep.checks.append(Check("max-map-count", "vm.max_map_count (large value avoids crashes in some games)", good,
                                "optional", str(mmc), "" if good else "lindos-tune apply --mode gaming --system"))

    # 18. ntsync (Lindos kernel) - faster Wine/Proton sync primitives (SPEC-KERNEL §15/§17)
    ntsync = exists(root_prefix + "/dev/ntsync")
    rep.checks.append(Check("ntsync", "ntsync kernel device (/dev/ntsync; PROTON_USE_NTSYNC)", bool(ntsync), "optional",
                            "present" if ntsync else "not present (Proton falls back to fsync/esync)",
                            "" if ntsync else "install the Lindos kernel: apt install lindos-kernel  (adds /dev/ntsync)"))

    # 19. sched_ext (pluggable BPF schedulers, low-latency gaming)
    schedext = isdir(root_prefix + "/sys/kernel/sched_ext")
    rep.checks.append(Check("sched-ext", "sched_ext (pluggable low-latency schedulers, e.g. scx_lavd)", bool(schedext),
                            "optional", "available" if schedext else "not available in the running kernel",
                            "" if schedext else "install the Lindos kernel + schedulers: apt install lindos-kernel scx-scheds"))

    # 20. gamescope (micro-compositor: resolution/HDR/FSR)
    gs = which("gamescope") or which("lindos-gamescope")
    rep.checks.append(Check("gamescope", "gamescope (game micro-compositor: --gamescope/--hdr/--fsr)", bool(gs),
                            "optional", gs or "not found", "" if gs else "apt install gamescope  (or install lindos-gaming)"))

    # 21. DXVK / VKD3D-Proton (Direct3D -> Vulkan).  Proton (umu) ships both; plain Wine
    #     prefixes get them per-prefix via lindos-compat install-dxvk / install-vkd3d.
    proton_dxvk = bool(umu) or proton is not None
    rep.checks.append(Check("dxvk", "DXVK (Direct3D 9/10/11 -> Vulkan)", proton_dxvk, "optional",
                            "built into Proton (umu)" if proton_dxvk else "no Proton; add per C:\\ drive with install-dxvk",
                            "" if proton_dxvk else "lindos-compat install-umu   (or per prefix: lindos-compat install-dxvk <slug>)"))
    rep.checks.append(Check("vkd3d", "VKD3D-Proton (Direct3D 12 -> Vulkan)", proton_dxvk, "optional",
                            "built into Proton (umu)" if proton_dxvk else "no Proton; add per C:\\ drive with install-vkd3d",
                            "" if proton_dxvk else "lindos-compat install-umu   (or per prefix: lindos-compat install-vkd3d <slug>)"))
    return rep


# ---------------------------------------------------------------------------
# presentation
# ---------------------------------------------------------------------------


def format_report(rep: DoctorReport, *, use_unicode: bool = True) -> str:
    ok_mark = "\u2713" if use_unicode else "OK "
    bad_mark = "\u2717" if use_unicode else "X  "
    lines = ["Lindos - Windows program support check", ""]
    width = max((len(c.label) for c in rep.checks), default=20)
    for c in rep.checks:
        mark = ok_mark if c.ok else bad_mark
        tag = "" if c.level == "required" else f" ({c.level})"
        line = f" {mark} {c.label.ljust(width)}  {c.detail}{tag}"
        lines.append(line)
        if not c.ok and c.fix:
            lines.append(f"      fix: {c.fix}")
    s = rep.summary()
    req = s["required"]
    rec = s["recommended"]
    opt = s["optional"]
    lines.append("")
    lines.append(
        f"Summary: required {req['passed']}/{req['total']}, recommended {rec['passed']}/{rec['total']}, "  # type: ignore[index]
        f"optional {opt['passed']}/{opt['total']}."  # type: ignore[index]
    )
    if rep.ok:
        lines.append("Windows programs can run on this PC" + (
            "." if rec["passed"] == rec["total"] else " - install the recommended items for best results."))  # type: ignore[index]
    else:
        lines.append("Windows program support is NOT ready - fix the required items above.")
    return "\n".join(lines)
