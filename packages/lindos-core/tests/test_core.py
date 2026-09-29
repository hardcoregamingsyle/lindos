"""lindos-core: paths, config, helper client/validation, helper dry-run, browsers, theme, hardware,
ram, the four CLIs and the packaging files (SPEC §1.1, §4)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

import pytest

import lindos
from lindos import browsers, config as lconfig, dualboot as ldualboot, hardware, helper as lhelper, paths, ram, theme

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
    for name in ("paths", "config", "modes", "browsers", "hardware", "helper", "theme", "compat", "ram", "dualboot",
                 "session", "installstate"):
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
    assert paths.INSTALL_STATE == "/var/lib/lindos/install-state.json"


def test_paths_env_overrides(core_env) -> None:
    root, home = core_env["root"], core_env["home"]
    assert Path(paths.system_conf()) == root / "etc" / "lindos" / "system.json"
    assert Path(paths.modes_dir()) == root / "usr" / "share" / "lindos" / "modes"
    assert Path(paths.user_conf()) == home / ".config" / "lindos" / "config.json"
    assert Path(paths.apps_db()) == home / ".local" / "share" / "lindos" / "apps.json"
    assert Path(paths.resolve("~")) == home
    assert Path(paths.install_state()) == root / "var" / "lib" / "lindos" / "install-state.json"
    assert Path(paths.get("INSTALL_STATE")) == Path(paths.install_state())
    everything = paths.all_paths()
    assert set(everything) >= {"SYSTEM_CONF", "MODES_DIR", "USER_CONF", "APPS_DB", "LOG_DIR", "INSTALL_STATE"}
    assert all(str(root) in v or str(home) in v for v in everything.values())
    with pytest.raises(KeyError):
        paths.get("NOPE")


# --- config ---------------------------------------------------------------------------------
def test_config_defaults_and_roundtrip(core_env) -> None:
    assert lconfig.DEFAULTS == {"mode": "everyday", "browser": "chrome", "theme": "dark", "accent": "#60CDFF",
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
    # no "browser" key in the user config -> falls back to the system default (chrome, SPEC §3/§6)
    assert lconfig.effective_mode() == "gaming" and lconfig.effective_browser() == "chrome"


def test_system_config_and_effective(core_env) -> None:
    assert lconfig.load_system() == {"mode": "everyday", "browser": "chrome", "oem": False}
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
                               "write-system-config", "enable-earlyoom",
                               "reboot-to-windows", "firmware-setup", "import-wifi", "set-binfmt",
                               "apt-get-update", "system-upgrade", "cleanup-old-packages", "install-local-debs",
                               "run-batch"]
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
    ("install-flatpaks", {"flatpaks": ["com.nvidia.geforcenow"],
                          "remote": {"name": "GeForceNOW",
                                     "url": "https://international.download.nvidia.com/GFNLinux/flatpak/geforcenow.flatpakrepo"}}),
    # a well-formed, contained apply_system path must still validate (sec-core:F1 regression
    # guard: the tightened containment check must not reject legitimate paths).
    ("apply-mode", {"mode": "gaming", "apply_system": "/usr/share/lindos/modes/gaming/apply-system.sh"}),
    ("apply-mode", {"mode": "gaming", "install": False}),
    ("apply-mode", {"mode": "gaming", "install": True}),
    ("reboot-to-windows", {"method": "bootnext", "entry": "0001"}),
    ("reboot-to-windows", {"method": "bootnext", "entry": "00AB", "reboot": False}),
    ("reboot-to-windows", {"method": "grub-reboot", "menuentry": "osprober-efi-ABCD-1234"}),
    ("reboot-to-windows", {"method": "grub-reboot", "menuentry": "osprober-chain-hd0,gpt2"}),
    ("firmware-setup", {"confirm": True}),
    ("import-wifi", {"networks": [{"ssid": "HomeNet", "security": "open"}]}),
    ("import-wifi", {"networks": [{"ssid": "HomeNet", "security": "wpa-psk", "psk": "correcthorsebattery"}]}),
    ("import-wifi", {"networks": [{"ssid": "HomeNet", "security": "wpa-psk", "psk": "p@ssw0rd's finest!"}]}),
    ("import-wifi", {"networks": [{"ssid": "HomeNet", "security": "wpa-psk", "psk": "a" * 64}]}),
    ("import-wifi", {"networks": [{"ssid": "HomeNet", "security": "sae", "agent_owned": True, "hidden": True}]}),
    ("set-binfmt", {"enabled": True}),
    ("set-binfmt", {"enabled": False}),
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
    # sec-core:F2 -- a "package"/"flatpak id" starting with '-' must never slip through as an
    # apt-get/flatpak command-line option (e.g. disabling GPG verification, changing scope).
    ("install-packages", {"packages": ["--allow-unauthenticated"]}),
    ("install-packages", {"packages": ["-y"]}),
    ("install-packages", {"packages": ["gimp", "--reinstall"]}),
    ("install-flatpaks", {"flatpaks": ["org.bad/app"]}),
    ("install-flatpaks", {"flatpaks": ["--user"]}),
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
    # sec-core:F1 -- a naive string-prefix check on 'apply_system' is bypassable with '..': the
    # unnormalized string passes basename()/startswith() while resolving (once bash/os.path.isfile
    # touch the real filesystem path) to a script outside the modes directory, which the root
    # helper then bashes as root.
    ("apply-mode", {"mode": "gaming",
                    "apply_system": "/usr/share/lindos/modes/../../../../tmp/evil/apply-system.sh"}),
    ("apply-mode", {"mode": "gaming",
                    "apply_system": "/usr/share/lindos/modes/gaming/../../../../tmp/evil/apply-system.sh"}),
    ("apply-mode", {"mode": "gaming",
                    "apply_system": r"\usr\share\lindos\modes\..\..\..\..\tmp\evil\apply-system.sh"}),
    ("apply-mode", {"mode": "gaming",
                    "apply_system": "/usr/share/lindos/modes/./apply-system.sh"}),
    ("apply-mode", {"mode": "nope"}),
    ("apply-mode", {"mode": "gaming", "install": "no"}),
    ("enable-earlyoom", {"enable": "yes"}),
    ("set-fan-profile", {"profile": "a" * 100}),
    ("set-sched", {"profile": "scx_evil"}),
    ("set-sched", {}),
    ("install-flatpaks", {"flatpaks": ["com.nvidia.geforcenow"],
                          "remote": {"name": "flathub", "url": "https://dl.flathub.org/repo/flathub.flatpakrepo"}}),
    ("install-flatpaks", {"flatpaks": ["x"], "remote": {"name": "evil; rm -rf /", "url": "https://example.com/x"}}),
    ("install-flatpaks", {"flatpaks": ["x"], "remote": {"name": "GeForceNOW", "url": "http://example.com/x"}}),
    ("install-flatpaks", {"flatpaks": ["x"], "remote": {"name": "GeForceNOW", "url": "https://example.com/x; rm -rf /"}}),
    ("reboot-to-windows", {"method": "bootnext", "entry": "0001; rm -rf /"}),
    ("reboot-to-windows", {"method": "bootnext", "entry": "not-hex"}),
    ("reboot-to-windows", {"method": "bootnext", "entry": "00001"}),
    ("reboot-to-windows", {"method": "bootnext"}),
    ("reboot-to-windows", {"method": "grub-reboot", "menuentry": "osprober-efi-'; reboot; #"}),
    ("reboot-to-windows", {"method": "grub-reboot", "menuentry": "gnulinux-simple"}),
    ("reboot-to-windows", {"method": "grub-reboot", "menuentry": "osprober-efi-$(reboot)"}),
    ("reboot-to-windows", {"method": "bad-method", "entry": "0001"}),
    ("firmware-setup", {}),
    ("firmware-setup", {"confirm": False}),
    ("import-wifi", {"networks": []}),
    ("import-wifi", {"networks": [{"ssid": "x" * 33, "security": "open"}]}),
    ("import-wifi", {"networks": [{"ssid": "", "security": "open"}]}),
    ("import-wifi", {"networks": [{"ssid": "HomeNet", "security": "wpa"}]}),
    ("import-wifi", {"networks": [{"ssid": "HomeNet", "security": "wpa-psk", "psk": "short1"}]}),
    ("import-wifi", {"networks": [{"ssid": "HomeNet", "security": "wpa-psk", "psk": "g" * 64}]}),
    ("import-wifi", {"networks": [{"ssid": "HomeNet", "security": "wpa-psk", "psk": "line1\nkey-mgmt=none"}]}),
    ("import-wifi", {"networks": [{"ssid": "HomeNet", "security": "open", "psk": "shouldnotbehere"}]}),
    ("import-wifi", {"networks": [{"ssid": "evil\n[wifi-security]\nkey-mgmt=none", "security": "open"}]}),
    ("import-wifi", {"networks": [{"ssid": "HomeNet\x00hidden", "security": "open"}]}),
    ("import-wifi", {"networks": "not-a-list"}),
    ("set-binfmt", {}),
    ("set-binfmt", {"enabled": "yes"}),
])
def test_validate_payload_rejects(action: str, payload) -> None:
    with pytest.raises(lhelper.PayloadError):
        lhelper.validate_payload(action, payload)


def test_apply_mode_install_flag_is_validated_and_defaults_to_true() -> None:
    assert lhelper.validate_payload("apply-mode", {"mode": "gaming"})["install"] is True
    assert lhelper.validate_payload("apply-mode", {"mode": "gaming", "install": False})["install"] is False
    assert lhelper.validate_payload("apply-mode", {"mode": "gaming", "install": True})["install"] is True
    batch = lhelper.validate_payload("run-batch", {"steps": [
        {"id": "apply-mode", "action": "apply-mode", "payload": {"mode": "gaming", "install": False}}]})
    assert batch["steps"][0]["payload"]["install"] is False


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
    assert "flatpak install -y --noninteractive --system -- flathub org.prismlauncher.PrismLauncher" in out
    assert "lindos-tune apply --mode gaming --system" in out
    assert "system.json" in out
    log_file = core_env["root"] / "var" / "log" / "lindos" / "helper.log"
    assert log_file.is_file() and "apply-mode" in log_file.read_text(encoding="utf-8")


def test_helper_dry_run_apply_mode_install_false_installs_nothing(core_env, run_cli) -> None:
    """The first-boot wizard sends install=false: even online, no apt/flatpak install runs - but
    the configuration half (lindos-tune apply ...) still does."""
    from lindos import modes
    plan = modes.build_system_plan(modes.get_mode("gaming"), install=False)
    assert plan["install"] is False and plan["packages"] and plan["flatpaks"]   # plan still describes the mode
    proc = run_cli("lindos-helper", "apply-mode", json.dumps(plan), env=ONLINE_ENV)
    assert proc.returncode == 0, proc.stderr
    out = _unquoted(proc.stdout)
    assert "apt-get install" not in out and "apt-get update" not in out
    assert "flatpak install" not in out and "flatpak remote-add" not in out
    assert "install disabled" in (proc.stdout + proc.stderr)
    assert "lindos-tune apply --mode gaming --system" in out


def test_helper_dry_run_apply_mode_install_false_inside_run_batch(core_env, run_cli) -> None:
    from lindos import modes
    plan = modes.build_system_plan(modes.get_mode("creator"), install=False)
    batch = {"steps": [{"id": "apply-mode", "action": "apply-mode", "payload": plan}]}
    proc = run_cli("lindos-helper", "run-batch", json.dumps(batch), env=ONLINE_ENV)
    assert proc.returncode == 0, proc.stderr
    out = _unquoted(proc.stdout)
    assert "apt-get install" not in out and "flatpak install" not in out
    assert "lindos-tune apply --mode creator --system" in out


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
    ("install-packages", {"packages": ["gimp"]}, "apt-get install -y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold -- gimp"),
    ("install-flatpaks", {"flatpaks": ["org.vinegarhq.Sober"]}, "-- flathub org.vinegarhq.Sober"),
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
def test_cli_lindos_mode_set_no_install(core_env, run_cli) -> None:
    def plan_of(*extra: str) -> dict:
        proc = run_cli("lindos-mode", "set", "gaming", "--dry-run", "--json", *extra)
        assert proc.returncode == 0, proc.stderr
        message = next(s["message"] for s in json.loads(proc.stdout)["steps"] if s["name"] == "system")
        return json.loads(message.split("with plan: ", 1)[1])

    assert plan_of()["install"] is True
    assert plan_of("--no-install")["install"] is False


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
    assert run_cli("lindos-browser", "get").stdout.strip() == "chrome"
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


@pytest.mark.skipif(bash_available() is None, reason="bash not available")
@pytest.mark.parametrize("bid", ["edge", "chrome"])
def test_install_browser_repo_only_never_installs_the_package(core_env, bid: str) -> None:
    """--repo-only (used by build/chroot/30-lindos-debs.sh to pre-stage Chrome on the ISO,
    SPEC §0.1) must add the repo/key and refresh that one source list, but never run
    'apt-get install' — the package itself must never end up on the image."""
    proc = subprocess.run([bash_available(), str(LIBEXEC / "install-browser.sh"), bid, "--repo-only", "--dry-run"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                          env={**os.environ, "LINDOS_ROOT": str(core_env["root"])})
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "would fetch" in proc.stdout and "would write" in proc.stdout
    assert "apt-get update" in proc.stdout   # refreshes only that source list
    assert "apt-get install" not in proc.stdout
    assert "repository staged" in proc.stdout


@pytest.mark.skipif(bash_available() is None, reason="bash not available")
def test_install_browser_repo_only_rejects_firefox() -> None:
    proc = subprocess.run([bash_available(), str(LIBEXEC / "install-browser.sh"), "firefox", "--repo-only"],
                          capture_output=True, text=True)
    assert proc.returncode == 2


# --- lindos-browser-firstboot.service / browser-firstboot.sh (SPEC §0.1, §4.4, §6, §8) -----------
FIRSTBOOT_SERVICE = ROOT / "usr" / "lib" / "systemd" / "system" / "lindos-browser-firstboot.service"
FIRSTBOOT_SCRIPT = LIBEXEC / "browser-firstboot.sh"
PYLIB = ROOT / "usr" / "lib" / "python3" / "dist-packages"
NOT_LIVE_CMDLINE = "BOOT_IMAGE=/boot/vmlinuz-6.14.0-lindos root=UUID=0000 ro quiet splash"


def test_browser_firstboot_service_unit() -> None:
    unit = FIRSTBOOT_SERVICE.read_text(encoding="utf-8")
    assert "\r\n" not in unit
    # never in the live/ISO session
    assert "ConditionKernelCommandLine=!boot=casper" in unit and "ConditionKernelCommandLine=!boot=live" in unit
    # not before the new user finished Ubiquity's oem-config first-boot wizard
    assert "ConditionPathExists=!/lib/systemd/system/oem-config.target" in unit
    # run-once guard + never blocks boot
    assert "ConditionPathExists=!/var/lib/lindos/browser-firstboot.done" in unit
    assert "ConditionVirtualization=!container" in unit
    assert "Type=oneshot" in unit and "RemainAfterExit=yes" in unit
    assert "ExecStart=/usr/libexec/lindos/browser-firstboot.sh" in unit
    # retries later when offline: ordered after (not required by) network-online.target
    assert "Wants=network-online.target" in unit and "After=network-online.target" in unit
    assert "WantedBy=multi-user.target" in unit


def test_browser_firstboot_script_shape() -> None:
    text = FIRSTBOOT_SCRIPT.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/bash\n") and "\r\n" not in text
    assert "set -Eeuo pipefail" in text
    # never in the live session (redundant guard even though the unit already gates this)
    assert "boot=casper" in text
    # run-once marker (STATE_DIR="${LINDOS_ROOT:-}/var/lib/lindos", MARKER="${STATE_DIR}/browser-firstboot.done")
    assert "/var/lib/lindos" in text and "browser-firstboot.done" in text
    # the live-session test is the shared helper's, not a private copy of the cmdline grep
    assert "is-live-session" in text and "oem-config-pending" in text
    assert "/proc/cmdline" not in text
    # install-state first: terminal -> exit at once; retry outcomes are recorded
    assert "lindos.installstate" in text and "status browser" in text and "mark browser" in text
    # reuses install-browser.sh; never duplicates Google's repo/key URLs or calls apt directly
    assert "install-browser.sh" in text
    assert "dl.google.com" not in text
    for forbidden in ("apt-get install", "apt install"):
        assert forbidden not in text, forbidden
    # sets the system-wide default for new users via the standard xdg fallback file
    assert "/etc/xdg/mimeapps.list" in text


@pytest.mark.skipif(bash_available() is None, reason="bash not available")
def test_browser_firstboot_script_syntax() -> None:
    syntax = subprocess.run([bash_available(), "-n", str(FIRSTBOOT_SCRIPT)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr


def _firstboot_sandbox(tmp_path: Path, *, install_rc: int = 0, system_browser: Optional[str] = "chrome"):
    """Build a scratch LINDOS_ROOT with a fake install-browser.sh + a fake root-uid 'id'.

    Returns (root, fakebin, canary) — canary is written by the fake install-browser.sh iff it
    was actually invoked, so tests can prove the run-once/live-session guards short-circuit
    before ever reaching it.
    """
    root = tmp_path / "root"
    (root / "usr" / "libexec" / "lindos").mkdir(parents=True)
    (root / "etc" / "lindos").mkdir(parents=True)
    shutil.copy(FIRSTBOOT_SCRIPT, root / "usr" / "libexec" / "lindos" / "browser-firstboot.sh")
    # the shared helpers the script asks (shipped next to it by lindos-core)
    for helper_name in ("is-live-session", "oem-config-pending"):
        shutil.copy(LIBEXEC / helper_name, root / "usr" / "libexec" / "lindos" / helper_name)
    canary = tmp_path / "install-browser-was-called"
    fake_install = root / "usr" / "libexec" / "lindos" / "install-browser.sh"
    fake_install.write_text(
        "#!/bin/bash\n"
        f"printf '%s\\n' \"$*\" > \"{canary.as_posix()}\"\n"
        f"exit {install_rc}\n",
        encoding="utf-8", newline="\n",
    )
    fake_install.chmod(0o755)
    if system_browser is not None:
        (root / "etc" / "lindos" / "system.json").write_text(
            json.dumps({"mode": "everyday", "browser": system_browser, "oem": False}), encoding="utf-8")
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    fake_id = fakebin / "id"
    fake_id.write_text(
        "#!/bin/bash\n[ \"$1\" = \"-u\" ] && echo 0 || echo root\n", encoding="utf-8", newline="\n")
    # Path.write_text() never sets the execute bit. On Windows/git-bash this goes unnoticed (NTFS
    # has no POSIX exec bit, so a shebang script "just runs" regardless), but on real Linux a
    # non-executable match is skipped during PATH search -- bash falls through to the *real*
    # /usr/bin/id, which reports the CI runner's actual (non-root) uid, and every test below that
    # relies on this fake to simulate root sees browser-firstboot.sh's real "must run as root"
    # error instead of the behavior it's trying to exercise. Seen for real on CI; never on Windows.
    fake_id.chmod(0o755)
    return root, fakebin, canary


def _firstboot_env(root: Path, *, cmdline: Optional[str] = None) -> dict:
    """Hermetic environment: fake (by default NOT live) kernel command line, the in-tree lindos module."""
    env = dict(os.environ)
    env["LINDOS_ROOT"] = str(root)
    cmdline_file = root.parent / "fake-cmdline"
    cmdline_file.write_text(NOT_LIVE_CMDLINE if cmdline is None else cmdline, encoding="utf-8")
    env["LINDOS_TEST_CMDLINE"] = str(cmdline_file)
    env["LINDOS_PYTHON"] = sys.executable
    env["PYTHONPATH"] = str(PYLIB) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _run_firstboot(root: Path, fakebin: Path, *, cmdline: Optional[str] = None, force: bool = False,
                   tmp_path: Optional[Path] = None):
    env = _firstboot_env(root, cmdline=cmdline)
    env["PATH"] = str(fakebin) + os.pathsep + env.get("PATH", "")
    args = [bash_available(), str(root / "usr" / "libexec" / "lindos" / "browser-firstboot.sh")]
    if force:
        args.append("--force")
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=60, env=env)


@pytest.mark.skipif(bash_available() is None, reason="bash not available")
def test_browser_firstboot_never_runs_in_live_session(tmp_path: Path) -> None:
    root, fakebin, canary = _firstboot_sandbox(tmp_path)
    proc = _run_firstboot(root, fakebin, cmdline="BOOT_IMAGE=/casper/vmlinuz boot=casper quiet splash",
                          tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "boot=casper" in proc.stderr or "live/ISO session" in proc.stderr
    assert not canary.exists(), "install-browser.sh must never be invoked in the live session"
    assert not (root / "var" / "lib" / "lindos" / "browser-firstboot.done").exists()


@pytest.mark.skipif(bash_available() is None, reason="bash not available")
def test_browser_firstboot_run_once_guard(tmp_path: Path) -> None:
    root, fakebin, canary = _firstboot_sandbox(tmp_path)
    marker = root / "var" / "lib" / "lindos" / "browser-firstboot.done"
    marker.parent.mkdir(parents=True)
    marker.write_text("", encoding="utf-8")
    proc = _run_firstboot(root, fakebin, tmp_path=tmp_path)
    assert proc.returncode == 0
    assert "already done" in proc.stderr
    assert not canary.exists(), "must not re-install once the marker exists"
    # --force bypasses the marker and (with our fake root 'id') actually runs
    proc2 = _run_firstboot(root, fakebin, force=True, tmp_path=tmp_path)
    assert proc2.returncode == 0
    assert canary.exists() and canary.read_text(encoding="utf-8").strip() == "chrome"


@pytest.mark.skipif(bash_available() is None, reason="bash not available")
def test_browser_firstboot_requires_root(tmp_path: Path) -> None:
    root, _fakebin, canary = _firstboot_sandbox(tmp_path)
    # no fake 'id' on PATH here -> the real (non-root) id -u is used
    env = _firstboot_env(root)
    proc = subprocess.run([bash_available(), str(root / "usr" / "libexec" / "lindos" / "browser-firstboot.sh")],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, env=env)
    assert proc.returncode == 0
    assert "must run as root" in proc.stderr
    assert not canary.exists()
    assert not (root / "var" / "lib" / "lindos" / "browser-firstboot.done").exists()


@pytest.mark.skipif(bash_available() is None, reason="bash not available")
def test_browser_firstboot_installs_chrome_and_sets_default(tmp_path: Path) -> None:
    root, fakebin, canary = _firstboot_sandbox(tmp_path, install_rc=0, system_browser="chrome")
    proc = _run_firstboot(root, fakebin, tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert canary.exists() and canary.read_text(encoding="utf-8").strip() == "chrome"
    assert (root / "var" / "lib" / "lindos" / "browser-firstboot.done").exists()
    mimeapps = (root / "etc" / "xdg" / "mimeapps.list").read_text(encoding="utf-8")
    assert "[Default Applications]" in mimeapps
    for key in ("x-scheme-handler/http", "x-scheme-handler/https", "text/html", "application/xhtml+xml"):
        assert f"{key}=google-chrome.desktop" in mimeapps


@pytest.mark.skipif(bash_available() is None, reason="bash not available")
def test_browser_firstboot_offline_retries_later(tmp_path: Path) -> None:
    root, fakebin, canary = _firstboot_sandbox(tmp_path, install_rc=3, system_browser="chrome")
    proc = _run_firstboot(root, fakebin, tmp_path=tmp_path)
    assert proc.returncode == 0
    assert "offline" in proc.stderr and "retry" in proc.stderr
    assert canary.exists()  # install-browser.sh really was tried
    # no marker: a later boot must retry
    assert not (root / "var" / "lib" / "lindos" / "browser-firstboot.done").exists()
    assert not (root / "etc" / "xdg" / "mimeapps.list").exists()


@pytest.mark.skipif(bash_available() is None, reason="bash not available")
def test_browser_firstboot_skips_when_another_browser_chosen(tmp_path: Path) -> None:
    root, fakebin, canary = _firstboot_sandbox(tmp_path, system_browser="firefox")
    proc = _run_firstboot(root, fakebin, tmp_path=tmp_path)
    assert proc.returncode == 0
    assert "not chrome" in proc.stderr
    assert not canary.exists(), "must not download Chrome when the user picked another browser"
    assert (root / "var" / "lib" / "lindos" / "browser-firstboot.done").exists()


def _write_install_state(root: Path, steps: dict) -> Path:
    path = root / "var" / "lib" / "lindos" / "install-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": 1, "updated": "2026-09-29T10:00:00Z", "online": True, "steps": {
        name: {"status": status, "detail": "", "time": "2026-09-29T10:00:00Z"} for name, status in steps.items()}}),
        encoding="utf-8")
    return path


def _read_install_state(root: Path) -> dict:
    return json.loads((root / "var" / "lib" / "lindos" / "install-state.json").read_text(encoding="utf-8"))


@pytest.mark.skipif(bash_available() is None, reason="bash not available")
@pytest.mark.parametrize("recorded", ["done", "skipped"])
def test_browser_firstboot_terminal_install_state_exits_at_once(tmp_path: Path, recorded: str) -> None:
    """The installer already did (or consciously skipped) Chrome: no retry, marker written."""
    root, fakebin, canary = _firstboot_sandbox(tmp_path)
    _write_install_state(root, {"browser": recorded})
    proc = _run_firstboot(root, fakebin)
    assert proc.returncode == 0, proc.stderr
    assert f"browser={recorded}" in proc.stderr
    assert not canary.exists(), "install-browser.sh must not run when the installer handled the step"
    assert (root / "var" / "lib" / "lindos" / "browser-firstboot.done").exists()
    assert _read_install_state(root)["steps"]["browser"]["status"] == recorded   # untouched
    mimeapps = root / "etc" / "xdg" / "mimeapps.list"
    if recorded == "done":
        # the installer installed Chrome: the one thing left for us is the system-wide default for new users
        assert "x-scheme-handler/https=google-chrome.desktop" in mimeapps.read_text(encoding="utf-8")
    else:
        assert not mimeapps.exists()


@pytest.mark.skipif(bash_available() is None, reason="bash not available")
def test_browser_firstboot_done_but_another_browser_configured_leaves_the_default_alone(tmp_path: Path) -> None:
    root, fakebin, canary = _firstboot_sandbox(tmp_path, system_browser="firefox")
    _write_install_state(root, {"browser": "done"})
    proc = _run_firstboot(root, fakebin)
    assert proc.returncode == 0 and not canary.exists()
    assert not (root / "etc" / "xdg" / "mimeapps.list").exists()
    assert (root / "var" / "lib" / "lindos" / "browser-firstboot.done").exists()


@pytest.mark.skipif(bash_available() is None, reason="bash not available")
def test_browser_firstboot_installer_flag_beats_the_live_kernel_command_line(tmp_path: Path) -> None:
    """Inside the installer's 'chroot /target' /proc/cmdline still says boot=casper; LINDOS_INSTALLER=1
    is the explicit 'this is the installed system' signal."""
    root, fakebin, canary = _firstboot_sandbox(tmp_path)
    live = "BOOT_IMAGE=/casper/vmlinuz boot=casper quiet splash"
    refused = _run_firstboot(root, fakebin, cmdline=live)
    assert "refusing to run" in refused.stderr and not canary.exists()
    env = _firstboot_env(root, cmdline=live)
    env["PATH"] = str(fakebin) + os.pathsep + env.get("PATH", "")
    env["LINDOS_INSTALLER"] = "1"
    proc = subprocess.run([bash_available(), str(root / "usr" / "libexec" / "lindos" / "browser-firstboot.sh")],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr
    assert canary.exists() and (root / "var" / "lib" / "lindos" / "browser-firstboot.done").exists()


@pytest.mark.skipif(bash_available() is None, reason="bash not available")
@pytest.mark.parametrize("recorded", ["pending", "failed"])
def test_browser_firstboot_retries_pending_and_records_done(tmp_path: Path, recorded: str) -> None:
    root, fakebin, canary = _firstboot_sandbox(tmp_path, install_rc=0)
    _write_install_state(root, {"browser": recorded, "drivers": "done"})
    proc = _run_firstboot(root, fakebin)
    assert proc.returncode == 0, proc.stderr
    assert canary.read_text(encoding="utf-8").strip() == "chrome"          # the silent retry really ran
    state = _read_install_state(root)
    assert state["steps"]["browser"]["status"] == "done"
    assert state["steps"]["drivers"]["status"] == "done"                     # other steps untouched
    assert (root / "var" / "lib" / "lindos" / "browser-firstboot.done").exists()
    assert "google-chrome.desktop" in (root / "etc" / "xdg" / "mimeapps.list").read_text(encoding="utf-8")


@pytest.mark.skipif(bash_available() is None, reason="bash not available")
def test_browser_firstboot_retry_failure_is_recorded_and_retried_later(tmp_path: Path) -> None:
    root, fakebin, canary = _firstboot_sandbox(tmp_path, install_rc=1)
    _write_install_state(root, {"browser": "pending"})
    proc = _run_firstboot(root, fakebin)
    assert proc.returncode == 0
    assert canary.exists()
    entry = _read_install_state(root)["steps"]["browser"]
    assert entry["status"] == "failed" and "exit 1" in entry["detail"]
    assert not (root / "var" / "lib" / "lindos" / "browser-firstboot.done").exists()


@pytest.mark.skipif(bash_available() is None, reason="bash not available")
def test_browser_firstboot_offline_records_pending_when_nothing_was_recorded(tmp_path: Path) -> None:
    root, fakebin, _canary = _firstboot_sandbox(tmp_path, install_rc=3)
    proc = _run_firstboot(root, fakebin)        # no install-state.json at all (legacy / installer skipped)
    assert proc.returncode == 0
    entry = _read_install_state(root)["steps"]["browser"]
    assert entry["status"] == "pending" and "offline" in entry["detail"]
    assert not (root / "var" / "lib" / "lindos" / "browser-firstboot.done").exists()


@pytest.mark.skipif(bash_available() is None, reason="bash not available")
def test_browser_firstboot_other_browser_is_recorded_as_skipped(tmp_path: Path) -> None:
    root, fakebin, canary = _firstboot_sandbox(tmp_path, system_browser="firefox")
    _write_install_state(root, {"browser": "pending"})
    proc = _run_firstboot(root, fakebin)
    assert proc.returncode == 0
    assert not canary.exists()
    assert _read_install_state(root)["steps"]["browser"]["status"] == "skipped"
    assert (root / "var" / "lib" / "lindos" / "browser-firstboot.done").exists()


@pytest.mark.skipif(bash_available() is None, reason="bash not available")
def test_browser_firstboot_waits_for_the_oem_config_wizard(tmp_path: Path) -> None:
    """oem-config.target present = Ubiquity's first-boot wizard armed/not finished: nothing is touched."""
    root, fakebin, canary = _firstboot_sandbox(tmp_path)
    _write_install_state(root, {"browser": "pending"})
    unit = root / "lib" / "systemd" / "system" / "oem-config.target"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Unit]\nDescription=oem-config\n", encoding="utf-8")
    proc = _run_firstboot(root, fakebin)
    assert proc.returncode == 0
    assert "oem-config" in proc.stderr and "pending" in proc.stderr
    assert not canary.exists()
    assert not (root / "var" / "lib" / "lindos" / "browser-firstboot.done").exists()
    assert _read_install_state(root)["steps"]["browser"]["status"] == "pending"
    # the wizard finished and removed its unit: the retry runs now
    unit.unlink()
    proc2 = _run_firstboot(root, fakebin)
    assert proc2.returncode == 0 and canary.exists()


