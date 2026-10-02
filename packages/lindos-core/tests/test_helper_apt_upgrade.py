"""The base-system upgrade and the safe cleanup in the privileged helper (SPEC-UPDATE.md §36.4):

* ``apt-full-upgrade``: payload validation (``lindos.helper``), the dry-run output, and the real flow
  driven with a fake ``ctx.run`` / ``_run_capture`` - simulate as root, refuse unless the result has the
  digest the user was shown, protected removals, kernel and removal approvals, disk space, download first,
  the in-progress marker (written before the download), the plan checked AGAIN after the download and apt told
  not to remove what was not approved, the shared apt lock, the always-run repair pass, the state refresh
  (``refreshed_at`` advances after a successful manual refresh; upgrades and sideloads rewrite the state).
* ``cleanup-old-packages``: the ported ``safe_autoremove`` (the desktop, the base system and the running /
  newest / previous kernel are never removed; a second simulation must prove the rest removes nothing else).

Hermetic like the rest of lindos-core's tests: no apt, no root, every path under a scratch ``LINDOS_ROOT``.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from lindos import helper as lhelper
from lindos import paths
from lindos import updatestate as lstate

_PKG_ROOT = Path(__file__).resolve().parent.parent
HELPER = _PKG_ROOT / "root" / "usr" / "libexec" / "lindos" / "lindos-helper"

ZERO_DIGEST = "sha256:" + "0" * 64
ONLINE_ENV = {"LINDOS_HELPER_ONLINE": "1", "LINDOS_FORCE_OFFLINE": "0"}

SIM_LINDOS = """NOTE: This is only a simulation!
Reading package lists...
The following packages will be upgraded:
  lindos-core lindos-meta
