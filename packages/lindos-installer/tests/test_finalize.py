"""finalize.sh, the installer's ubiquity/success_command, run for real against a fake /target.

It runs synchronously in Ubiquity's GTK thread (the window freezes meanwhile), so it has to be short,
must never hang, and must fail SAFE: arming oem-config only when oem-config is really in the new system.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from installer_testlib import LIBEXEC, Sandbox, needs_bash

pytestmark = needs_bash

TWO = "browser drivers"
RESET_SEED = "d-i user-setup/allow-password-empty boolean false\n"
# what the armed path adds: the wizard's window title and the command that removes its theme drop-in afterwards
WIZARD_SEED = ("ubiquity ubiquity/custom_title_text string Lindos Setup\n"
               "oem-config oem-config/late_command string rm -rf /etc/systemd/system/oem-config.service.d\n")
DROPIN = "etc/systemd/system/oem-config.service.d/10-lindos.conf"
NOTICE = "home/oem/Desktop/LINDOS-ACCOUNT-SETUP-FAILED.txt"


def _finalize(sb: Sandbox, **env: str):
    proc = sb.run_finalize(LINDOS_INSTALLER_STEPS=TWO, **env)
    assert proc.returncode == 0, proc.stderr[-2500:]
    assert proc.stdout == "", "the success command wrote to stdout: %r" % proc.stdout[:200]
    return proc


def _installer_log(sb: Sandbox) -> str:
    return (sb.target / "var/log/lindos/installer.log").read_text(encoding="utf-8", errors="replace")


def _shadow_field(sb: Sandbox) -> str:
    for line in (sb.target / "etc/shadow").read_text(encoding="utf-8").splitlines():
        if line.startswith("oem:"):
            return line.split(":")[1]
    raise AssertionError("no oem entry in the fake shadow file")


def _generated_password(sb: Sandbox) -> str:
    """What the finalizer handed to chpasswd on stdin ('oem:PASSWORD'), the only place a password may travel."""
    lines = [ln for ln in (sb.state / "chpasswd-stdin").read_text(encoding="utf-8").splitlines() if ln]
    assert len(lines) == 1 and lines[0].startswith("oem:"), lines
    return lines[0][len("oem:"):]


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
    assert (t / "usr/lib/ubiquity/dm-scripts/install/50lindos-noblank").is_file()
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
    assert not (t / "usr/lib/ubiquity/dm-scripts").exists(), "the ubiquity-dm hook (X blanking) is only useful in the installer"
    assert not (t / "etc/apt/preferences.d/00lindos-installer.pref").exists()
    assert not (t / "var/cache/lindos-installer").exists()
    assert sandbox.held() == ["other"] and not (t / "var/lib/lindos/installer-holds").exists()
    assert not (t / "var/lib/lindos/oem-config-not-armed").exists()
    # the installer-only answer that allows an empty password is not left for the first-boot wizard's real account
    assert f"chroot {t.as_posix()} debconf-set-selections" in chroots, chroots
    assert (sandbox.state / "debconf-selections").read_text(encoding="utf-8") == RESET_SEED + WIZARD_SEED
    # the installer's own GTK_THEME drop-in is gone (the installed system never runs ubiquity.service again)
    assert not (t / "etc/systemd/system/ubiquity.service.d").exists()
    # steps the hook never recorded are pending for the silent retries
    assert sandbox.statuses() == {"browser": "pending", "drivers": "pending"}
    assert "did not record" in sandbox.step("browser")["detail"]
    log = (t / "var/log/lindos/installer.log").read_text(encoding="utf-8", errors="replace")
    assert "oem-config is armed" in log and "finalize: done" in log


def test_steps_the_hook_recorded_are_left_alone(sandbox: Sandbox) -> None:
    sandbox.make_oem_target()
    from lindos import installstate
    installstate.mark("browser", "done", "google-chrome-stable installed", root=str(sandbox.target))
    (sandbox.state / "installed").write_text((sandbox.state / "installed").read_text(encoding="utf-8") + "google-chrome-stable\n",
                                             encoding="utf-8", newline="\n")
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


def test_without_oem_config_the_desktop_stays_but_the_temporary_account_is_not_left_open(sandbox: Sandbox) -> None:
    """The fallback: no first-boot wizard, so the machine keeps signing in as 'oem' - whose password the installer's
    page told the user to leave EMPTY, in the sudo group, forever.  The desktop stays usable, the account is closed."""
    sandbox.make_oem_target(with_oem_config=False)
    t = sandbox.target
    assert _shadow_field(sandbox) == "", "the fixture starts with the empty password the installer page asks for"
    proc = _finalize(sandbox)
    # a machine nobody can log in to is worse: the autologin and everything else of the desktop stay as they are
    assert "autologin-user=oem" in (t / "etc/lightdm/lightdm.conf").read_text(encoding="utf-8")
    assert not any(c.startswith("systemctl") for c in sandbox.call_log())
    assert not (t / "etc/systemd/system/default.target").exists() and not (t / "lib/systemd/system/oem-config.target").exists()
    assert not (t / "usr/lib/ubiquity/target-config/50lindos-install").exists(), "cleanup still happens"
    # ... but the account is not open: a random password went to chpasswd (stdin only), nothing was locked
    pw = _generated_password(sandbox)
    assert re.fullmatch(r"[A-Za-z0-9]{18}", pw), pw
    assert _shadow_field(sandbox) not in ("", "!"), "the empty password is gone"
    assert f"chroot {t.as_posix()} chpasswd" in sandbox.calls_of("chroot")
    assert not any("passwd -l oem" in c for c in sandbox.call_log()), "locking would strand the screen-lock unlock"
    # the reason is recorded (first line, as before), with what was done to the account (no secret in it)
    marker = (t / "var/lib/lindos/oem-config-not-armed").read_text(encoding="utf-8").splitlines()
    assert marker[0] == "oem-config is not in the new system and no bundled copy could be installed", marker
    assert marker[1] == "temporary account: the temporary account got a random password", marker
    # loud: CRITICAL in the log, and the person at the keyboard is told on the desktop of the account
    log = _installer_log(sandbox)
    assert "CRITICAL" in log and "temporary 'oem' account" in log and "random password" in log
    notice = (t / NOTICE).read_text(encoding="utf-8")
    assert pw in notice and "passwd" in notice and marker[0] in notice
    # the password lives in the root-only file and on the account's own desktop - nowhere else
    assert (t / "var/lib/lindos/oem-temporary-password").read_text(encoding="utf-8") == pw + "\n"
    for where in (log, sandbox.log_text(), marker[0] + marker[1], proc.stdout, proc.stderr, "\n".join(sandbox.call_log())):
        assert pw not in where
    if os.name != "nt":
        assert (t / "var/lib/lindos/oem-temporary-password").stat().st_mode & 0o777 == 0o600
        assert (t / NOTICE).stat().st_mode & 0o777 == 0o600
    # the installer-only answer is reset on this path too
    assert (sandbox.state / "debconf-selections").read_text(encoding="utf-8") == RESET_SEED


def test_a_password_the_user_chose_is_kept_and_a_locked_account_stays_locked(sandbox: Sandbox) -> None:
    sandbox.make_oem_target(with_oem_config=False, shadow="oem:$6$user$chosen:19000:0:99999:7:::")
    _finalize(sandbox)
    assert _shadow_field(sandbox) == "$6$user$chosen"
    assert not sandbox.calls_of("chpasswd") and not any("passwd -l" in c for c in sandbox.call_log())
    marker = (sandbox.target / "var/lib/lindos/oem-config-not-armed").read_text(encoding="utf-8")
    assert "keeps the password chosen during the installation" in marker
    notice = (sandbox.target / NOTICE).read_text(encoding="utf-8")
    assert "keeps the password chosen" in notice and "$6$" not in notice
    assert not (sandbox.target / "var/lib/lindos/oem-temporary-password").exists()


def test_a_locked_temporary_account_gets_a_password_when_it_is_the_only_account(sandbox: Sandbox) -> None:
    # the image seed creates the temporary account LOCKED (pre-crypted '!'): in the normal path the wizard deletes it, but when
    # oem-config could not be armed it is the person's only account, and a locked one would leave them without sudo or screen lock
    sandbox.make_oem_target(with_oem_config=False, shadow="oem:!:19000:0:99999:7:::")
    _finalize(sandbox)
    assert _shadow_field(sandbox) != "!" and sandbox.calls_of("chpasswd")
    assert (sandbox.target / "var/lib/lindos/oem-temporary-password").exists()
    marker = (sandbox.target / "var/lib/lindos/oem-config-not-armed").read_text(encoding="utf-8")
    assert "random password" in marker


def test_a_locked_temporary_account_is_left_alone_when_the_wizard_is_armed(sandbox: Sandbox) -> None:
    sandbox.make_oem_target(with_oem_config=True, shadow="oem:!:19000:0:99999:7:::")
    _finalize(sandbox)
    assert _shadow_field(sandbox).startswith("!") and not sandbox.calls_of("chpasswd")   # still locked ('passwd -l' may stack another '!')
    assert not (sandbox.target / "var/lib/lindos/oem-temporary-password").exists()


def test_when_chpasswd_fails_the_temporary_account_is_locked_instead(sandbox: Sandbox) -> None:
    sandbox.make_oem_target(with_oem_config=False)
    _finalize(sandbox, FAKE_CHPASSWD_RC="1")
    assert any("passwd -l oem" in c for c in sandbox.call_log())
    assert _shadow_field(sandbox) == "!"
    assert "autologin-user=oem" in (sandbox.target / "etc/lightdm/lightdm.conf").read_text(encoding="utf-8")
    assert not (sandbox.target / "var/lib/lindos/oem-temporary-password").exists(), "no password was set: none is kept"
    log = _installer_log(sandbox)
    assert "could not give the temporary account a random password" in log and "CRITICAL" in log
    marker = (sandbox.target / "var/lib/lindos/oem-config-not-armed").read_text(encoding="utf-8")
    assert "temporary account: the temporary account is locked" in marker
    assert "is locked" in (sandbox.target / NOTICE).read_text(encoding="utf-8")


def test_when_the_tools_of_the_new_system_fail_the_shadow_file_is_edited_directly(sandbox: Sandbox) -> None:
    sandbox.make_oem_target(with_oem_config=False)
    _finalize(sandbox, FAKE_CHPASSWD_RC="1", FAKE_PASSWD_RC="1")
    assert _shadow_field(sandbox) == "!"
    assert "by editing /etc/shadow" in (sandbox.target / "var/lib/lindos/oem-config-not-armed").read_text(encoding="utf-8")
    assert "root:*:19000" in (sandbox.target / "etc/shadow").read_text(encoding="utf-8"), "the rest of the file is untouched"


def test_an_account_that_cannot_be_closed_is_reported_critical_and_the_finalizer_still_ends(sandbox: Sandbox) -> None:
    sandbox.make_oem_target(with_oem_config=False, shadow=None)
    proc = _finalize(sandbox, FAKE_CHPASSWD_RC="1", FAKE_PASSWD_RC="1")
    assert "may still have an EMPTY password" in (sandbox.target / "var/lib/lindos/oem-config-not-armed").read_text(encoding="utf-8")
    assert "could neither set a password nor lock it" in _installer_log(sandbox) and "finalize: done" in _installer_log(sandbox)
    assert proc.stdout == ""


def test_a_random_source_that_gives_nothing_never_yields_a_short_or_empty_password(sandbox: Sandbox) -> None:
    sandbox.make_oem_target(with_oem_config=False)
    empty = sandbox.root / "no-randomness"
    empty.write_bytes(b"")
    _finalize(sandbox, LINDOS_URANDOM=empty.as_posix())
    assert not sandbox.calls_of("chpasswd"), "an empty password must never reach chpasswd"
    assert _shadow_field(sandbox) == "!"


def test_two_fallback_installs_never_share_a_password(sandbox: Sandbox) -> None:
    sandbox.make_oem_target(with_oem_config=False)
    _finalize(sandbox)
    other = Sandbox(sandbox.root / "second")
    other.make_oem_target(with_oem_config=False)
    _finalize(other)
    assert _generated_password(sandbox) != _generated_password(other)


def test_a_service_whose_program_is_missing_counts_as_not_installed(sandbox: Sandbox) -> None:
    sandbox.make_oem_target()
    (sandbox.target / "usr/sbin/oem-config-firstboot").unlink()
    _finalize(sandbox)
    assert (sandbox.target / "var/lib/lindos/oem-config-not-armed").is_file()
    assert "autologin-user=oem" in (sandbox.target / "etc/lightdm/lightdm.conf").read_text(encoding="utf-8")
    assert _shadow_field(sandbox) != "", "the fallback closes the account whatever the reason for it was"


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
    assert not any(c.startswith(("systemctl", "passwd", "chpasswd", "apt-mark")) for c in sandbox.call_log())
    # the only thing done in the new system: the installer-only debconf answer is reset (it was baked for the
    # temporary account's page; whichever account the installer made, nothing should read 'empty is fine' later)
    assert sandbox.calls_of("chroot") == [f"chroot {sandbox.target.as_posix()} debconf-set-selections"]
    assert (sandbox.state / "debconf-selections").read_text(encoding="utf-8") == RESET_SEED
    assert not (sandbox.target / "lib/systemd/system/oem-config.target").exists()
    assert "autologin-user=oem" in (sandbox.target / "etc/lightdm/lightdm.conf").read_text(encoding="utf-8")
    assert not (sandbox.target / "usr/lib/ubiquity/target-config/50lindos-install").exists()
    assert not (sandbox.target / "usr/lib/ubiquity/dm-scripts").exists()


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
    # a password only ever travels on stdin: never as an argument of a command, never through li_log
    assert "printf '%s:%s\\n' \"$1\" \"$2\" | timeout -k" in code
    assert not re.search(r"li_log[^\n]*(\$\{?pw\b|FIN_TEMP_PW)", code)
    assert not re.search(r"(chpasswd|passwd)[^|\n]*\$\{?pw\b", code)
    # the essentials of oem-config-prepare WITHOUT its deletion of the saved network connections
    assert "system-connections" not in code and "70-persistent" not in code


# --------------------------------------------------------------------------- the look of the account wizard
def test_the_wizard_gets_the_lindos_skin_through_a_drop_in_a_title_and_a_way_to_remove_it_again(sandbox: Sandbox) -> None:
    sandbox.make_oem_target()
    _finalize(sandbox)
    dropin = (sandbox.target / DROPIN).read_text(encoding="utf-8")
    # the unit's environment reaches ubiquity-dm and the GTK process (start-ubiquity-dm/oem-config-firstboot do not clear
    # it; ubiquity-dm only adds to os.environ), and GTK honours GTK_THEME over xsettings and settings.ini
    assert "[Service]\nEnvironment=GTK_THEME=Lindos-Setup\n" in dropin
    seeds = (sandbox.state / "debconf-selections").read_text(encoding="utf-8")
    assert "ubiquity ubiquity/custom_title_text string Lindos Setup\n" in seeds, "the window title is Ubiquity's own answer"
    late = [ln for ln in seeds.splitlines() if ln.startswith("oem-config oem-config/late_command string ")]
    assert late == ["oem-config oem-config/late_command string rm -rf /etc/systemd/system/oem-config.service.d"], (
        "the wizard removes the drop-in itself when it has finished (root shell, before the packages are purged)")
    # the drop-in is not owned by lindos-installer (Ubiquity purges it): nothing but our own file in that directory
    assert [p.name for p in (sandbox.target / "etc/systemd/system/oem-config.service.d").iterdir()] == ["10-lindos.conf"]
    assert "Lindos-Setup GTK skin" in _installer_log(sandbox)


def test_without_the_skin_there_is_no_useless_theme_drop_in_but_the_title_is_still_set(sandbox: Sandbox) -> None:
    sandbox.make_oem_target()
    import shutil
    shutil.rmtree(sandbox.target / "usr/share/themes/Lindos-Setup")
    _finalize(sandbox)
    assert not (sandbox.target / DROPIN).exists()
    assert "Lindos-Setup GTK skin is not in the new system" in _installer_log(sandbox)
    assert "ubiquity/custom_title_text string Lindos Setup" in (sandbox.state / "debconf-selections").read_text(encoding="utf-8")
    assert (sandbox.target / "lib/systemd/system/oem-config.target").is_file(), "the wizard is armed all the same"


def test_a_wizard_that_could_not_be_armed_gets_no_look_and_no_cleanup_command(sandbox: Sandbox) -> None:
    sandbox.make_oem_target(with_oem_config=False)
    _finalize(sandbox)
    assert not (sandbox.target / DROPIN).exists()
    assert (sandbox.state / "debconf-selections").read_text(encoding="utf-8") == RESET_SEED


# --------------------------------------------------------------------------- a step that said done must still be true
def _done(sandbox: Sandbox, step: str, detail: str = "") -> None:
    from lindos import installstate
    installstate.mark(step, "done", detail, root=str(sandbox.target))


def _installed(sandbox: Sandbox, *names: str) -> None:
    f = sandbox.state / "installed"
    f.write_text(f.read_text(encoding="utf-8") + "".join(n + "\n" for n in names), encoding="utf-8", newline="\n")


def test_a_step_whose_result_was_removed_again_before_the_end_is_failed_not_done(sandbox: Sandbox) -> None:
    """Ubiquity's own package clean-up runs after the hook; whatever a step called 'done' is looked at again."""
    sandbox.make_oem_target()
    _done(sandbox, "browser", "google-chrome-stable installed")
    _done(sandbox, "gaming", "steam lutris")
    (sandbox.target / "var/lib/lindos/browser-firstboot.done").write_text("", encoding="utf-8")
    _installed(sandbox, "lutris")              # Chrome and Steam are gone, Lutris is not
    proc = sandbox.run_finalize(LINDOS_INSTALLER_STEPS="browser gaming")
    assert proc.returncode == 0 and proc.stdout == "", proc.stderr[-1500:]
    assert sandbox.statuses() == {"browser": "failed", "gaming": "failed"}
    assert "google-chrome-stable" in sandbox.step("browser")["detail"]
    assert "steam" in sandbox.step("gaming")["detail"] and "lutris" not in sandbox.step("gaming")["detail"]
    assert not (sandbox.target / "var/lib/lindos/browser-firstboot.done").exists(), "no marker: the silent retry installs Chrome again"
    assert "something removed it again" in _installer_log(sandbox)