@pytest.mark.skipif(bash_available() is None, reason="bash not available")
def test_browser_firstboot_without_the_shared_helper_assumes_live(tmp_path: Path) -> None:
    """Cannot ask is-live-session -> the safe answer is 'live': never install on a guess."""
    root, fakebin, canary = _firstboot_sandbox(tmp_path)
    (root / "usr" / "libexec" / "lindos" / "is-live-session").unlink()
    proc = _run_firstboot(root, fakebin)
    assert proc.returncode == 0 and "refusing to run" in proc.stderr
    assert not canary.exists()


@pytest.mark.skipif(bash_available() is None, reason="bash not available")
def test_browser_firstboot_live_session_leaves_install_state_alone(tmp_path: Path) -> None:
    root, fakebin, canary = _firstboot_sandbox(tmp_path)
    state_file = _write_install_state(root, {"browser": "pending"})
    before = state_file.read_text(encoding="utf-8")
    proc = _run_firstboot(root, fakebin, cmdline="BOOT_IMAGE=/casper/vmlinuz boot=live quiet splash")
    assert proc.returncode == 0 and not canary.exists()
    assert state_file.read_text(encoding="utf-8") == before


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
        assert json.load(fh) == {"mode": "everyday", "browser": "chrome", "oem": False}


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