2 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.
Inst lindos-core [1.0.0] (1.0.1 Lindos:stable [all])
Inst lindos-meta [1.0.0] (1.0.1 Lindos:stable [all])
Conf lindos-core (1.0.1 Lindos:stable [all])
Conf lindos-meta (1.0.1 Lindos:stable [all])
"""
SIM_KERNEL = SIM_LINDOS + (
    "Inst linux-image-6.8.0-47-generic (6.8.0-47.47 Ubuntu:24.04/noble-updates, Ubuntu:24.04/noble-security [amd64])\n")
SIM_REMOVE_OTHER = SIM_LINDOS + "Remv libold1 [1.0-1]\n"
SIM_REMOVE_PROTECTED = SIM_LINDOS + "Remv lindos-desktop [1.0.0]\n"
SIM_EMPTY = "NOTE: This is only a simulation!\nReading package lists...\n0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
SIM_ERROR = "E: dpkg was interrupted, you must manually run 'dpkg --configure -a' to correct the problem.\n"


def _digest(text: str) -> str:
    return lstate.parse_simulation(text).digest


def _load_bin(path: Path, modname: str) -> types.ModuleType:
    loader = importlib.machinery.SourceFileLoader(modname, str(path))
    spec = importlib.util.spec_from_loader(modname, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    prev = sys.dont_write_bytecode
    sys.dont_write_bytecode = True   # never leave a __pycache__ inside root/usr/libexec (deb payload)
    try:
        loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = prev
    return module


@pytest.fixture()
def helper_mod(core_env):
    return _load_bin(HELPER, "lindos_core_helper_bin_apt_upgrade_test")


def _unquoted(text: str) -> str:
    return text.replace("'", "")


# =================================================================================================
# payload validation
# =================================================================================================
def test_apt_full_upgrade_is_a_helper_action_and_may_run_in_a_batch() -> None:
    assert "apt-full-upgrade" in lhelper.ACTIONS
    assert "apt-full-upgrade" not in lhelper.BATCH_FORBIDDEN_ACTIONS


def test_validate_payload_apt_full_upgrade_defaults_to_no_approvals() -> None:
    out = lhelper.validate_payload("apt-full-upgrade", {"plan_digest": ZERO_DIGEST})
    assert out == {"plan_digest": ZERO_DIGEST, "allow_kernel": False, "allow_removals": False}


def test_validate_payload_apt_full_upgrade_takes_the_two_approvals() -> None:
    out = lhelper.validate_payload("apt-full-upgrade", {"plan_digest": ZERO_DIGEST, "allow_kernel": True,
                                                         "allow_removals": True})
    assert out["allow_kernel"] is True and out["allow_removals"] is True


@pytest.mark.parametrize("payload", [
    {},                                                        # the digest is mandatory
    {"plan_digest": ""},
    {"plan_digest": "sha256:abc"},                             # too short
    {"plan_digest": "sha256:" + "0" * 63},
    {"plan_digest": "sha256:" + "0" * 65},
    {"plan_digest": "md5:" + "0" * 64},
    {"plan_digest": "sha256:" + "G" * 64},                     # not hex
    {"plan_digest": "sha256:" + "A" * 64},                     # upper case is not what the engine writes
    {"plan_digest": 12345},
    {"plan_digest": ZERO_DIGEST, "allow_kernel": "yes"},       # not a bool
    {"plan_digest": ZERO_DIGEST, "allow_removals": 1},
])
def test_validate_payload_apt_full_upgrade_rejects(payload: Dict[str, Any]) -> None:
    with pytest.raises(lhelper.PayloadError):
        lhelper.validate_payload("apt-full-upgrade", payload)


def test_apt_full_upgrade_payload_builder_matches_the_validator() -> None:
    from lindos import update as lupdate
    payload = lupdate.apply_all_payload(ZERO_DIGEST, allow_kernel=True)
    assert payload == {"plan_digest": ZERO_DIGEST, "allow_kernel": True}
    assert lhelper.validate_payload("apt-full-upgrade", payload)["plan_digest"] == ZERO_DIGEST
    assert lupdate.apply_all_payload(ZERO_DIGEST) == {"plan_digest": ZERO_DIGEST}


# =================================================================================================
# dry run through the real executable
# =================================================================================================
def test_helper_dry_run_apt_full_upgrade_shows_the_whole_sequence(core_env, run_cli) -> None:
    proc = run_cli("lindos-helper", "apt-full-upgrade", json.dumps({"plan_digest": ZERO_DIGEST}), env=ONLINE_ENV)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    out = _unquoted(proc.stdout)
    assert ZERO_DIGEST in out
    # simulate -> marker -> download -> simulate AGAIN -> upgrade -> repair, in that order
    sim = "apt-get -q -s dist-upgrade"
    download = "apt-get -d -y -q dist-upgrade"
    positions = [out.index(sim), out.index("update-in-progress"), out.index(download)]
    positions += [out.index(sim, positions[-1]), out.index("apt-get dist-upgrade -y -q"),
                  out.index("dpkg --configure -a --force-confdef --force-confold"), out.index("apt-get -f install -y -q")]
    assert positions == sorted(positions), out
    assert out.count(sim) == 2                                           # the plan is checked again after the download
    # no removal nobody approved: apt itself is told to abort rather than remove, in the download and the real run
    assert all("--no-remove" in line for line in out.splitlines() if download in line or "apt-get dist-upgrade -y -q" in line)
    assert "--force-confold" in out and "DPkg::Lock::Timeout=300" in out


def test_helper_apt_full_upgrade_needs_a_digest_at_the_door(core_env, run_cli) -> None:
    proc = run_cli("lindos-helper", "apt-full-upgrade", "{}", env=ONLINE_ENV)
    assert proc.returncode == 2
    assert "plan_digest" in (proc.stdout + proc.stderr)
    assert "would run" not in proc.stdout


def test_helper_lists_apt_full_upgrade(core_env, run_cli) -> None:
    assert "apt-full-upgrade" in run_cli("lindos-helper", "--list").stdout.split()


# =================================================================================================
# the real flow, with fake apt
# =================================================================================================
class Fake:
    """Records every command; each ``apt-get -s dist-upgrade`` returns *sim*; other commands succeed unless
    listed in *fail* (a substring of the joined argv)."""

    def __init__(self, sim: str, fail: Tuple[str, ...] = ()) -> None:
        self.sim = sim
        self.fail = fail
        self.calls: List[List[str]] = []
        self.marker_when_upgrading: Optional[bool] = None
        self.marker_when_downloading: Optional[bool] = None
        self.env_when_upgrading: Dict[str, str] = {}
        self.sim_after: Optional[str] = None       # what apt plans once the download has run (the lists changed)
        self.downloaded = False
        self.state_updates = 0

    def failing(self, cmd: List[str]) -> bool:
        line = " ".join(cmd)
        return any(f in line for f in self.fail)


def _make_ctx(helper_mod, monkeypatch: pytest.MonkeyPatch, fake: Fake, *, online: bool = True,
              disk_free: Optional[int] = 50 * 1024 ** 3, running: str = "6.8.0-45-generic"):
    ctx = helper_mod.Ctx(dry_run=False)
    monkeypatch.setattr(ctx, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(ctx, "online", lambda: online)

    def fake_run(cmd, **_kwargs):
        cmd = list(cmd)
        fake.calls.append(cmd)
        if cmd[:3] == ["apt-get", "-d", "-y"]:
            fake.marker_when_downloading = os.path.isfile(paths.resolve(lstate.IN_PROGRESS_PATH))
            fake.downloaded = True
        if cmd[:3] == ["apt-get", "dist-upgrade", "-y"]:
            fake.marker_when_upgrading = os.path.isfile(paths.resolve(lstate.IN_PROGRESS_PATH))
            fake.env_when_upgrading = dict(ctx.env)
        if fake.failing(cmd):
            return False, "boom"
        return True, ""

    def fake_capture(_ctx, cmd, timeout=30):
        cmd = list(cmd)
        if cmd[:4] == ["apt-get", "-q", "-s", "dist-upgrade"]:
            fake.calls.append(cmd)
            return (not fake.failing(cmd)), (fake.sim_after if fake.downloaded and fake.sim_after is not None else fake.sim)
        return True, ""

    monkeypatch.setattr(ctx, "run", fake_run)
    monkeypatch.setattr(helper_mod, "_run_capture", fake_capture)
    monkeypatch.setattr(helper_mod, "_disk_free", lambda path: disk_free)
    monkeypatch.setattr(helper_mod, "_running_kernel_release", lambda: running)
    return ctx


def _payload(sim: str, **extra: Any) -> Dict[str, Any]:
    return dict({"plan_digest": _digest(sim), "allow_kernel": False, "allow_removals": False}, **extra)


def _ran(fake: Fake, *prefix: str) -> bool:
    return any(c[:len(prefix)] == list(prefix) for c in fake.calls)


def _marker() -> bool:
    return os.path.isfile(paths.resolve(lstate.IN_PROGRESS_PATH))


def test_a_matching_plan_is_downloaded_first_marked_upgraded_repaired_and_reported(helper_mod, monkeypatch, core_env) -> None:
    fake = Fake(SIM_LINDOS)
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    assert helper_mod.act_apt_full_upgrade(ctx, _payload(SIM_LINDOS)) == helper_mod.EXIT_OK
    kinds = [" ".join(c[:3]) for c in fake.calls]
    order = [kinds.index("apt-get -d -y"), kinds.index("apt-get dist-upgrade -y"),
             kinds.index("dpkg --configure -a"), kinds.index("apt-get -f install")]
    assert order == sorted(order), kinds
    assert fake.marker_when_upgrading is True          # the marker exists while dpkg runs ...
    assert not _marker()                               # ... and is gone once the repair pass succeeded
    state = lstate.read_state()
    assert state is not None and state["digest"] == _digest(SIM_LINDOS)   # the state file was refreshed
    for cmd in fake.calls:                             # nothing ever goes through a shell
        assert all(isinstance(a, str) for a in cmd)
    upgrade = next(c for c in fake.calls if c[:3] == ["apt-get", "dist-upgrade", "-y"])
    assert "Dpkg::Options::=--force-confold" in upgrade


def test_a_plan_that_is_not_the_one_shown_is_refused_and_nothing_changes(helper_mod, monkeypatch, core_env) -> None:
    fake = Fake(SIM_LINDOS)
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    payload = {"plan_digest": ZERO_DIGEST}
    assert helper_mod.act_apt_full_upgrade(ctx, payload) == helper_mod.EXIT_ERROR
    assert not _ran(fake, "apt-get", "-d") and not _ran(fake, "apt-get", "dist-upgrade")
    assert not _ran(fake, "dpkg") and not _marker()


def test_the_digest_is_of_what_would_change_not_of_the_wording(helper_mod, monkeypatch, core_env) -> None:
    """The same Inst set with different noise (notes, Conf lines, order) has the same digest."""
    shuffled = ("NOTE: something else\n"
                "Inst lindos-meta [1.0.0] (1.0.1 Lindos:stable [all])\n"
                "Inst lindos-core [1.0.0] (1.0.1 Lindos:stable [all])\n")
    assert _digest(shuffled) == _digest(SIM_LINDOS)
    fake = Fake(shuffled)
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    assert helper_mod.act_apt_full_upgrade(ctx, _payload(SIM_LINDOS)) == helper_mod.EXIT_OK


def test_a_kernel_in_the_plan_needs_the_explicit_approval(helper_mod, monkeypatch, core_env) -> None:
    fake = Fake(SIM_KERNEL)
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    assert helper_mod.act_apt_full_upgrade(ctx, _payload(SIM_KERNEL)) == helper_mod.EXIT_ERROR
    assert not _ran(fake, "apt-get", "dist-upgrade")
    fake2 = Fake(SIM_KERNEL)
    ctx2 = _make_ctx(helper_mod, monkeypatch, fake2)
    assert helper_mod.act_apt_full_upgrade(ctx2, _payload(SIM_KERNEL, allow_kernel=True)) == helper_mod.EXIT_OK
    assert _ran(fake2, "apt-get", "dist-upgrade", "-y")


def test_a_protected_package_is_never_removed_even_when_removals_are_allowed(helper_mod, monkeypatch, core_env) -> None:
    fake = Fake(SIM_REMOVE_PROTECTED)
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    payload = _payload(SIM_REMOVE_PROTECTED, allow_removals=True)
    assert helper_mod.act_apt_full_upgrade(ctx, payload) == helper_mod.EXIT_ERROR
    assert not _ran(fake, "apt-get", "-d") and not _marker()


def test_the_running_kernel_is_never_removed(helper_mod, monkeypatch, core_env) -> None:
    sim = SIM_LINDOS + "Remv linux-image-6.8.0-45-generic [6.8.0-45.45]\n"
    fake = Fake(sim)
    ctx = _make_ctx(helper_mod, monkeypatch, fake, running="6.8.0-45-generic")
    assert helper_mod.act_apt_full_upgrade(ctx, _payload(sim, allow_removals=True, allow_kernel=True)) == helper_mod.EXIT_ERROR
    assert not _ran(fake, "apt-get", "dist-upgrade")


def test_other_removals_need_the_explicit_approval(helper_mod, monkeypatch, core_env) -> None:
    fake = Fake(SIM_REMOVE_OTHER)
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    assert helper_mod.act_apt_full_upgrade(ctx, _payload(SIM_REMOVE_OTHER)) == helper_mod.EXIT_ERROR
    fake2 = Fake(SIM_REMOVE_OTHER)
    ctx2 = _make_ctx(helper_mod, monkeypatch, fake2)
    assert helper_mod.act_apt_full_upgrade(ctx2, _payload(SIM_REMOVE_OTHER, allow_removals=True)) == helper_mod.EXIT_OK


def test_apt_errors_in_the_simulation_refuse_the_upgrade(helper_mod, monkeypatch, core_env) -> None:
    fake = Fake(SIM_ERROR)
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    assert helper_mod.act_apt_full_upgrade(ctx, {"plan_digest": ZERO_DIGEST}) == helper_mod.EXIT_ERROR
    assert not _ran(fake, "apt-get", "-d")


def test_an_empty_plan_is_nothing_to_do(helper_mod, monkeypatch, core_env) -> None:
    fake = Fake(SIM_EMPTY)
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    assert helper_mod.act_apt_full_upgrade(ctx, _payload(SIM_EMPTY)) == helper_mod.EXIT_OK
    assert not _ran(fake, "apt-get", "-d") and not _marker()


def test_offline_is_refused_before_anything_runs(helper_mod, monkeypatch, core_env) -> None:
    fake = Fake(SIM_LINDOS)
    ctx = _make_ctx(helper_mod, monkeypatch, fake, online=False)
    assert helper_mod.act_apt_full_upgrade(ctx, _payload(SIM_LINDOS)) == helper_mod.EXIT_ERROR
    assert fake.calls == []


def test_too_little_disk_space_is_refused_before_the_download(helper_mod, monkeypatch, core_env) -> None:
    fake = Fake(SIM_LINDOS)
    ctx = _make_ctx(helper_mod, monkeypatch, fake, disk_free=100 * 1024 * 1024)
    assert helper_mod.act_apt_full_upgrade(ctx, _payload(SIM_LINDOS)) == helper_mod.EXIT_ERROR
    assert not _ran(fake, "apt-get", "-d") and not _marker()


def test_a_new_kernel_needs_room_in_boot(helper_mod, monkeypatch, core_env) -> None:
    fake = Fake(SIM_KERNEL)
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    monkeypatch.setattr(helper_mod, "_disk_free", lambda path: 50 * 1024 ** 3 if path == "/" else 10 * 1024 * 1024)
    assert helper_mod.act_apt_full_upgrade(ctx, _payload(SIM_KERNEL, allow_kernel=True)) == helper_mod.EXIT_ERROR
    assert not _ran(fake, "apt-get", "-d")


def test_unreadable_disk_space_does_not_block_the_upgrade(helper_mod, monkeypatch, core_env) -> None:
    fake = Fake(SIM_LINDOS)
    ctx = _make_ctx(helper_mod, monkeypatch, fake, disk_free=None)
    assert helper_mod.act_apt_full_upgrade(ctx, _payload(SIM_LINDOS)) == helper_mod.EXIT_OK


def test_a_failed_download_stops_before_dpkg_and_leaves_no_marker(helper_mod, monkeypatch, core_env) -> None:
    fake = Fake(SIM_LINDOS, fail=("apt-get -d",))
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    assert helper_mod.act_apt_full_upgrade(ctx, _payload(SIM_LINDOS)) == helper_mod.EXIT_ERROR
    assert not _ran(fake, "apt-get", "dist-upgrade") and not _ran(fake, "dpkg")
    assert not _marker()


def test_a_failed_upgrade_is_still_repaired_and_the_marker_cleared(helper_mod, monkeypatch, core_env) -> None:
    fake = Fake(SIM_LINDOS, fail=("apt-get dist-upgrade",))
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    assert helper_mod.act_apt_full_upgrade(ctx, _payload(SIM_LINDOS)) == helper_mod.EXIT_ERROR
    assert _ran(fake, "dpkg", "--configure", "-a") and _ran(fake, "apt-get", "-f", "install")
    assert not _marker()                    # the repair worked: nothing is left half-done


def test_when_the_repair_fails_the_marker_stays_for_the_boot_time_repair(helper_mod, monkeypatch, core_env) -> None:
    fake = Fake(SIM_LINDOS, fail=("dpkg --configure",))
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    assert helper_mod.act_apt_full_upgrade(ctx, _payload(SIM_LINDOS)) == helper_mod.EXIT_ERROR
    assert _marker()


def test_an_interrupted_earlier_upgrade_is_repaired_first(helper_mod, monkeypatch, core_env) -> None:
    lstate.write_marker("apt-full-upgrade", digest=ZERO_DIGEST)
    fake = Fake(SIM_LINDOS)
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    assert helper_mod.act_apt_full_upgrade(ctx, _payload(SIM_LINDOS)) == helper_mod.EXIT_OK
    kinds = [" ".join(c[:3]) for c in fake.calls]
    assert kinds.index("dpkg --configure -a") < kinds.index("apt-get -d -y")      # repaired before the new work
    assert not _marker()


def test_an_earlier_upgrade_that_cannot_be_repaired_stops_everything(helper_mod, monkeypatch, core_env) -> None:
    lstate.write_marker("apt-full-upgrade")
    fake = Fake(SIM_LINDOS, fail=("dpkg --configure",))
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    assert helper_mod.act_apt_full_upgrade(ctx, _payload(SIM_LINDOS)) == helper_mod.EXIT_ERROR
    assert not _ran(fake, "apt-get", "-d") and _marker()


# =================================================================================================
# the plan is checked AGAIN after the download, and apt is told not to remove what was not approved
# =================================================================================================
# The regression: the digest, the kernel gate and the protected-removal list were checked once, on one simulation; the
# download (up to an hour) and the real 'apt-get dist-upgrade' were new processes that re-resolved from whatever the
# lists said by then - so a refresh in between could turn an approved plan into one with a kernel or a removal.
def _simulations(fake: Fake) -> int:
    return sum(1 for c in fake.calls if c[:4] == ["apt-get", "-q", "-s", "dist-upgrade"])


def test_the_plan_is_simulated_again_after_the_download_and_before_dpkg_runs(helper_mod, monkeypatch, core_env) -> None:
    fake = Fake(SIM_LINDOS)
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    assert helper_mod.act_apt_full_upgrade(ctx, _payload(SIM_LINDOS)) == helper_mod.EXIT_OK
    kinds = [" ".join(c[:4]) for c in fake.calls]
    first, download = kinds.index("apt-get -q -s dist-upgrade"), kinds.index("apt-get -d -y -q")
    second = kinds.index("apt-get -q -s dist-upgrade", download)
    upgrade = kinds.index("apt-get dist-upgrade -y -q")
    assert first < download < second < upgrade
    assert _simulations(fake) >= 2


def test_a_kernel_that_appears_during_the_download_is_refused_before_dpkg(helper_mod, monkeypatch, core_env) -> None:
    fake = Fake(SIM_LINDOS)
    fake.sim_after = SIM_KERNEL                        # the lists changed while the packages were downloading
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    assert helper_mod.act_apt_full_upgrade(ctx, _payload(SIM_LINDOS)) == helper_mod.EXIT_ERROR
    assert _ran(fake, "apt-get", "-d")                 # it did download (the first check passed) ...
    assert not _ran(fake, "apt-get", "dist-upgrade") and not _ran(fake, "dpkg")   # ... and never ran dpkg
    assert not _marker()                               # nothing was installed, so nothing is left to repair


def test_a_protected_removal_that_appears_during_the_download_is_refused_even_when_removals_are_allowed(helper_mod, monkeypatch, core_env) -> None:
    fake = Fake(SIM_LINDOS)
    fake.sim_after = SIM_REMOVE_PROTECTED
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    payload = _payload(SIM_LINDOS, allow_removals=True, allow_kernel=True)
    assert helper_mod.act_apt_full_upgrade(ctx, payload) == helper_mod.EXIT_ERROR
    assert not _ran(fake, "apt-get", "dist-upgrade") and not _marker()


@pytest.mark.parametrize("after", [SIM_REMOVE_OTHER, SIM_KERNEL, SIM_LINDOS.replace("1.0.1", "1.0.2"), SIM_EMPTY])
def test_any_change_of_the_plan_during_the_download_is_refused(helper_mod, monkeypatch, core_env, after: str) -> None:
    fake = Fake(SIM_LINDOS)
    fake.sim_after = after
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    payload = _payload(SIM_LINDOS, allow_removals=True, allow_kernel=True)
    assert helper_mod.act_apt_full_upgrade(ctx, payload) == helper_mod.EXIT_ERROR
    assert not _ran(fake, "apt-get", "dist-upgrade") and not _marker()


def test_a_plan_that_changed_during_the_download_says_so_and_refreshes_the_state(helper_mod, monkeypatch, core_env, capsys) -> None:
    fake = Fake(SIM_LINDOS)
    fake.sim_after = SIM_KERNEL
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    assert helper_mod.act_apt_full_upgrade(ctx, _payload(SIM_LINDOS)) == helper_mod.EXIT_ERROR
    err = capsys.readouterr().err
    assert "changed while the packages were downloading" in err and "nothing was installed" in err
    state = lstate.read_state()
    assert state is not None and state["digest"] == _digest(SIM_KERNEL)     # the next `status` shows the new plan


def test_an_unchanged_plan_after_the_download_still_goes_through(helper_mod, monkeypatch, core_env) -> None:
    fake = Fake(SIM_KERNEL)
    fake.sim_after = SIM_KERNEL
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    assert helper_mod.act_apt_full_upgrade(ctx, _payload(SIM_KERNEL, allow_kernel=True)) == helper_mod.EXIT_OK
    assert _ran(fake, "apt-get", "dist-upgrade", "-y")


def test_apt_is_told_not_to_remove_anything_unless_removals_were_approved(helper_mod, monkeypatch, core_env) -> None:
    fake = Fake(SIM_LINDOS)
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    assert helper_mod.act_apt_full_upgrade(ctx, _payload(SIM_LINDOS)) == helper_mod.EXIT_OK
    download = next(c for c in fake.calls if c[:3] == ["apt-get", "-d", "-y"])
    upgrade = next(c for c in fake.calls if c[:3] == ["apt-get", "dist-upgrade", "-y"])
    assert "--no-remove" in download and "--no-remove" in upgrade
    approved = Fake(SIM_REMOVE_OTHER)
    ctx2 = _make_ctx(helper_mod, monkeypatch, approved)
    assert helper_mod.act_apt_full_upgrade(ctx2, _payload(SIM_REMOVE_OTHER, allow_removals=True)) == helper_mod.EXIT_OK
    for cmd in approved.calls:
        assert "--no-remove" not in cmd                # approved: the plan itself contains the removal


def test_the_marker_is_written_before_the_download_not_after_it(helper_mod, monkeypatch, core_env) -> None:
    """A list refresh that starts during the (up to one hour) download must already see it."""
    fake = Fake(SIM_LINDOS)
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    assert helper_mod.act_apt_full_upgrade(ctx, _payload(SIM_LINDOS)) == helper_mod.EXIT_OK
    assert fake.marker_when_downloading is True and fake.marker_when_upgrading is True and not _marker()


def test_the_marker_is_never_written_when_the_first_checks_refuse(helper_mod, monkeypatch, core_env) -> None:
    fake = Fake(SIM_LINDOS)
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    assert helper_mod.act_apt_full_upgrade(ctx, {"plan_digest": ZERO_DIGEST}) == helper_mod.EXIT_ERROR
    assert fake.marker_when_downloading is None and not _marker()


# =================================================================================================
# the helper takes its turn behind every other Lindos apt job (the flock apt-serialise uses)
# =================================================================================================
class FakeFcntl:
    LOCK_EX, LOCK_NB, LOCK_UN = 2, 4, 8

    def __init__(self, busy: int = 0) -> None:
        self.busy = busy
        self.calls: List[int] = []

    def flock(self, _fh: Any, op: int) -> None:
        self.calls.append(op)
        if op & self.LOCK_NB and self.busy > 0:
            self.busy -= 1
            raise BlockingIOError(11, "busy")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps = 0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.sleeps += 1


def _turn_env(helper_mod, monkeypatch, fcntl_module, *, wait: float = 30.0) -> Clock:
    clock = Clock()
    monkeypatch.setattr(helper_mod, "_fcntl", fcntl_module)
    monkeypatch.setattr(helper_mod, "_apt_clock", clock.monotonic)
    monkeypatch.setattr(helper_mod, "_apt_sleep", clock.sleep)
    monkeypatch.setattr(helper_mod, "APT_TURN_WAIT", wait)
    return clock


def test_the_upgrade_holds_the_shared_apt_lock_from_start_to_finish(helper_mod, monkeypatch, core_env) -> None:
    fcntl_module = FakeFcntl()
    _turn_env(helper_mod, monkeypatch, fcntl_module)
    fake = Fake(SIM_LINDOS)
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    assert helper_mod.act_apt_full_upgrade(ctx, _payload(SIM_LINDOS)) == helper_mod.EXIT_OK
    assert fcntl_module.calls == [FakeFcntl.LOCK_EX | FakeFcntl.LOCK_NB, FakeFcntl.LOCK_UN]     # taken once, released once
    assert os.path.isfile(paths.resolve(helper_mod.APT_TURN_LOCK))                             # the file apt-serialise queues on
    assert fake.env_when_upgrading.get("LINDOS_APT_SERIALISED") == "1"    # a script that queues behind it must not wait for us
    assert "LINDOS_APT_SERIALISED" not in ctx.env                          # ... and only while we hold it


def test_the_lock_is_released_when_the_upgrade_fails(helper_mod, monkeypatch, core_env) -> None:
    fcntl_module = FakeFcntl()
    _turn_env(helper_mod, monkeypatch, fcntl_module)
    ctx = _make_ctx(helper_mod, monkeypatch, Fake(SIM_LINDOS, fail=("apt-get dist-upgrade",)))
    assert helper_mod.act_apt_full_upgrade(ctx, _payload(SIM_LINDOS)) == helper_mod.EXIT_ERROR
    assert fcntl_module.calls[-1] == FakeFcntl.LOCK_UN


def test_it_waits_for_a_job_that_is_ahead_and_then_goes(helper_mod, monkeypatch, core_env, capsys) -> None:
    fcntl_module = FakeFcntl(busy=3)
    clock = _turn_env(helper_mod, monkeypatch, fcntl_module)
    ctx = _make_ctx(helper_mod, monkeypatch, Fake(SIM_LINDOS))
    assert helper_mod.act_apt_full_upgrade(ctx, _payload(SIM_LINDOS)) == helper_mod.EXIT_OK
    assert clock.sleeps == 3 and capsys.readouterr().out.count("waiting for another Lindos update job") == 1


def test_it_refuses_when_the_turn_never_comes_and_changes_nothing(helper_mod, monkeypatch, core_env, capsys) -> None:
    fcntl_module = FakeFcntl(busy=10 ** 6)
    _turn_env(helper_mod, monkeypatch, fcntl_module, wait=5.0)
    fake = Fake(SIM_LINDOS)
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    assert helper_mod.act_apt_full_upgrade(ctx, _payload(SIM_LINDOS)) == helper_mod.EXIT_ERROR
    assert fake.calls == [] and not _marker()                              # not a single apt command, no marker
    assert "another Lindos update job is still running" in capsys.readouterr().err
    assert FakeFcntl.LOCK_UN not in fcntl_module.calls                     # it never held the lock


def test_inside_apt_serialise_the_lock_is_not_taken_again(helper_mod, monkeypatch, core_env) -> None:
    """apt-serialise holds the flock and says so; waiting for our own parent's lock would deadlock."""
    fcntl_module = FakeFcntl(busy=10 ** 6)
    _turn_env(helper_mod, monkeypatch, fcntl_module)
    ctx = _make_ctx(helper_mod, monkeypatch, Fake(SIM_LINDOS))
    ctx.env["LINDOS_APT_SERIALISED"] = "1"
    assert helper_mod.act_apt_full_upgrade(ctx, _payload(SIM_LINDOS)) == helper_mod.EXIT_OK
    assert fcntl_module.calls == []
    assert ctx.env["LINDOS_APT_SERIALISED"] == "1"                         # the parent's marker is left alone


