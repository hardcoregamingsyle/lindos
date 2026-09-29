"""finalize.sh, the installer's ubiquity/success_command, run for real against a fake /target.

It runs synchronously in Ubiquity's GTK thread (the window freezes meanwhile), so it has to be short,
must never hang, and must fail SAFE: arming oem-config only when oem-config is really in the new system.
"""
from __future__ import annotations

import re
from pathlib import Path

from installer_testlib import LIBEXEC, Sandbox, needs_bash

pytestmark = needs_bash

TWO = "browser drivers"


def _finalize(sb: Sandbox, **env: str):
    proc = sb.run_finalize(LINDOS_INSTALLER_STEPS=TWO, **env)
    assert proc.returncode == 0, proc.stderr[-2500:]
    assert proc.stdout == "", "the success command wrote to stdout: %r" % proc.stdout[:200]
    return proc


def _link_target(sb: Sandbox) -> str:
    """Where etc/systemd/system/default.target points (the harness keeps links as one-line files)."""
    f = sb.target / "etc/systemd/system/default.target"
    text = f.read_text(encoding="utf-8").strip() if f.is_file() else ""
    return text[len("LINK:"):] if text.startswith("LINK:") else text


def test_arms_oem_config_and_cleans_up(sandbox: Sandbox) -> None:
    sandbox.make_oem_target()
    t = sandbox.target
    (t / "etc/apt/preferences.d/00lindos-installer.pref").write_text("Package: ubiquity\n", encoding="utf-8")
    (t / "var/lib/lindos/installer-holds").write_text("ubiquity\ncasper\n", encoding="utf-8", newline="\n")
    (sandbox.state / "held").write_text("ubiquity\ncasper\nother\n", encoding="utf-8", newline="\n")
    (t / "var/cache/lindos-installer").mkdir(parents=True)
    _finalize(sandbox)
    # oem-config's units are in place and enabled, and it is the default target (oem-config-prepare, minus its harm)
    for unit in ("oem-config.service", "oem-config.target"):
        assert (t / "lib/systemd/system" / unit).read_text(encoding="utf-8") == (t / "usr/lib/oem-config" / unit).read_text(encoding="utf-8")
    systemctl = sandbox.calls_of("systemctl")
    assert any(f"--root={t.as_posix()} enable oem-config.service oem-config.target" in c for c in systemctl), systemctl
    assert any("set-default oem-config.target" in c for c in systemctl), systemctl
    assert _link_target(sandbox).endswith("oem-config.target")
    # the stale autologin of the temporary account is gone, the rest of lightdm.conf stays
    conf = (t / "etc/lightdm/lightdm.conf").read_text(encoding="utf-8")
    assert "autologin" not in conf and "[Seat:*]" in conf and "greeter-session=slick-greeter" in conf
    # the temporary account is locked and never gets the first-run wizard
    chroots = sandbox.calls_of("chroot")
    assert f"chroot {t.as_posix()} passwd -l oem" in chroots, chroots
    assert f"chroot {t.as_posix()} apt-mark unhold ubiquity casper" in chroots, chroots
    assert (t / "home/oem/.config/lindos/setup-done").is_file()
    # leftovers of the installer are removed: hook copy, version pin, holds, cache
    assert not (t / "usr/lib/ubiquity/target-config/50lindos-install").exists()
    assert not (t / "etc/apt/preferences.d/00lindos-installer.pref").exists()
    assert not (t / "var/cache/lindos-installer").exists()
    assert sandbox.held() == ["other"] and not (t / "var/lib/lindos/installer-holds").exists()
    assert not (t / "var/lib/lindos/oem-config-not-armed").exists()
    # the installer-only answer that allows an empty password is not left for the first-boot wizard's real account
    assert f"chroot {t.as_posix()} debconf-set-selections" in chroots, chroots
    assert (sandbox.state / "debconf-selections").read_text(encoding="utf-8") == "d-i user-setup/allow-password-empty boolean false\n"
    # steps the hook never recorded are pending for the silent retries
    assert sandbox.statuses() == {"browser": "pending", "drivers": "pending"}
    assert "did not record" in sandbox.step("browser")["detail"]
    log = (t / "var/log/lindos/installer.log").read_text(encoding="utf-8", errors="replace")
    assert "oem-config is armed" in log and "finalize: done" in log


