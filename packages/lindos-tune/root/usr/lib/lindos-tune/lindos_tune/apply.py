"""``lindos-tune apply --mode <id> [--system] [--offline] [--dry-run]`` (SPEC §11, §3, §8, §13).

Applies the **base tune** plus the **mode overrides** as an ordered list of idempotent steps;
every step is recorded as ``(name, ok, detail)`` in a :class:`~lindos_tune.common.Report`:

==  ==================  =========================================================================
 #  step                what it does
==  ==================  =========================================================================
 1  sysctl-base         ``/etc/sysctl.d/70-lindos-base.conf`` (swappiness 180 with zram else 60 …)
 2  sysctl-mode         ``/etc/sysctl.d/90-lindos-mode.conf`` from ``mode.json`` ``sysctl``
 3  modules             ``/etc/modules-load.d/lindos.conf`` (tcp_bbr) + ``modprobe`` when live
 4  sysctl-reload       ``sysctl --system`` (live only)
 5  zram                zram-generator / zram-tools config for the mode's percent (+ restart)
 6  earlyoom            ``/etc/default/earlyoom`` (``-m 4``, lite ``-m 8``) + enable
 7  journald            ``/etc/systemd/journald.conf.d/lindos.conf`` (64 M cap, persistent)
 8  tmp-tmpfs           ``tmp.mount`` enable (copied from /usr/share/systemd if needed) or fstab
 9  preset              ``90-lindos.preset`` present + ``systemctl preset`` (first apply only)
10  services            ``systemctl disable/enable`` for the mode's whitelisted unit lists
11  autostart-hide      ``NotShowIn=XFCE;`` for entries in ``autostart-hide.list``
12  governor            cpufreq governor / EPP / power-profiles-daemon (gaming → performance)
13  ananicy             ``ananicy-cpp`` on in gaming (rules in ``/etc/ananicy.d/lindos``), off else
14  fstrim              ``fstrim.timer`` enable
15  compositor          informational — the compositor is a per-user setting (lindos-compositor)
16  state               ``/var/lib/lindos-tune/state.json``
==  ==================  =========================================================================

Settings come from three layers (later wins): built-in per-mode defaults →
``/usr/share/lindos/modes/<id>/mode.json`` (``zram_percent``, ``governor``, ``compositor``,
``sysctl``, ``services_disable/enable``) → ``/etc/lindos/tune.d/<id>.conf`` (``KEY=VALUE``:
``ZRAM_PERCENT``, ``GOVERNOR``, ``EARLYOOM_MIN``, ``COMPOSITOR``, ``ANANICY``, optional
``EARLYOOM_SWAP_MIN``, ``SWAPPINESS``, ``SERVICES_DISABLE``, ``SERVICES_ENABLE``).

Chroot / ``--offline`` (ISO build, SPEC §8 ``50-tune.sh``): files are written and units are
enabled/disabled/preset (symlinks only) but nothing is ever started or restarted and no
kernel knob is written — those take effect at boot.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from . import __version__ as _VERSION
from . import common, governor as lgovernor, sched as lsched, services as lservices, zram as lzram
from .common import Context, Report

SYSCTL_KEY_RE = re.compile(r"^(vm|kernel|fs|net|abi|dev)\.[a-z0-9_.-]+$")
SYSCTL_VALUE_RE = re.compile(r"^[A-Za-z0-9 ._:+-]{1,128}$")

MODULES_CONF = "/etc/modules-load.d/lindos.conf"
TMP_MOUNT_SHARE = "/usr/share/systemd/tmp.mount"
TMP_MOUNT_ETC = "/etc/systemd/system/tmp.mount"
FSTAB_TMP_LINE = "tmpfs /tmp tmpfs defaults,nosuid,nodev,size=50% 0 0"
EARLYOOM_AVOID = "(^|/)(Xorg|xfwm4|xfce4-panel|lightdm)$"
EARLYOOM_UNIT = "earlyoom.service"
ANANICY_UNIT = "ananicy-cpp.service"
FSTRIM_UNIT = "fstrim.timer"
JOURNALD_UNIT = "systemd-journald.service"

#: kernel-memory knobs applied at runtime (SPEC-KERNEL §16); also set at boot by the grub drop-in.
THP_SYS = "/sys/kernel/mm/transparent_hugepage/enabled"
MGLRU_SYS = "/sys/kernel/mm/lru_gen/enabled"
THP_MODES = ("madvise", "always", "never")
MGLRU_VALUES = {"on": "y", "off": "n"}

#: Units whose ``Also=`` companions must be re-enabled after ``systemctl disable`` so that
#: socket/path activation keeps working (cups: keep the socket, drop the always-on daemon).
COMPANIONS = {"cups.service": ["cups.socket", "cups.path"]}

#: Built-in per-mode defaults (mirrored by /etc/lindos/tune.d/<mode>.conf).
MODE_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "everyday": {"zram_percent": 50, "governor": "schedutil", "earlyoom_min": 4, "compositor": "picom", "ananicy": False,
                 "sysctl": {}, "services_disable": [], "services_enable": [], "sched": "none", "thp": "madvise", "mglru": None},
    "gaming": {"zram_percent": 75, "governor": "performance", "earlyoom_min": 4, "compositor": "picom", "ananicy": True,
               "sysctl": {"vm.max_map_count": "2147483642"}, "services_disable": [], "services_enable": [],
               "sched": "scx_lavd", "thp": "madvise", "mglru": None},
    "work": {"zram_percent": 50, "governor": "schedutil", "earlyoom_min": 4, "compositor": "picom", "ananicy": False,
             "sysctl": {}, "services_disable": [], "services_enable": [], "sched": "none", "thp": "madvise", "mglru": None},
    "creator": {"zram_percent": 50, "governor": "schedutil", "earlyoom_min": 4, "compositor": "picom", "ananicy": False,
                "sysctl": {}, "services_disable": [], "services_enable": [], "sched": "scx_bpfland", "thp": "madvise", "mglru": None},
    "lite": {"zram_percent": 100, "governor": "schedutil", "earlyoom_min": 8, "compositor": "none", "ananicy": False,
             "sysctl": {}, "services_disable": ["bluetooth.service", "ModemManager.service", "cups-browsed.service"],
             "services_enable": [], "sched": "none", "thp": "madvise", "mglru": "on"},
}
PRESET_ENABLE = ["earlyoom.service", "fstrim.timer", "zramswap.service", "cups.socket", "cups.path", "avahi-daemon.service",
                 "lindos-sensors-detect.service", "bluetooth.service", "lindos-driver-firstboot.service",
                 "lindos-browser-firstboot.service", "lindos-ci-boot-smoke-test.service"]
PRESET_DISABLE = ["ModemManager.service", "cups.service", "cups-browsed.service",
                  "NetworkManager-wait-online.service", "apport.service", "whoopsie.service",
                  "kerneloops.service", "brltty.service", "speech-dispatcher.service",
                  "ubuntu-report.service", "motd-news.timer", "apt-daily.timer", "apt-daily-upgrade.timer"]

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


# --- settings -------------------------------------------------------------------------------------
def load_mode_json(ctx: Context, mode_id: str) -> Optional[Dict[str, Any]]:
    """``/usr/share/lindos/modes/<id>/mode.json`` (via lindos-core when importable)."""
    modes_dir = ctx.path(common.MODES_DIR)
    mode_json = os.path.join(modes_dir, mode_id, "mode.json")
    data = common.read_json(mode_json)
    if not data:
        return None
    try:
        from lindos import modes as core_modes  # type: ignore

        mode = core_modes.Mode.from_dict(data, path=os.path.dirname(mode_json))
        return mode.to_dict()
    except Exception:
        return data


def load_tune_conf(ctx: Context, mode_id: str) -> Dict[str, str]:
    """``/etc/lindos/tune.d/<mode>.conf`` as ``{KEY: value}`` (empty when absent)."""
    return common.read_kv(ctx.path(os.path.join(common.TUNE_D_DIR, mode_id + ".conf")))


def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


def _to_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return common.clamp(int(str(value).strip()), lo, hi)
    except (TypeError, ValueError):
        return default


def _split_units(value: Any) -> List[str]:
    if isinstance(value, list):
        items = [str(v) for v in value]
    else:
        items = re.split(r"[\s,;]+", str(value or ""))
    return [lservices.normalize_unit(v) for v in items if v.strip()]


def resolve_settings(mode_id: str, mode_json: Optional[Dict[str, Any]] = None,
                     tune_conf: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Merge defaults ← mode.json ← tune.d conf into one settings dict (pure function)."""
    if mode_id not in common.MODE_IDS:
        raise ValueError(f"unknown mode {mode_id!r} (choose from {', '.join(common.MODE_IDS)})")
    s: Dict[str, Any] = json.loads(json.dumps(MODE_DEFAULTS[mode_id]))
    s["mode"] = mode_id
    s["sources"] = ["defaults"]
    mj = mode_json or {}
    if mj:
        s["sources"].append("mode.json")
        if isinstance(mj.get("zram_percent"), int) and not isinstance(mj.get("zram_percent"), bool):
            s["zram_percent"] = common.clamp(int(mj["zram_percent"]), 0, 200)
        if isinstance(mj.get("governor"), str) and mj["governor"] in lgovernor.GOVERNORS:
            s["governor"] = mj["governor"]
        if isinstance(mj.get("compositor"), str) and mj["compositor"] in ("picom", "xfwm", "none"):
            s["compositor"] = mj["compositor"]
        if isinstance(mj.get("sysctl"), dict):
            merged = dict(s["sysctl"])
            for k, v in mj["sysctl"].items():
                merged[str(k)] = "1" if v is True else "0" if v is False else str(v)
            s["sysctl"] = merged
        for key in ("services_disable", "services_enable"):
            if isinstance(mj.get(key), list):
                s[key] = _split_units(mj[key]) or s[key]
        if "ananicy" in mj:
            s["ananicy"] = _to_bool(mj["ananicy"], s["ananicy"])
    tc = tune_conf or {}
    if tc:
        s["sources"].append("tune.d")
        if "ZRAM_PERCENT" in tc:
            s["zram_percent"] = _to_int(tc["ZRAM_PERCENT"], s["zram_percent"], 0, 200)
        if tc.get("GOVERNOR") in lgovernor.GOVERNORS:
            s["governor"] = tc["GOVERNOR"]
        if "EARLYOOM_MIN" in tc:
            s["earlyoom_min"] = _to_int(tc["EARLYOOM_MIN"], s["earlyoom_min"], 1, 50)
        if tc.get("COMPOSITOR") in ("picom", "xfwm", "none"):
            s["compositor"] = tc["COMPOSITOR"]
        if "ANANICY" in tc:
            s["ananicy"] = _to_bool(tc["ANANICY"], s["ananicy"])
        if "EARLYOOM_SWAP_MIN" in tc:
            s["earlyoom_swap_min"] = _to_int(tc["EARLYOOM_SWAP_MIN"], 100, 0, 100)
        if "SWAPPINESS" in tc:
            s["swappiness"] = _to_int(tc["SWAPPINESS"], 0, 0, 200) or None
        if "SERVICES_DISABLE" in tc:
            s["services_disable"] = _split_units(tc["SERVICES_DISABLE"])
        if "SERVICES_ENABLE" in tc:
            s["services_enable"] = _split_units(tc["SERVICES_ENABLE"])
        # SPEC-KERNEL §16 keys are lowercase (sched=/thp=/mglru=); accept UPPER too for symmetry.
        sched_v = tc.get("sched", tc.get("SCHED"))
        if sched_v is not None and sched_v.strip() in lsched.SETTABLE_PROFILES:
            s["sched"] = sched_v.strip()
        thp_v = tc.get("thp", tc.get("THP"))
        if thp_v is not None and thp_v.strip() in THP_MODES:
            s["thp"] = thp_v.strip()
        mglru_v = tc.get("mglru", tc.get("MGLRU"))
        if mglru_v is not None and mglru_v.strip().lower() in MGLRU_VALUES:
            s["mglru"] = mglru_v.strip().lower()
    s.setdefault("sched", "none")
    s.setdefault("thp", "madvise")
    s.setdefault("mglru", None)
    s.setdefault("earlyoom_swap_min", 100)
    s.setdefault("swappiness", None)
    # sanitise sysctl
    clean: Dict[str, str] = {}
    dropped: List[str] = []
    for k, v in s["sysctl"].items():
        if SYSCTL_KEY_RE.match(k) and SYSCTL_VALUE_RE.match(str(v).strip()):
            clean[k] = str(v).strip()
        else:
            dropped.append(k)
    s["sysctl"] = clean
    s["sysctl_dropped"] = dropped
    return s