def test_a_dry_run_takes_no_lock(helper_mod, monkeypatch, core_env) -> None:
    fcntl_module = FakeFcntl()
    _turn_env(helper_mod, monkeypatch, fcntl_module)
    ctx = helper_mod.Ctx(dry_run=True)
    assert helper_mod.act_apt_full_upgrade(ctx, {"plan_digest": ZERO_DIGEST}) == helper_mod.EXIT_OK
    assert fcntl_module.calls == []


def test_without_fcntl_or_a_usable_lock_file_the_upgrade_still_runs(helper_mod, monkeypatch, core_env) -> None:
    """Best effort like apt-serialise: this never stops an upgrade that would otherwise run."""
    _turn_env(helper_mod, monkeypatch, None)                               # not Linux
    assert helper_mod.act_apt_full_upgrade(_make_ctx(helper_mod, monkeypatch, Fake(SIM_LINDOS)), _payload(SIM_LINDOS)) == helper_mod.EXIT_OK
    fcntl_module = FakeFcntl()
    _turn_env(helper_mod, monkeypatch, fcntl_module)
    Path(paths.resolve("/run")).write_text("a file where the lock directory should be", encoding="utf-8")
    ctx = _make_ctx(helper_mod, monkeypatch, Fake(SIM_LINDOS))
    assert helper_mod.act_apt_full_upgrade(ctx, _payload(SIM_LINDOS)) == helper_mod.EXIT_OK
    assert fcntl_module.calls == []