def test_steps_the_hook_recorded_are_left_alone(sandbox: Sandbox) -> None:
    sandbox.make_oem_target()
    from lindos import installstate
    installstate.mark("browser", "done", "google-chrome-stable installed", root=str(sandbox.target))
    _finalize(sandbox)
    assert sandbox.statuses() == {"browser": "done", "drivers": "pending"}
    assert sandbox.step("browser")["detail"] == "google-chrome-stable installed"


def test_the_hook_and_the_finalisation_make_one_consistent_installation(sandbox: Sandbox) -> None:
    """What Ubiquity does in order: the hook downloads and installs, later the success command arms the wizard."""
    hook = sandbox.run_hook(FAKE_UPGRADES="libfoo", LINDOS_INSTALLER_STEPS="browser updates")
    assert hook.returncode == 0 and hook.stdout == "", hook.stderr[-1500:]
    assert sandbox.statuses() == {"browser": "done", "updates": "done"} and sandbox.held() == []
    sandbox.make_oem_target()
    _finalize(sandbox)
    # what the hook recorded is kept; only the step it never ran is pending (the silent retry / Settings picks it up)
    assert sandbox.statuses() == {"browser": "done", "updates": "done", "drivers": "pending"}
    assert (sandbox.target / "var/lib/lindos/browser-firstboot.done").is_file(), "the hook's marker survives the finalisation"
    assert _link_target(sandbox).endswith("oem-config.target")
    assert sandbox.held() == [] and not (sandbox.target / "etc/apt/preferences.d/00lindos-installer.pref").exists()
    log = (sandbox.target / "var/log/lindos/installer.log").read_text(encoding="utf-8", errors="replace")
    assert log.index("step browser: done") < log.index("finalize: start") < log.index("oem-config is armed")


def test_without_oem_config_nothing_is_locked_or_stripped(sandbox: Sandbox) -> None:
    sandbox.make_oem_target(with_oem_config=False)
    t = sandbox.target
    _finalize(sandbox)
    assert "autologin-user=oem" in (t / "etc/lightdm/lightdm.conf").read_text(encoding="utf-8"), \
        "the temporary account must stay usable: a machine nobody can log in to is worse"
    assert not any("passwd -l oem" in c for c in sandbox.call_log())
    assert not any(c.startswith("systemctl") for c in sandbox.call_log())
    assert not (t / "etc/systemd/system/default.target").exists() and not (t / "lib/systemd/system/oem-config.target").exists()
    assert "CRITICAL" in (t / "var/log/lindos/installer.log").read_text(encoding="utf-8", errors="replace")
    assert "oem-config is not in the new system" in (t / "var/lib/lindos/oem-config-not-armed").read_text(encoding="utf-8")
    assert not (t / "usr/lib/ubiquity/target-config/50lindos-install").exists(), "cleanup still happens"


def test_a_service_whose_program_is_missing_counts_as_not_installed(sandbox: Sandbox) -> None:
    sandbox.make_oem_target()
    (sandbox.target / "usr/sbin/oem-config-firstboot").unlink()
    _finalize(sandbox)
    assert (sandbox.target / "var/lib/lindos/oem-config-not-armed").is_file()
    assert "autologin-user=oem" in (sandbox.target / "etc/lightdm/lightdm.conf").read_text(encoding="utf-8")


def test_the_bundled_copy_is_installed_when_the_pool_did_not_deliver(sandbox: Sandbox) -> None:
    sandbox.make_oem_target(with_oem_config=False)
    debs = sandbox.root / "oem-debs"
    debs.mkdir()
    (debs / "oem-config_24.04.3+mint18_all.deb").write_bytes(b"!<arch>\n")
    _finalize(sandbox, FAKE_DPKG_INSTALLS_OEM="1")
    assert any(c.startswith("dpkg -i --force-confold") for c in sandbox.call_log())
    assert (sandbox.target / "lib/systemd/system/oem-config.target").is_file()
    assert "autologin" not in (sandbox.target / "etc/lightdm/lightdm.conf").read_text(encoding="utf-8")
    assert not (sandbox.target / "tmp/lindos-oem-debs").exists()


def test_outside_the_installer_nothing_is_done_and_nothing_is_created(sandbox: Sandbox) -> None:
    """The debconf answer that names this script is also read by Ubiquity's OEM first-boot pass on the installed system."""
    missing = sandbox.root / "no-target"
    proc = sandbox.run_finalize(LINDOS_TARGET=missing.as_posix(), LINDOS_INSTALLER_STEPS=TWO)
    assert proc.returncode == 0 and proc.stdout == "", proc.stderr[-1500:]
    assert not missing.exists(), "no /target may appear on a running system"
    assert not any(c.startswith(("systemctl", "chroot", "apt-mark")) for c in sandbox.call_log())
    assert "no installed system" in proc.stderr


