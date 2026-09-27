"""``lindos-tune apply`` — dry-run per mode, file contents for both zram back-ends, earlyoom
args, preset content, offline/chroot safety, tune.d overrides, autostart hiding, idempotency."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import tune_testlib as tl
from lindos_tune import apply, common, zram
from lindos_tune.common import CmdResult

MODES = list(common.MODE_IDS)


def _steps(report):
    return {s.name: s for s in report.steps}


# --- dry-run per mode -----------------------------------------------------------------------------
@pytest.mark.parametrize("mode", MODES)
def test_dry_run_each_mode_touches_nothing(staging: Path, make_ctx, mode: str) -> None:
    ctx = make_ctx(dry_run=True, zram_backend="zram-generator")
    before = sorted(p.relative_to(staging) for p in staging.rglob("*") if p.is_file())
    report = apply.run(mode, ctx=ctx, system=True, dry_run=True)
    after = sorted(p.relative_to(staging) for p in staging.rglob("*") if p.is_file())
    assert report.ok, report.render()
    assert report.dry_run
    assert before == after, "dry-run must not create or modify files"
    assert not tl.read(staging, common.STATE_FILE)
    names = [s.name for s in report.steps]
    for expected in ("mode", "sysctl-base", "sysctl-mode", "zram-config", "earlyoom-config", "journald",
                     "tmp-tmpfs", "preset-file", "governor-tmpfiles", "compositor", "state"):
        assert expected in names, f"{expected} missing from {names}"
    settings = report.extra["settings"]
    assert settings["mode"] == mode
    expected = {"everyday": (50, "schedutil", 4, False, "picom"), "gaming": (75, "performance", 4, True, "picom"),
                "work": (50, "schedutil", 4, False, "picom"), "creator": (50, "schedutil", 4, False, "picom"),
                "lite": (100, "schedutil", 8, False, "none")}[mode]
    assert (settings["zram_percent"], settings["governor"], settings["earlyoom_min"], settings["ananicy"],
            settings["compositor"]) == expected
    # the shipped tune.d conf was read
    assert "tune.d" in settings.get("sources", []) or "tune.d" in report.steps[0].detail


def test_gaming_mode_sysctl_max_map_count(make_ctx) -> None:
    ctx = make_ctx(dry_run=True)
    s = apply.load_settings(ctx, "gaming")
    assert s["sysctl"]["vm.max_map_count"] == "2147483642"
    text = apply.render_sysctl_mode("gaming", s["sysctl"])
    assert "vm.max_map_count = 2147483642" in text


# --- sysctl / zram-generator back-end ---------------------------------------------------------------
def test_apply_generator_backend_writes_expected_files(staging: Path, make_ctx, recorder) -> None:
    ctx = make_ctx(zram_backend="zram-generator")
    report = apply.run("everyday", ctx=ctx, system=True)
    assert report.ok, report.render()
    base = tl.read(staging, common.SYSCTL_BASE_CONF)
    assert base is not None
    for line in ("vm.swappiness = 180", "vm.page-cluster = 0", "vm.vfs_cache_pressure = 50", "vm.dirty_ratio = 10",
                 "kernel.nmi_watchdog = 0", "net.core.default_qdisc = fq", "net.ipv4.tcp_congestion_control = bbr"):
        assert line in base, line
    mode_conf = tl.read(staging, common.SYSCTL_MODE_CONF)
    assert mode_conf and "mode: everyday" in mode_conf and "=" not in mode_conf.split("\n", 2)[2].replace("# (no mode-specific sysctl overrides)", "")
    gen = tl.read(staging, common.ZRAM_GENERATOR_CONF)
    assert gen is not None
    assert "[zram0]" in gen
    assert "zram-size = ram * 0.50" in gen
    assert "compression-algorithm = zstd" in gen
    assert "swap-priority = 100" in gen
    assert tl.read(staging, common.ZRAMSWAP_DEFAULT) is None
    assert tl.read(staging, apply.MODULES_CONF) == apply.render_modules()
    journald = tl.read(staging, common.JOURNALD_DROPIN)
    assert journald and "SystemMaxUse=64M" in journald and "Storage=persistent" in journald
    state = json.loads(tl.read(staging, common.STATE_FILE) or "{}")
    assert state["mode"] == "everyday" and state["zram_backend"] == "zram-generator" and state["zram_percent"] == 50
    # nothing started/restarted in chroot; no sysctl reload
    assert not any(v in ("start", "restart", "try-restart", "stop") for v in recorder.verbs())
    assert not recorder.find("sysctl")
    assert _steps(report)["sysctl-reload"].skipped
    assert _steps(report)["zram-restart"].skipped


def test_gaming_generator_percent_75(staging: Path, make_ctx) -> None:
    ctx = make_ctx(zram_backend="zram-generator")
    apply.run("gaming", ctx=ctx, system=True)
    gen = tl.read(staging, common.ZRAM_GENERATOR_CONF) or ""
    assert "zram-size = ram * 0.75" in gen
    assert zram.parse_generator_percent(gen) == 75
    mode_conf = tl.read(staging, common.SYSCTL_MODE_CONF) or ""
    assert "vm.max_map_count = 2147483642" in mode_conf
    assert "performance" in (tl.read(staging, common.TMPFILES_GOVERNOR) or "")


# --- zram-tools back-end ---------------------------------------------------------------------------
def test_apply_zramtools_backend_lite(staging: Path, make_ctx, recorder) -> None:
    tl.unit(staging, zram.ZRAMTOOLS_UNIT)
    ctx = make_ctx(zram_backend="zram-tools")
    report = apply.run("lite", ctx=ctx, system=True)
    assert report.ok, report.render()
    conf = tl.read(staging, common.ZRAMSWAP_DEFAULT) or ""
    kv = common.parse_kv(conf)
    assert kv == {"ALGO": "zstd", "PERCENT": "100", "PRIORITY": "100"}
    assert zram.parse_zramswap_percent(conf) == 100
    assert tl.read(staging, common.ZRAM_GENERATOR_CONF) is None
    assert recorder.find("systemctl", "enable", zram.ZRAMTOOLS_UNIT)
    assert not recorder.find("systemctl", "restart")
    assert "180" in (tl.read(staging, common.SYSCTL_BASE_CONF) or "")


def test_no_zram_backend_means_swappiness_60(staging: Path, make_ctx) -> None:
    ctx = make_ctx(zram_backend=None)
    report = apply.run("everyday", ctx=ctx, system=True)
    assert report.ok
    base = tl.read(staging, common.SYSCTL_BASE_CONF) or ""
    assert "vm.swappiness = 60" in base and "180" not in base
    step = _steps(report)["zram"]
    assert step.skipped and "install" in step.detail


def test_zram_backend_detection_by_files(staging: Path, make_ctx) -> None:
    ctx = make_ctx()
    assert zram.detect_backend(ctx) is None
    tl.write(staging, "/usr/lib/systemd/system-generators/zram-generator", "")
    assert zram.detect_backend(ctx) == "zram-generator"


def test_zram_zero_disables(staging: Path, make_ctx, recorder) -> None:
    tl.unit(staging, zram.ZRAMTOOLS_UNIT)
    ctx = make_ctx(zram_backend="zram-tools")
    report = zram.set_percent(ctx, 0)
    assert report.ok
    assert common.parse_kv(tl.read(staging, common.ZRAMSWAP_DEFAULT))["PERCENT"] == "0"
    assert recorder.find("systemctl", "disable", zram.ZRAMTOOLS_UNIT)


# --- earlyoom ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("mode,minimum", [("everyday", 4), ("gaming", 4), ("work", 4), ("creator", 4), ("lite", 8)])
def test_earlyoom_args(staging: Path, make_ctx, mode: str, minimum: int) -> None:
    ctx = make_ctx()
    apply.run(mode, ctx=ctx, system=True)
    args = apply.earlyoom_args_from(tl.read(staging, common.EARLYOOM_DEFAULT))
    assert args == f"-m {minimum} -s 100 --avoid '{apply.EARLYOOM_AVOID}'"
    assert "(^|/)(Xorg|xfwm4|xfce4-panel|lightdm)$" in args


def test_earlyoom_enabled_when_installed(staging: Path, make_ctx, recorder) -> None:
    tl.unit(staging, "earlyoom.service")
    ctx = make_ctx()
    report = apply.run("everyday", ctx=ctx, system=True)
    assert recorder.find("systemctl", "enable", "earlyoom.service")
    assert _steps(report)["earlyoom"].ok and "boot" in _steps(report)["earlyoom"].detail


# --- preset ------------------------------------------------------------------------------------------
def test_shipped_preset_matches_renderer_and_spec_lists() -> None:
    shipped = (tl.ROOT / "usr/lib/systemd/system-preset/90-lindos.preset").read_text(encoding="utf-8")
    assert shipped == apply.render_preset()
    entries = apply.parse_preset(shipped)
    disabled = {u for v, u in entries if v == "disable"}
    enabled = {u for v, u in entries if v == "enable"}
    for unit in ("ModemManager.service", "cups-browsed.service", "NetworkManager-wait-online.service",
                 "apport.service", "whoopsie.service", "kerneloops.service", "brltty.service", "speech-dispatcher.service"):
        assert unit in disabled, unit
    assert {"earlyoom.service", "fstrim.timer", "cups.socket", "avahi-daemon.service"} <= enabled
    assert "avahi-daemon.service" not in disabled  # SPEC §8: keep for printers
    # Bluetooth is cheap when idle and useful for laptops (headphones/mice) — enabled by
    # default; only the Lite mode's own services_disable turns it off (see MODE_DEFAULTS).
    assert "bluetooth.service" in enabled and "bluetooth.service" not in disabled
    # First-boot oneshots (driver autodetect/offer, Chrome from Google) must actually be
    # enabled at build time — never left to the ambient systemd default policy.
    assert {"lindos-driver-firstboot.service", "lindos-browser-firstboot.service"} <= enabled


def test_preset_applied_once_and_only_for_present_units(staging: Path, make_ctx, recorder) -> None:
    tl.unit(staging, "bluetooth.service")
    tl.unit(staging, "earlyoom.service")
    ctx = make_ctx()
    report = apply.run("everyday", ctx=ctx, system=True)
    presets = recorder.find("systemctl", "preset")
    units = {argv[-1] for argv in presets}
    # lindos-sensors-detect.service is shipped by this package (staging copies it)
    assert units == {"bluetooth.service", "earlyoom.service", "lindos-sensors-detect.service"}
    assert _steps(report)["preset"].ok
    recorder.calls.clear()
    report2 = apply.run("everyday", ctx=ctx, system=True)
    assert not recorder.find("systemctl", "preset")
    assert "already applied" in _steps(report2)["preset"].detail


# --- shipped static configs == renderers -------------------------------------------------------------
def test_shipped_static_files_match_renderers() -> None:
    root = tl.ROOT
    assert (root / "etc/sysctl.d/70-lindos-base.conf").read_text(encoding="utf-8") == apply.render_sysctl_base(True)
    assert (root / "etc/systemd/journald.conf.d/lindos.conf").read_text(encoding="utf-8") == apply.render_journald()
    assert (root / "usr/share/lindos/tune/earlyoom.default").read_text(encoding="utf-8") == apply.render_earlyoom(4)


def test_first_apply_on_shipped_tree_leaves_static_files_unchanged(staging: Path, make_ctx) -> None:
    """A fresh install already carries the base files → apply reports them as unchanged."""
    ctx = make_ctx(zram_backend="zram-generator")
    tl.write(staging, common.EARLYOOM_DEFAULT, apply.render_earlyoom(4))
    report = apply.run("everyday", ctx=ctx, system=True)
    steps = _steps(report)
    for name in ("sysctl-base", "journald", "earlyoom-config"):
        assert "unchanged" in steps[name].detail, f"{name}: {steps[name].detail}"
    assert "present" in steps["preset-file"].detail


# --- idempotency ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("backend", ["zram-generator", "zram-tools"])
def test_apply_twice_is_idempotent(staging: Path, make_ctx, backend: str) -> None:
    tl.unit(staging, "earlyoom.service")
    tl.unit(staging, "tmp.mount")
    ctx = make_ctx(zram_backend=backend)
    apply.run("gaming", ctx=ctx, system=True)
    snapshot = {p.relative_to(staging): p.read_bytes() for p in staging.rglob("*") if p.is_file()}
    report = apply.run("gaming", ctx=ctx, system=True)
    assert report.ok, report.render()
    after = {p.relative_to(staging): p.read_bytes() for p in staging.rglob("*") if p.is_file()}
    changed = [str(k) for k in after if k in snapshot and after[k] != snapshot[k] and not str(k).endswith("state.json")]
    assert changed == [], f"second apply changed: {changed}"
    steps = _steps(report)
    for name in ("sysctl-base", "sysctl-mode", "modules", "zram-config", "earlyoom-config", "journald", "governor-tmpfiles"):
        assert "unchanged" in steps[name].detail, f"{name}: {steps[name].detail}"


def test_fstab_tmp_line_added_once(staging: Path, make_ctx) -> None:
    ctx = make_ctx()
    tl.write(staging, common.FSTAB, "UUID=abc / ext4 defaults 0 1\n")
    apply.run("everyday", ctx=ctx, system=True)
    apply.run("lite", ctx=ctx, system=True)
    fstab = tl.read(staging, common.FSTAB) or ""
    assert fstab.count(apply.FSTAB_TMP_LINE) == 1
    assert fstab.startswith("UUID=abc / ext4 defaults 0 1\n")


def test_tmp_mount_unit_preferred_over_fstab(staging: Path, make_ctx, recorder) -> None:
    tl.write(staging, apply.TMP_MOUNT_SHARE, "[Mount]\nWhat=tmpfs\nWhere=/tmp\n")
    ctx = make_ctx()
    report = apply.run("everyday", ctx=ctx, system=True)
    assert tl.read(staging, apply.TMP_MOUNT_ETC)
    assert recorder.find("systemctl", "enable", "tmp.mount")
    assert tl.read(staging, common.FSTAB) is None
    assert _steps(report)["tmp-tmpfs"].ok


# --- offline / chroot safety --------------------------------------------------------------------------
def test_offline_never_starts_units(staging: Path, make_ctx, recorder) -> None:
    for u in ("earlyoom.service", "fstrim.timer", "ananicy-cpp.service", "bluetooth.service", "zramswap.service"):
        tl.unit(staging, u)
    ctx = make_ctx(offline=True, tools=("systemctl", "sysctl", "modprobe", "powerprofilesctl"), zram_backend="zram-tools")
    report = apply.run("gaming", ctx=ctx, system=True, offline=True)
    assert report.ok, report.render()
    forbidden = {"start", "restart", "try-restart", "stop", "daemon-reload"}
    used = set(recorder.verbs())
    assert not (used & forbidden), used
    assert not recorder.find("sysctl") and not recorder.find("modprobe") and not recorder.find("powerprofilesctl")
    for argv in recorder.find("systemctl"):
        assert "--now" not in argv
    assert recorder.find("systemctl", "enable", "ananicy-cpp.service")
    assert recorder.find("systemctl", "enable", "fstrim.timer")


def test_ananicy_disabled_outside_gaming(staging: Path, make_ctx, recorder) -> None:
    tl.unit(staging, "ananicy-cpp.service")
    ctx = make_ctx()
    apply.run("work", ctx=ctx, system=True)
    assert recorder.find("systemctl", "disable", "ananicy-cpp.service")
    assert not recorder.find("systemctl", "enable", "ananicy-cpp.service")


# --- tune.d overrides ------------------------------------------------------------------------------
def test_tune_d_overrides_win(staging: Path, make_ctx) -> None:
    tl.write(staging, "/etc/lindos/tune.d/everyday.conf",
             "ZRAM_PERCENT=30\nGOVERNOR=performance\nEARLYOOM_MIN=6\nCOMPOSITOR=none\nANANICY=on\n"
             "SERVICES_DISABLE=\"bluetooth.service NetworkManager.service\"\n")
    ctx = make_ctx(zram_backend="zram-generator")
    s = apply.load_settings(ctx, "everyday")
    assert (s["zram_percent"], s["governor"], s["earlyoom_min"], s["compositor"], s["ananicy"]) == (30, "performance", 6, "none", True)
    assert s["services_disable"] == ["bluetooth.service", "NetworkManager.service"]
    report = apply.run("everyday", ctx=ctx, system=True)
    steps = _steps(report)
    assert "ram * 0.30" in (tl.read(staging, common.ZRAM_GENERATOR_CONF) or "")
    assert "-m 6" in apply.earlyoom_args_from(tl.read(staging, common.EARLYOOM_DEFAULT))
    # NetworkManager is not whitelisted → refused, never touched
    assert steps["disable:NetworkManager.service"].skipped and "whitelist" in steps["disable:NetworkManager.service"].detail


def test_mode_json_layer(staging: Path, make_ctx) -> None:
    tl.write(staging, "/usr/share/lindos/modes/creator/mode.json", json.dumps({
        "id": "creator", "name": "Creator", "zram_percent": 60, "governor": "ondemand", "compositor": "xfwm",
        "sysctl": {"vm.dirty_bytes": 268435456, "bad key": "1"}, "services_disable": ["ModemManager"],
    }))
    tl.write(staging, "/etc/lindos/tune.d/creator.conf", "# nothing overridden\n")
    ctx = make_ctx()
    s = apply.load_settings(ctx, "creator")
    assert s["zram_percent"] == 60 and s["governor"] == "ondemand" and s["compositor"] == "xfwm"
    assert s["sysctl"] == {"vm.dirty_bytes": "268435456"} and s["sysctl_dropped"] == ["bad key"]
    assert s["services_disable"] == ["ModemManager.service"]


def test_unknown_mode_is_an_error(make_ctx) -> None:
    with pytest.raises(ValueError):
        apply.resolve_settings("turbo")
    report = apply.run("turbo", ctx=make_ctx(dry_run=True))
    assert not report.ok and "unknown mode" in report.steps[0].detail


# --- autostart hiding --------------------------------------------------------------------------------
_DESKTOP = "[Desktop Entry]\nType=Application\nName={name}\nExec={name}\nOnlyShowIn={only}\n"


def test_autostart_hidden_and_restored(staging: Path, make_ctx, recorder) -> None:
    tl.write(staging, "/etc/xdg/autostart/mintwelcome.desktop", "[Desktop Entry]\nType=Application\nName=Welcome\nExec=mintwelcome\n")
    tl.write(staging, "/etc/xdg/autostart/blueman-applet.desktop", _DESKTOP.format(name="blueman-applet", only="GNOME;XFCE;"))
    tl.unit(staging, "bluetooth.service")
    ctx = make_ctx()
    report = apply.run("lite", ctx=ctx, system=True)  # lite disables bluetooth.service
    detail = _steps(report)["autostart-hide"].detail
    assert "hidden: " in detail and "mintwelcome" in detail and "blueman-applet" in detail
    mw = tl.read(staging, "/etc/xdg/autostart/mintwelcome.desktop") or ""
    assert "NotShowIn=XFCE;" in mw and "X-Lindos-Hidden=true" in mw
    ba = tl.read(staging, "/etc/xdg/autostart/blueman-applet.desktop") or ""
    assert "OnlyShowIn=GNOME;" in ba and "XFCE" not in ba.split("OnlyShowIn=")[1].split("\n")[0]
    # second run: nothing to do
    report = apply.run("lite", ctx=ctx, system=True)
    assert "already hidden" in _steps(report)["autostart-hide"].detail
    # switch to everyday: bluetooth enabled again → blueman-applet restored, mintwelcome stays hidden
    recorder.add(["systemctl", "is-enabled", "bluetooth.service"], CmdResult(True, 0, "enabled"))
    report = apply.run("everyday", ctx=ctx, system=True)
    detail = _steps(report)["autostart-hide"].detail
    assert "restored: blueman-applet" in detail
    ba = tl.read(staging, "/etc/xdg/autostart/blueman-applet.desktop") or ""
    assert "OnlyShowIn=GNOME;XFCE;" in ba and "X-Lindos-Hidden" not in ba
    assert "NotShowIn=XFCE;" in (tl.read(staging, "/etc/xdg/autostart/mintwelcome.desktop") or "")


# --- report shape -----------------------------------------------------------------------------------
def test_report_dict_and_render(make_ctx) -> None:
    ctx = make_ctx(dry_run=True, zram_backend="zram-generator")
    report = apply.run("everyday", ctx=ctx, system=True, dry_run=True)
    data = report.to_dict()
    assert data["ok"] is True and data["dry_run"] is True and data["mode"] == "everyday"
    assert all(set(s) == {"name", "ok", "detail", "skipped"} for s in data["steps"])
    text = report.render()
    assert "(dry run)" in text and "result: ok" in text
    assert re.search(r"^\[ok  \] sysctl-base", text, re.M)
    json.dumps(data)  # JSON-able
