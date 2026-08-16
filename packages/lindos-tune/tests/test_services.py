"""``lindos-tune services`` — whitelist enforcement (file + helper agreement), enable/disable
through the runner, autostart hide/unhide helpers, list rendering."""
from __future__ import annotations

from pathlib import Path

import pytest

import tune_testlib as tl
from lindos_tune import common, services
from lindos_tune.common import CmdResult, Report

WHITELIST_FILE = tl.SHARE / "services-whitelist.txt"


# --- whitelist -----------------------------------------------------------------------------------
def test_shipped_whitelist_parses_and_covers_spec_units() -> None:
    units = services.parse_whitelist(WHITELIST_FILE.read_text(encoding="utf-8"))
    assert units, "whitelist must not be empty"
    assert len(units) == len(set(units))
    for spec_unit in ("bluetooth.service", "ModemManager.service", "avahi-daemon.service", "cups.service",
                      "cups-browsed.service", "NetworkManager-wait-online.service", "apport.service", "whoopsie.service",
                      "kerneloops.service", "brltty.service", "speech-dispatcher.service", "earlyoom.service",
                      "fstrim.timer", "tmp.mount", "zramswap.service", "ananicy-cpp.service", "power-profiles-daemon.service"):
        assert spec_unit in units, spec_unit
    for forbidden in ("NetworkManager.service", "lightdm.service", "dbus.service", "polkit.service", "systemd-logind.service",
                      "udev.service", "ssh.service"):
        assert forbidden not in units, forbidden
    # every entry is a valid unit name and normalises to itself
    assert all(services.valid_unit_name(u) and services.normalize_unit(u) == u for u in units)


def test_shipped_whitelist_agrees_with_embedded_default() -> None:
    units = set(services.parse_whitelist(WHITELIST_FILE.read_text(encoding="utf-8")))
    assert units == set(services.DEFAULT_WHITELIST)


@pytest.mark.skipif(not tl.core_available(), reason="lindos-core not importable")
def test_shipped_whitelist_is_accepted_by_core_helper() -> None:
    from lindos import helper as core_helper

    for unit in services.parse_whitelist(WHITELIST_FILE.read_text(encoding="utf-8")):
        assert core_helper.unit_allowed(unit), f"{unit} would be refused by lindos-helper set-services"


def test_load_whitelist_from_staging_and_fallback(staging: Path, make_ctx) -> None:
    ctx = make_ctx()
    assert "bluetooth.service" in services.load_whitelist(ctx)
    tl.write(staging, common.SERVICES_WHITELIST, "# only one\ncups-browsed\n")
    assert services.load_whitelist(ctx) == ["cups-browsed.service"]
    tl.write(staging, common.SERVICES_WHITELIST, "# empty\n")
    assert services.load_whitelist(ctx) == list(services.DEFAULT_WHITELIST)


def test_normalize_and_validate_names() -> None:
    assert services.normalize_unit("bluetooth") == "bluetooth.service"
    assert services.normalize_unit("fstrim.timer") == "fstrim.timer"
    assert services.normalize_unit("tmp.mount") == "tmp.mount"
    assert services.normalize_unit("cups.socket") == "cups.socket"
    assert services.base_name("bluetooth.service") == "bluetooth"
    assert services.base_name("tmp.mount") == "tmp.mount"
    assert services.valid_unit_name("systemd-zram-setup@zram0.service")
    assert not services.valid_unit_name("../etc/passwd")
    assert not services.valid_unit_name("a b.service")
    assert not services.valid_unit_name("")


def test_check_units_enforces_whitelist(make_ctx) -> None:
    ctx = make_ctx()
    assert services.check_units(["bluetooth", "cups.socket", "bluetooth.service"], ctx=ctx) == ["bluetooth.service", "cups.socket"]
    with pytest.raises(services.ServiceError, match="whitelist"):
        services.check_units(["NetworkManager"], ctx=ctx)
    with pytest.raises(services.ServiceError, match="whitelist"):
        services.check_units(["lightdm.service"], ctx=ctx)
    with pytest.raises(services.ServiceError, match="invalid unit name"):
        services.check_units(["../../evil"], ctx=ctx)
    with pytest.raises(services.ServiceError):
        services.check_units(["bluetooth; rm -rf /"], ctx=ctx)
    allowed, refused = services.filter_whitelisted(["bluetooth", "sshd", "cups.path"], ctx=ctx)
    assert allowed == ["bluetooth.service", "cups.path"] and refused == ["sshd"]


# --- toggling ------------------------------------------------------------------------------------
def test_disable_enable_call_systemctl_with_root(staging: Path, make_ctx, recorder) -> None:
    tl.unit(staging, "bluetooth.service")
    ctx = make_ctx()
    report = services.disable(ctx, "bluetooth")
    assert report.ok, report.render()
    calls = recorder.find("systemctl", "disable", "bluetooth.service")
    assert calls and any(a.startswith("--root=") for a in calls[0]), recorder.calls
    assert "--now" not in calls[0]  # chroot / not live
    assert "boot" in report.steps[-1].detail
    report = services.enable(ctx, "bluetooth.service")
    assert recorder.find("systemctl", "enable", "bluetooth.service") and report.ok


def test_toggle_refuses_non_whitelisted(make_ctx, recorder) -> None:
    ctx = make_ctx()
    with pytest.raises(services.ServiceError):
        services.disable(ctx, "NetworkManager.service")
    with pytest.raises(services.ServiceError):
        services.enable(ctx, "sshd")
    assert not recorder.calls, "refused units must never reach systemctl"


def test_toggle_missing_unit_is_skipped(make_ctx, recorder) -> None:
    ctx = make_ctx()
    report = services.disable(ctx, "whoopsie.service")
    assert report.ok and report.steps[-1].skipped and "not installed" in report.steps[-1].detail
    assert not recorder.calls