def load_settings(ctx: Context, mode_id: str) -> Dict[str, Any]:
    return resolve_settings(mode_id, load_mode_json(ctx, mode_id), load_tune_conf(ctx, mode_id))


# --- renderers (pure) -----------------------------------------------------------------------------
def render_sysctl_base(zram_active: bool, swappiness: Optional[int] = None) -> str:
    sw = swappiness if swappiness else (180 if zram_active else 60)
    lines = [
        "# /etc/sysctl.d/70-lindos-base.conf — managed by lindos-tune (lindos-tune apply --mode <id>)",
        "# Lindos base tune (SPEC §11). Re-applied on every mode switch; edit /etc/lindos/tune.d/<mode>.conf instead.",
        "",
        "# --- memory ---",
        ("# zram-backed swap: high swappiness keeps file cache warm and pushes idle anon pages to compressed RAM"
         if zram_active else "# no zram back-end detected: conservative swappiness"),
        f"vm.swappiness = {sw}",
        "vm.page-cluster = 0",
        "vm.vfs_cache_pressure = 50",
        "vm.dirty_ratio = 10",
        "vm.dirty_background_ratio = 5",
    ]
    if zram_active:
        lines += ["vm.watermark_boost_factor = 0", "vm.watermark_scale_factor = 125"]
    lines += [
        "",
        "# --- misc ---",
        "kernel.nmi_watchdog = 0",
        "fs.inotify.max_user_watches = 524288",
        "",
        "# --- network (needs the tcp_bbr module: /etc/modules-load.d/lindos.conf) ---",
        "net.core.default_qdisc = fq",
        "net.ipv4.tcp_congestion_control = bbr",
        "",
    ]
    return "\n".join(lines)


