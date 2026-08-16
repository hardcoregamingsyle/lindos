"""DXVK/VKD3D install-into-prefix, components pins, and the new doctor checks (SPEC-KERNEL §17.2)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lindos_compat import cli_compat, doctor, installers

SHARE = Path(__file__).resolve().parent.parent / "root" / "usr" / "share" / "lindos"
LIBEXEC = Path(__file__).resolve().parent.parent / "root" / "usr" / "libexec" / "lindos"


class RecordRun:
    def __init__(self, rc: int = 0, out: str = "ok"):
        self.calls = []
        self.rc = rc
        self.out = out

    def __call__(self, argv, *a, **k):
        self.calls.append([str(x) for x in argv])
        return SimpleNamespace(returncode=self.rc, stdout=self.out, stderr="")


# --------------------------------------------------------------------------- #
# components.json
# --------------------------------------------------------------------------- #
def test_components_json_pins():
    data = json.loads((SHARE / "compat" / "components.json").read_text(encoding="utf-8"))
    assert data["schema"] == 1
    for key in ("dxvk", "vkd3d_proton", "gamescope"):
        assert key in data, key
    assert data["dxvk"]["tag"] and data["dxvk"]["repo"] and data["dxvk"]["asset"]
    assert data["vkd3d_proton"]["tag"] and data["vkd3d_proton"]["asset"]
    assert data["gamescope"]["min"]


def test_install_into_prefix_script_exists():
    assert (LIBEXEC / "install-into-prefix.sh").is_file()
    text = (LIBEXEC / "install-into-prefix.sh").read_text(encoding="utf-8")
    assert text.startswith("#!/bin/bash\n") and "set -Eeuo pipefail" in text


# --------------------------------------------------------------------------- #
# install_into_prefix (injected run/script)
# --------------------------------------------------------------------------- #
COMPS = {
    "dxvk": {"repo": "doitsujin/dxvk", "tag": "v2.7.1", "asset": "dxvk-2.7.1.tar.gz", "sha256": "abc"},
    "vkd3d_proton": {"repo": "HansKristian-Work/vkd3d-proton", "tag": "v2.14.1", "asset": "vkd3d-proton-2.14.1.tar.zst"},
}


def _prefix(tmp_path: Path) -> Path:
    p = tmp_path / "pfx"
    (p / "drive_c").mkdir(parents=True)
    (p / "system.reg").write_text("WINE REGISTRY Version 2\n")
    return p


def test_install_dxvk_builds_expected_argv(tmp_path, fake_which):
    pfx = _prefix(tmp_path)
    rr = RecordRun()
    script = LIBEXEC / "install-into-prefix.sh"
    res = installers.install_dxvk(pfx, components=COMPS, run=rr, which=fake_which("bash"), script=script)
    assert res.ok and res.what == "dxvk"
    argv = rr.calls[0]
    assert "--component" in argv and argv[argv.index("--component") + 1] == "dxvk"
    assert "--prefix" in argv and str(pfx) in argv
    url = argv[argv.index("--url") + 1]
    assert url == "https://github.com/doitsujin/dxvk/releases/download/v2.7.1/dxvk-2.7.1.tar.gz"
    assert argv[argv.index("--tag") + 1] == "v2.7.1"
    assert argv[argv.index("--sha256") + 1] == "abc"


def test_install_vkd3d_and_component_key(tmp_path, fake_which):
    pfx = _prefix(tmp_path)
    rr = RecordRun()
    res = installers.install_vkd3d(pfx, components=COMPS, run=rr, which=fake_which("bash"),
                                   script=LIBEXEC / "install-into-prefix.sh")
    assert res.ok
    argv = rr.calls[0]
    assert argv[argv.index("--component") + 1] == "vkd3d"
    assert "vkd3d-proton-2.14.1.tar.zst" in argv[argv.index("--url") + 1]


def test_install_into_prefix_dry_run_uninstall_and_errors(tmp_path, fake_which):
    pfx = _prefix(tmp_path)
    rr = RecordRun()
    sh = LIBEXEC / "install-into-prefix.sh"
    res = installers.install_into_prefix("dxvk", pfx, components=COMPS, run=rr, which=fake_which("bash"),
                                         script=sh, dry_run=True)
    assert "--dry-run" in rr.calls[0]
    res = installers.install_into_prefix("dxvk", pfx, components=COMPS, run=RecordRun(), which=fake_which("bash"),
                                         script=sh, uninstall=True)
    assert res.ok and res.method == "uninstall"
    # offline: the script exits 3
    res = installers.install_into_prefix("dxvk", pfx, components=COMPS, run=RecordRun(rc=3), which=fake_which("bash"),
                                         script=sh)
    assert not res.ok and res.offline
    # unknown component
    assert not installers.install_into_prefix("bogus", pfx, components=COMPS, run=RecordRun(), script=sh).ok
    # no verify -> no --sha256
    rr = RecordRun()
    installers.install_into_prefix("dxvk", pfx, components=COMPS, run=rr, which=fake_which("bash"), script=sh,
                                   verify=False)
    assert "--sha256" not in rr.calls[0]


def test_load_components_reads_shipped(monkeypatch):
    monkeypatch.setenv("LINDOS_COMPONENTS", str(SHARE / "compat" / "components.json"))
    comps = installers.load_components()
    assert comps["dxvk"]["tag"] and comps["vkd3d_proton"]["asset"]


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #
def test_cli_install_dxvk_parses_and_dispatches(monkeypatch, capsys, home, fake_core):
    p = cli_compat.build_parser()
    ns = p.parse_args(["install-dxvk", "mygame", "--tag", "v2.7.1", "--arch", "win32", "--dry-run", "--json"])
    assert ns.cmd == "install-dxvk" and ns.prefix == "mygame" and ns.arch == "win32" and ns.dry_run

    captured = {}

    def fake_install(prefix, **kw):
        captured["prefix"] = prefix
        captured["kw"] = kw
        return installers.InstallResult(ok=True, what="dxvk", message="ok", method="dry-run")

    monkeypatch.setattr(cli_compat, "install_dxvk", fake_install)
    rc = cli_compat.main(["install-dxvk", "mygame", "--dry-run", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] and data["what"] == "dxvk"
    assert str(captured["prefix"]).endswith("mygame")


# --------------------------------------------------------------------------- #
# doctor: the new gaming/perf checks
# --------------------------------------------------------------------------- #
def test_doctor_gaming_checks_missing(fake_core, home, fake_which):
    rep = doctor.run_doctor(which=fake_which(), run=lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr=""),
                            dpkg=lambda p: False, env={"LINDOS_ROOT": "/fakeroot"}, home=home,
                            isdir=lambda p: False, isfile=lambda p: False, exists=lambda p: False)
    by_id = {c.id: c for c in rep.checks}
    for cid in ("ntsync", "sched-ext", "gamescope", "dxvk", "vkd3d"):
        assert cid in by_id, cid
        assert not by_id[cid].ok and by_id[cid].fix and by_id[cid].level == "optional"
    # these are optional -> they do not flip required readiness
    assert "lindos-kernel" in by_id["ntsync"].fix and "scx-scheds" in by_id["sched-ext"].fix


def test_doctor_gaming_checks_present(fake_core, home, fake_which):
    def exists(p):
        return p == "/fakeroot/dev/ntsync"

    def isdir(p):
        return p == "/fakeroot/sys/kernel/sched_ext"

    rep = doctor.run_doctor(which=fake_which("gamescope", "umu-run"),
                            run=lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr=""),
                            dpkg=lambda p: False, env={"LINDOS_ROOT": "/fakeroot"}, home=home,
                            isdir=isdir, isfile=lambda p: False, exists=exists)
    by_id = {c.id: c for c in rep.checks}
    assert by_id["ntsync"].ok and by_id["sched-ext"].ok and by_id["gamescope"].ok
    assert by_id["dxvk"].ok and by_id["vkd3d"].ok            # umu present -> Proton ships them
    assert all(c.fix == "" for c in (by_id["ntsync"], by_id["gamescope"], by_id["dxvk"]))
