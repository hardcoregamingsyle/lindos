"""``lindos-tune sched`` + the §16 memory tuning (SPEC-KERNEL §16, §18, §19).

Covers: sched_ext support probing against a faked ``/sys`` + ``/boot`` tree, scheduler listing
(scx_loader / PATH / known), profile validation, ``set`` via the systemd fallback, the
degrade-to-exit-3 path when the kernel has no sched_ext, the privileged op → helper action
mapping, the tune.d ``sched``/``thp``/``mglru`` keys, and THP/MGLRU apply + status reporting.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import tune_testlib as tl
from lindos_tune import apply, cli, common, privileged, sched, status
from lindos_tune.common import CmdResult


def _mkdir(root: Path, system_path: str) -> None:
    Path(common.path(system_path, str(root))).mkdir(parents=True, exist_ok=True)


# --- sched_ext support probing ------------------------------------------------------------------
def test_sched_ext_absent_by_default(make_ctx) -> None:
    ctx = make_ctx()
    assert sched.sched_ext_supported(ctx) is False
    assert status.thp_mode(ctx.root) is None


def test_sched_ext_via_sysfs_dir(staging: Path, make_ctx) -> None:
    _mkdir(staging, sched.SCHED_EXT_SYS)
    assert sched.sched_ext_supported(make_ctx()) is True


def test_sched_ext_via_kernel_config(staging: Path, make_ctx) -> None:
    tl.write(staging, "/proc/sys/kernel/osrelease", "6.14.0-lindos\n")
    tl.write(staging, "/boot/config-6.14.0-lindos", "CONFIG_FOO=y\nCONFIG_SCHED_CLASS_EXT=y\n")
    assert sched.sched_ext_supported(make_ctx()) is True


def test_kernel_config_symbol_not_set(staging: Path, make_ctx) -> None:
    tl.write(staging, "/proc/sys/kernel/osrelease", "6.8.0-generic\n")
    tl.write(staging, "/boot/config-6.8.0-generic", "# CONFIG_SCHED_CLASS_EXT is not set\n")
    assert sched.sched_ext_supported(make_ctx()) is False


# --- listing ------------------------------------------------------------------------------------
def test_list_known_when_nothing_installed(make_ctx) -> None:
    data = sched.list_schedulers(make_ctx())
    assert data["source"] == "known" and data["active"] is None
    by_name = {s["name"]: s for s in data["schedulers"]}
    for name in sched.SETTABLE_PROFILES:
        if name != "none":
            assert by_name[name]["installed"] is False and by_name[name]["settable"] is True


def test_list_from_path_binaries(make_ctx) -> None:
    ctx = make_ctx(tools=("scx_lavd", "scx_flash"))
    data = sched.list_schedulers(ctx)
    assert data["source"] == "PATH"
    by_name = {s["name"]: s for s in data["schedulers"]}
    assert by_name["scx_lavd"]["installed"] and by_name["scx_flash"]["installed"]
    assert by_name["scx_rustland"]["installed"] is False


def test_list_from_scx_loader(make_ctx, recorder) -> None:
    recorder.add("SupportedSchedulers", CmdResult(True, 0, 'as 2 "scx_lavd" "scx_rustland"'))
    ctx = make_ctx(tools=("scx_loader", "busctl"), runner=recorder)
    data = sched.list_schedulers(ctx)
    assert data["source"] == "scx_loader" and data["loader"] is True
    by_name = {s["name"]: s for s in data["schedulers"]}
    assert by_name["scx_lavd"]["installed"] and by_name["scx_rustland"]["installed"]


def test_active_scheduler_from_sysfs(staging: Path, make_ctx) -> None:
    _mkdir(staging, sched.SCHED_EXT_SYS + "/root")
    tl.write(staging, sched.SCHED_EXT_SYS + "/state", "enabled\n")
    tl.write(staging, sched.SCHED_EXT_SYS + "/root/ops", "scx_lavd\n")
    assert sched.active_scheduler(make_ctx()) == "scx_lavd"


# --- set: systemd fallback ----------------------------------------------------------------------
def test_set_scheduler_systemd_enable(staging: Path, make_ctx, recorder) -> None:
    _mkdir(staging, sched.SCHED_EXT_SYS)
    tl.unit(staging, sched.SCX_UNIT)
    ctx = make_ctx(tools=("systemctl",), runner=recorder)
    report = sched.set_scheduler(ctx, "scx_lavd")
    assert report.ok, report.render()
    assert "SCX_SCHEDULER=scx_lavd" in (tl.read(staging, sched.SCX_DEFAULT) or "")
    assert "enable" in recorder.verbs("systemctl")


def test_set_scheduler_none_stops(staging: Path, make_ctx, recorder) -> None:
    _mkdir(staging, sched.SCHED_EXT_SYS)
    tl.unit(staging, sched.SCX_UNIT)
    ctx = make_ctx(tools=("systemctl",), runner=recorder)
    report = sched.set_scheduler(ctx, "none")
    assert report.ok, report.render()
    assert "SCX_SCHEDULER=\n" in (tl.read(staging, sched.SCX_DEFAULT) or "")
    assert "disable" in recorder.verbs("systemctl")


def test_set_scheduler_skips_without_sched_ext(make_ctx, recorder) -> None:
    ctx = make_ctx(tools=("systemctl",), runner=recorder)
    report = sched.set_scheduler(ctx, "scx_lavd")
    assert report.ok  # a skip is not a failure
    assert any(s.skipped for s in report.steps)
    assert not recorder.find("systemctl")


def test_set_scheduler_rejects_bad_profile(staging: Path, make_ctx) -> None:
    _mkdir(staging, sched.SCHED_EXT_SYS)
    report = sched.set_scheduler(make_ctx(), "scx_bogus")
    assert not report.ok


# --- CLI ----------------------------------------------------------------------------------------
def test_cli_sched_list_json(capsys) -> None:
    assert cli.main(["sched", "list", "--json"]) == common.EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert "schedulers" in data and "sched_ext" in data


def test_cli_sched_status(capsys) -> None:
    assert cli.main(["sched", "status"]) == common.EXIT_OK
    assert "scheduler" in capsys.readouterr().out


def test_cli_sched_set_exit3_without_sched_ext(staging: Path, capsys) -> None:
    # `staging` points LINDOS_ROOT at an empty tree (no /sys/kernel/sched_ext),
    # so the probe reports no sched_ext regardless of the *host* kernel — a CI
    # runner's own kernel may actually provide sched_ext.
    assert cli.main(["sched", "set", "scx_lavd"]) == common.EXIT_NO_SCHED_EXT
    assert "sched_ext" in capsys.readouterr().err


def test_cli_sched_set_bad_profile(capsys) -> None:
    assert cli.main(["sched", "set", "scx_nope"]) == common.EXIT_USAGE


def test_cli_sched_set_dry_run_supported(staging: Path, capsys) -> None:
    _mkdir(staging, sched.SCHED_EXT_SYS)
    assert cli.main(["sched", "set", "scx_lavd", "--dry-run"]) == common.EXIT_OK


# --- privileged op mapping ----------------------------------------------------------------------
def test_privileged_sched_maps_to_set_sched() -> None:
    action, payload = privileged.helper_call(privileged.Request.sched("scx_lavd"))
    assert action == "set-sched" and payload == {"profile": "scx_lavd"}
    assert "sched" in privileged.OPS


def test_privileged_sched_rejects_bad_profile() -> None:
    with pytest.raises(privileged.EscalationError):
        privileged.helper_call(privileged.Request.sched("scx_evil"))


def test_privileged_sched_describe() -> None:
    assert "set-sched" in privileged.describe(privileged.Request.sched("none"))


# --- tune.d keys + apply (THP / MGLRU / sched) --------------------------------------------------
@pytest.mark.parametrize("mode, sched_v, thp_v, mglru_v", [
    ("everyday", "none", "madvise", None),
    ("gaming", "scx_lavd", "madvise", None),
    ("work", "none", "madvise", None),
    ("creator", "scx_bpfland", "madvise", None),
    ("lite", "none", "madvise", "on"),
])
def test_tune_d_sched_keys(make_ctx, mode, sched_v, thp_v, mglru_v) -> None:
    s = apply.load_settings(make_ctx(), mode)
    assert s["sched"] == sched_v and s["thp"] == thp_v and s["mglru"] == mglru_v


def test_apply_thp_written_under_root(staging: Path, make_ctx) -> None:
    ctx = make_ctx()
    report = apply.run("gaming", ctx=ctx, system=True)
    assert report.ok, report.render()
    assert (tl.read(staging, apply.THP_SYS) or "").strip() == "madvise"


def test_apply_mglru_only_for_lite(staging: Path, make_ctx) -> None:
    apply.run("lite", ctx=make_ctx(), system=True)
    assert (tl.read(staging, apply.MGLRU_SYS) or "").strip() == "y"


def test_apply_mglru_skipped_when_unset(staging: Path, make_ctx) -> None:
    report = apply.run("everyday", ctx=make_ctx(), system=True)
    steps = {s.name: s for s in report.steps}
    assert steps["mglru"].skipped
    assert tl.read(staging, apply.MGLRU_SYS) is None


def test_apply_records_kernel_knobs_in_state(staging: Path, make_ctx) -> None:
    apply.run("gaming", ctx=make_ctx(), system=True)
    state = json.loads(tl.read(staging, common.STATE_FILE) or "{}")
    assert state["sched"] == "scx_lavd" and state["thp"] == "madvise"


# --- status reporting ---------------------------------------------------------------------------
def test_status_reports_thp_and_scheduler(staging: Path, make_ctx) -> None:
    tl.write(staging, "/sys/kernel/mm/transparent_hugepage/enabled", "always [madvise] never\n")
    snap = status.snapshot(make_ctx())
    assert snap["thp"] == "madvise"
    assert "sched" in snap and "sched_ext" in snap["sched"]
    text = status.render_text(snap)
    assert "THP" in text and "scheduler" in text