def test_a_real_flock_held_by_another_process_makes_the_helper_wait_and_then_refuse(helper_mod, monkeypatch, core_env) -> None:
    fcntl_module = pytest.importorskip("fcntl")
    monkeypatch.setattr(helper_mod, "_fcntl", fcntl_module)
    monkeypatch.setattr(helper_mod, "APT_TURN_WAIT", 0.3)
    monkeypatch.setattr(helper_mod, "_apt_sleep", lambda _s: __import__("time").sleep(0.05))
    lock = Path(paths.resolve(helper_mod.APT_TURN_LOCK))
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+") as other:                                        # an apt-serialise'd refresh, say
        fcntl_module.flock(other, fcntl_module.LOCK_EX)
        fake = Fake(SIM_LINDOS)
        ctx = _make_ctx(helper_mod, monkeypatch, fake)
        assert helper_mod.act_apt_full_upgrade(ctx, _payload(SIM_LINDOS)) == helper_mod.EXIT_ERROR
        assert fake.calls == []
        fcntl_module.flock(other, fcntl_module.LOCK_UN)
    ctx = _make_ctx(helper_mod, monkeypatch, Fake(SIM_LINDOS))
    assert helper_mod.act_apt_full_upgrade(ctx, _payload(SIM_LINDOS)) == helper_mod.EXIT_OK