def test_a_normal_installation_is_not_armed(sandbox: Sandbox) -> None:
    """No temporary 'oem' account: somebody installed without OEM mode - there is no wizard to arm."""
    sandbox.make_oem_target(with_user=False)
    _finalize(sandbox)
    assert not any(c.startswith(("systemctl", "chroot")) for c in sandbox.call_log())
    assert not (sandbox.target / "lib/systemd/system/oem-config.target").exists()
    assert "autologin-user=oem" in (sandbox.target / "etc/lightdm/lightdm.conf").read_text(encoding="utf-8")
    assert not (sandbox.target / "usr/lib/ubiquity/target-config/50lindos-install").exists()


def test_the_reset_never_sees_ubiquitys_own_debconf_variables(sandbox: Sandbox) -> None:
    """They may point at the live system's database: the change must land in the NEW system's."""
    sandbox.make_oem_target()
    _finalize(sandbox, DEBCONF_DB_REPLACE="live-db", DEBCONF_SYSTEMRC="/live/debconf.conf", DEBIAN_HAS_FRONTEND="1")
    assert (sandbox.state / "debconf-selections").is_file()
    assert (sandbox.state / "debconf-env").read_text(encoding="utf-8") == ""


def test_a_debconf_reset_that_fails_is_reported_and_changes_nothing_else(sandbox: Sandbox) -> None:
    sandbox.make_oem_target()
    _finalize(sandbox, FAKE_DEBCONF_RC="1")
    assert "could not reset user-setup/allow-password-empty" in (sandbox.target / "var/log/lindos/installer.log").read_text(encoding="utf-8", errors="replace")
    assert (sandbox.target / "lib/systemd/system/oem-config.target").is_file(), "the wizard is still armed"


def test_the_default_target_is_linked_by_hand_when_systemctl_did_nothing(sandbox: Sandbox) -> None:
    sandbox.make_oem_target()
    _finalize(sandbox, FAKE_SYSTEMCTL_NOOP="1", FAKE_SYSTEMCTL_RC="1")
    assert _link_target(sandbox) == "/lib/systemd/system/oem-config.target"
    assert "systemctl enable reported a problem" in (sandbox.target / "var/log/lindos/installer.log").read_text(encoding="utf-8", errors="replace")


def test_a_failing_passwd_is_reported_but_the_wizard_is_still_armed(sandbox: Sandbox) -> None:
    sandbox.make_oem_target()
    _finalize(sandbox, FAKE_PASSWD_RC="1")
    assert "could not lock the temporary account" in (sandbox.target / "var/log/lindos/installer.log").read_text(encoding="utf-8", errors="replace")
    assert (sandbox.target / "lib/systemd/system/oem-config.target").is_file()


def test_autologin_of_another_user_is_not_touched(sandbox: Sandbox) -> None:
    sandbox.make_oem_target(autologin=False)
    conf = sandbox.target / "etc/lightdm/lightdm.conf"
    conf.write_text("[Seat:*]\nautologin-user=alice\nautologin-user-timeout=0\n", encoding="utf-8", newline="\n")
    _finalize(sandbox)
    assert "autologin-user=alice" in conf.read_text(encoding="utf-8")


def test_holds_that_cannot_be_released_stay_listed_for_a_later_try(sandbox: Sandbox) -> None:
    sandbox.make_oem_target()
    (sandbox.target / "var/lib/lindos/installer-holds").write_text("ubiquity\n", encoding="utf-8", newline="\n")
    _finalize(sandbox, FAKE_APT_MARK_FAIL="1")
    assert (sandbox.target / "var/lib/lindos/installer-holds").is_file()
    assert "could not release the package holds" in (sandbox.target / "var/log/lindos/installer.log").read_text(encoding="utf-8", errors="replace")


def test_finalize_source_is_short_guarded_and_never_fails() -> None:
    text = (LIBEXEC / "finalize.sh").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert code.rstrip().endswith("exit 0")
    assert "set -e" not in code and "set -u" not in code
    # nothing in the arming path can hang the installer window: every call has a time limit
    for line in code.splitlines():
        if re.search(r"systemctl --root|chroot [\"$]", line):
            assert "timeout -k" in line, line
    assert "apt-get" not in code, "no package work in the success command (it freezes the installer window)"
    # the essentials of oem-config-prepare WITHOUT its deletion of the saved network connections
    assert "system-connections" not in code and "70-persistent" not in code
