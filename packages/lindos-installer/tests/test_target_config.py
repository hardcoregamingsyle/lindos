"""The Ubiquity target-config hook (target-config.sh), run for real against a fake /target.

Every scenario proves part of the contract in the hook's header: it ALWAYS exits 0, never prints to
stdout (Ubiquity's debconf pipe), releases the package holds on every exit path, records honest
install-state ('done' only after a verified success), never leaves the target half-configured or with
leftovers, and degrades to 'pending' when offline, out of time, out of space or unable to enter the target.

Most scenarios run only the steps they are about (LINDOS_INSTALLER_STEPS), because every fake command
costs a process start; one module-wide run covers all seven steps.
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

import pytest
from installer_testlib import ALL_STEPS, LIBEXEC, Sandbox, needs_bash

pytestmark = needs_bash

UPGRADES = "libfoo libbar linux-image-6.14.0-lindos grub-common ubiquity firefox-locale-de"
TWO = "browser drivers"


def _ok(proc: "subprocess.CompletedProcess[str]") -> None:
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert proc.stdout == "", "the hook wrote to stdout (Ubiquity's debconf pipe): %r" % proc.stdout[:300]


@pytest.fixture(scope="module")
def full(tmp_path_factory: pytest.TempPathFactory):
    """One complete, successful run of all seven steps (shared by the tests that only read it)."""
    sb = Sandbox(tmp_path_factory.mktemp("full"))
    proc = sb.run_hook(FAKE_UPGRADES=UPGRADES, FAKE_NO_CANDIDATE="ananicy-cpp")
    return sb, proc


# --------------------------------------------------------------------------- the good path
def test_full_success_installs_everything_and_records_it(full) -> None:
    sb, proc = full
    _ok(proc)
    assert sb.statuses() == {s: "done" for s in ALL_STEPS}, (sb.statuses(), sb.log_text()[-2500:])
    assert sb.install_state()["online"] is True
    apt = sb.calls_of("apt-get")
    for verb in ("apt-get -q -s upgrade", "apt-get -y -q -d upgrade", "apt-get -y -q --no-download upgrade"):
        assert sum(1 for c in apt if c.startswith(verb)) == 1, (verb, apt)
    assert sb.upgraded() == ["libfoo", "libbar"], "the held families and what Ubiquity removes stay as they are"
    assert not any("dist-upgrade" in c or "full-upgrade" in c or "--only-upgrade" in c for c in sb.call_log())
    assert "2 packages upgraded" in sb.step("updates")["detail"]
    assert "ananicy-cpp" in sb.step("mode_extras")["detail"], "an extra no archive carries is named, not a failure"
    installed = (sb.state / "installed").read_text(encoding="utf-8").split()
    assert "gimp" in installed and "ananicy-cpp" not in installed and "google-chrome-stable" in installed
    assert set((sb.state / "flatpaks").read_text(encoding="utf-8").split()) == {
        "com.usebottles.bottles", "com.heroicgameslauncher.hgl", "org.prismlauncher.PrismLauncher",
        "org.vinegarhq.Sober"}
    assert (sb.target / "var/lib/lindos/browser-firstboot.done").is_file()
    assert (sb.target / "var/lib/lindos/driver-firstboot.done").is_file()
    assert not (sb.target / "var/lib/lindos/driver-proprietary-consent").exists()


def test_steps_run_in_order_and_every_download_precedes_its_install(full) -> None:
    sb, _proc = full
    calls = sb.call_log()
    marks = {}
    for key, prefix in (("browser", "install-browser.sh chrome --in-installer --download-only"),
                        ("drivers", "ubuntu-drivers install"), ("updates", "apt-get -y -q -d upgrade"),
                        ("compat", "install-compat.sh --in-installer --download-only"),
                        ("gaming", "install-gaming.sh --in-installer --download-only"),
                        ("extras", "apt-cache policy gimp"), ("flatpaks", "flatpak remote-add")):
        marks[key] = next((i for i, c in enumerate(calls) if c.startswith(prefix) or (prefix == "apt-cache policy gimp" and c.startswith("apt-cache policy") and "gimp" in c)), -1)
        assert marks[key] > 0, key
    order = sorted(marks, key=lambda k: marks[k])
    assert order == ["browser", "drivers", "updates", "compat", "gaming", "extras", "flatpaks"]
    for script in ("install-browser.sh", "install-compat.sh", "install-gaming.sh"):
        mine = [c for c in calls if c.startswith(script)]
        assert len(mine) == 2 and "--in-installer --download-only" in mine[0] and "--in-installer --no-download" in mine[1], mine
    assert sb.first_index("apt-get -y -q -d upgrade") < sb.first_index("apt-get -y -q --no-download upgrade")


def test_holds_pin_apt_conf_and_mounts_lifecycle(full) -> None:
    sb, _proc = full
    calls = sb.call_log()
    hold, update, unhold = sb.first_index("apt-mark hold"), sb.first_index("apt-get -y -q update"), sb.last_index("apt-mark unhold")
    assert 0 <= hold < update < unhold
    held_words = calls[hold].split()
    for family in ("ubiquity", "ubiquity-frontend-gtk", "casper", "linux-image-6.14.0-lindos", "grub-efi-amd64-signed",
                   "shim-signed", "initramfs-tools", "plymouth"):
        assert family in held_words, family
    for other in ("libfoo", "libplymouth5", "libreoffice-writer", "firefox", "flatpak", "linux-firmware"):
        assert other not in held_words[3:], other
    # what Ubiquity removes anyway is held in a second call, only for the updates step (and released right after it)
    holds = [c for c in calls if c.startswith("apt-mark hold")]
    assert len(holds) == 2, holds
    for removed_anyway in ("firefox-locale-de", "language-pack-de"):
        assert removed_anyway not in held_words, "%s must not be held for the whole hook" % removed_anyway
        assert removed_anyway in holds[1].split(), "%s is in filesystem.manifest-remove: no point upgrading it" % removed_anyway
    assert "ubiquity" not in holds[1].split()[3:], "a family is held once"
    unholds = [c for c in calls if c.startswith("apt-mark unhold")]
    assert unholds[0].split()[2:] == holds[1].split()[2:], "the same packages are released as were held for the updates step"
    assert sb.held() == [] and not (sb.target / "var/lib/lindos/installer-holds").exists()
    pin = (sb.target / "etc/apt/preferences.d/00lindos-installer.pref").read_text(encoding="utf-8")
    assert "Package: ubiquity ubiquity-* oem-config oem-config-*" in pin
    assert "Pin: version 24.04.3+mint18" in pin and "Pin-Priority: 1001" in pin
    conf = (sb.state / "apt.conf.seen").read_text(encoding="utf-8")
    assert 'Dir::Etc::SourceList "/dev/null";' in conf and 'APT::Get::List-Cleanup "0";' in conf
    assert 'DPkg::Lock::Timeout "120";' in conf and '"--force-confold"' in conf
    assert 'APT::Update::Error-Mode "any";' in conf, "stock apt-get update exits 0 after transient fetch failures"
    assert not (sb.target / "var/lib/lindos/installer-apt.conf").exists()
    assert not (sb.target / "usr/sbin/policy-rc.d").exists()
    assert (sb.target / "etc/resolv.conf").read_text(encoding="utf-8") == "# original resolv.conf\n"
    assert sb.calls_of("apt-get")[-1].endswith("clean")
    assert (sb.target / "var/log/lindos/installer.log").is_file()
    tlog = (sb.target / "var/log/lindos/installer.log").read_text(encoding="utf-8", errors="replace")
    assert "step browser: done" in tlog and "cleaning up" in tlog


def test_the_hook_runs_once_under_the_inhibitor(full) -> None:
    sb, _proc = full
    assert len(sb.calls_of("systemd-inhibit")) == 1
    assert "--what=sleep:idle:handle-lid-switch --mode=block" in sb.calls_of("systemd-inhibit")[0]
    assert sb.log_text().count("start (version") == 1, "the body must not run twice"


# --------------------------------------------------------------------------- offline, disabled, no time, no room
def test_offline_marks_everything_pending_quickly_and_touches_nothing(sandbox: Sandbox) -> None:
    _ok(sandbox.run_hook(FAKE_NET_RC="4"))
    assert sandbox.statuses() == {s: "pending" for s in ALL_STEPS}
    assert all("offline" in sandbox.step(s)["detail"] for s in ALL_STEPS)
    assert sandbox.install_state()["online"] is False
    assert sandbox.calls_of("apt-get") == [] and sandbox.calls_of("apt-mark") == []
    assert not (sandbox.target / "var/lib/lindos/browser-firstboot.done").exists()
    assert not (sandbox.target / "var/lib/lindos/driver-firstboot.done").exists()
    assert not (sandbox.target / "etc/apt/preferences.d/00lindos-installer.pref").exists()


def test_disabled_on_the_kernel_command_line(sandbox: Sandbox) -> None:
    sandbox.set_cmdline("boot=casper lindos.install=off quiet")
    _ok(sandbox.run_hook(LINDOS_INSTALLER_STEPS=TWO))
    assert sandbox.statuses() == {"browser": "skipped", "drivers": "skipped"}
    assert sandbox.calls_of("apt-get") == []


def test_budget_from_the_kernel_command_line_and_no_time_left(sandbox: Sandbox) -> None:
    sandbox.set_cmdline("boot=casper lindos.install_budget=0")
    _ok(sandbox.run_hook(LINDOS_INSTALLER_STEPS=TWO))
    assert sandbox.statuses() == {"browser": "pending", "drivers": "pending"}
    assert all("time" in sandbox.step(s)["detail"] for s in ("browser", "drivers"))
    assert not any(c.startswith(("install-browser.sh", "ubuntu-drivers")) for c in sandbox.call_log())
    assert sandbox.held() == []


def test_not_enough_disk_space_is_pending_not_failed(sandbox: Sandbox) -> None:
    _ok(sandbox.run_hook(LINDOS_FREE_KB="1000"))
    assert sandbox.statuses() == {s: "pending" for s in ALL_STEPS}
    assert all("disk space" in sandbox.step(s)["detail"] for s in ALL_STEPS)
    assert not any(c.startswith("install-") for c in sandbox.call_log())


def test_dry_run_mode_does_nothing(sandbox: Sandbox) -> None:
    _ok(sandbox.run_hook(LINDOS_DRY_RUN="1"))
    assert sandbox.call_log() == [] and sandbox.statuses() == {}


def test_no_target_system_means_pending_and_exit_zero(sandbox: Sandbox) -> None:
    (sandbox.target / "usr/bin/apt-get").unlink()
    _ok(sandbox.run_hook(LINDOS_INSTALLER_STEPS=TWO))
    assert sandbox.statuses() == {"browser": "pending", "drivers": "pending"}
    assert sandbox.calls_of("apt-mark") == []


def test_without_a_target_directory_nothing_is_created(sandbox: Sandbox) -> None:
    missing = sandbox.root / "no-target"
    proc = sandbox.run_hook(LINDOS_TARGET=missing.as_posix(), LINDOS_INSTALLER_STEPS=TWO)
    _ok(proc)
    assert not missing.exists()
    assert sandbox.calls_of("apt-get") == [] and sandbox.calls_of("apt-mark") == []
    assert "no installed system" in proc.stderr


def test_cannot_enter_the_target_is_pending(sandbox: Sandbox) -> None:
    _ok(sandbox.run_hook(FAKE_ENTER_FAIL="1", LINDOS_INSTALLER_STEPS=TWO))
    assert sandbox.statuses() == {"browser": "pending", "drivers": "pending"}
    assert "could not enter" in sandbox.step("browser")["detail"]


def test_without_a_runner_it_enters_through_unshare(sandbox: Sandbox) -> None:
    _ok(sandbox.run_hook(LINDOS_TARGET_RUNNER="", FAKE_UNSHARE_FAIL="1", LINDOS_INSTALLER_STEPS=TWO))
    assert sandbox.statuses() == {"browser": "pending", "drivers": "pending"}
    unshare = sandbox.calls_of("unshare")
    assert unshare and "--mount --propagation private --" in unshare[0] and "--enter" in unshare[0]


def test_lists_that_cannot_be_refreshed_stop_the_package_steps_but_not_the_install(sandbox: Sandbox) -> None:
    for f in (sandbox.target / "var/lib/apt/lists").iterdir():
        f.unlink()
    _ok(sandbox.run_hook(FAKE_UPDATE_RC="100", LINDOS_INSTALLER_STEPS=TWO))
    assert sandbox.statuses() == {"browser": "pending", "drivers": "pending"}
    assert "package lists" in sandbox.step("browser")["detail"] and sandbox.held() == []


def test_the_media_lists_alone_are_not_a_refreshed_archive(sandbox: Sandbox) -> None:
    """apt-setup leaves 'cdrom:' lists in the target: with only those, 'nothing to upgrade' would be a lie."""
    lists = sandbox.target / "var/lib/apt/lists"
    for f in lists.iterdir():
        f.unlink()
    # a real one is named 'cdrom:[...]_dists_...' (a ':' is not a file name character on every host running these tests)
    (lists / "cdrom_5bLindos_5d_dists_noble_main_binary-amd64_Packages").write_text("Package: x\n", encoding="utf-8")
    _ok(sandbox.run_hook(FAKE_UPDATE_RC="100", LINDOS_INSTALLER_STEPS="browser updates"))
    assert sandbox.statuses() == {"browser": "pending", "updates": "pending"}
    assert "package lists" in sandbox.step("updates")["detail"]
    assert not any(c.startswith(("install-browser.sh", "apt-get -q -s upgrade")) for c in sandbox.call_log())


def test_partly_refreshed_lists_never_make_an_up_to_date_or_not_available_claim(sandbox: Sandbox) -> None:
    _ok(sandbox.run_hook(FAKE_UPDATE_RC="100", FAKE_UPGRADES="libfoo", FAKE_NO_CANDIDATE="ananicy-cpp",
                         LINDOS_INSTALLER_STEPS="updates mode_extras"))
    updates, extras = sandbox.step("updates"), sandbox.step("mode_extras")
    assert sandbox.upgraded() == ["libfoo"], "what the lists did show is still installed"
    assert updates["status"] == "pending" and "partly refreshed" in updates["detail"] and "1 packages upgraded" in updates["detail"]
    assert extras["status"] == "pending" and "partly refreshed" in extras["detail"] and "ananicy-cpp" in extras["detail"]
    assert "gimp" in (sandbox.state / "installed").read_text(encoding="utf-8").split()
    # complete lists: the same run is a plain success
    other = Sandbox(sandbox.root / "second")
    _ok(other.run_hook(FAKE_UPGRADES="libfoo", FAKE_NO_CANDIDATE="ananicy-cpp", LINDOS_INSTALLER_STEPS="updates mode_extras"))
    assert other.statuses() == {"updates": "done", "mode_extras": "done"}


# apt-get update exits 0 when index fetches fail with TRANSIENT errors (timeouts, DNS, refused connections): it only
# prints "Err:" / "W: Failed to fetch" lines.  What the hook concludes from lists that are missing or partial must
# never be a terminal "done" - nothing would ever retry it.
FAILED_FETCH = (
    "Hit:1 http://archive.ubuntu.com/ubuntu noble InRelease\n"
    "Err:2 http://security.ubuntu.com/ubuntu noble-security InRelease\n"
    "  Connection failed [IP: 91.189.91.83 80]\n"
    "Reading package lists...\n"
    "W: Failed to fetch http://security.ubuntu.com/ubuntu/dists/noble-security/InRelease  Connection failed [IP: 91.189.91.83 80]\n"
    "W: Some index files failed to download. They have been ignored, or old ones used instead.\n")
CLEAN_UPDATE = (
    "Hit:1 http://archive.ubuntu.com/ubuntu noble InRelease\n"
    "Get:2 http://security.ubuntu.com/ubuntu noble-security InRelease [126 kB]\n"
    "Ign:3 http://archive.ubuntu.com/ubuntu noble/main Translation-en\n"
    "Fetched 126 kB in 2s (63.0 kB/s)\n"
    "Reading package lists...\n")


def _update_output(sandbox: Sandbox, text: str) -> str:
    f = sandbox.root / "update-output.txt"
    f.write_text(text, encoding="utf-8", newline="\n")
    return f.as_posix()


def _update_calls(sandbox: Sandbox) -> int:
    f = sandbox.state / "update-calls"
    return int(f.read_text(encoding="utf-8").strip()) if f.is_file() else 0


def test_an_update_that_exits_zero_after_failed_fetches_never_records_a_final_answer(sandbox: Sandbox) -> None:
    """The reported failure: Wi-Fi flaps during 'apt-get update', apt exits 0, and updates/drivers/extras all said 'done'."""
    out = _update_output(sandbox, FAILED_FETCH)
    _ok(sandbox.run_hook(FAKE_UPDATE_RC="0", FAKE_UPDATE_OUT_FILE=out, FAKE_UPGRADES="libfoo", FAKE_NO_CANDIDATE="ananicy-cpp",
                         LINDOS_INSTALLER_STEPS="drivers updates mode_extras"))
    assert _update_calls(sandbox) == 2, "a failed fetch is retried once, like a non-zero exit"
    st = sandbox.statuses()
    assert st == {"drivers": "pending", "updates": "pending", "mode_extras": "pending"}, (st, sandbox.log_text()[-2000:])
    for step in ("drivers", "updates", "mode_extras"):
        assert "partly refreshed" in sandbox.step(step)["detail"], (step, sandbox.step(step))
    assert sandbox.upgraded() == ["libfoo"], "what the lists did show is still installed"
    # not final: the silent first-boot retry of the drivers still runs
    assert not (sandbox.target / "var/lib/lindos/driver-firstboot.done").exists()
    log = sandbox.log_text()
    assert "reported failed fetches" in log and "Err:2 http://security.ubuntu.com" in log, "the reason is in the log"
    assert "did not complete cleanly" in log


def test_a_failed_fetch_that_the_retry_fixes_is_a_complete_update(sandbox: Sandbox) -> None:
    out = _update_output(sandbox, FAILED_FETCH)
    _ok(sandbox.run_hook(FAKE_UPDATE_OUT_FILE=out, FAKE_UPDATE_OUT_ONCE="1", LINDOS_INSTALLER_STEPS="updates"))
    assert _update_calls(sandbox) == 2
    assert sandbox.step("updates")["status"] == "done" and sandbox.step("updates")["detail"] == "already up to date"


def test_the_ordinary_output_of_a_good_update_is_not_a_failure(sandbox: Sandbox) -> None:
    """'Ign:' lines and translation misses are normal: only Err:/E:/'Failed to fetch' count."""
    out = _update_output(sandbox, CLEAN_UPDATE)
    _ok(sandbox.run_hook(FAKE_UPDATE_OUT_FILE=out, LINDOS_INSTALLER_STEPS="updates"))
    assert _update_calls(sandbox) == 1
    assert sandbox.step("updates")["status"] == "done"
    assert "Fetched 126 kB" in sandbox.log_text(), "apt's output still reaches the log"


def test_an_update_that_exits_zero_but_leaves_no_lists_stops_the_package_steps(sandbox: Sandbox) -> None:
    for f in (sandbox.target / "var/lib/apt/lists").iterdir():
        f.unlink()
    _ok(sandbox.run_hook(FAKE_UPDATE_RC="0", LINDOS_INSTALLER_STEPS="browser updates"))
    assert _update_calls(sandbox) == 2
    assert sandbox.statuses() == {"browser": "pending", "updates": "pending"}
    assert "package lists" in sandbox.step("updates")["detail"]
    assert not any(c.startswith(("install-browser.sh", "apt-get -q -s upgrade")) for c in sandbox.call_log())
    assert "exited 0 but no package list" in sandbox.log_text()


def test_an_empty_list_file_is_no_list(sandbox: Sandbox) -> None:
    lists = sandbox.target / "var/lib/apt/lists"
    for f in lists.iterdir():
        f.unlink()
    (lists / "archive.ubuntu.com_ubuntu_dists_noble_main_binary-amd64_Packages").write_text("", encoding="utf-8")
    _ok(sandbox.run_hook(FAKE_UPDATE_RC="0", LINDOS_INSTALLER_STEPS="updates"))
    assert sandbox.step("updates")["status"] == "pending" and "package lists" in sandbox.step("updates")["detail"]


def test_partly_refreshed_lists_do_not_silence_the_driver_retry(sandbox: Sandbox) -> None:
    """'no drivers/firmware candidates' from incomplete lists is no answer: no marker, no terminal state."""
    _ok(sandbox.run_hook(FAKE_UPDATE_RC="100", LINDOS_INSTALLER_STEPS="drivers"))
    step = sandbox.step("drivers")
    assert step["status"] == "pending" and "partly refreshed" in step["detail"] and "free drivers and firmware installed" in step["detail"]
    assert not (sandbox.target / "var/lib/lindos/driver-firstboot.done").exists()
    # the same for a step that would have been a (terminal) skip
    other = Sandbox(sandbox.root / "second")
    other.set_cmdline(CONSENT)
    _ok(other.run_hook(FAKE_UPDATE_RC="100", FAKE_LSPCI=GPU_NVIDIA, FAKE_SB="SecureBoot enabled", LINDOS_INSTALLER_STEPS="drivers"))
    assert other.step("drivers")["status"] == "pending" and "Secure Boot" in other.step("drivers")["detail"]
    assert not (other.target / "var/lib/lindos/driver-firstboot.done").exists()
    # complete lists: the same run ends as before
    third = Sandbox(sandbox.root / "third")
    _ok(third.run_hook(LINDOS_INSTALLER_STEPS="drivers"))
    assert third.step("drivers")["status"] == "done" and (third.target / "var/lib/lindos/driver-firstboot.done").is_file()


# --------------------------------------------------------------------------- failures
def test_every_command_failing_still_exits_zero_prints_nothing_and_never_says_done(sandbox: Sandbox) -> None:
    _ok(sandbox.run_hook(FAKE_ALL_FAIL="1", FAKE_UPGRADES=UPGRADES, LINDOS_INSTALLER_STEPS="browser drivers updates mode_extras"))
    st = sandbox.statuses()
    assert set(st) == {"browser", "drivers", "updates", "mode_extras"} and "done" not in st.values(), st
    assert not (sandbox.target / "var/lib/lindos/browser-firstboot.done").exists()
    assert not (sandbox.target / "var/lib/lindos/driver-firstboot.done").exists()
    assert sandbox.held() == [] and (sandbox.target / "var/log/lindos/installer.log").is_file()


def test_a_failed_browser_install_leaves_no_marker_and_triggers_the_repair(sandbox: Sandbox) -> None:
    _ok(sandbox.run_hook(FAKE_INSTALL_BROWSER_INST_RC="1", LINDOS_INSTALLER_STEPS=TWO))
    assert sandbox.statuses()["browser"] == "failed" and "not installed" in sandbox.step("browser")["detail"]
    assert sandbox.statuses()["drivers"] == "done", "one failed step does not stop the next"
    assert not (sandbox.target / "var/lib/lindos/browser-firstboot.done").exists()
    assert not any(c.startswith("browser-firstboot.sh") for c in sandbox.call_log()), "no default-browser change without Chrome"
    failed_at = sandbox.first_index("install-browser.sh chrome --in-installer --no-download")
    assert sandbox.first_index("dpkg --configure -a") > failed_at
    assert any(c.startswith("dpkg --audit") for c in sandbox.call_log()[failed_at:])


def test_a_script_that_says_installed_while_dpkg_denies_it_is_not_done(sandbox: Sandbox) -> None:
    # (FAKE_NO_EVIDENCE: the fake install-browser.sh exits 0 and leaves nothing installed)
    _ok(sandbox.run_hook(FAKE_NO_EVIDENCE="install-browser.sh", LINDOS_INSTALLER_STEPS="browser"))
    assert sandbox.step("browser")["status"] == "failed"
    assert not (sandbox.target / "var/lib/lindos/browser-firstboot.done").exists()


def test_offline_browser_download_is_pending_for_the_silent_retry(sandbox: Sandbox) -> None:
    _ok(sandbox.run_hook(FAKE_INSTALL_BROWSER_DL_RC="3", LINDOS_INSTALLER_STEPS="browser"))
    assert sandbox.step("browser")["status"] == "pending" and "offline" in sandbox.step("browser")["detail"]
    assert not (sandbox.target / "var/lib/lindos/browser-firstboot.done").exists()
    assert not any("install-browser.sh" in c and "--no-download" in c for c in sandbox.call_log())


def test_the_repair_removes_only_broken_new_packages(sandbox: Sandbox) -> None:
    (sandbox.state / "audit").write_text(
        "The following packages are only half configured, probably due to problems\n"
        "configuring them the first time.  The configuration should be retried using\n"
        "dpkg --configure <package> or the configure menu option in dselect:\n"
        " google-chrome-stable  The Google Chrome Browser\n"
        " casper                Tools to boot live\n", encoding="utf-8", newline="\n")
    _ok(sandbox.run_hook(FAKE_CONFIGURE_FAILS="1", LINDOS_INSTALLER_STEPS="browser"))
    removed = [c for c in sandbox.call_log() if c.startswith(("dpkg --remove", "dpkg --purge"))]
    assert any("google-chrome-stable" in c for c in removed), sandbox.call_log()
    assert not any("casper" in c for c in removed), "a package that was on the image is never removed"
    assert "left alone (it was on the image): casper" in sandbox.log_text()
    assert (sandbox.state / "audit").read_text(encoding="utf-8") == ""


def test_download_timeout_is_pending_and_the_hook_still_finishes_and_unholds(sandbox: Sandbox) -> None:
    started = time.time()
    _ok(sandbox.run_hook(FAKE_UPGRADES="libfoo", FAKE_SLEEP_UPGRADE="1", LINDOS_TIMEOUT_PCT="0",
                         LINDOS_TIMEOUT_MIN="12", LINDOS_INSTALLER_STEPS="updates"))
    assert time.time() - started < 400
    assert sandbox.step("updates")["status"] == "pending" and "in time" in sandbox.step("updates")["detail"]
    assert "timed out after" in sandbox.log_text() and sandbox.held() == []
    assert not any("--no-download upgrade" in c for c in sandbox.call_log()), "nothing is installed after a timed-out download"
    assert sandbox.upgraded() == []


def test_updates_are_skipped_when_the_holds_did_not_take_effect(sandbox: Sandbox) -> None:
    _ok(sandbox.run_hook(FAKE_APT_MARK_FAIL="1", FAKE_UPGRADES=UPGRADES, LINDOS_INSTALLER_STEPS="updates"))
    step = sandbox.step("updates")
    assert step["status"] == "pending" and "held" in step["detail"]
    assert sandbox.upgraded() == [] and not any(c.startswith("apt-get -y -q -d upgrade") for c in sandbox.call_log())


def test_a_simulated_upgrade_that_touches_a_held_family_upgrades_nothing(sandbox: Sandbox) -> None:
    """Defence in depth: hold reported success, yet apt would upgrade the kernel - never go on."""
    _ok(sandbox.run_hook(FAKE_IGNORE_HOLDS="1", FAKE_UPGRADES="libfoo linux-image-6.14.0-lindos", LINDOS_INSTALLER_STEPS="updates"))
    step = sandbox.step("updates")
    assert step["status"] == "failed" and "linux-image-6.14.0-lindos" in step["detail"]
    assert sandbox.upgraded() == [] and sandbox.held() == []


def test_an_update_that_is_already_current_is_done_without_downloading(sandbox: Sandbox) -> None:
    _ok(sandbox.run_hook(LINDOS_INSTALLER_STEPS="updates"))
    assert sandbox.step("updates")["status"] == "done" and sandbox.step("updates")["detail"] == "already up to date"
    assert not any(c.startswith("apt-get -y -q -d upgrade") for c in sandbox.call_log())


def test_the_hold_list_does_not_depend_on_the_manifest_file(sandbox: Sandbox) -> None:
    """An empty or missing filesystem.manifest-remove must not swallow the package list (awk NR==FNR trap)."""
    sandbox.manifest_remove.write_text("", encoding="utf-8")
    _ok(sandbox.run_hook(LINDOS_INSTALLER_STEPS="updates", FAKE_UPGRADES="libfoo"))
    held = sandbox.calls_of("apt-mark")[0].split()
    assert "ubiquity" in held and "plymouth" in held and "firefox-locale-de" not in held
    assert sandbox.step("updates")["status"] == "done" and sandbox.upgraded() == ["libfoo"]


def test_a_failing_package_in_the_extras_group_does_not_take_the_others_along(sandbox: Sandbox) -> None:
    _ok(sandbox.run_hook(FAKE_FAIL_PKGS="krita", LINDOS_INSTALLER_STEPS="mode_extras flatpaks"))
    step = sandbox.step("mode_extras")
    assert step["status"] == "failed" and "krita" in step["detail"]
    installed = (sandbox.state / "installed").read_text(encoding="utf-8").split()
    assert "gimp" in installed and "kdenlive" in installed and "krita" not in installed
    assert sandbox.statuses()["flatpaks"] == "done", "one bad package must not stop the later steps"


def test_a_lost_connection_during_the_extras_download_is_pending_without_a_slow_fallback(sandbox: Sandbox) -> None:
    _ok(sandbox.run_hook(FAKE_DL_RC="100", FAKE_DL_DROPS_NET="1", LINDOS_INSTALLER_STEPS="mode_extras"))
    step = sandbox.step("mode_extras")
    assert step["status"] == "pending" and "connection was lost" in step["detail"]
    downloads = [c for c in sandbox.calls_of("apt-get") if c.startswith("apt-get -y -q -d install")]
    assert len(downloads) == 1, "one package at a time would wait for every single package: %s" % downloads


def test_a_download_that_fails_with_the_network_up_falls_back_to_one_package_at_a_time(sandbox: Sandbox) -> None:
    _ok(sandbox.run_hook(FAKE_DL_RC="100", LINDOS_INSTALLER_STEPS="mode_extras"))
    assert sandbox.step("mode_extras")["status"] == "failed"
    downloads = [c for c in sandbox.calls_of("apt-get") if c.startswith("apt-get -y -q -d install")]
    assert len(downloads) > 5, "each package is tried on its own: %d" % len(downloads)


def test_a_firmware_package_no_archive_carries_is_left_out_instead_of_failing_the_step(sandbox: Sandbox) -> None:
    _ok(sandbox.run_hook(FAKE_NO_CANDIDATE="amd64-microcode intel-microcode", LINDOS_INSTALLER_STEPS="drivers"))
    assert sandbox.step("drivers")["status"] == "done"
    fw = [c for c in sandbox.calls_of("apt-get") if c.startswith("apt-get -y -q -d install")]
    assert len(fw) == 1 and "linux-firmware" in fw[0] and "microcode" not in fw[0], fw


def test_drivers_without_ubuntu_drivers_say_so_instead_of_claiming_detection(sandbox: Sandbox) -> None:
    (sandbox.target / "usr/bin/ubuntu-drivers").unlink()
    _ok(sandbox.run_hook(LINDOS_INSTALLER_STEPS="drivers"))
    step = sandbox.step("drivers")
    assert step["status"] == "done" and "driver detection is not available" in step["detail"]
    assert "free drivers" not in step["detail"]


def test_flatpak_failures_are_pending_for_settings(sandbox: Sandbox) -> None:
    _ok(sandbox.run_hook(FAKE_FLATPAK_FAIL="org.vinegarhq.Sober", LINDOS_INSTALLER_STEPS="flatpaks"))
    step = sandbox.step("flatpaks")
    assert step["status"] == "pending" and "org.vinegarhq.Sober" in step["detail"]
    other = Sandbox(sandbox.root / "second")
    _ok(other.run_hook(FAKE_FLATPAK_REMOTE_RC="1", LINDOS_INSTALLER_STEPS="flatpaks"))
    assert other.step("flatpaks")["status"] == "failed"


# --------------------------------------------------------------------------- drivers
GPU_NVIDIA = "01:00.0 VGA compatible controller [0300]: NVIDIA Corporation GA106 [10de:2503]"
GPU_AMD = "03:00.0 VGA compatible controller [0300]: Advanced Micro Devices, Inc. [AMD/ATI] Navi 23 [1002:73ff]"
CONSENT = "boot=casper lindos.proprietary_drivers=1"


def _drivers(sandbox: Sandbox, **env: str) -> "subprocess.CompletedProcess[str]":
    proc = sandbox.run_hook(LINDOS_INSTALLER_STEPS="drivers", **env)
    _ok(proc)
    return proc


def test_drivers_without_consent_are_free_only(sandbox: Sandbox) -> None:
    _drivers(sandbox, FAKE_LSPCI=GPU_NVIDIA)
    assert sandbox.calls_of("ubuntu-drivers") == ["ubuntu-drivers install --free-only"] and sandbox.calls_of("lindos-drivers") == []
    assert sandbox.step("drivers")["status"] == "done" and "consent" in sandbox.step("drivers")["detail"]
    assert not (sandbox.target / "var/lib/lindos/driver-proprietary-consent").exists()


def test_proprietary_nvidia_with_consent_but_secure_boot_is_skipped(sandbox: Sandbox) -> None:
    sandbox.set_cmdline(CONSENT)
    _drivers(sandbox, FAKE_LSPCI=GPU_NVIDIA, FAKE_SB="SecureBoot enabled")
    assert sandbox.calls_of("ubuntu-drivers") and sandbox.calls_of("lindos-drivers") == []
    assert sandbox.step("drivers")["status"] == "skipped" and "Secure Boot" in sandbox.step("drivers")["detail"]
    assert (sandbox.target / "var/lib/lindos/driver-proprietary-consent").is_file()
    assert (sandbox.target / "var/lib/lindos/driver-firstboot.done").is_file(), "skipped is terminal"


def test_unknown_secure_boot_state_counts_as_enabled_for_nvidia(sandbox: Sandbox) -> None:
    sandbox.set_cmdline(CONSENT)
    efi = sandbox.root / "efi"
    (efi / "efivars").mkdir(parents=True)
    _drivers(sandbox, FAKE_LSPCI=GPU_NVIDIA, LINDOS_SYS_EFI=str(efi))
    assert sandbox.step("drivers")["status"] == "skipped" and sandbox.calls_of("lindos-drivers") == []


def test_secure_boot_efivar_is_read_when_mokutil_is_absent(sandbox: Sandbox) -> None:
    sandbox.set_cmdline(CONSENT)
    efi = sandbox.root / "efi"
    (efi / "efivars").mkdir(parents=True)
    (efi / "efivars" / "SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c").write_bytes(b"\x06\x00\x00\x00\x00")
    (sandbox.bin / "mokutil").unlink()
    _drivers(sandbox, FAKE_LSPCI=GPU_NVIDIA, LINDOS_SYS_EFI=str(efi))
    assert sandbox.step("drivers")["status"] == "done" and sandbox.calls_of("lindos-drivers") == ["lindos-drivers install --auto"]


def test_proprietary_with_consent_and_secure_boot_off(sandbox: Sandbox) -> None:
    sandbox.set_cmdline(CONSENT)
    _drivers(sandbox, FAKE_LSPCI=GPU_NVIDIA, FAKE_SB="SecureBoot disabled")
    assert sandbox.calls_of("lindos-drivers") == ["lindos-drivers install --auto"]
    assert sandbox.step("drivers")["status"] == "done" and "proprietary" in sandbox.step("drivers")["detail"]
    assert (sandbox.target / "var/lib/lindos/driver-proprietary-consent").is_file()


def test_amd_gpu_needs_no_key_so_secure_boot_does_not_block_it(sandbox: Sandbox) -> None:
    sandbox.set_cmdline(CONSENT)
    _drivers(sandbox, FAKE_LSPCI=GPU_AMD, FAKE_SB="SecureBoot enabled")
    assert sandbox.calls_of("lindos-drivers") == ["lindos-drivers install --auto"] and sandbox.step("drivers")["status"] == "done"


def test_nothing_to_install_is_not_a_driver_failure_even_with_a_non_zero_exit(sandbox: Sandbox) -> None:
    _drivers(sandbox, FAKE_UBUNTU_DRIVERS_RC="1", FAKE_UBUNTU_DRIVERS_OUT="No drivers found for installation.")
    assert sandbox.step("drivers")["status"] == "done"
    assert sandbox.calls_of("ubuntu-drivers") == ["ubuntu-drivers install --free-only"]
    assert (sandbox.target / "var/lib/lindos/driver-firstboot.done").is_file()


def test_a_failing_driver_install_is_failed_and_leaves_no_marker(sandbox: Sandbox) -> None:
    _drivers(sandbox, FAKE_UBUNTU_DRIVERS_RC="1")
    assert sandbox.step("drivers")["status"] == "failed" and "ubuntu-drivers" in sandbox.step("drivers")["detail"]
    assert not (sandbox.target / "var/lib/lindos/driver-firstboot.done").exists()


# --------------------------------------------------------------------------- debconf, stdout, signals, inhibitor
_PROTOCOL = re.compile(r"^(X_LOADTEMPLATEFILE|SUBST|PROGRESS|GET) ")


def test_with_a_debconf_frontend_only_protocol_lines_reach_the_pipe(sandbox: Sandbox) -> None:
    proc = sandbox.run_hook(DEBIAN_HAS_FRONTEND="1", LINDOS_CONFMODULE=str(sandbox.confmodule),
                            FAKE_UPGRADES="libfoo", LINDOS_INSTALLER_STEPS="browser updates")
    assert proc.returncode == 0, proc.stderr[-2000:]
    lines = [ln for ln in proc.stdout.splitlines() if ln]
    assert lines and all(_PROTOCOL.match(ln) for ln in lines), lines[:10]
    assert any(ln.startswith("X_LOADTEMPLATEFILE") and "lindos-installer" in ln for ln in lines)
    assert "PROGRESS INFO lindos-installer/msg" in lines
    text = "\n".join(ln for ln in lines if ln.startswith("SUBST "))
    for phase in ("Checking the internet connection", "Refreshing package lists", "Downloading Google Chrome",
                  "Installing Google Chrome", "Installing system updates", "Finishing up"):
        assert phase in text, phase
    for ln in lines:
        if ln.startswith("SUBST "):
            msg = ln.split("MSG ", 1)[1]
            assert len(msg) <= 60 and msg.isascii(), msg
    assert sandbox.statuses() == {"browser": "done", "updates": "done"}


def test_the_use_nonfree_answer_is_the_consent(sandbox: Sandbox) -> None:
    proc = sandbox.run_hook(DEBIAN_HAS_FRONTEND="1", LINDOS_CONFMODULE=str(sandbox.confmodule), FAKE_USE_NONFREE="true",
                            FAKE_SB="SecureBoot disabled", FAKE_LSPCI=GPU_NVIDIA, LINDOS_INSTALLER_STEPS="drivers")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "GET ubiquity/use_nonfree" in proc.stdout and "GET mirror/http/proxy" in proc.stdout
    assert sandbox.calls_of("lindos-drivers") == ["lindos-drivers install --auto"]


def test_consent_is_recorded_even_when_the_install_is_offline(sandbox: Sandbox) -> None:
    proc = sandbox.run_hook(DEBIAN_HAS_FRONTEND="1", LINDOS_CONFMODULE=str(sandbox.confmodule), FAKE_USE_NONFREE="true",
                            FAKE_NET_RC="4")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert sandbox.statuses() == {s: "pending" for s in ALL_STEPS}
    assert (sandbox.target / "var/lib/lindos/driver-proprietary-consent").exists()


def test_no_consent_file_without_consent_when_offline(sandbox: Sandbox) -> None:
    _ok(sandbox.run_hook(FAKE_NET_RC="4"))
    assert not (sandbox.target / "var/lib/lindos/driver-proprietary-consent").exists()


def test_sigterm_mid_run_still_releases_the_holds_and_exits_zero(sandbox: Sandbox) -> None:
    _ok(sandbox.run_hook(FAKE_KILL_HOOK="1", FAKE_UPGRADES="libfoo", LINDOS_INSTALLER_STEPS="updates"))
    assert "signal received" in sandbox.log_text()
    assert sandbox.held() == [], "the trap must run on TERM"
    assert sandbox.last_index("apt-mark unhold") > sandbox.first_index("apt-mark hold") >= 0
    assert not (sandbox.target / "var/lib/lindos/installer-holds").exists()
    assert not (sandbox.target / "usr/sbin/policy-rc.d").exists()
    assert not (sandbox.target / "var/lib/lindos/installer-apt.conf").exists()


def test_a_broken_inhibitor_does_not_stop_the_hook(sandbox: Sandbox) -> None:
    _ok(sandbox.run_hook(FAKE_INHIBIT_FAIL="1", LINDOS_INSTALLER_STEPS="browser"))
    assert sandbox.statuses() == {"browser": "done"}
    assert sandbox.log_text().count("start (version") == 1


def test_dns_inside_the_target_is_repaired_and_restored(sandbox: Sandbox) -> None:
    _ok(sandbox.run_hook(FAKE_DNS_BROKEN="1", LINDOS_INSTALLER_STEPS="browser"))
    assert "resolv.conf inside the target now copies" in sandbox.log_text()
    assert len(sandbox.calls_of("getent")) >= 2
    assert (sandbox.target / "etc/resolv.conf").read_text(encoding="utf-8") == "# original resolv.conf\n"
    assert sandbox.statuses() == {"browser": "done"}


def test_the_hook_source_never_sets_errexit_and_always_ends_with_exit_zero() -> None:
    text = (LIBEXEC / "target-config.sh").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert not re.search(r"^\s*set\s+-[a-zA-Z]*[eu]", code, re.M), "the hook must not use set -e / set -u"
    assert code.rstrip().endswith("exit 0")
    assert "exec </dev/null" in code and "cd / || true" in code
    assert "trap li_on_exit EXIT" in code and "trap li_on_signal HUP INT TERM" in code


# --------------------------------------------------------------------------- apt gets its configuration as an option
def test_the_hooks_own_apt_calls_carry_the_installer_config_as_an_option_never_as_a_variable(full) -> None:
    """APT_CONFIG in the environment would be inherited by dpkg's maintainer scripts: google-chrome-stable's postinst
    assigns APT_CONFIG=/usr/bin/apt-config (a shell variable it runs later) - once the variable is exported already
    apt-config reads the BINARY as a configuration file ('E: Syntax error /usr/bin/apt-config:13: Extra junk')."""
    sb, _proc = full
    apt = [c for c in sb.raw_call_log() if c.startswith(("apt-get ", "apt-cache "))]
    assert len(apt) > 10
    for call in apt:
        if call == "apt-get clean":
            continue
        assert call.split()[1:3] == ["-c", "/var/lib/lindos/installer-apt.conf"], call
    seen = [ln.split(" ", 1) for ln in (sb.state / "apt-config-seen").read_text(encoding="utf-8").splitlines() if ln]
    for name, value in seen:
        if name in ("ubuntu-drivers", "lindos-drivers"):
            continue                       # started with an 'env APT_CONFIG=...' prefix: they run apt themselves
        assert value == "APT_CONFIG=", "%s inherited %s" % (name, value)
    drivers = [v for n, v in seen if n == "ubuntu-drivers"]
    assert drivers == ["APT_CONFIG=/var/lib/lindos/installer-apt.conf"], "the helper that starts apt itself keeps the options"


# --------------------------------------------------------------------------- the hold policy
def _held_blocks(sb: Sandbox):
    """[(call, held packages)] for every real download/install/upgrade call the fake apt-get saw."""
    blocks, cur = [], None
    for ln in (sb.state / "held-log").read_text(encoding="utf-8").splitlines():
        if ln.startswith("== "):
            cur = (ln[3:], [])
            blocks.append(cur)
        elif ln and cur is not None:
            cur[1].append(ln)
    return blocks


def test_the_packages_ubiquity_removes_anyway_are_held_only_while_the_updates_step_runs(full) -> None:
    sb, _proc = full
    blocks = _held_blocks(sb)
    upgrades = [held for call, held in blocks if call.startswith("upgrade")]
    installs = [held for call, held in blocks if call.startswith("install")]
    assert upgrades and installs
    for held in upgrades:
        assert "firefox-locale-de" in held and "ubiquity" in held
    for held in installs:
        assert "firefox-locale-de" not in held and "language-pack-de" not in held, "held packages block what needs them to change"
        assert "ubiquity" in held and "linux-image-6.14.0-lindos" in held, "the installer and kernel families stay held throughout"


def test_a_held_l10n_pack_can_no_longer_block_the_libreoffice_extras(sandbox: Sandbox) -> None:
    """First real install: 'pkgProblemResolver::Resolve generated breaks, this may be caused by held packages' - the
    held libreoffice-l10n-* / -help-* packs (exact-version dependency on libreoffice-common) refused every libreoffice-*
    extra, and the step failed although the runner was online."""
    blocked = " ".join("%s=firefox-locale-de" % p for p in ("libreoffice-writer", "libreoffice-calc", "libreoffice-impress", "libreoffice-gtk3"))
    _ok(sandbox.run_hook(FAKE_BLOCKED_BY_HOLD=blocked, FAKE_UPGRADES="libfoo", LINDOS_INSTALLER_STEPS="updates mode_extras"))
    assert sandbox.statuses() == {"updates": "done", "mode_extras": "done"}, (sandbox.step("mode_extras"), sandbox.log_text()[-1500:])
    installed = (sandbox.state / "installed").read_text(encoding="utf-8").split()
    for pkg in ("libreoffice-writer", "libreoffice-calc", "libreoffice-impress", "libreoffice-gtk3"):
        assert pkg in installed, pkg
    assert sandbox.held() == []


def test_a_package_that_stays_blocked_is_named_with_apts_words_and_the_others_are_installed(sandbox: Sandbox) -> None:
    _ok(sandbox.run_hook(FAKE_UNRESOLVABLE="krita", LINDOS_INSTALLER_STEPS="mode_extras"))
    step = sandbox.step("mode_extras")
    assert step["status"] == "failed" and step["detail"].startswith("not installed: krita;"), step
    assert "held broken packages" in step["detail"], "precisely why: apt's own first error line"
    installed = (sandbox.state / "installed").read_text(encoding="utf-8").split()
    assert "gimp" in installed and "kdenlive" in installed and "krita" not in installed, "the rest of its Mode is installed"
    assert "libreoffice-calc" in installed and "gamemode" in installed, "and so are the other Modes"
    downloads = [c for c in sandbox.calls_of("apt-get") if c.startswith("apt-get -y -q -d install")]
    assert not any(c.split()[-1] == "krita" for c in downloads), "nothing was downloaded for what apt cannot resolve"


# --------------------------------------------------------------------------- extras: one Mode at a time, simulated first
def test_the_extras_are_installed_one_modes_apps_at_a_time(sandbox: Sandbox) -> None:
    import json
    from installer_testlib import SHARE
    doc = json.loads((SHARE / "extras.json").read_text(encoding="utf-8"))
    mode_of = {pkg: modes[0] for pkg, modes in doc["apt_sources"].items()}
    _ok(sandbox.run_hook(FAKE_NO_CANDIDATE="ananicy-cpp", LINDOS_INSTALLER_STEPS="mode_extras"))
    assert sandbox.step("mode_extras")["status"] == "done"
    downloads = [c.split() for c in sandbox.calls_of("apt-get") if c.startswith("apt-get -y -q -d install")]
    assert len(downloads) == len(set(mode_of.values())) >= 3, downloads
    for words in downloads:
        pkgs = [w for w in words if w in mode_of]
        assert len({mode_of[w] for w in pkgs}) == 1, "one transaction must not mix Modes: %s" % pkgs
    assert set(w for words in downloads for w in words if w in mode_of) == set(doc["apt"]) - {"ananicy-cpp"}


def test_an_extra_whose_install_would_remove_an_installed_package_is_refused_and_named(sandbox: Sandbox) -> None:
    """First real install: 'apt-get install steam-devices' REMOVED Valve's steam-launcher, which the gaming step had just
    reported as installed.  Every extras install is simulated first; one that removes something that has to stay is
    not run."""
    (sandbox.state / "installed").write_text((sandbox.state / "installed").read_text(encoding="utf-8") + "steam-launcher\n",
                                             encoding="utf-8", newline="\n")
    _ok(sandbox.run_hook(FAKE_REMOVES="gimp=steam-launcher", LINDOS_INSTALLER_STEPS="mode_extras"))
    step = sandbox.step("mode_extras")
    assert step["status"] == "failed" and step["detail"].startswith("not installed: gimp;") and "would remove steam-launcher" in step["detail"], step
    installed = (sandbox.state / "installed").read_text(encoding="utf-8").split()
    assert "steam-launcher" in installed, "the launcher is still there"
    assert "gimp" not in installed and "krita" in installed and "kdenlive" in installed, "only the offender is left out"
    assert not any(c.startswith("apt-get -y -q -d install") and "gimp" in c.split() for c in sandbox.calls_of("apt-get"))


def test_removals_that_are_on_purpose_or_on_the_removal_list_are_fine(sandbox: Sandbox) -> None:
    """wine -> wine-staging is an intended replacement; what Ubiquity removes anyway (filesystem.manifest-remove) may go."""
    _ok(sandbox.run_hook(FAKE_REMOVES="gimp=wine krita=firefox-locale-de,language-pack-de:amd64", LINDOS_INSTALLER_STEPS="mode_extras"))
    assert sandbox.step("mode_extras")["status"] == "done", sandbox.step("mode_extras")
    installed = (sandbox.state / "installed").read_text(encoding="utf-8").split()
    assert "gimp" in installed and "krita" in installed


def test_the_firmware_install_is_simulated_too(sandbox: Sandbox) -> None:
    (sandbox.state / "installed").write_text((sandbox.state / "installed").read_text(encoding="utf-8") + "foo-app\n",
                                             encoding="utf-8", newline="\n")
    _ok(sandbox.run_hook(FAKE_REMOVES="linux-firmware=foo-app", LINDOS_INSTALLER_STEPS="drivers"))
    step = sandbox.step("drivers")
    assert step["status"] == "failed" and "would remove foo-app" in step["detail"], step
    assert not any(c.startswith("apt-get -y -q -d install") for c in sandbox.calls_of("apt-get"))


# --------------------------------------------------------------------------- the end: 'done' is looked at again
def test_a_script_that_says_it_worked_but_left_nothing_is_failed_at_the_end(sandbox: Sandbox) -> None:
    """The gaming step said 'done' with Steam missing (install-gaming.sh had seen steam-launcher earlier; something removed it)."""
    _ok(sandbox.run_hook(FAKE_NO_EVIDENCE="install-gaming.sh", LINDOS_INSTALLER_STEPS="compat gaming"))
    assert sandbox.statuses() == {"compat": "done", "gaming": "failed"}, sandbox.statuses()
    assert "steam" in sandbox.step("gaming")["detail"] and "lutris" in sandbox.step("gaming")["detail"]
    assert "something removed it again" in sandbox.log_text()


def test_a_later_repair_that_removes_an_earlier_result_turns_it_from_done_to_failed(sandbox: Sandbox) -> None:
    _ok(sandbox.run_hook(FAKE_REPAIR_REMOVES="google-chrome-stable", LINDOS_INSTALLER_STEPS="browser"))
    assert sandbox.statuses() == {"browser": "failed"}
    assert "google-chrome-stable" in sandbox.step("browser")["detail"]
    assert not (sandbox.target / "var/lib/lindos/browser-firstboot.done").exists(), "the silent retry must run again"


def test_a_package_list_that_cannot_be_read_at_the_end_leaves_the_results_alone(sandbox: Sandbox) -> None:
    _ok(sandbox.run_hook(FAKE_DPKG_QUERY_FAIL="1", LINDOS_INSTALLER_STEPS="browser"))
    assert sandbox.statuses() == {"browser": "done"}
    assert "cannot be read" in sandbox.log_text()