def test_apt_get_update_takes_its_turn_too(helper_mod, monkeypatch, core_env, capsys) -> None:
    busy = FakeFcntl(busy=10 ** 6)
    _turn_env(helper_mod, monkeypatch, busy, wait=3.0)
    fake = Fake(SIM_LINDOS)
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    assert helper_mod.act_apt_get_update(ctx, {}) == helper_mod.EXIT_ERROR
    assert fake.calls == [] and "still running" in capsys.readouterr().err
    free = FakeFcntl()
    _turn_env(helper_mod, monkeypatch, free)
    assert helper_mod.act_apt_get_update(_make_ctx(helper_mod, monkeypatch, Fake(SIM_LINDOS)), {}) == helper_mod.EXIT_OK
    assert free.calls == [FakeFcntl.LOCK_EX | FakeFcntl.LOCK_NB, FakeFcntl.LOCK_UN]


# =================================================================================================
# the state file after a refresh, an upgrade of the Lindos components and a sideload
# =================================================================================================
OLD_STAMP = "2026-01-01T00:00:00+00:00"


def _seed_state(refreshed_at: str = OLD_STAMP) -> None:
    lstate.write_json_atomic(paths.resolve(lstate.STATE_PATH), {"schema": 1, "refreshed_at": refreshed_at, "digest": "old"})


