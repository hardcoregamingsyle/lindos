"""Tests for the SPEC-WINDOWS §28.9 doctor additions: DOSBox, PowerShell 7, udisks2, binfmt_misc,
case-insensitive C:\\ drives (casefold), Wine's WoW64 mode (16-bit programs) and python3-hivex.

Each probe is injectable (``dosbox_probe``/``wow64_probe``/``binfmt_probe``/``casefold``/``hivex``)
so these tests never touch a real Wine, kernel or filesystem — see ``lindos_compat.doctor.run_doctor``.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from lindos_compat import doctor


def _default_run(argv, *a, **k):
    """A harmless stand-in for ``subprocess.run`` (e.g. ``wine --version`` / ``pwsh --version``)."""
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def _base_kwargs(which, home: Path, **overrides):
    """``which`` is an already-built ``which`` callable (e.g. ``fake_which()`` or ``fake_which("pwsh")``)."""
    kwargs = dict(
        which=which,
        run=_default_run,
        dpkg=lambda p: False,
        env={},
        home=home,
        isdir=lambda p: False,
        isfile=lambda p: False,
        dosbox_probe=lambda: None,
        wow64_probe=lambda: "unknown",
        binfmt_probe=lambda: None,
        casefold=lambda: {"state": "error", "errno": 0, "detail": "not probed"},
        hivex=lambda: False,
    )
    kwargs.update(overrides)
    return kwargs


def _by_id(rep) -> dict:
    return {c.id: c for c in rep.checks}


# --------------------------------------------------------------------------- #
# DOSBox
# --------------------------------------------------------------------------- #
def test_dosbox_check_found_and_missing(fake_core, home: Path, fake_which):
    rep = doctor.run_doctor(**_base_kwargs(fake_which(), home, dosbox_probe=lambda: "dosbox-x (/usr/bin/dosbox-x)"))
    c = _by_id(rep)["dosbox"]
    assert c.ok and "dosbox-x" in c.detail and c.level == "recommended"

    rep = doctor.run_doctor(**_base_kwargs(fake_which(), home, dosbox_probe=lambda: None))
    c = _by_id(rep)["dosbox"]
    assert not c.ok and "dosbox-x" in c.fix  # DOSBOX_INSTALL_HINT / apt install dosbox-x


# --------------------------------------------------------------------------- #
# PowerShell 7 (Microsoft's own repository, never the snap; optional)
# --------------------------------------------------------------------------- #
def test_powershell_check(fake_core, home: Path, fake_which):
    def run(argv, *a, **k):
        from types import SimpleNamespace
        return SimpleNamespace(returncode=0, stdout="PowerShell 7.4.6\n", stderr="")

    rep = doctor.run_doctor(**_base_kwargs(fake_which("pwsh"), home, run=run))
    c = _by_id(rep)["powershell"]
    assert c.ok and c.level == "optional" and "7.4.6" in c.detail

    rep = doctor.run_doctor(**_base_kwargs(fake_which(), home))
    c = _by_id(rep)["powershell"]
    assert not c.ok
    assert "packages.microsoft.com/config/ubuntu/24.04" in c.fix  # hard-coded: Mint's VERSION_ID 404s
    assert c.fix == doctor.POWERSHELL_INSTALL_CMD


# --------------------------------------------------------------------------- #
# udisks2 (disk images)
# --------------------------------------------------------------------------- #
def test_udisks2_check(fake_core, home: Path, fake_which):
    rep = doctor.run_doctor(**_base_kwargs(fake_which("udisksctl"), home))
    assert _by_id(rep)["udisks2"].ok

    rep = doctor.run_doctor(**_base_kwargs(fake_which(), home))
    c = _by_id(rep)["udisks2"]
    assert not c.ok and "apt install udisks2" in c.fix


# --------------------------------------------------------------------------- #
# binfmt_misc (terminal ./setup.exe)
# --------------------------------------------------------------------------- #
def test_binfmt_check_active_no_conflicts(fake_core, home: Path, fake_which):
    status = {"registered": True, "enabled": True, "masked": False, "conflicts": [], "note": "active"}
    rep = doctor.run_doctor(**_base_kwargs(fake_which(), home, binfmt_probe=lambda: status))
    c = _by_id(rep)["binfmt"]
    assert c.ok and c.level == "optional" and "active" in c.detail


def test_binfmt_check_masked(fake_core, home: Path, fake_which):
    status = {"registered": False, "enabled": False, "masked": True, "conflicts": [], "note": ""}
    rep = doctor.run_doctor(**_base_kwargs(fake_which(), home, binfmt_probe=lambda: status))
    c = _by_id(rep)["binfmt"]
    assert not c.ok and "masked" in c.detail.lower()
    assert "lindos-compat binfmt enable" in c.fix


def test_binfmt_check_not_registered(fake_core, home: Path, fake_which):
    status = {"registered": False, "enabled": False, "masked": False, "conflicts": [], "note": ""}
    rep = doctor.run_doctor(**_base_kwargs(fake_which(), home, binfmt_probe=lambda: status))
    c = _by_id(rep)["binfmt"]
    assert not c.ok and "not registered" in c.detail


def test_binfmt_check_conflict_is_reported_honestly_and_never_fixed_by_lindos(fake_core, home: Path, fake_which):
    status = {"registered": True, "enabled": True, "masked": False,
             "conflicts": [{"name": "wine", "interpreter": "/usr/bin/wine"}], "note": ""}
    rep = doctor.run_doctor(**_base_kwargs(fake_which(), home, binfmt_probe=lambda: status))
    c = _by_id(rep)["binfmt"]
    assert not c.ok
    assert "'wine'" in c.detail and "/usr/bin/wine" in c.detail
    assert "Lindos never changes other handlers" in c.fix


def test_binfmt_check_missing_module_is_unknown_not_a_false_pass(fake_core, home: Path, fake_which):
    rep = doctor.run_doctor(**_base_kwargs(fake_which(), home, binfmt_probe=lambda: None))
    c = _by_id(rep)["binfmt"]
    assert not c.ok and "unknown" in c.detail


# --------------------------------------------------------------------------- #
# casefold (ext4 casefold C:\ drives) - decided by *trying*, not by /sys alone
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("state,ok_expected,fix_substr", [
    ("active", True, ""),
    ("not-enabled", False, "tune2fs -O casefold"),
    ("no-kernel-support", False, "CONFIG_UNICODE"),
    ("unsupported", False, "nothing to do"),
])
def test_casefold_check_states(fake_core, home: Path, fake_which, state, ok_expected, fix_substr):
    cf = {"state": state, "errno": 0, "detail": f"detail for {state}"}
    rep = doctor.run_doctor(**_base_kwargs(fake_which(), home, casefold=lambda: cf))
    c = _by_id(rep)["casefold"]
    assert c.ok is ok_expected
    if fix_substr:
        assert fix_substr in c.fix
    assert c.detail.endswith(f"detail for {state}")


def test_casefold_never_runs_tune2fs(fake_core, home: Path, fake_which, fake_run):
    """§28.8/§28.9: Lindos only *suggests* tune2fs in the fix text, it never runs it."""
    cf = {"state": "not-enabled", "errno": 95, "detail": "not enabled on this filesystem"}
    rep = doctor.run_doctor(**_base_kwargs(fake_which(), home, casefold=lambda: cf, run=fake_run))
    c = _by_id(rep)["casefold"]
    assert "tune2fs -O casefold" in c.fix  # a suggestion for the user to run manually, offline
    assert not fake_run.has("tune2fs")  # run_doctor itself never invokes it


# --------------------------------------------------------------------------- #
# Wine WoW64 mode (decides how 16-bit programs run)
# --------------------------------------------------------------------------- #
def test_wow64_check_needs_wine_installed_first(fake_core, home: Path, fake_which):
    rep = doctor.run_doctor(**_base_kwargs(fake_which(), home, wow64_probe=lambda: "old-wow64"))
    c = _by_id(rep)["wine-wow64"]
    assert not c.ok and "Wine is not installed" in c.detail


@pytest.mark.parametrize("mode,ok_expected", [
    ("old-wow64", True),
    ("new-wow64-16bit", True),
    ("new-wow64-no16bit", False),
    ("unknown", False),
])
def test_wow64_check_modes(fake_core, home: Path, fake_which, mode, ok_expected):
    rep = doctor.run_doctor(**_base_kwargs(fake_which("wine"), home, wow64_probe=lambda: mode))
    c = _by_id(rep)["wine-wow64"]
    assert c.ok is ok_expected
    assert mode in c.detail
    if mode == "new-wow64-no16bit":
        assert "Wine 11" in c.fix


# --------------------------------------------------------------------------- #
# python3-hivex (MSIX package registry settings)
# --------------------------------------------------------------------------- #
def test_hivex_check(fake_core, home: Path, fake_which):
    rep = doctor.run_doctor(**_base_kwargs(fake_which(), home, hivex=lambda: True))
    c = _by_id(rep)["hivex"]
    assert c.ok and c.level == "recommended"

    rep = doctor.run_doctor(**_base_kwargs(fake_which(), home, hivex=lambda: False))
    c = _by_id(rep)["hivex"]
    assert not c.ok and "apt install python3-hivex" in c.fix


# --------------------------------------------------------------------------- #
# Every §28.9 check is present, has a fix when failing, and JSON-round-trips
# --------------------------------------------------------------------------- #
def test_all_w28_9_checks_present_and_json_safe(fake_core, home: Path, fake_which):
    import json

    rep = doctor.run_doctor(**_base_kwargs(fake_which(), home))
    by_id = _by_id(rep)
    for cid in ("dosbox", "powershell", "udisks2", "binfmt", "casefold", "wine-wow64", "hivex"):
        assert cid in by_id, cid
        if not by_id[cid].ok:
            assert by_id[cid].fix, f"{cid} failed with no fix"
    json.dumps(rep.as_dict())
