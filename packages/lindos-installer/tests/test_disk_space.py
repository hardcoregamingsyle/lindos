"""The installer hook's disk-space policy, its crash breadcrumbs and the install-state it leaves on every exit path.

Why: on the owner's real laptop the installer crashed and hung mid-install and the next boot showed xfce4-session
"Unable to load failsafe session / xfconfd isn't running" - consistent with the target root filesystem filling up (the
full install adds roughly 15-25 GB) or with a half-finished dpkg run.  So the hook (lib.sh / target-config.sh)

* measures the target at the start, keeps a RESERVE free for the user (the larger of 8 GB and 12 % of the partition,
  ``lindos.install_reserve=GB``, never below a 2 GB floor) and runs a step only when ``estimate + reserve`` is free at the
  moment its turn comes - otherwise the step is 'pending' with the reason and the cheaper steps go on;
* skips the extra apps and the Flatpaks on a partition below 40 GB;
* checks apt's own "After this operation" figure, again between package groups and Flatpaks, and before every dpkg run;
* writes a one-line breadcrumb (/var/lib/lindos/installer-progress) before and after every step and group, flushed with
  ``sync -f``, and keeps install-state.json truthful on every exit path.

Everything runs against the fake-target sandbox (installer_testlib.py): a fake disk (``Sandbox.disk``) that the fake
commands fill up (``FAKE_EAT_*``) stands in for ``df``.  What it cannot show: the real df/apt/dpkg numbers.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import time

import pytest
from installer_testlib import ALL_STEPS, BASH, GB, LIBEXEC, REPO, Sandbox, needs_bash

pytestmark = needs_bash

UPDATES_KB = 3 * GB           # the table of estimates (lib.sh LI_NEED_KB)
BROWSER_KB = 629146
DRIVERS_KB = 838861
COMPAT_KB = 2 * GB
GAMING_KB = 3 * GB // 2
EXTRAS_KB = 6 * GB
FLATPAKS_KB = 5 * GB


def kb(gb: float) -> int:
    return int(gb * GB)


def dec(gb: int) -> int:
    """KB of *gb* decimal gigabytes - only for sizes apt prints without rounding (an even number of GB)."""
    assert (gb * 10 ** 9) % 1024 == 0
    return gb * 10 ** 9 // 1024


def gbs(kilobytes: int) -> str:
    """What lib.sh's li_gb prints."""
    tenths = (kilobytes * 10 + GB // 2) // GB
    return "%d.%d" % (tenths // 10, tenths % 10)


def _ok(proc: "subprocess.CompletedProcess[str]") -> None:
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert proc.stdout == "", "the hook wrote to stdout (Ubiquity's debconf pipe): %r" % proc.stdout[:300]


def lib(sb: Sandbox, snippet: str, **env: str) -> str:
    """Source lib.sh in bash and run *snippet* (the helpers have no side effects when loaded)."""
    assert BASH is not None
    proc = subprocess.run([BASH, "-c", '. "${LINDOS_INSTALLER_LIB}"; ' + snippet], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120, env=sb.env(**env), stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def installed(sb: Sandbox) -> list:
    return (sb.state / "installed").read_text(encoding="utf-8").split()


def downloads(sb: Sandbox) -> list:
    return [c for c in sb.calls_of("apt-get") if c.startswith("apt-get -y -q -d install")]


def unpacks(sb: Sandbox) -> list:
    return [c for c in sb.calls_of("apt-get") if c.startswith("apt-get -y -q --no-download install")]


# ============================================================================================ the pieces
@pytest.mark.parametrize("text, get, add, known", [
    ("Need to get 456 MB of archives.\nAfter this operation, 1,234 MB of additional disk space will be used.\n",
     math.ceil(456e6 / 1024), math.ceil(1234e6 / 1024), 1),
    ("Need to get 0 B/456 MB of archives.\nAfter this operation, 12.3 MB of additional disk space will be used.\n",
     math.ceil(456e6 / 1024), math.ceil(12.3e6 / 1024), 1),
    ("Need to get 5,120 kB of archives.\nAfter this operation, 2.5 GB of additional disk space will be used.\n",
     math.ceil(5120e3 / 1024), math.ceil(2.5e9 / 1024), 1),
    ("After this operation, 512 kB of additional disk space will be used.\n", 0, 500, 1),
    ("Need to get 100 B of archives.\nAfter this operation, 100 B of additional disk space will be used.\n", 1, 1, 1),
    ("Need to get 80 MB of archives.\nAfter this operation, 512 kB disk space will be freed.\n", math.ceil(80e6 / 1024), 0, 1),
    ("Reading package lists...\nInst libfoo [1.0] (2.0 Ubuntu:24.04/noble-updates [amd64])\n", 0, 0, 0),
    ("Need to get 80 MB of archives.\n", 0, 0, 0),                      # no 'After this operation': nothing is known
    ("After this operation, lots of additional disk space will be used.\n", 0, 0, 0),
    ("", 0, 0, 0),
])
def test_apts_own_disk_figures_are_parsed_in_every_unit_it_prints(sandbox: Sandbox, text: str, get: int, add: int, known: int) -> None:
    out = lib(sandbox, 'li_sim_parse "${SIM_TEXT}"; echo "${LI_SIM_KNOWN} ${LI_SIM_GET_KB} ${LI_SIM_ADD_KB}"', SIM_TEXT=text)
    assert out.split() == [str(known), str(get), str(add)], out


@pytest.mark.parametrize("value, shown", [(0, "0.0"), (GB, "1.0"), (3 * GB, "3.0"), (629146, "0.6"), (12 * GB, "12.0"),
                                          (3 * GB // 2, "1.5"), (1100000, "1.0"), (1200000, "1.1"), (11534335, "11.0")])
def test_sizes_are_shown_in_gigabytes_with_one_decimal(sandbox: Sandbox, value: int, shown: str) -> None:
    assert lib(sandbox, "li_gb %d" % value).strip() == shown
    assert gbs(value) == shown


@pytest.mark.parametrize("total_gb, reserve_kb", [
    (30, 8 * GB),                                    # 12 % of 30 GB is 3.6 GB: the 8 GB minimum
    (60, 8 * GB),                                    # 12 % is 7.2 GB: still the minimum
    (66, 8 * GB),
    (100, 104857600 * 12 // 100),                    # 12 GB
    (200, 209715200 * 12 // 100)])                   # 24 GB
def test_the_default_reserve_is_the_larger_of_8_gb_and_12_percent_of_the_partition(sandbox: Sandbox, total_gb: int, reserve_kb: int) -> None:
    out = lib(sandbox, 'li_disk_init; echo "${LI_RESERVE_KB}"', LINDOS_TOTAL_KB=str(kb(total_gb)))
    assert int(out.strip()) == reserve_kb


@pytest.mark.parametrize("env, word, expected_kb", [
    ("20", "", 20 * GB),                 # the environment knob
    ("30", "15", 15 * GB),               # the kernel word wins over it
    ("", "15", 15 * GB),
    ("", "1", 2 * GB),                   # never below the 2 GB floor ...
    ("", "0", 2 * GB),
    ("", "2", 2 * GB),
    ("", "abc", 12582912),               # junk: the default (12 % of 100 GB), and a log line says so
    ("", "9999999", 12582912),           # absurd: too many digits
    ("", "-5", 12582912),
])
def test_the_reserve_can_be_overridden_by_a_kernel_word_or_the_environment_but_never_below_the_floor(
        sandbox: Sandbox, env: str, word: str, expected_kb: int) -> None:
    if word:
        sandbox.set_cmdline("boot=casper lindos.install_reserve=%s" % word)
    out = lib(sandbox, 'li_disk_init; echo "${LI_RESERVE_KB}"', LINDOS_TOTAL_KB=str(100 * GB), LINDOS_INSTALL_RESERVE_GB=env)
    assert int(out.strip()) == expected_kb
    if word in ("abc", "-5"):
        assert "ignoring lindos.install_reserve" in sandbox.log_text()


@pytest.mark.parametrize("total_kb, small", [(40 * GB, 0), (40 * GB - 1, 1), (10 * GB, 1), (500 * GB, 0)])
def test_a_partition_below_40_gb_is_small(sandbox: Sandbox, total_kb: int, small: int) -> None:
    out = lib(sandbox, 'li_disk_init; echo "${LI_SMALL_PART}"', LINDOS_TOTAL_KB=str(total_kb))
    assert out.strip() == str(small)


def test_the_estimates_are_one_table_derived_from_extras_json(sandbox: Sandbox) -> None:
    out = lib(sandbox, 'li_extras_load; for s in updates browser drivers compat gaming mode_extras flatpaks; do '
                       'echo "${s} $(li_need_kb ${s})"; done')
    need = {line.split()[0]: int(line.split()[1]) for line in out.splitlines()}
    assert need == {"updates": UPDATES_KB, "browser": BROWSER_KB, "drivers": DRIVERS_KB, "compat": COMPAT_KB,
                    "gaming": GAMING_KB, "mode_extras": EXTRAS_KB, "flatpaks": FLATPAKS_KB}
    # nothing to install = needs nothing: a Chrome that is not wanted, an empty extras list
    sandbox.target.joinpath("etc/lindos/system.json").write_text(json.dumps({"browser": "firefox"}), encoding="utf-8")
    empty = sandbox.root / "empty-extras.json"
    empty.write_text(json.dumps({"schema": 1, "apt": [], "compat": [], "gaming": [], "flatpaks": []}), encoding="utf-8")
    out = lib(sandbox, 'li_extras_load; for s in browser compat gaming mode_extras flatpaks; do echo "${s} $(li_need_kb ${s})"; done',
              LINDOS_EXTRAS_JSON=empty.as_posix())
    assert {line.split()[0]: int(line.split()[1]) for line in out.splitlines()} == {
        "browser": 0, "compat": 0, "gaming": 0, "mode_extras": 0, "flatpaks": 0}


def test_the_flatpak_estimate_follows_the_number_of_apps(sandbox: Sandbox) -> None:
    two = sandbox.root / "two-flatpaks.json"
    two.write_text(json.dumps({"schema": 1, "flatpaks": ["a.b.C", "d.e.F"]}), encoding="utf-8")
    out = lib(sandbox, "li_extras_load; li_need_kb flatpaks", LINDOS_EXTRAS_JSON=two.as_posix())
    assert int(out.strip()) == FLATPAKS_KB // 2


def test_li_dl_reports_no_time_left_in_li_rc_too(sandbox: Sandbox) -> None:
    """li_extras_install reads LI_RC after a failed download: 125 (no time left) must be there, not the status of an
    earlier command (the simulation, 0) - otherwise budget exhaustion is recorded as a failure."""
    out = lib(sandbox, 'LI_RC=0; LI_BUDGET=0; li_dl 100 true; echo "$? ${LI_RC}"')
    assert out.split() == ["125", "125"]


def test_a_multi_arch_package_counts_as_installed_by_its_bare_name(sandbox: Sandbox) -> None:
    """dpkg-query prints libvulkan1:amd64 for a Multi-Arch: same package; a package installed only for i386 is not installed."""
    (sandbox.state / "installed").write_text("libvulkan1\nmesa-vulkan-drivers\nlibfoo\nlibonly32:i386\nplain\n", encoding="utf-8", newline="\n")
    out = lib(sandbox, 'li_installed_load; printf "%s\\n" "${!LI_INSTALLED[@]}" | sort')
    keys = out.split()
    for bare in ("libvulkan1", "mesa-vulkan-drivers", "libfoo", "plain"):
        assert bare in keys, (bare, keys)
    assert "libvulkan1:amd64" in keys, "both spellings are known"
    assert "libonly32" not in keys and "libonly32:i386" in keys, keys


# ============================================================================================ the policy in the hook
def test_the_start_of_the_hook_notes_the_disk_in_the_log(sandbox: Sandbox) -> None:
    _ok(sandbox.run_hook(LINDOS_INSTALLER_STEPS="browser"))
    assert "disk: the target partition is 100.0 GB with 57.2 GB free; the reserve kept free for the user is 12.0 GB" in sandbox.log_text()


def test_a_disk_with_room_runs_every_step(sandbox: Sandbox) -> None:
    env = sandbox.disk(total_gb=100, free_gb=60)
    _ok(sandbox.run_hook(FAKE_UPGRADES="libfoo", **env))
    assert sandbox.statuses() == {s: "done" for s in ALL_STEPS}, (sandbox.statuses(), sandbox.log_text()[-2000:])


def test_a_small_partition_gets_no_extra_apps_and_no_flatpaks(sandbox: Sandbox) -> None:
    env = sandbox.disk(total_gb=30, free_gb=25)
    _ok(sandbox.run_hook(FAKE_UPGRADES="libfoo", **env))
    st = sandbox.statuses()
    assert st["mode_extras"] == "pending" and st["flatpaks"] == "pending", st
    for step in ("mode_extras", "flatpaks"):
        assert sandbox.step(step)["detail"].startswith("partition too small"), sandbox.step(step)
    for step in ("updates", "browser", "drivers", "compat", "gaming"):
        assert st[step] == "done", (step, st)
    assert not any(c.startswith("flatpak") for c in sandbox.call_log()), "no Flatpaks, not even the remote"
    assert not any("gimp" in c for c in sandbox.calls_of("apt-cache")), "no extra apps, not even looked for"


@pytest.mark.parametrize("total_kb, extras", [(40 * GB, "done"), (40 * GB - 1, "pending")])
def test_exactly_40_gb_is_not_small(sandbox: Sandbox, total_kb: int, extras: str) -> None:
    env = sandbox.disk_kb(total_kb=total_kb, free_kb=total_kb)
    _ok(sandbox.run_hook(LINDOS_INSTALLER_STEPS="mode_extras flatpaks", **env))
    assert sandbox.statuses() == {"mode_extras": extras, "flatpaks": extras}, sandbox.statuses()
    if extras == "pending":
        assert "partition too small" in sandbox.step("flatpaks")["detail"]


def test_a_disk_with_no_room_leaves_every_step_pending_with_the_reason_and_installs_nothing(sandbox: Sandbox) -> None:
    env = sandbox.disk(total_gb=100, free_gb=1)
    _ok(sandbox.run_hook(FAKE_UPGRADES="libfoo", **env))
    assert sandbox.statuses() == {s: "pending" for s in ALL_STEPS}
    for step in ALL_STEPS:
        assert sandbox.step(step)["detail"].startswith("not enough disk space: needs ~"), (step, sandbox.step(step))
        assert "GB free" in sandbox.step(step)["detail"]
    assert not any(c.startswith(("install-", "ubuntu-drivers", "flatpak", "apt-get -y -q -d", "apt-get -y -q --no-download"))
                   for c in sandbox.call_log())
    assert sandbox.upgraded() == [] and sandbox.held() == []


def test_the_reason_names_the_estimate_plus_the_reserve_and_the_free_space(sandbox: Sandbox) -> None:
    env = sandbox.disk(total_gb=100, free_gb=10)               # reserve 12 GB: updates need 3 + 12 = 15
    _ok(sandbox.run_hook(LINDOS_INSTALLER_STEPS="updates", **env))
    detail = sandbox.step("updates")["detail"]
    assert detail.startswith("not enough disk space: needs ~15.0 GB, 10.0 GB free"), detail
    assert "3.0 GB for this step" in detail and "12.0 GB kept free for you" in detail
    assert len(detail) <= 200, "the state file keeps 200 characters: the whole reason has to fit"


@pytest.mark.parametrize("delta, status", [(0, "done"), (1, "done"), (-1, "pending")])
def test_the_boundary_is_exactly_estimate_plus_reserve(sandbox: Sandbox, delta: int, status: str) -> None:
    """60 GB partition: 12 % is 7.2 GB, so the reserve is the 8 GB minimum; updates need 3 GB."""
    free = UPDATES_KB + 8 * GB + delta
    env = sandbox.disk_kb(total_kb=60 * GB, free_kb=free)
    _ok(sandbox.run_hook(LINDOS_INSTALLER_STEPS="updates", FAKE_UPGRADES="libfoo", **env))
    assert sandbox.statuses() == {"updates": status}, (sandbox.step("updates"), sandbox.log_text()[-1500:])
    assert (sandbox.upgraded() == ["libfoo"]) == (status == "done")


@pytest.mark.parametrize("word, free_gb, status", [
    ("", 14, "pending"),              # default reserve 12 GB: 3 + 12 = 15 > 14
    ("8", 14, "done"),                # 3 + 8 = 11 <= 14
    ("12", 14, "pending"),
    ("0", 14, "done"),                # 0 is raised to the 2 GB floor: 3 + 2 = 5 <= 14
    ("0", 4, "pending"),              # ... and never less than 2 GB is left: 3 + 2 = 5 > 4
    ("0", 5, "done"),
    ("1", 4, "pending"),
])
def test_the_reserve_override_changes_what_runs_but_the_floor_stays(sandbox: Sandbox, word: str, free_gb: int, status: str) -> None:
    if word:
        sandbox.set_cmdline("boot=casper lindos.install_reserve=%s" % word)
    env = sandbox.disk(total_gb=100, free_gb=free_gb)
    _ok(sandbox.run_hook(LINDOS_INSTALLER_STEPS="updates", FAKE_UPGRADES="libfoo", **env))
    assert sandbox.statuses() == {"updates": status}, (word, free_gb, sandbox.step("updates"))


def test_the_environment_knob_is_overridden_by_the_kernel_word(sandbox: Sandbox) -> None:
    sandbox.set_cmdline("boot=casper lindos.install_reserve=8")
    env = sandbox.disk(total_gb=100, free_gb=14)
    _ok(sandbox.run_hook(LINDOS_INSTALLER_STEPS="updates", FAKE_UPGRADES="libfoo", LINDOS_INSTALL_RESERVE_GB="30", **env))
    assert sandbox.statuses() == {"updates": "done"}


# --- a disk that fills up while the hook works --------------------------------------------------------------------
def test_a_medium_disk_skips_the_flatpaks_first(sandbox: Sandbox) -> None:
    """60 GB partition with 25 GB free (reserve 8 GB): everything fits until the extras have taken their 6 GB; the Flatpaks
    (5 GB + 8 GB reserve) no longer do."""
    env = sandbox.disk(total_gb=60, free_gb=25)
    _ok(sandbox.run_hook(
        FAKE_UPGRADES="libfoo", FAKE_EAT_UPGRADE_KB=str(UPDATES_KB), FAKE_EAT_INSTALL_BROWSER_INST_KB=str(BROWSER_KB),
        FAKE_EAT_DRIVERS_KB=str(DRIVERS_KB), FAKE_EAT_INSTALL_COMPAT_INST_KB=str(COMPAT_KB),
        FAKE_EAT_INSTALL_GAMING_INST_KB=str(GAMING_KB),
        FAKE_EAT_PKG="gimp=%d libreoffice-writer=%d mangohud=%d" % (2 * GB, 2 * GB, 2 * GB),
        FAKE_EAT_FLATPAK_KB=str(kb(1.25)), **env))
    st = sandbox.statuses()
    assert {s: st[s] for s in ALL_STEPS if s != "flatpaks"} == {s: "done" for s in ALL_STEPS if s != "flatpaks"}, (st, sandbox.log_text()[-2500:])
    assert st["flatpaks"] == "pending"
    detail = sandbox.step("flatpaks")["detail"]
    assert detail.startswith("not enough disk space: needs ~13.0 GB, 11.1 GB free"), detail
    assert not any(c.startswith("flatpak") for c in sandbox.call_log()), "not even the remote was added"
    assert abs(sandbox.free_kb() - kb(25 - 3 - 0.6 - 0.8 - 2 - 1.5 - 6)) < 20000
    assert sandbox.free_kb() >= 8 * GB, "the reserve is still there at the end"


def test_the_disk_shrinking_between_steps_stops_the_later_steps_and_the_hook_still_ends_cleanly(sandbox: Sandbox) -> None:
    env = sandbox.disk(total_gb=100, free_gb=40)               # reserve 12 GB
    _ok(sandbox.run_hook(FAKE_UPGRADES="libfoo", FAKE_EAT_UPGRADE_KB=str(20 * GB),
                         FAKE_EAT_INSTALL_BROWSER_INST_KB=str(5 * GB), FAKE_EAT_INSTALL_COMPAT_INST_KB=str(3 * GB), **env))
    st = sandbox.statuses()
    assert [s for s in ALL_STEPS if st[s] == "done"] == ["updates", "browser", "drivers", "compat"], st
    assert [s for s in ALL_STEPS if st[s] == "pending"] == ["gaming", "mode_extras", "flatpaks"], st
    for step in ("gaming", "mode_extras", "flatpaks"):
        assert sandbox.step(step)["detail"].startswith("not enough disk space: needs ~"), sandbox.step(step)
    assert sandbox.held() == [] and not (sandbox.target / "usr/sbin/policy-rc.d").exists()


def test_a_step_that_does_not_fit_does_not_stop_a_cheaper_one_after_it(sandbox: Sandbox) -> None:
    """13.5 GB free on a 60 GB partition (reserve 8): the extras (6 + 8) do not fit, the Flatpaks (5 + 8) do."""
    env = sandbox.disk(total_gb=60, free_gb=13.5)
    _ok(sandbox.run_hook(LINDOS_INSTALLER_STEPS="mode_extras flatpaks", **env))
    assert sandbox.statuses() == {"mode_extras": "pending", "flatpaks": "done"}
    assert "needs ~14.0 GB, 13.5 GB free" in sandbox.step("mode_extras")["detail"]


def test_the_packages_a_step_downloaded_are_cleaned_out_before_the_next_step_looks_at_the_disk(sandbox: Sandbox) -> None:
    """The updates leave 14 GB of downloaded .deb files behind them; without the clean-up after the step the compat step (2 +
    12 GB) would find 13 GB and be refused."""
    env = sandbox.disk(total_gb=100, free_gb=30)
    _ok(sandbox.run_hook(LINDOS_INSTALLER_STEPS="updates compat", FAKE_UPGRADES="libfoo", FAKE_EAT_UPGRADE_DL_KB=str(14 * GB),
                         FAKE_EAT_UPGRADE_KB=str(3 * GB), **env))
    assert sandbox.statuses() == {"updates": "done", "compat": "done"}, (sandbox.statuses(), sandbox.step("compat"))
    first_clean = sandbox.first_index("apt-get clean")
    assert 0 < first_clean < sandbox.first_index("install-compat.sh"), "cleaned between the two steps"
    assert abs(sandbox.free_kb() - 27 * GB) < 20000


def test_an_updates_simulation_that_does_not_fit_is_refused_before_anything_is_downloaded(sandbox: Sandbox) -> None:
    env = sandbox.disk(total_gb=100, free_gb=20)               # the step gate wants 15: fine; apt says 32 GB + 4 GB of archives
    _ok(sandbox.run_hook(LINDOS_INSTALLER_STEPS="updates", FAKE_UPGRADES="libfoo", FAKE_UPGRADE_ADD_KB=str(dec(32)),
                         FAKE_UPGRADE_GET_KB=str(dec(4)), **env))
    step = sandbox.step("updates")
    assert step["status"] == "pending", step
    assert step["detail"].startswith("not enough disk space: needs ~%s GB, 20.0 GB free" % gbs(dec(36) + 12 * GB)), step
    assert not any(c.startswith("apt-get -y -q -d upgrade") for c in sandbox.call_log()), "nothing was downloaded"
    assert sandbox.upgraded() == []


@pytest.mark.parametrize("delta, status", [(0, "done"), (-1, "pending")])
def test_the_archives_and_the_unpacked_files_are_both_counted(sandbox: Sandbox, delta: int, status: str) -> None:
    """4 GB of archives + 4 GB unpacked = 8 GB at the peak (apt keeps the archives until 'apt-get clean'): exactly that
    plus the 12 GB reserve is enough, one kilobyte less is not - although the unpacked 4 GB alone would fit."""
    free = dec(8) + 12 * GB + delta
    env = sandbox.disk_kb(total_kb=100 * GB, free_kb=free)
    _ok(sandbox.run_hook(LINDOS_INSTALLER_STEPS="updates", FAKE_UPGRADES="libfoo", FAKE_UPGRADE_ADD_KB=str(dec(4)),
                         FAKE_UPGRADE_GET_KB=str(dec(4)), **env))
    assert sandbox.step("updates")["status"] == status, sandbox.step("updates")


def test_dpkg_is_not_started_when_the_download_has_eaten_the_room(sandbox: Sandbox) -> None:
    """The simulation fitted, the download then used far more than it said: what dpkg still has to unpack plus the 2 GB
    floor is checked again before it starts (dpkg is never stopped half way)."""
    env = sandbox.disk(total_gb=100, free_gb=20)
    _ok(sandbox.run_hook(LINDOS_INSTALLER_STEPS="updates", FAKE_UPGRADES="libfoo", FAKE_UPGRADE_ADD_KB=str(dec(2)),
                         FAKE_UPGRADE_GET_KB=str(dec(2)), FAKE_EAT_UPGRADE_DL_KB=str(19 * GB), **env))
    step = sandbox.step("updates")
    assert step["status"] == "pending", step
    assert step["detail"].startswith("not enough disk space: needs ~%s GB, 1.0 GB free" % gbs(dec(2) + 2 * GB)), step
    assert not any("--no-download upgrade" in c for c in sandbox.call_log()), "dpkg never started"
    assert sandbox.upgraded() == []
    assert sandbox.free_kb() > 15 * GB, "and the downloaded packages are given back when the hook ends"


# --- between the package groups of the extras -----------------------------------------------------------------------
def test_the_extras_stop_between_groups_when_the_disk_is_down_to_the_reserve(sandbox: Sandbox) -> None:
    env = sandbox.disk(total_gb=100, free_gb=18.5)             # reserve 12 GB; the step gate wants 6 + 12 = 18
    _ok(sandbox.run_hook(LINDOS_INSTALLER_STEPS="mode_extras", FAKE_EAT_INSTALL_KB=str(7 * GB), **env))
    step = sandbox.step("mode_extras")
    assert step["status"] == "pending", step
    assert step["detail"].startswith("not enough disk space: needs ~12.0 GB, 11.5 GB free; left out: "), step
    have = installed(sandbox)
    assert "mangohud" in have and "gamemode" in have, "the first group (gaming) was installed"
    assert "gimp" not in have and "thunderbird" not in have, "the groups after it were not started"
    assert len(downloads(sandbox)) == 1, downloads(sandbox)
    assert "left for later" in sandbox.log_text()
    assert sandbox.held() == []


def test_a_group_whose_simulated_size_does_not_fit_is_left_out_and_the_others_still_go(sandbox: Sandbox) -> None:
    env = sandbox.disk(total_gb=100, free_gb=30)               # reserve 12 GB
    _ok(sandbox.run_hook(LINDOS_INSTALLER_STEPS="mode_extras", FAKE_SIM_ADD_KB="0", FAKE_SIM_ADD_PKG="gimp=%d" % dec(32), **env))
    step = sandbox.step("mode_extras")
    assert step["status"] == "pending", step
    assert step["detail"].startswith("not enough disk space: needs ~%s GB, 30.0 GB free; left out: " % gbs(dec(32) + 12 * GB)), step
    assert "gimp" in step["detail"] and "krita" in step["detail"]
    have = installed(sandbox)
    assert "gimp" not in have and "krita" not in have and "kdenlive" not in have, "the creator group is left out"
    assert "mangohud" in have and "thunderbird" in have, "the gaming and work groups are installed"
    assert len(downloads(sandbox)) == 2, "nothing was downloaded for the group that does not fit"


def test_a_group_the_disk_cannot_take_after_its_download_is_not_unpacked(sandbox: Sandbox) -> None:
    """The gaming group's download uses 36 of the 40 GB: 4 GB are left, 4 GB (apt's figure) still to unpack plus the 2 GB
    floor does not fit - dpkg is not started for it (the downloaded files are cleaned out, the other groups go on)."""
    env = sandbox.disk(total_gb=100, free_gb=40)
    _ok(sandbox.run_hook(LINDOS_INSTALLER_STEPS="mode_extras", FAKE_SIM_ADD_KB="0", FAKE_SIM_ADD_PKG="ananicy-cpp=%d" % dec(4),
                         FAKE_EAT_DL_KB=str(36 * GB), **env))
    step = sandbox.step("mode_extras")
    assert step["status"] == "pending" and "left out: " in step["detail"] and "mangohud" in step["detail"], step
    assert not any("mangohud" in c.split() for c in unpacks(sandbox)), "dpkg never started for the gaming group"
    have = installed(sandbox)
    assert "mangohud" not in have and "gimp" in have and "thunderbird" in have


# --- the Flatpaks ------------------------------------------------------------------------------------------------------
def test_the_flatpak_loop_stops_when_the_next_app_would_not_fit_above_the_reserve(sandbox: Sandbox) -> None:
    env = sandbox.disk(total_gb=100, free_gb=17.5)             # reserve 12 GB, 4 apps at 1.25 GB: the gate wants 17
    _ok(sandbox.run_hook(LINDOS_INSTALLER_STEPS="flatpaks", FAKE_EAT_FLATPAK_KB=str(3 * GB), **env))
    step = sandbox.step("flatpaks")
    assert step["status"] == "pending", step
    assert step["detail"].startswith("not enough disk space: needs ~13.3 GB, 11.5 GB free"), step
    assert "left out: org.prismlauncher.PrismLauncher org.vinegarhq.Sober" in step["detail"], step
    assert (sandbox.state / "flatpaks").read_text(encoding="utf-8").split() == ["com.heroicgameslauncher.hgl", "com.usebottles.bottles"]


def test_the_flatpaks_that_fit_are_installed_as_before(sandbox: Sandbox) -> None:
    env = sandbox.disk(total_gb=100, free_gb=60)
    _ok(sandbox.run_hook(LINDOS_INSTALLER_STEPS="flatpaks", FAKE_EAT_FLATPAK_KB=str(kb(1.25)), **env))
    assert sandbox.statuses() == {"flatpaks": "done"}


# --- a disk the hook cannot measure -----------------------------------------------------------------------------------
def test_nothing_that_needs_room_is_started_when_the_free_space_cannot_be_measured(sandbox: Sandbox) -> None:
    env = sandbox.disk(total_gb=100, free_gb=50)
    _ok(sandbox.run_hook(FAKE_DF_FAIL="1", **env))
    assert sandbox.statuses() == {s: "pending" for s in ALL_STEPS}
    for step in ALL_STEPS:
        assert sandbox.step(step)["detail"].startswith("the free disk space could not be measured"), (step, sandbox.step(step))
    assert not any(c.startswith(("install-", "apt-get -y -q -d", "apt-get -y -q --no-download")) for c in sandbox.call_log())


# ============================================================================================ other round-B leftovers
def test_the_final_check_does_not_fail_the_extras_for_multi_arch_packages(sandbox: Sandbox) -> None:
    """libvulkan1 and mesa-vulkan-drivers are Multi-Arch: same: dpkg-query prints them as 'name:amd64' (the fake does too
    now); the end-of-install 'still installed' check used to look for the bare name and mark the whole step failed."""
    _ok(sandbox.run_hook(LINDOS_INSTALLER_STEPS="mode_extras"))
    assert sandbox.statuses() == {"mode_extras": "done"}, (sandbox.step("mode_extras"), sandbox.log_text()[-1500:])
    assert "libvulkan1" in installed(sandbox) and "mesa-vulkan-drivers" in installed(sandbox)
    rows = subprocess.run([BASH, "-c", "dpkg-query -W -f='${binary:Package} ${db:Status-Status}\\n'"], capture_output=True, text=True,
                          env=sandbox.env(), stdin=subprocess.DEVNULL).stdout
    assert "libvulkan1:amd64 installed" in rows, "the fake behaves like the real dpkg-query"


def test_a_download_that_finds_no_time_left_is_pending_not_failed(sandbox: Sandbox) -> None:
    """The simulation took all the budget: the group download gets 125 from li_dl without running anything.  The status has
    to come from LI_RC (it used to stay at the simulation's 0), or the step falls into the one-package-at-a-time loop and
    ends 'failed' with a pointless repair pass."""
    sandbox.enable_clock()
    _ok(sandbox.run_hook(LINDOS_INSTALLER_STEPS="mode_extras", FAKE_SIM_ADVANCE_CLOCK="5000"))
    step = sandbox.step("mode_extras")
    assert step["status"] == "pending" and "did not finish in time" in step["detail"], step
    assert len(downloads(sandbox)) == 0, "no download was started, and no slow one-by-one fallback either"
    assert "no time left for" in sandbox.log_text()


# ============================================================================================ breadcrumbs
CRUMB = re.compile(r"^step=(?P<step>[a-z_-]+) phase=(?P<phase>[a-z-]+) free_kb=(?P<free>\d+|unknown) "
                   r"utc=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ(?: .+)?$")


def test_the_breadcrumb_is_one_line_with_step_phase_free_space_and_utc_time(sandbox: Sandbox) -> None:
    _ok(sandbox.run_hook(LINDOS_INSTALLER_STEPS="updates browser"))
    text = sandbox.breadcrumb()
    assert text.endswith("\n") and text.count("\n") == 1, repr(text)
    m = CRUMB.match(text.strip())
    assert m, text
    assert (m["step"], m["phase"]) == ("-", "finished") and m["free"] == "60000000", text
    assert not (sandbox.target / "var/lib/lindos/installer-progress.new").exists(), "written to a new file and renamed"


def test_every_breadcrumb_is_flushed_with_sync_on_the_target(sandbox: Sandbox) -> None:
    _ok(sandbox.run_hook(LINDOS_INSTALLER_STEPS="updates browser"))
    syncs = sandbox.calls_of("sync")
    assert len(syncs) >= 2, syncs
    for call in syncs:
        assert call == "sync -f %s/var/lib/lindos/installer-progress" % sandbox.target.as_posix(), call


def test_a_hard_kill_leaves_the_last_thing_the_installer_was_doing_on_the_disk(sandbox: Sandbox) -> None:
    """SIGKILL: no trap runs, nothing can be written any more.  The breadcrumb names the step that was running and the
    install state already says every step is pending because the installer ended before it."""
    proc = sandbox.run_hook(FAKE_KILL_HOOK="1", FAKE_KILL_SIGNAL="KILL", FAKE_UPGRADES="libfoo",
                            LINDOS_INSTALLER_STEPS="updates browser drivers")
    assert proc.stdout == ""
    crumb = sandbox.breadcrumb().strip()
    m = CRUMB.match(crumb)
    assert m and (m["step"], m["phase"]) == ("updates", "start"), crumb
    assert sandbox.statuses() == {"updates": "pending", "browser": "pending", "drivers": "pending"}, sandbox.statuses()
    for step in ("updates", "browser", "drivers"):
        assert sandbox.step(step)["detail"] == "the installer ended before this step", sandbox.step(step)
    assert not list((sandbox.target / "var/lib/lindos").glob(".lindos-*.tmp")), "no half-written state file"
    assert sandbox.install_state()["steps"], "the state file is complete JSON"


def test_a_hard_kill_in_the_extras_names_the_group(sandbox: Sandbox) -> None:
    proc = sandbox.run_hook(FAKE_KILL_HOOK="1", FAKE_KILL_SIGNAL="KILL", LINDOS_INSTALLER_STEPS="mode_extras")
    assert proc.stdout == ""
    m = CRUMB.match(sandbox.breadcrumb().strip())
    assert m and (m["step"], m["phase"]) == ("mode_extras", "group-start") and "group=gaming" in sandbox.breadcrumb(), sandbox.breadcrumb()


def test_a_terminated_hook_says_which_step_it_was_in_and_marks_the_rest(sandbox: Sandbox) -> None:
    _ok(sandbox.run_hook(FAKE_KILL_HOOK="1", FAKE_UPGRADES="libfoo", LINDOS_INSTALLER_STEPS="updates browser"))
    assert sandbox.statuses() == {"updates": "pending", "browser": "pending"}
    assert sandbox.step("updates")["detail"] == "the installer was stopped during this step"
    assert sandbox.step("browser")["detail"] == "the installer ended before this step"
    crumb = CRUMB.match(sandbox.breadcrumb().strip())
    assert crumb and (crumb["step"], crumb["phase"]) == ("updates", "interrupted"), sandbox.breadcrumb()
    assert sandbox.held() == [], "and everything it held is released"


def test_offline_every_step_says_so(sandbox: Sandbox) -> None:
    _ok(sandbox.run_hook(FAKE_NET_RC="4"))
    assert sandbox.statuses() == {s: "pending" for s in ALL_STEPS}
    assert all(sandbox.step(s)["detail"] == "offline while installing" for s in ALL_STEPS)


def _hook_seconds(sb: Sandbox, **env: str) -> float:
    """Run the hook with output going to files (a pipe would be held open by an orphaned fake 'sync' on a Windows host, and
    the test would then measure the sleep, not the hook) and say how long it took."""
    out_file, err_file = sb.root / "hook.stdout", sb.root / "hook.stderr"
    started = time.time()
    with open(out_file, "wb") as out, open(err_file, "wb") as err:
        assert BASH is not None
        proc = subprocess.run([BASH, str(LIBEXEC / "target-config.sh")], stdout=out, stderr=err, stdin=subprocess.DEVNULL, timeout=300,
                              env=sb.env(LINDOS_INSTALLER_STEPS="browser drivers", **env))
    elapsed = time.time() - started
    assert proc.returncode == 0 and out_file.read_bytes() == b""
    assert sb.statuses() == {"browser": "done", "drivers": "done"}
    return elapsed


def test_the_hook_does_not_wait_for_a_sync_that_never_comes_back(sandbox: Sandbox) -> None:
    """'sync -f' that stops answering (a disk that hung) is waited for a second the first time and never again: the
    breadcrumbs of a dozen later phases must not add a minute to the install.  Compared with a run whose sync is quick, so a
    slow host does not matter; waiting for each stuck sync would add 25 seconds or more."""
    normal = _hook_seconds(sandbox)
    stuck = _hook_seconds(Sandbox(sandbox.root / "stuck"), FAKE_SYNC_SLEEP="25")
    assert stuck < normal + 12, (normal, stuck)


def test_the_hook_and_the_docs_agree_on_the_breadcrumb_and_the_knob() -> None:
    text = (REPO / "docs" / "INSTALLER.md").read_text(encoding="utf-8")
    for word in ("installer-progress", "lindos.install_reserve", "40 GB", "2 GB", "sync -f", "partition too small"):
        assert word in text, word
    assert "/var/lib/lindos/installer-progress" in (LIBEXEC / "lib.sh").read_text(encoding="utf-8")


def test_scripts_stay_lf_only() -> None:
    for name in ("lib.sh", "target-config.sh", "finalize.sh"):
        assert b"\r" not in (LIBEXEC / name).read_bytes(), name