def _age(state: Dict[str, Any]) -> float:
    age = lstate.state_age_seconds(state)
    assert age is not None
    return age


def test_a_successful_manual_refresh_advances_refreshed_at(helper_mod, monkeypatch, core_env) -> None:
    """A laptop that was off for days: `lindos-update check --refresh` succeeds, yet `status` went on saying the
    lists had not been refreshed for days, because the helper re-wrote the state without touching the stamp."""
    _seed_state()
    ctx = _make_ctx(helper_mod, monkeypatch, Fake(SIM_LINDOS))
    assert helper_mod.act_apt_get_update(ctx, {}) == helper_mod.EXIT_OK
    state = lstate.read_state()
    assert state is not None and state["refreshed_at"] != OLD_STAMP
    assert _age(state) < 120 and _age(state) < lstate.STALE_AFTER_SECONDS
    assert state["refresh"]["ok"] is True


def test_a_failed_refresh_leaves_the_old_stamp_alone(helper_mod, monkeypatch, core_env) -> None:
    _seed_state()
    ctx = _make_ctx(helper_mod, monkeypatch, Fake(SIM_LINDOS, fail=("apt-get update",)))
    assert helper_mod.act_apt_get_update(ctx, {}) == helper_mod.EXIT_ERROR
    state = lstate.read_state()
    assert state is not None and state["refreshed_at"] == OLD_STAMP