def render_sysctl_mode(mode_id: str, sysctl: Dict[str, str]) -> str:
    lines = [f"# /etc/sysctl.d/90-lindos-mode.conf — managed by lindos-tune (mode: {mode_id})",
             "# Values come from /usr/share/lindos/modes/<id>/mode.json \"sysctl\"."]
    if not sysctl:
        lines.append("# (no mode-specific sysctl overrides)")
    for key in sorted(sysctl):
        lines.append(f"{key} = {sysctl[key]}")
    lines.append("")
    return "\n".join(lines)


def render_earlyoom(min_percent: int, swap_min: int = 100, extra: str = "") -> str:
    args = f"-m {int(min_percent)} -s {int(swap_min)} --avoid '{EARLYOOM_AVOID}'"
    if extra:
        args += " " + extra.strip()
    return (
        "# /etc/default/earlyoom — managed by lindos-tune (lindos-tune apply)\n"
        "# -m: act below N % free RAM (Lite mode: 8), -s: swap threshold, --avoid: never kill the desktop\n"
        f'EARLYOOM_ARGS="{args}"\n'
    )


def render_journald() -> str:
    return (
        "# /etc/systemd/journald.conf.d/lindos.conf — managed by lindos-tune\n"
        "[Journal]\n"
        "Storage=persistent\n"
        "SystemMaxUse=64M\n"
        "SystemMaxFileSize=8M\n"
        "RuntimeMaxUse=32M\n"
        "Compress=yes\n"
    )


