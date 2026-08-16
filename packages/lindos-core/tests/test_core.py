"""lindos-core: paths, config, helper client/validation, helper dry-run, browsers, theme, hardware,
ram, the four CLIs and the packaging files (SPEC §1.1, §4)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import lindos
from lindos import browsers, config as lconfig, hardware, helper as lhelper, paths, ram, theme

# paths of the package under test (computed here, not imported from conftest: under
# --import-mode=importlib "conftest" resolves to the repository-level tests/conftest.py)
_PKG_ROOT = Path(__file__).resolve().parent.parent
ROOT = _PKG_ROOT / "root"
BIN = ROOT / "usr" / "bin"
LIBEXEC = ROOT / "usr" / "libexec" / "lindos"
HELPER = LIBEXEC / "lindos-helper"
DEBIAN = _PKG_ROOT / "DEBIAN"
META_DEBIAN = _PKG_ROOT.parent / "lindos-meta" / "DEBIAN"


def bash_available():
    import shutil
    return shutil.which("bash")


# --- package / paths ------------------------------------------------------------------------
def test_package_metadata() -> None:
    assert lindos.__version__ == "1.0.0" and lindos.__codename__ == "Aurora"
    for name in ("paths", "config", "modes", "browsers", "hardware", "helper", "theme", "compat", "ram"):
        __import__(f"lindos.{name}")


def test_spec_paths_constants() -> None:
    assert paths.SYSTEM_CONF == "/etc/lindos/system.json"
    assert paths.MODES_DIR == "/usr/share/lindos/modes"
    assert paths.RECIPES_DIR == "/usr/share/lindos/recipes"
    assert paths.HELPER == "/usr/libexec/lindos/lindos-helper"
    assert paths.USER_CONF == "~/.config/lindos/config.json"
    assert paths.SETUP_DONE == "~/.config/lindos/setup-done"
    assert paths.PREFIXES_DIR == "~/.local/share/lindos/prefixes"
    assert paths.APPS_DB == "~/.local/share/lindos/apps.json"
    assert paths.LOG_DIR == "~/.local/state/lindos"


def test_paths_env_overrides(core_env) -> None:
    root, home = core_env["root"], core_env["home"]
    assert Path(paths.system_conf()) == root / "etc" / "lindos" / "system.json"
    assert Path(paths.modes_dir()) == root / "usr" / "share" / "lindos" / "modes"
    assert Path(paths.user_conf()) == home / ".config" / "lindos" / "config.json"
    assert Path(paths.apps_db()) == home / ".local" / "share" / "lindos" / "apps.json"
    assert Path(paths.resolve("~")) == home
    everything = paths.all_paths()
    assert set(everything) >= {"SYSTEM_CONF", "MODES_DIR", "USER_CONF", "APPS_DB", "LOG_DIR"}
    assert all(str(root) in v or str(home) in v for v in everything.values())
    with pytest.raises(KeyError):
        paths.get("NOPE")


# --- config ---------------------------------------------------------------------------------
def test_config_defaults_and_roundtrip(core_env) -> None:
    assert lconfig.DEFAULTS == {"mode": "everyday", "browser": "firefox", "theme": "dark", "accent": "#60CDFF",
                                "wallpaper": "/usr/share/backgrounds/lindos/aurora-dark.svg", "setup_done": False,
                                "gamemode_auto": True, "mangohud": False, "telemetry": False, "schema": 1}
    cfg = lconfig.Config.load()
    assert not cfg.exists and cfg["mode"] == "everyday" and cfg.get("nope", 7) == 7
    cfg.set("mode", "gaming")
    cfg["accent"] = "#0067C0"
    cfg["custom"] = {"nested": [1, 2]}
    cfg.save()
    assert Path(cfg.path).is_file() and not list(Path(cfg.path).parent.glob(".lindos-*"))   # atomic, no temp left
    again = lconfig.Config.load()
    assert again.as_dict()["mode"] == "gaming" and again["accent"] == "#0067C0" and again["custom"] == {"nested": [1, 2]}
    assert "custom" in again and len(again) >= len(lconfig.DEFAULTS)
    assert lconfig.effective_mode() == "gaming" and lconfig.effective_browser() == "firefox"


def test_system_config_and_effective(core_env) -> None:
    assert lconfig.load_system() == {"mode": "everyday", "browser": "firefox", "oem": False}
    lconfig.save_system({"mode": "work", "browser": "edge", "vendor": "acme"})
    sysconf = lconfig.load_system()
    assert sysconf["mode"] == "work" and sysconf["browser"] == "edge" and sysconf["vendor"] == "acme"
    assert lconfig.effective_mode() == "work" and lconfig.effective_browser() == "edge"
    Path(paths.user_conf()).parent.mkdir(parents=True, exist_ok=True)
    Path(paths.user_conf()).write_text('{"mode": "bogus", "browser": "chrome"}', encoding="utf-8")
    assert lconfig.effective_mode() == "work"        # invalid user value ignored
    assert lconfig.effective_browser() == "chrome"
    Path(paths.user_conf()).write_text("not json", encoding="utf-8")
    assert lconfig.Config.load()["mode"] == "everyday"


def test_parse_value_and_setup_done(core_env) -> None:
    assert lconfig.parse_value("true") is True and lconfig.parse_value("Off") is False
    assert lconfig.parse_value("5") == 5 and lconfig.parse_value('{"a":1}') == {"a": 1}
    assert lconfig.parse_value("hello") == "hello" and lconfig.parse_value("null") is None
    assert not lconfig.is_setup_done()
    Path(paths.setup_done()).parent.mkdir(parents=True, exist_ok=True)
    Path(paths.setup_done()).write_text("", encoding="utf-8")
    assert lconfig.is_setup_done()


# --- helper client: validation & command building ---------------------------------------------
def test_helper_actions_match_spec() -> None:
    assert lhelper.ACTIONS == ["apply-mode", "install-browser", "install-packages", "install-flatpaks", "set-governor",
                               "set-services", "apply-sysctl", "apply-tune", "set-zram", "install-compat",
                               "install-gaming", "install-drivers", "set-fan-profile", "set-sched",
                               "write-system-config", "enable-earlyoom"]
    assert lhelper.POLKIT_ACTION_ID == "org.lindos.helper"


@pytest.mark.parametrize("action, payload", [
    ("install-browser", {"browser": "edge"}),
    ("install-packages", {"packages": ["gimp", "libreoffice-calc", "python3.12"]}),
    ("install-flatpaks", {"flatpaks": ["org.prismlauncher.PrismLauncher", "com.usebottles.bottles"]}),
    ("set-governor", {"governor": "performance"}),
    ("set-services", {"disable": ["bluetooth", "cups.socket"], "enable": ["earlyoom"]}),
    ("apply-sysctl", {"sysctl": {"vm.max_map_count": 2147483642, "kernel.nmi_watchdog": "0"}}),
    ("apply-tune", {"mode": "lite", "offline": True}),
    ("set-zram", {"percent": 75}),
    ("install-compat", {"items": ["wine", "umu"]}),
    ("install-gaming", {"items": ["steam", "sober"]}),
    ("install-drivers", {"driver": "nvidia-open"}),
    ("set-fan-profile", {"profile": "quiet"}),
    ("set-sched", {"profile": "scx_lavd"}),
    ("set-sched", {"profile": "none"}),
    ("write-system-config", {"mode": "gaming", "oem": True}),
    ("enable-earlyoom", {"enable": False}),
])
def test_validate_payload_accepts(action: str, payload: dict) -> None:
    out = lhelper.validate_payload(action, payload)
    assert isinstance(out, dict) and json.dumps(out)


@pytest.mark.parametrize("action, payload", [
    ("bogus", {}),
    ("install-browser", {"browser": "opera"}),
    ("install-packages", {"packages": ["gimp; rm -rf /"]}),
    ("install-packages", {"packages": []}),
    ("install-packages", {"packages": "gimp lutris"}),
    ("install-flatpaks", {"flatpaks": ["org.bad/app"]}),
    ("set-governor", {"governor": "ludicrous"}),
    ("set-services", {"disable": ["ssh"]}),
    ("set-services", {}),
    ("apply-sysctl", {"sysctl": {"evil": "1"}}),
    ("apply-sysctl", {"sysctl": {"vm.swappiness": "10; reboot"}}),
    ("set-zram", {"percent": 500}),
    ("set-zram", {"percent": "50"}),
    ("install-gaming", {"items": ["fortnite"]}),
    ("install-drivers", {"args": ["--rm-rf"]}),
    ("write-system-config", {}),
    ("apply-mode", {"mode": "gaming", "apply_system": "/tmp/evil.sh"}),
    ("apply-mode", {"mode": "nope"}),
    ("enable-earlyoom", {"enable": "yes"}),
    ("set-fan-profile", {"profile": "a" * 100}),
    ("set-sched", {"profile": "scx_evil"}),
    ("set-sched", {}),
])
def test_validate_payload_rejects(action: str, payload) -> None:
    with pytest.raises(lhelper.PayloadError):
        lhelper.validate_payload(action, payload)


def test_unit_whitelist_helpers() -> None:
    assert lhelper.normalize_unit("bluetooth.service") == "bluetooth"
    assert lhelper.normalize_unit("fstrim.timer") == "fstrim"
    assert lhelper.normalize_unit("tmp.mount") == "tmp.mount"
    assert lhelper.unit_allowed("cups.socket") and lhelper.unit_allowed("earlyoom")
    assert not lhelper.unit_allowed("sshd") and not lhelper.unit_allowed("../etc")


def test_build_command_dry_run_uses_interpreter(core_env) -> None:
    cmd = lhelper.build_command("set-zram", {"percent": 10})
    assert cmd[0] == sys.executable and cmd[1] == str(HELPER) and cmd[2] == "set-zram"
    assert json.loads(cmd[3]) == {"percent": 10}
    assert lhelper.helper_path() == str(HELPER)
    assert lhelper.dry_run_enabled()


def test_run_privileged_invalid_payload_and_missing_helper(core_env, monkeypatch: pytest.MonkeyPatch) -> None:
    res = lhelper.run_privileged("set-zram", {"percent": "x"})
    assert not res.ok and res.code == lhelper.EXIT_USAGE and "invalid payload" in res.err
    monkeypatch.setenv("LINDOS_HELPER", str(core_env["tmp"] / "no-such-helper"))
    res = lhelper.run_privileged("set-zram", {"percent": 10})
    assert not res.ok and res.code == 127 and "not found" in res.err
    assert res.message and res.to_dict()["code"] == 127


def test_run_privileged_dry_run_roundtrip(core_env) -> None:
    lines = []
    res = lhelper.run_privileged("set-zram", {"percent": 60}, log=lines.append)
    assert res.ok and res.code == 0, res.err
    assert any("lindos-tune zram 60" in ln for ln in lines)
    res2 = lhelper.set_governor("performance")
    assert res2.ok and "scaling_governor" in res2.out
    res3 = lhelper.write_system_config(mode="lite")
    assert res3.ok and "system.json" in res3.out


# --- helper executable: dry-run through subprocess -------------------------------------------
#: pretend to be online inside the dry-run helper (core_env forces offline by default)
ONLINE_ENV = {"LINDOS_HELPER_ONLINE": "1", "LINDOS_FORCE_OFFLINE": "0"}


def _unquoted(text: str) -> str:
    """The helper shell-quotes arguments with spaces/backslashes (Windows temp paths); strip quotes."""
    return text.replace("'", "")


def test_helper_dry_run_apply_mode(core_env, run_cli) -> None:
    from lindos import modes
    plan = modes.build_system_plan(modes.get_mode("gaming"), set_system_default=True)
    proc = run_cli("lindos-helper", "apply-mode", json.dumps(plan), env=ONLINE_ENV)
    assert proc.returncode == 0, proc.stderr
    out = _unquoted(proc.stdout)
    assert "[dry-run] would run: apt-get install" in out and "gamemode" in out
    assert "flatpak install -y --noninteractive --system flathub org.prismlauncher.PrismLauncher" in out
    assert "lindos-tune apply --mode gaming --system" in out
    assert "system.json" in out
    log_file = core_env["root"] / "var" / "log" / "lindos" / "helper.log"
    assert log_file.is_file() and "apply-mode" in log_file.read_text(encoding="utf-8")


def test_helper_dry_run_apply_mode_without_lindos_tune(core_env, run_cli) -> None:
    """Fallback path: services / sysctl drop-in / governor / zram applied directly by the helper."""
    from lindos import modes
    plan = modes.build_system_plan(modes.get_mode("lite"))
    proc = run_cli("lindos-helper", "apply-mode", json.dumps(plan), env={"LINDOS_HELPER_NO_TUNE": "1"})
    assert proc.returncode == 0, proc.stderr
    out = _unquoted(proc.stdout)
    assert "lindos-tune apply" not in out
    assert "systemctl disable --now -- cups-browsed.service" in out
    assert "systemctl enable --now -- earlyoom.service" in out
    assert "90-lindos-mode.conf" in out and "sysctl --system" in out
    assert "scaling_governor" in out
    assert "zramswap" in out or "zram-generator" in out


def test_helper_dry_run_offline_skips_installs(core_env, run_cli) -> None:
    proc = run_cli("lindos-helper", "apply-mode", json.dumps({"mode": "creator", "packages": ["gimp"], "flatpaks": ["com.usebottles.bottles"]}))
    assert proc.returncode == 0, proc.stderr
    assert "offline" in (proc.stdout + proc.stderr)
    assert "apt-get install" not in proc.stdout
    assert "lindos-tune apply --mode creator --system --offline" in proc.stdout


@pytest.mark.parametrize("action, payload, expect", [
    ("install-browser", {"browser": "chrome"}, "install-browser.sh chrome"),
    ("install-packages", {"packages": ["gimp"]}, "apt-get install -y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold gimp"),
    ("install-flatpaks", {"flatpaks": ["org.vinegarhq.Sober"]}, "flathub org.vinegarhq.Sober"),
    ("set-governor", {"governor": "powersave"}, "scaling_governor"),
    ("set-services", {"disable": ["bluetooth"], "mask": ["apport"]}, "lindos-tune services disable bluetooth"),
    ("set-services", {"disable": ["bluetooth"], "mask": ["apport"]}, "systemctl mask --now -- apport.service"),
    ("apply-sysctl", {"sysctl": {"vm.swappiness": "180"}}, "sysctl"),
    ("apply-tune", {"mode": "lite"}, "lindos-tune apply --mode lite --system"),
    ("set-zram", {"percent": 100}, "lindos-tune zram 100"),
    ("install-compat", {"items": ["wine", "umu"]}, "install-compat.sh wine umu"),
    ("install-gaming", {"items": ["steam", "sober"]}, "install-gaming.sh steam sober"),
    ("install-drivers", {"args": ["--nvidia-open"]}, "lindos-drivers install --nvidia-open"),
    ("set-fan-profile", {"profile": "quiet"}, "lindos-tune fan set quiet"),
    ("set-sched", {"profile": "scx_lavd"}, "lindos-tune sched set scx_lavd"),
    ("write-system-config", {"browser": "edge"}, "system.json"),
    ("enable-earlyoom", {"enable": True}, "systemctl enable --now earlyoom.service"),
])
def test_helper_dry_run_every_action(core_env, run_cli, action: str, payload: dict, expect: str) -> None:
    proc = run_cli("lindos-helper", action, json.dumps(payload), env=ONLINE_ENV)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert expect in _unquoted(proc.stdout), proc.stdout


def test_helper_set_services_without_tune_uses_systemctl(core_env, run_cli) -> None:
    """Without lindos-tune the helper toggles the units itself (SPEC §4.6 whitelist enforced)."""
    proc = run_cli("lindos-helper", "set-services", json.dumps({"disable": ["bluetooth"], "enable": ["fstrim.timer"]}),
                   env={"LINDOS_HELPER_NO_TUNE": "1"})
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    out = _unquoted(proc.stdout)
    assert "systemctl disable --now -- bluetooth.service" in out
    assert "systemctl enable --now -- fstrim.timer" in out
    assert "lindos-tune" not in out


def test_helper_apply_sysctl_writes_dropin_content(core_env, run_cli) -> None:
    proc = run_cli("lindos-helper", "apply-sysctl", json.dumps({"sysctl": {"vm.max_map_count": "2147483642"}}))
    assert proc.returncode == 0
    assert "90-lindos-mode.conf" in proc.stdout


def test_helper_rejects_bad_input(core_env, run_cli) -> None:
    assert run_cli("lindos-helper", "bogus", "{}").returncode == 2
    assert run_cli("lindos-helper", "set-zram", '{"percent": 999}').returncode == 2
    assert run_cli("lindos-helper", "set-zram", "{not json").returncode == 2
    assert run_cli("lindos-helper", "set-services", '{"disable": ["sshd"]}').returncode == 2
    assert run_cli("lindos-helper").returncode == 2
    lst = run_cli("lindos-helper", "--list")
    assert lst.returncode == 0 and lst.stdout.split() == lhelper.ACTIONS
    stdin = run_cli("lindos-helper", "set-zram", "-", input='{"percent": 25}')
    assert stdin.returncode == 0 and "zram 25" in stdin.stdout


def test_helper_refuses_without_root_when_not_dry_run(core_env, run_cli) -> None:
    if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("running as root")
    proc = run_cli("lindos-helper", "set-zram", '{"percent": 25}', env={"LINDOS_HELPER_DRYRUN": "0"})
    assert proc.returncode == 1 and "root" in proc.stderr


# --- browsers -------------------------------------------------------------------------------
def test_browsers_table_matches_spec() -> None:
    b = browsers.BROWSERS
    assert set(b) == {"edge", "chrome", "firefox"}
    assert b["edge"]["package"] == "microsoft-edge-stable" and b["edge"]["desktop"] == "microsoft-edge.desktop"
    assert b["edge"]["list"] == "/etc/apt/sources.list.d/microsoft-edge.list"
    assert "signed-by=/etc/apt/keyrings/microsoft.gpg" in b["edge"]["repo"]
    assert b["chrome"]["key_url"] == "https://dl.google.com/linux/linux_signing_key.pub"
    assert b["firefox"]["repo"] is None and b["firefox"]["package"] == "firefox"


def test_browsers_offline_install_and_listing(core_env) -> None:
    assert browsers.online() is False        # LINDOS_FORCE_OFFLINE=1
    lines = []
    assert browsers.install("edge", log=lines.append) is False or browsers.is_installed("edge")
    if not browsers.is_installed("edge"):
        assert any("offline" in ln for ln in lines)
    rows = browsers.list_browsers()
    assert [r["id"] for r in rows] == ["edge", "chrome", "firefox"]
    assert rows[2]["on_iso"] is True and rows[0]["on_iso"] is False
    with pytest.raises(KeyError):
        browsers.is_installed("opera")


def test_browsers_set_default_writes_config_and_helpers_rc(core_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browsers.shutil, "which", lambda name, *a, **k: None)
    assert browsers.set_default("chrome") is True
    assert lconfig.Config.load()["browser"] == "chrome"
    rc = Path(paths.resolve("~/.config/xfce4/helpers.rc"))
    assert rc.is_file() and "WebBrowser=google-chrome" in rc.read_text(encoding="utf-8")
    helper_desktop = core_env["home"] / ".local" / "share" / "xfce4" / "helpers" / "google-chrome.desktop"
    assert helper_desktop.is_file() and "X-XFCE-Category=WebBrowser" in helper_desktop.read_text(encoding="utf-8")


# --- theme (file-based parts; xfconf absent on the test host is fine) --------------------------
def test_theme_accent_and_dark(core_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(theme.shutil, "which", lambda name, *a, **k: None)
    assert theme.set_accent("#3eb489") is True
    css = core_env["home"] / ".config" / "gtk-3.0" / "lindos-accent.css"
    assert css.is_file() and "@define-color lindos_accent #3EB489;" in css.read_text(encoding="utf-8")
    gtk_css = core_env["home"] / ".config" / "gtk-3.0" / "gtk.css"
    assert '@import url("lindos-accent.css");' in gtk_css.read_text(encoding="utf-8")
    assert theme.current_accent() == "#3EB489"
    with pytest.raises(ValueError):
        theme.set_accent("blue")
    assert theme.set_dark(False) is True
    ini = (core_env["home"] / ".config" / "gtk-4.0" / "settings.ini").read_text(encoding="utf-8")
    assert "gtk-application-prefer-dark-theme=0" in ini and "gtk-theme-name=Lindos-Light" in ini
    assert theme.is_dark() is False
    theme.set_dark(True)
    assert theme.is_dark() is True
    assert [name for name, _ in theme.ACCENTS][0] == "Aurora Blue" and len(theme.ACCENTS) == 8
    assert theme.list_wallpapers() == []      # nothing shipped under LINDOS_ROOT


# --- hardware / ram ------------------------------------------------------------------------------
def test_hardware_functions_are_safe_everywhere() -> None:
    cpu = hardware.cpu_info()
    assert set(cpu) >= {"model", "vendor", "cores", "threads", "arch"} and cpu["threads"] >= 1
    assert isinstance(hardware.gpu_info(), list)
    assert hardware.primary_gpu_vendor() in {"nvidia", "amd", "intel", "other", "unknown"}
    info = hardware.ram_info()
    assert set(info) == {"total", "used", "available", "free", "swap_total", "swap_used"}
    assert isinstance(hardware.battery_present(), bool)
    assert isinstance(hardware.available_governors(), list)
    assert hardware.current_governor() is None or isinstance(hardware.current_governor(), str)
    assert isinstance(hardware.fan_sensors(), list) and isinstance(hardware.refresh_rates(), dict)
    assert hardware.map_governor("schedutil", ["performance", "powersave"]) == "powersave"
    assert hardware.map_governor("performance", ["performance", "powersave"]) == "performance"
    assert hardware.map_governor("ondemand", ["performance"]) is None
    assert hardware.map_governor("schedutil", []) == "schedutil"
    assert set(hardware.summary()) >= {"cpu", "gpus", "ram", "kernel"}


def test_ram_snapshot_and_report() -> None:
    snap = ram.snapshot(top_n=3)
    assert set(snap) >= {"total", "used", "available", "top", "target", "score", "zram"}
    assert snap["target"] == [350, 500] and len(snap["top"]) <= 3
    for name, mb in snap["top"]:
        assert isinstance(name, str) and isinstance(mb, float)
    text = ram.report("text")
    assert "RAM used:" in text and "350" in text and "500" in text
    data = json.loads(ram.report("json"))
    assert data["target"] == [350, 500]
    with pytest.raises(ValueError):
        ram.report("yaml")
    assert ram.score(300) == "excellent" and ram.score(450) == "on-target"
    assert ram.score(700) == "above-target" and ram.score(2000) == "high" and ram.score(0) == "unknown"
    assert ram.TARGET_MB == (350, 500) and ram.LITE_TARGET_MB == (300, 380)


# --- CLIs -----------------------------------------------------------------------------------------
def test_cli_lindos_mode(core_env, run_cli) -> None:
    lst = run_cli("lindos-mode", "list", "--json")
    assert lst.returncode == 0, lst.stderr
    rows = json.loads(lst.stdout)
    assert [r["id"] for r in rows] == ["everyday", "gaming", "work", "creator", "lite"]
    assert rows[0]["current"] is True
    assert run_cli("lindos-mode", "get").stdout.strip() == "everyday"
    show = run_cli("lindos-mode", "show", "gaming", "--json")
    assert show.returncode == 0 and json.loads(show.stdout)["plan"]["governor"] == "performance"
    dry = run_cli("lindos-mode", "set", "lite", "--dry-run", "--json")
    assert dry.returncode == 0, dry.stderr
    data = json.loads(dry.stdout)
    assert data["ok"] is True and data["dry_run"] is True and any(s["name"] == "system" for s in data["steps"])
    assert run_cli("lindos-mode", "get").stdout.strip() == "everyday"        # dry run changed nothing
    # a real (non-dry) switch only where it cannot touch a live XFCE session (no xfconf-query /
    # lindos-compositor on the host); the shipped apply-user.sh is replaced by a harmless echo
    import shutil as _shutil
    if _shutil.which("xfconf-query") is None and _shutil.which("lindos-compositor") is None:
        (core_env["modes"] / "work" / "apply-user.sh").write_text("#!/bin/bash\necho user-script-ran\n", encoding="utf-8")
        real = run_cli("lindos-mode", "set", "work", "--json")
        assert real.returncode == 0, real.stderr + real.stdout
        data = json.loads(real.stdout)
        assert data["ok"] is True and data["dry_run"] is False
        assert run_cli("lindos-mode", "get").stdout.strip() == "work"
        assert lconfig.Config.load()["mode"] == "work"
    assert run_cli("lindos-mode", "set", "nope").returncode == 1
    assert run_cli("lindos-mode").returncode == 2
    assert run_cli("lindos-mode", "frobnicate").returncode == 2


def test_cli_lindos_config(core_env, run_cli) -> None:
    show = run_cli("lindos-config", "show", "--json")
    assert show.returncode == 0 and json.loads(show.stdout)["mode"] == "everyday"
    assert run_cli("lindos-config", "set", "mangohud", "true").returncode == 0
    assert run_cli("lindos-config", "get", "mangohud").stdout.strip() == "true"
    assert run_cli("lindos-config", "set", "mode", "warp").returncode == 2
    assert run_cli("lindos-config", "get", "does-not-exist").returncode == 1
    sysset = run_cli("lindos-config", "set", "mode", "lite", "--system")     # helper in dry-run
    assert sysset.returncode == 0, sysset.stderr
    assert run_cli("lindos-config", "set", "mangohud", "true", "--system").returncode == 2
    paths_out = run_cli("lindos-config", "paths", "--json")
    assert json.loads(paths_out.stdout)["USER_CONF"].endswith("config.json")
    assert run_cli("lindos-config", "unset", "mangohud").returncode == 0
    assert run_cli("lindos-config", "get", "mangohud").stdout.strip() == "false"
    assert run_cli("lindos-config", "reset").returncode == 2
    assert run_cli("lindos-config", "reset", "--yes").returncode == 0
    eff = run_cli("lindos-config", "show", "--effective", "--json")
    assert json.loads(eff.stdout)["oem"] is False


def test_cli_lindos_browser(core_env, run_cli) -> None:
    lst = run_cli("lindos-browser", "list", "--json")
    assert lst.returncode == 0 and [r["id"] for r in json.loads(lst.stdout)] == ["edge", "chrome", "firefox"]
    assert run_cli("lindos-browser", "get").stdout.strip() == "firefox"
    status = run_cli("lindos-browser", "status", "--json")
    assert json.loads(status.stdout)["online"] is False
    inst = run_cli("lindos-browser", "install", "edge", "--json")
    data = json.loads(inst.stdout)
    assert data["browser"] == "edge" and (inst.returncode == 1 or data["installed"])
    assert run_cli("lindos-browser", "install", "opera").returncode == 2
    assert run_cli("lindos-browser").returncode == 2


def test_cli_lindos_ram(core_env, run_cli) -> None:
    proc = run_cli("lindos-ram", "--json", "--builtin", "--top", "2")
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["target"] == [350, 500] and len(data["top"]) <= 2
    text = run_cli("lindos-ram", "--builtin")
    assert text.returncode == 0 and "RAM used" in text.stdout
    assert run_cli("lindos-ram", "--top", "-1").returncode == 2


# --- shipped files -----------------------------------------------------------------------------------
def test_scripts_have_shebangs_and_lf_endings() -> None:
    for script in [BIN / n for n in ("lindos-mode", "lindos-browser", "lindos-config", "lindos-ram")] + [HELPER]:
        raw = script.read_bytes()
        assert raw.startswith(b"#!/usr/bin/python3\n"), script
        assert b"\r\n" not in raw, script
    sh = LIBEXEC / "install-browser.sh"
    raw = sh.read_bytes()
    assert raw.startswith(b"#!/bin/bash\n") and b"set -Eeuo pipefail" in raw and b"\r\n" not in raw
    assert b"sudo " not in raw
    for name in ("postinst", "postrm"):
        raw = (DEBIAN / name).read_bytes()
        assert raw.startswith(b"#!/bin/sh\n") and b"set -e" in raw and b"\r\n" not in raw


def test_install_browser_script_content_matches_browsers_table() -> None:
    text = (LIBEXEC / "install-browser.sh").read_text(encoding="utf-8")
    for bid in ("edge", "chrome"):
        info = browsers.BROWSERS[bid]
        assert info["key_url"] in text and info["package"] in text and info["list"] in text
        keyring = re.search(r"signed-by=(\S+)\]", info["repo"]).group(1)
        repo_tail = info["repo"].split("] ", 1)[1]           # "https://… stable main"
        assert keyring in text and repo_tail in text and "deb [arch=amd64 signed-by=" in text
    assert "Dir::Etc::sourcelist" in text and "APT::Get::List-Cleanup=0" in text
    assert "gpg --dearmor" in text and "snap" in text


@pytest.mark.skipif(bash_available() is None, reason="bash not available")
def test_install_browser_script_dry_run(core_env) -> None:
    proc = subprocess.run([bash_available(), str(LIBEXEC / "install-browser.sh"), "chrome", "--dry-run"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                          env={**os.environ, "LINDOS_ROOT": str(core_env["root"])})
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "google-chrome.list" in proc.stdout and "apt-get install" in proc.stdout
    usage = subprocess.run([bash_available(), str(LIBEXEC / "install-browser.sh")], capture_output=True, text=True)
    assert usage.returncode == 2
    syntax = subprocess.run([bash_available(), "-n", str(LIBEXEC / "install-browser.sh")], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr


def test_debian_control_and_conffiles() -> None:
    control = (DEBIAN / "control").read_text(encoding="utf-8")
    fields = dict(re.findall(r"^([A-Za-z-]+): (.*)$", control, re.M))
    assert fields["Package"] == "lindos-core" and fields["Version"] == "1.0.0" and fields["Architecture"] == "all"
    assert fields["Maintainer"] == "Lindos Team <team@lindos.dev>"
    for dep in ("python3 (>= 3.10)", "policykit-1 | polkitd", "pkexec", "xdg-utils", "ca-certificates", "curl | wget", "gpg"):
        assert dep in fields["Depends"], dep
    assert "python3-gi" not in control and "gir1.2" not in control       # no GTK dependency (SPEC §4)
    assert "xfconf" in fields["Recommends"] and "flatpak" in fields["Recommends"]
    assert (DEBIAN / "conffiles").read_text(encoding="utf-8").strip() == "/etc/lindos/system.json"
    with open(ROOT / "etc" / "lindos" / "system.json", encoding="utf-8") as fh:
        assert json.load(fh) == {"mode": "everyday", "browser": "firefox", "oem": False}


def test_polkit_policy() -> None:
    policy = ROOT / "usr" / "share" / "polkit-1" / "actions" / "org.lindos.helper.policy"
    tree = ET.parse(policy)
    action = tree.getroot().find("action")
    assert action is not None and action.get("id") == "org.lindos.helper"
    defaults = action.find("defaults")
    assert defaults is not None and defaults.findtext("allow_active") == "auth_admin_keep"
    annotations = {a.get("key"): a.text for a in action.findall("annotate")}
    assert annotations["org.freedesktop.policykit.exec.path"] == "/usr/libexec/lindos/lindos-helper"


def test_meta_package_control() -> None:
    control = (META_DEBIAN / "control").read_text(encoding="utf-8")
    fields = dict(re.findall(r"^([A-Za-z-]+): (.*)$", control, re.M))
    assert fields["Package"] == "lindos-meta" and fields["Version"] == "1.0.0"
    for dep in ("lindos-core", "lindos-desktop", "lindos-setup", "lindos-settings", "lindos-compat", "lindos-gaming",
                "lindos-tune"):
        assert dep in fields["Depends"], dep
    assert "firefox" in fields["Recommends"] and "steam-launcher | steam-installer" in fields["Recommends"]
    assert "wine-staging | wine" in fields["Recommends"]