def test_installing_the_lindos_components_rewrites_the_state_but_not_the_stamp(helper_mod, monkeypatch, core_env) -> None:
    """After `lindos-update apply` the state listed the just-installed updates until the next timer run (up to 12 h)."""
    _seed_state()
    fake = Fake(SIM_EMPTY)
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    assert helper_mod.act_system_upgrade(ctx, {"packages": ["lindos-core=1.0.1"]}) == helper_mod.EXIT_OK
    state = lstate.read_state()
    assert state is not None and state["digest"] == _digest(SIM_EMPTY) and state["counts"]["total"] == 0
    assert state["refreshed_at"] == OLD_STAMP                # no apt-get update ran: the lists are exactly as old as before


def test_a_failed_component_upgrade_still_refreshes_the_state(helper_mod, monkeypatch, core_env) -> None:
    _seed_state()
    ctx = _make_ctx(helper_mod, monkeypatch, Fake(SIM_LINDOS, fail=("--only-upgrade",)))
    assert helper_mod.act_system_upgrade(ctx, {"packages": ["lindos-core=1.0.1"]}) == helper_mod.EXIT_ERROR
    state = lstate.read_state()
    assert state is not None and state["digest"] == _digest(SIM_LINDOS)


def test_a_sideload_rewrites_the_state(helper_mod, monkeypatch, core_env, tmp_path) -> None:
    _seed_state()
    deb = tmp_path / "lindos-core_1.0.1_all.deb"
    deb.write_bytes(b"deb")
    fake = Fake(SIM_EMPTY)
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    previous_capture = helper_mod._run_capture

    def capture(_ctx, cmd, timeout=30):
        if list(cmd)[:2] == ["/usr/bin/dpkg-deb", "--field"]:
            return True, "lindos-core"
        return previous_capture(_ctx, cmd, timeout)

    monkeypatch.setattr(helper_mod, "_run_capture", capture)
    assert helper_mod.act_install_local_debs(ctx, {"files": [str(deb)]}) == helper_mod.EXIT_OK
    assert _ran(fake, "dpkg", "-i", "--")
    state = lstate.read_state()
    assert state is not None and state["digest"] == _digest(SIM_EMPTY)
    assert state["refreshed_at"] == OLD_STAMP


def test_the_action_is_registered_in_the_handler_table(helper_mod) -> None:
    assert helper_mod.HANDLERS["apt-full-upgrade"] is helper_mod.act_apt_full_upgrade
    assert set(helper_mod.HANDLERS) == set(lhelper.ACTIONS)


# =================================================================================================
# apt-get-update also refreshes the state file
# =================================================================================================
def test_apt_get_update_refreshes_the_state_file(helper_mod, monkeypatch, core_env) -> None:
    fake = Fake(SIM_LINDOS)
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    assert helper_mod.act_apt_get_update(ctx, {}) == helper_mod.EXIT_OK
    assert _ran(fake, "apt-get", "update", "-q")
    state = lstate.read_state()
    assert state is not None and state["counts"]["lindos"] == 2 and state["digest"] == _digest(SIM_LINDOS)


def test_a_failed_apt_get_update_does_not_touch_the_state(helper_mod, monkeypatch, core_env) -> None:
    fake = Fake(SIM_LINDOS, fail=("apt-get update",))
    ctx = _make_ctx(helper_mod, monkeypatch, fake)
    assert helper_mod.act_apt_get_update(ctx, {}) == helper_mod.EXIT_ERROR
    assert lstate.read_state() is None


# =================================================================================================
# the safe cleanup
# =================================================================================================
class CleanupFake:
    def __init__(self, autoremove: str, purge_sim: Optional[str] = None,
                 kernels: Tuple[str, ...] = ("linux-image-6.8.0-38-generic", "linux-image-6.8.0-40-generic",
                                             "linux-image-6.8.0-42-generic", "linux-image-6.8.0-45-generic")) -> None:
        self.autoremove = autoremove
        self.purge_sim = purge_sim
        self.kernels = kernels
        self.captured: List[List[str]] = []
        self.calls: List[List[str]] = []