def test_extras_that_are_still_installed_stay_done_and_excused_ones_are_not_missed(sandbox: Sandbox) -> None:
    import json
    from installer_testlib import SHARE
    extras = json.loads((SHARE / "extras.json").read_text(encoding="utf-8"))
    sandbox.make_oem_target()
    _installed(sandbox, *[p for p in extras["apt"] if p != "ananicy-cpp"])
    _done(sandbox, "mode_extras", "22 packages installed; not in the archives: ananicy-cpp")
    proc = sandbox.run_finalize(LINDOS_INSTALLER_STEPS="mode_extras")
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert sandbox.statuses() == {"mode_extras": "done"}
    # ... and one missing package is named
    (sandbox.state / "installed").write_text((sandbox.state / "installed").read_text(encoding="utf-8").replace("krita\n", ""),
                                             encoding="utf-8", newline="\n")
    assert sandbox.run_finalize(LINDOS_INSTALLER_STEPS="mode_extras").returncode == 0
    assert sandbox.statuses() == {"mode_extras": "failed"} and "krita" in sandbox.step("mode_extras")["detail"]


def test_a_package_list_that_cannot_be_read_never_turns_a_result_into_a_failure(sandbox: Sandbox) -> None:
    sandbox.make_oem_target()
    _done(sandbox, "browser", "google-chrome-stable installed")
    proc = sandbox.run_finalize(LINDOS_INSTALLER_STEPS="browser", FAKE_DPKG_QUERY_FAIL="1")
    assert proc.returncode == 0
    assert sandbox.statuses() == {"browser": "done"}
    assert "cannot be read" in _installer_log(sandbox)