def render_modules() -> str:
    return "# /etc/modules-load.d/lindos.conf — managed by lindos-tune\n# BBR congestion control (net.ipv4.tcp_congestion_control = bbr)\ntcp_bbr\n"


def render_preset() -> str:
    lines = ["# /usr/lib/systemd/system-preset/90-lindos.preset — Lindos idle-RAM debloat (SPEC §8, §11).",
             "# Applied by `systemctl preset` (lindos-tune apply, first run) and for freshly installed units.",
             "# Not listed → distribution default. avahi-daemon stays enabled (printer discovery),",
             "# cups is socket/path activated instead of always-on."]
    lines += [f"enable {u}" for u in PRESET_ENABLE]
    lines += [f"disable {u}" for u in PRESET_DISABLE]
    return "\n".join(lines) + "\n"


def parse_preset(text: Optional[str]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("enable", "disable") and lservices.valid_unit_name(parts[1]):
            out.append((parts[0], parts[1]))
    return out


def earlyoom_args_from(text: Optional[str]) -> str:
    return common.parse_kv(text).get("EARLYOOM_ARGS", "")


# --- steps ----------------------------------------------------------------------------------------
def _verb(ctx: Context, changed: bool) -> str:
    return "would write" if ctx.dry_run else ("wrote" if changed else "unchanged")


def step_sysctl(ctx: Context, s: Dict[str, Any], report: Report) -> None:
    zram_on = lzram.planned(ctx, s["zram_percent"])
    ok, changed = ctx.write(common.SYSCTL_BASE_CONF, render_sysctl_base(zram_on, s.get("swappiness")))
    report.add("sysctl-base", ok, f"{_verb(ctx, changed)} {common.SYSCTL_BASE_CONF} "
               f"(swappiness {s.get('swappiness') or (180 if zram_on else 60)}, zram {'planned' if zram_on else 'absent'})")
    ok, changed = ctx.write(common.SYSCTL_MODE_CONF, render_sysctl_mode(s["mode"], s["sysctl"]))
    detail = f"{_verb(ctx, changed)} {common.SYSCTL_MODE_CONF} ({len(s['sysctl'])} key(s)"
    detail += (", dropped invalid: " + ", ".join(s["sysctl_dropped"]) if s.get("sysctl_dropped") else "") + ")"
    report.add("sysctl-mode", ok, detail)
    ok, changed = ctx.write(MODULES_CONF, render_modules())
    mod_detail = f"{_verb(ctx, changed)} {MODULES_CONF} (tcp_bbr)"
    if ctx.live and ctx.which("modprobe"):
        res = ctx.run(["modprobe", "tcp_bbr"], timeout=30)
        mod_detail += "; modprobe tcp_bbr " + ("ok" if res.ok else f"failed: {res.tail()}")
    report.add("modules", ok, mod_detail)
    if ctx.dry_run:
        report.add("sysctl-reload", True, "would run: sysctl --system")
    elif ctx.live and ctx.which("sysctl"):
        res = ctx.run(["sysctl", "--system"], timeout=60)
        report.add("sysctl-reload", res.ok, "sysctl --system ok" if res.ok else res.tail())
    else:
        report.skip("sysctl-reload", "offline/chroot: applied at boot by systemd-sysctl")


def step_zram(ctx: Context, s: Dict[str, Any], report: Report) -> None:
    lzram.configure(ctx, s["zram_percent"], report)


def step_earlyoom(ctx: Context, s: Dict[str, Any], report: Report) -> None:
    content = render_earlyoom(s["earlyoom_min"], s.get("earlyoom_swap_min", 100))
    ok, changed = ctx.write(common.EARLYOOM_DEFAULT, content)
    report.add("earlyoom-config", ok, f"{_verb(ctx, changed)} {common.EARLYOOM_DEFAULT} (-m {s['earlyoom_min']} -s {s.get('earlyoom_swap_min', 100)})")
    if not ctx.unit_exists(EARLYOOM_UNIT) and not ctx.dry_run:
        report.skip("earlyoom", "earlyoom not installed (apt install earlyoom)")
        return
    if not ctx.can_systemctl:
        report.skip("earlyoom", "systemctl not available")
        return
    res = ctx.systemctl("enable", EARLYOOM_UNIT)
    if not res.ok and not ctx.dry_run:
        report.add("earlyoom", False, res.tail())
        return
    if ctx.live:
        res = ctx.systemctl("restart" if changed else "start", EARLYOOM_UNIT, timeout=60)
        report.add("earlyoom", res.ok, ("enabled + " + ("restarted" if changed else "started")) if res.ok else res.tail())
    else:
        report.add("earlyoom", True, "would enable" if ctx.dry_run else "enabled (starts at boot)")


def step_journald(ctx: Context, s: Dict[str, Any], report: Report) -> None:
    ok, changed = ctx.write(common.JOURNALD_DROPIN, render_journald())
    detail = f"{_verb(ctx, changed)} {common.JOURNALD_DROPIN} (SystemMaxUse=64M, persistent)"
    if ok and changed and ctx.live and ctx.can_systemctl:
        res = ctx.systemctl("try-restart", JOURNALD_UNIT, timeout=60)
        detail += "; journald " + ("restarted" if res.ok else f"restart failed: {res.tail()}")
    report.add("journald", ok, detail)


def step_tmp(ctx: Context, s: Dict[str, Any], report: Report) -> None:
    unit_present = ctx.unit_exists("tmp.mount")
    if not unit_present and ctx.exists(TMP_MOUNT_SHARE):
        text = common.read_text(ctx.path(TMP_MOUNT_SHARE)) or ""
        ok, _changed = ctx.write(TMP_MOUNT_ETC, text)
        if ok:
            unit_present = True
            report.add("tmp-mount-unit", True, f"{'would copy' if ctx.dry_run else 'copied'} {TMP_MOUNT_SHARE} → {TMP_MOUNT_ETC}")
        else:
            report.add("tmp-mount-unit", False, f"cannot copy {TMP_MOUNT_SHARE} → {TMP_MOUNT_ETC}")
    if unit_present:
        if not ctx.can_systemctl:
            report.skip("tmp-tmpfs", "systemctl not available")
            return
        res = ctx.systemctl("enable", "tmp.mount")
        report.add("tmp-tmpfs", res.ok, ("would enable tmp.mount" if ctx.dry_run else "tmp.mount enabled") + " (tmpfs /tmp from next boot)" if res.ok else res.tail())
        return
    fstab = common.read_text(ctx.path(common.FSTAB)) or ""
    if re.search(r"^\s*tmpfs\s+/tmp\s+tmpfs", fstab, re.M):
        report.add("tmp-tmpfs", True, "/etc/fstab already mounts /tmp as tmpfs")
        return
    new = fstab.rstrip("\n") + ("\n" if fstab.strip() else "") + "# lindos-tune: /tmp in RAM (tmp.mount unit unavailable)\n" + FSTAB_TMP_LINE + "\n"
    ok, _changed = ctx.write(common.FSTAB, new)
    report.add("tmp-tmpfs", ok, f"{'would append' if ctx.dry_run else 'appended'} tmpfs /tmp line to /etc/fstab" if ok else "cannot write /etc/fstab")


def step_preset(ctx: Context, s: Dict[str, Any], report: Report, state: Dict[str, Any]) -> None:
    text = common.read_text(ctx.path(common.PRESET_FILE))
    if text is None:
        ok, _ = ctx.write(common.PRESET_FILE, render_preset())
        report.add("preset-file", ok, f"{'would write' if ctx.dry_run else 'wrote'} {common.PRESET_FILE} (was missing)")
        text = render_preset()
    else:
        report.add("preset-file", True, f"{common.PRESET_FILE} present")
    if state.get("preset_applied") and not ctx.dry_run:
        report.add("preset", True, "already applied earlier (systemctl preset runs once; use lindos-tune services to change units)")
        return
    if not ctx.can_systemctl:
        report.skip("preset", "systemctl not available")
        return
    entries = parse_preset(text)
    done: List[str] = []
    failed: List[str] = []
    for _verb_, unit in entries:
        if not ctx.unit_exists(unit) and not ctx.dry_run:
            continue
        res = ctx.systemctl("preset", unit)
        (done if res.ok else failed).append(unit)
    if failed:
        report.add("preset", False, "preset failed for: " + ", ".join(failed))
    else:
        report.add("preset", True, ("would preset " if ctx.dry_run else "preset applied to ") + (", ".join(done) or "no installed units"))
        if not ctx.dry_run:
            state["preset_applied"] = True


def step_services(ctx: Context, s: Dict[str, Any], report: Report) -> None:
    wl = lservices.load_whitelist(ctx)
    disable_units = s.get("services_disable", [])
    enable_units = s.get("services_enable", [])
    if not disable_units and not enable_units:
        report.add("services", True, "no unit changes requested by this mode")
        return
    lservices.apply_lists(ctx, report, disable_units=disable_units, enable_units=enable_units, whitelist=wl)
    for unit in disable_units:
        for companion in COMPANIONS.get(lservices.normalize_unit(unit), []):
            if ctx.unit_exists(companion) or ctx.dry_run:
                res = ctx.systemctl("enable", companion)
                report.add(f"enable:{companion}", res.ok, "kept for socket/path activation" if res.ok else res.tail())


def step_autostart(ctx: Context, s: Dict[str, Any], report: Report) -> None:
    disabled = set(lservices.normalize_unit(u) for u in s.get("services_disable", []))
    # units already disabled on the system also count (e.g. bluetooth disabled by Lite mode's
    # own tune.d/mode.json services_disable — bluetooth stays ENABLED by the preset otherwise)
    for unit in ("bluetooth.service",):
        if unit in disabled or not ctx.can_systemctl or (ctx.root and not ctx.commands_allowed):
            continue
        res = ctx.systemctl("is-enabled", unit, timeout=15)
        first = res.out.strip().splitlines()[0].strip() if res.out.strip() else ""
        if first in ("disabled", "masked") or (not ctx.dry_run and not ctx.unit_exists(unit)):
            disabled.add(unit)
    lservices.hide_autostart(ctx, report, disabled_units=disabled)


def step_governor(ctx: Context, s: Dict[str, Any], report: Report) -> None:
    lgovernor.apply(ctx, s["governor"], report, persist=True, prefer_ppd=True)


def step_ananicy(ctx: Context, s: Dict[str, Any], report: Report) -> None:
    want = bool(s.get("ananicy"))
    rules_present = os.path.isdir(ctx.path(common.ANANICY_RULES_DIR))
    if not ctx.unit_exists(ANANICY_UNIT):
        report.skip("ananicy", ("wanted on for this mode but ananicy-cpp is not installed (optional Recommends)" if want
                                else "ananicy-cpp not installed (not needed for this mode)"))
        return
    if not ctx.can_systemctl:
        report.skip("ananicy", "systemctl not available")
        return
    if want:
        args = ["enable"] + (["--now"] if ctx.live else [])
        res = ctx.systemctl(*args, ANANICY_UNIT, timeout=60)
        report.add("ananicy", res.ok, ("would enable" if ctx.dry_run else "enabled") + (" --now" if ctx.live else "") +
                   f" (rules {common.ANANICY_RULES_DIR}{'' if rules_present else ' MISSING'})" if res.ok else res.tail())
    else:
        args = ["disable"] + (["--now"] if ctx.live else [])
        res = ctx.systemctl(*args, ANANICY_UNIT, timeout=60)
        report.add("ananicy", res.ok, ("would disable" if ctx.dry_run else "disabled") + " (only gaming mode enables it)" if res.ok else res.tail())


def step_fstrim(ctx: Context, s: Dict[str, Any], report: Report) -> None:
    if not ctx.unit_exists(FSTRIM_UNIT) and not ctx.dry_run:
        report.skip("fstrim", "fstrim.timer not present (util-linux)")
        return
    if not ctx.can_systemctl:
        report.skip("fstrim", "systemctl not available")
        return
    args = ["enable"] + (["--now"] if ctx.live else [])
    res = ctx.systemctl(*args, FSTRIM_UNIT)
    report.add("fstrim", res.ok, ("would enable" if ctx.dry_run else "enabled") + " fstrim.timer (weekly TRIM)" if res.ok else res.tail())


def step_compositor(ctx: Context, s: Dict[str, Any], report: Report) -> None:
    report.add("compositor", True, f"{s['compositor']} — per-user setting applied by lindos-mode / lindos-compositor in the XFCE session (not a system tune)")


def _write_sysfs(ctx: Context, system_path: str, value: str) -> Tuple[bool, str]:
    """Write *value* to a sysfs knob (no atomic replace — sysfs files can't be renamed onto)."""
    target = ctx.path(system_path)
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
    except OSError:
        pass
    try:
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(value + "\n")
        return True, ""
    except OSError as exc:
        return False, str(exc)


def _apply_kernel_knob(ctx: Context, report: Report, name: str, system_path: str, value: str, boot_note: str) -> None:
    """Set a ``/sys/kernel/mm`` knob at runtime; skip (applied at boot) when offline/chroot."""
    if ctx.dry_run:
        report.add(name, True, f"would set {system_path} = {value}")
        return
    if not (ctx.live or ctx.root):
        report.skip(name, boot_note)
        return
    if ctx.live and not ctx.exists(system_path):
        report.skip(name, f"{system_path} not exposed by this kernel")
        return
    ok, err = _write_sysfs(ctx, system_path, value)
    report.add(name, ok, f"set {system_path} = {value}" if ok else f"cannot set {name}: {err}")


def step_thp(ctx: Context, s: Dict[str, Any], report: Report) -> None:
    thp = s.get("thp")
    if thp not in THP_MODES:
        report.skip("thp", "no transparent-hugepage mode for this mode")
        return
    _apply_kernel_knob(ctx, report, "thp", THP_SYS, thp,
                       f"offline/chroot: THP set at boot via kernel cmdline (transparent_hugepage={thp})")


def step_mglru(ctx: Context, s: Dict[str, Any], report: Report) -> None:
    mglru = s.get("mglru")
    if mglru not in MGLRU_VALUES:
        report.skip("mglru", "no MGLRU override for this mode (kernel default kept)")
        return
    _apply_kernel_knob(ctx, report, "mglru", MGLRU_SYS, MGLRU_VALUES[mglru],
                       f"offline/chroot: MGLRU ({mglru}) is a boot-time default on the Lindos kernel")


def step_sched(ctx: Context, s: Dict[str, Any], report: Report) -> None:
    profile = s.get("sched") or "none"
    lsched.set_scheduler(ctx, profile, report)


def step_state(ctx: Context, s: Dict[str, Any], report: Report, state: Dict[str, Any]) -> None:
    state.update({
        "mode": s["mode"],
        "applied_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
        "zram_percent": s["zram_percent"],
        "zram_backend": lzram.detect_backend(ctx),
        "governor": s["governor"],
        "earlyoom_min": s["earlyoom_min"],
        "ananicy": bool(s.get("ananicy")),
        "compositor": s["compositor"],
        "sched": s.get("sched", "none"),
        "thp": s.get("thp"),
        "mglru": s.get("mglru"),
        "offline": bool(ctx.offline or ctx.chroot),
        "sources": s.get("sources", []),
        "version": _VERSION,
    })
    if ctx.dry_run:
        report.add("state", True, f"would write {common.STATE_FILE}")
        return
    ok = common.write_json(ctx.path(common.STATE_FILE), state)
    report.add("state", ok, f"{'wrote' if ok else 'cannot write'} {common.STATE_FILE}")


# --- entry point ----------------------------------------------------------------------------------
def run(mode_id: str, *, ctx: Optional[Context] = None, system: bool = True, offline: bool = False,
        dry_run: bool = False) -> Report:
    """Apply base tune + *mode_id* overrides.  Returns a :class:`Report` (never raises for
    environment problems; ``ValueError`` only for an unknown mode id)."""
    ctx = ctx or Context(dry_run=dry_run, offline=offline)
    if dry_run:
        ctx.dry_run = True
    if offline:
        ctx.offline = True
    report = Report(title=f"lindos-tune apply --mode {mode_id}" + (" --system" if system else "") + (" --offline" if ctx.offline else ""),
                    dry_run=ctx.dry_run)
    try:
        s = load_settings(ctx, mode_id)
    except ValueError as exc:
        report.add("mode", False, str(exc))
        return report
    report.extra["mode"] = mode_id
    report.extra["settings"] = {k: v for k, v in s.items() if k not in ("sources",)}
    env = "chroot/offline" if (ctx.chroot or ctx.offline) else "live"
    report.add("mode", True, f"{mode_id}: zram {s['zram_percent']}%, governor {s['governor']}, earlyoom -m {s['earlyoom_min']}, "
               f"ananicy {'on' if s['ananicy'] else 'off'}, compositor {s['compositor']} [{' + '.join(s['sources'])}; {env}]")
    state = common.read_json(ctx.path(common.STATE_FILE))
    steps = [
        ("sysctl", lambda: step_sysctl(ctx, s, report)),
        ("zram", lambda: step_zram(ctx, s, report)),
        ("earlyoom", lambda: step_earlyoom(ctx, s, report)),
        ("journald", lambda: step_journald(ctx, s, report)),
        ("tmp", lambda: step_tmp(ctx, s, report)),
        ("preset", lambda: step_preset(ctx, s, report, state)),
        ("services", lambda: step_services(ctx, s, report)),
        ("autostart", lambda: step_autostart(ctx, s, report)),
        ("governor", lambda: step_governor(ctx, s, report)),
        ("ananicy", lambda: step_ananicy(ctx, s, report)),
        ("fstrim", lambda: step_fstrim(ctx, s, report)),
        ("compositor", lambda: step_compositor(ctx, s, report)),
        ("thp", lambda: step_thp(ctx, s, report)),
        ("mglru", lambda: step_mglru(ctx, s, report)),
        ("sched", lambda: step_sched(ctx, s, report)),
        ("state", lambda: step_state(ctx, s, report, state)),
    ]
    for name, fn in steps:
        try:
            fn()
        except Exception as exc:  # a step must never abort the run
            common.log.exception("step %s crashed", name)
            report.add(name, False, f"error: {exc}")
    return report


__all__ = [
    "MODE_DEFAULTS", "PRESET_ENABLE", "PRESET_DISABLE", "COMPANIONS",
    "MODULES_CONF", "FSTAB_TMP_LINE", "EARLYOOM_AVOID", "THP_SYS", "MGLRU_SYS", "THP_MODES", "MGLRU_VALUES",
    "load_mode_json", "load_tune_conf", "resolve_settings", "load_settings", "render_sysctl_base",
    "render_sysctl_mode", "render_earlyoom", "render_journald", "render_modules", "render_preset",
    "parse_preset", "earlyoom_args_from", "step_thp", "step_mglru", "step_sched", "run",
]