def _cleanup_ctx(helper_mod, monkeypatch, fake: CleanupFake, *, running: str = "6.8.0-45-generic"):
    ctx = helper_mod.Ctx(dry_run=False)
    monkeypatch.setattr(ctx, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(ctx, "online", lambda: True)

    def fake_run(cmd, **_kwargs):
        fake.calls.append(list(cmd))
        return True, ""

    def fake_capture(_ctx, cmd, timeout=30):
        cmd = list(cmd)
        fake.captured.append(cmd)
        if cmd[:2] == ["dpkg-query", "-W"]:
            return True, "\n".join(fake.kernels) + "\n"
        if cmd[:5] == ["apt-get", "-q", "-s", "autoremove", "--purge"]:
            return True, fake.autoremove
        if cmd[:5] == ["apt-get", "-q", "-s", "purge", "--"]:
            if fake.purge_sim is not None:
                return True, fake.purge_sim
            return True, "".join(f"Purg {name} [1.0]\n" for name in cmd[5:])
        return True, ""

    monkeypatch.setattr(ctx, "run", fake_run)
    monkeypatch.setattr(helper_mod, "_run_capture", fake_capture)
    monkeypatch.setattr(helper_mod, "_running_kernel_release", lambda: running)
    return ctx


def _purged(fake: CleanupFake) -> List[str]:
    for cmd in fake.calls:
        if cmd[:5] == ["apt-get", "purge", "-y", "-q", "--"]:
            return cmd[5:]
    return []


AUTOREMOVE = (
    "Purg libold1 [1.0]\nPurg linux-image-6.8.0-38-generic [6.8.0-38.38]\nPurg linux-modules-6.8.0-38-generic [6.8.0-38.38]\n"
    "Purg linux-image-6.8.0-40-generic [6.8.0-40.40]\nPurg linux-image-6.8.0-42-generic [6.8.0-42.42]\n"
    "Purg linux-image-6.8.0-45-generic [6.8.0-45.45]\nPurg xfce4-goodies [4.18]\nPurg lindos-extra [1.0.0]\n"
    "Purg mint-thing [1.0]\nPurg libgtk-3-0t64 [3.24]\n")


def test_cleanup_removes_only_what_is_safe(helper_mod, monkeypatch, core_env) -> None:
    fake = CleanupFake(AUTOREMOVE)
    ctx = _cleanup_ctx(helper_mod, monkeypatch, fake)
    assert helper_mod.act_cleanup_old_packages(ctx, {}) == helper_mod.EXIT_OK
    # of four installed kernels the running (45), the newest (also 45) and the one before (42) stay
    assert sorted(_purged(fake)) == sorted(["libold1", "linux-image-6.8.0-38-generic",
                                            "linux-modules-6.8.0-38-generic", "linux-image-6.8.0-40-generic"])


def test_cleanup_keeps_the_running_newest_and_previous_kernel(helper_mod, monkeypatch, core_env) -> None:
    fake = CleanupFake(AUTOREMOVE)
    ctx = _cleanup_ctx(helper_mod, monkeypatch, fake, running="6.8.0-45-generic")
    helper_mod.act_cleanup_old_packages(ctx, {})
    purged = _purged(fake)
    for name in ("linux-image-6.8.0-45-generic", "linux-image-6.8.0-42-generic"):
        assert name not in purged                 # the running kernel and the newest previous one
    assert "linux-image-6.8.0-38-generic" in purged


def test_cleanup_never_touches_the_desktop_or_the_base_system(helper_mod, monkeypatch, core_env) -> None:
    fake = CleanupFake(AUTOREMOVE)
    ctx = _cleanup_ctx(helper_mod, monkeypatch, fake)
    helper_mod.act_cleanup_old_packages(ctx, {})
    purged = _purged(fake)
    for name in ("xfce4-goodies", "lindos-extra", "mint-thing", "libgtk-3-0t64"):
        assert name not in purged


def test_cleanup_proves_the_subset_with_a_second_simulation(helper_mod, monkeypatch, core_env) -> None:
    fake = CleanupFake(AUTOREMOVE)
    ctx = _cleanup_ctx(helper_mod, monkeypatch, fake)
    helper_mod.act_cleanup_old_packages(ctx, {})
    sims = [c for c in fake.captured if c[:5] == ["apt-get", "-q", "-s", "purge", "--"]]
    assert len(sims) == 1 and set(sims[0][5:]) == set(_purged(fake))


def test_cleanup_refuses_when_removing_the_subset_would_take_something_else_along(helper_mod, monkeypatch, core_env) -> None:
    extra = "Purg libold1 [1.0]\nPurg xfce4-panel [4.18]\n"
    fake = CleanupFake("Purg libold1 [1.0]\n", purge_sim=extra)
    ctx = _cleanup_ctx(helper_mod, monkeypatch, fake)
    assert helper_mod.act_cleanup_old_packages(ctx, {}) == helper_mod.EXIT_ERROR
    assert _purged(fake) == []


def test_cleanup_refuses_a_very_long_list(helper_mod, monkeypatch, core_env) -> None:
    fake = CleanupFake("".join(f"Purg libjunk{i} [1.0]\n" for i in range(40)))
    ctx = _cleanup_ctx(helper_mod, monkeypatch, fake)
    assert helper_mod.act_cleanup_old_packages(ctx, {}) == helper_mod.EXIT_ERROR
    assert _purged(fake) == []


def test_cleanup_with_nothing_to_remove_is_a_no_op(helper_mod, monkeypatch, core_env) -> None:
    fake = CleanupFake("")
    ctx = _cleanup_ctx(helper_mod, monkeypatch, fake)
    assert helper_mod.act_cleanup_old_packages(ctx, {}) == helper_mod.EXIT_OK
    assert _purged(fake) == []


def test_cleanup_with_only_protected_candidates_removes_nothing(helper_mod, monkeypatch, core_env) -> None:
    fake = CleanupFake("Purg xfce4-goodies [4.18]\nPurg lindos-extra [1.0.0]\n")
    ctx = _cleanup_ctx(helper_mod, monkeypatch, fake)
    assert helper_mod.act_cleanup_old_packages(ctx, {}) == helper_mod.EXIT_OK
    assert _purged(fake) == []


def test_cleanup_dry_run_only_simulates(core_env, run_cli) -> None:
    proc = run_cli("lindos-helper", "cleanup-old-packages", "{}")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    out = _unquoted(proc.stdout)
    assert "apt-get -q -s autoremove --purge" in out
    assert "apt-get autoremove --purge -y" not in out      # the unsafe bare autoremove is gone