# --------------------------------------------------------------------------- /run
def test_the_run_leftovers_of_ubiquitys_own_user_setup_are_tidied_and_nothing_else_is_touched(sandbox: Sandbox) -> None:
    """user-setup-apply runs 'mount' and adduser in a bare chroot before /run is bound: /run/mount and /run/adduser stay."""
    sandbox.make_oem_target()
    run = sandbox.target / "run"
    for name in ("adduser", "mount", "keepme"):
        (run / name).mkdir(parents=True)
    _finalize(sandbox)
    assert sorted(p.name for p in run.iterdir()) == ["keepme"], "only what Ubiquity's own tools are known to leave"


def test_a_run_that_is_a_mount_is_left_alone(sandbox: Sandbox) -> None:
    """A bind mount of the LIVE /run must never be emptied: the device number of /run differs from the target's."""
    sandbox.make_oem_target()
    run = sandbox.target / "run"
    (run / "adduser").mkdir(parents=True)
    shim = sandbox.root / "shim"
    shim.mkdir()
    # 'stat -c %d PATH': the target's /run reports another device than the target itself
    (shim / "stat").write_text(
        '#!/bin/bash\ncase "$*" in *"%d"*"/run") echo 99 ;; *"%d"*) echo 1 ;; *) exec /usr/bin/stat "$@" ;; esac\n',
        encoding="utf-8", newline="\n")
    (shim / "stat").chmod(0o755)
    proc = sandbox.run_finalize(LINDOS_INSTALLER_STEPS=TWO, PATH=str(shim) + os.pathsep + sandbox.env()["PATH"])
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert (run / "adduser").is_dir()
    assert "left alone" in _installer_log(sandbox)