def test_toggle_failure_reported(staging: Path, make_ctx, recorder) -> None:
    tl.unit(staging, "cups-browsed.service")
    recorder.add(["systemctl", "disable", "cups-browsed.service"], CmdResult(False, 1, "Failed to disable unit: Access denied"))
    ctx = make_ctx()
    report = services.disable(ctx, "cups-browsed")
    assert not report.ok and "Access denied" in report.steps[-1].detail


def test_apply_lists_records_refusals(staging: Path, make_ctx, recorder) -> None:
    tl.unit(staging, "ModemManager.service")
    ctx = make_ctx()
    report = Report()
    services.apply_lists(ctx, report, disable_units=["ModemManager", "gdm3.service"], enable_units=["earlyoom"])
    steps = {s.name: s for s in report.steps}
    assert steps["disable:ModemManager.service"].ok
    assert steps["disable:gdm3.service"].skipped and "refused" in steps["disable:gdm3.service"].detail
    assert steps["enable:earlyoom.service"].skipped  # not installed in the staging tree
    assert not recorder.find("systemctl", "disable", "gdm3.service")


def test_dry_run_records_command_without_running(staging: Path, make_ctx, recorder) -> None:
    tl.unit(staging, "bluetooth.service")
    ctx = make_ctx(dry_run=True)
    report = services.disable(ctx, "bluetooth")
    assert report.ok and "would run" in report.steps[-1].detail
    assert not recorder.calls


# --- list ------------------------------------------------------------------------------------------
def test_list_units_and_render(staging: Path, make_ctx, recorder) -> None:
    tl.unit(staging, "bluetooth.service")
    tl.write(staging, common.SERVICES_WHITELIST, "bluetooth.service\nearlyoom.service\n")
    recorder.add(["systemctl", "is-enabled"], CmdResult(False, 1, "disabled\nenabled\n"))
    recorder.add(["systemctl", "is-active"], CmdResult(False, 3, "inactive\nactive\n"))
    ctx = make_ctx()
    rows = services.list_units(ctx)
    assert rows == [
        {"unit": "bluetooth.service", "exists": True, "enabled": "disabled", "active": "inactive"},
        {"unit": "earlyoom.service", "exists": False, "enabled": "enabled", "active": "active"},
    ]
    text = services.render_units(rows)
    assert "bluetooth.service" in text and "disabled" in text and "Only the units above" in text


def test_unit_state_without_systemctl(make_ctx) -> None:
    ctx = make_ctx(tools=())
    assert services.unit_state(ctx, "bluetooth.service") == {"unit": "bluetooth.service", "exists": False,
                                                              "enabled": "unknown", "active": "unknown"}


# --- autostart hiding ---------------------------------------------------------------------------------
def test_hide_and_unhide_in_xfce_roundtrip() -> None:
    original = "[Desktop Entry]\nType=Application\nName=Report\nExec=mintreport-tray\n"
    hidden, changed = services.hide_in_xfce(original)
    assert changed and "NotShowIn=XFCE;" in hidden and "X-Lindos-Hidden=true" in hidden
    again, changed2 = services.hide_in_xfce(hidden)
    assert not changed2 and again == hidden
    restored, changed3 = services.unhide_in_xfce(hidden)
    assert changed3 and restored == original
    # entries already Hidden=true are left alone
    assert services.hide_in_xfce("[Desktop Entry]\nHidden=true\nName=x\n")[1] is False
    # NotShowIn present: XFCE appended
    text, ch = services.hide_in_xfce("[Desktop Entry]\nName=x\nNotShowIn=GNOME;\n")
    assert ch and "NotShowIn=GNOME;XFCE;" in text
    back, ch2 = services.unhide_in_xfce(text)
    assert ch2 and "NotShowIn=GNOME;" in back and "XFCE" not in back
    # OnlyShowIn without XFCE: nothing to do
    assert services.hide_in_xfce("[Desktop Entry]\nName=x\nOnlyShowIn=GNOME;\n")[1] is False
    # no [Desktop Entry] group: untouched
    assert services.hide_in_xfce("[Other]\nName=x\n")[1] is False


def test_parse_hide_list() -> None:
    entries = services.parse_hide_list("# c\nmintwelcome\nblueman-applet.desktop if-disabled=bluetooth\n\nbad entry here\n")
    assert entries == [("mintwelcome", None), ("blueman-applet", "bluetooth.service")]
    shipped = services.parse_hide_list((tl.SHARE / "autostart-hide.list").read_text(encoding="utf-8"))
    names = dict(shipped)
    assert "mintwelcome" in names and "mintreport" in names
    assert names["blueman-applet"] == "bluetooth.service"
    assert "light-locker" not in names


def test_sync_conditional_autostart(staging: Path, make_ctx) -> None:
    tl.write(staging, "/etc/xdg/autostart/blueman-applet.desktop", "[Desktop Entry]\nName=Blueman\nExec=blueman-applet\n")
    ctx = make_ctx()
    report = Report()
    services.sync_conditional_autostart(ctx, report, "bluetooth", disabled=True)
    assert "NotShowIn=XFCE;" in (tl.read(staging, "/etc/xdg/autostart/blueman-applet.desktop") or "")
    services.sync_conditional_autostart(ctx, report, "bluetooth.service", disabled=False)
    assert "NotShowIn" not in (tl.read(staging, "/etc/xdg/autostart/blueman-applet.desktop") or "")
    # unrelated unit: nothing recorded
    before = len(report.steps)
    services.sync_conditional_autostart(ctx, report, "cups.service", disabled=True)
    assert len(report.steps) == before
