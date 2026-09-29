"""build/lib/verify_oem_pool.py - can the installed system get oem-config from the medium?

Ubiquity's OEM mode installs ``oem-config-gtk`` into the new system from the installation medium's package
pool and silently skips a package it cannot find: the install would "succeed" and the first boot would have no
account wizard.  build-iso.sh runs this check on the finished ISO tree (REQUIRE_OEM_POOL=1, the default) and fails
the build when the pool cannot deliver oem-config of exactly the ubiquity version in the squashfs and no bundled
fallback (OEM_DEBS_DIR -> /lindos/oem-debs, installed by finalize.sh) is given.  Pure stdlib.
"""
from __future__ import annotations

import gzip
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
MODULE = REPO / "build" / "lib" / "verify_oem_pool.py"
BUILD_ISO = REPO / "build" / "build-iso.sh"

VERSION = "24.04.3+mint18"


@pytest.fixture(scope="module")
def vop():
    spec = importlib.util.spec_from_file_location("verify_oem_pool", MODULE)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _status(root: Path, version: str = VERSION, installed: bool = True, deps: bool = True) -> None:
    status = root / "var" / "lib" / "dpkg" / "status"
    status.parent.mkdir(parents=True, exist_ok=True)
    state = "install ok installed" if installed else "deinstall ok config-files"
    text = ("Package: aaa\nStatus: install ok installed\nVersion: 1\n\n"
            "Package: ubiquity\nStatus: %s\nPriority: optional\nVersion: %s\nDescription: Ubiquity\n Live installer\n\n"
            "Package: ubiquity-frontend-gtk\nStatus: install ok installed\nVersion: %s\n" % (state, version, version))
    if deps:   # what the Mint 22.2 squashfs has installed and oem-config-gtk depends on
        text += ("\nPackage: aptdaemon\nStatus: install ok installed\nVersion: 1.1.1\n"
                 "\nPackage: python3-aptdaemon.gtk3widgets\nStatus: install ok installed\nVersion: 1.1.1\n")
    status.write_text(text, encoding="utf-8", newline="\n")


def _iso(tmp: Path, *, version: str = VERSION, debs: bool = True, index: bool = True, disk: bool = True,
         dists: bool = True) -> Path:
    iso = tmp / "iso"
    if debs:
        pool = iso / "pool" / "main" / "u" / "ubiquity"
        pool.mkdir(parents=True)
        for name in ("oem-config", "oem-config-gtk", "ubiquity-frontend-gtk"):
            (pool / ("%s_%s_all.deb" % (name, version))).write_bytes(b"!<arch>\n")
    if disk:
        (iso / ".disk").mkdir(parents=True)
        (iso / ".disk" / "info").write_text("Lindos 1.0\n", encoding="utf-8")
        (iso / ".disk" / "cd_type").write_text("full_cd/single\n", encoding="utf-8")
    if dists:
        idx = iso / "dists" / "noble" / "main" / "binary-amd64"
        idx.mkdir(parents=True)
        body = "Package: oem-config-gtk\nVersion: %s\n\nPackage: ubiquity\nVersion: %s\n" % (version, version) if index else "Package: ubiquity\n"
        with gzip.open(idx / "Packages.gz", "wt", encoding="utf-8") as fh:
            fh.write(body)
    return iso


def _squash(tmp: Path, **kw) -> Path:
    root = tmp / "sq"
    _status(root, **kw)
    return root


def test_the_pool_of_a_matching_medium_is_enough(vop, tmp_path: Path) -> None:
    res = vop.check(_iso(tmp_path), _squash(tmp_path))
    assert res["ok"] is True and res["via"] == "pool" and res["version"] == VERSION and res["problems"] == []


def test_the_ubiquity_version_comes_from_the_squashfs_status_file(vop, tmp_path: Path) -> None:
    assert vop.installed_version(_squash(tmp_path) / "var/lib/dpkg/status") == VERSION
    assert vop.installed_version(tmp_path / "nowhere") is None
    _status(tmp_path / "removed", installed=False)
    assert vop.installed_version(tmp_path / "removed" / "var/lib/dpkg/status") is None, "a removed package is not installed"


def test_a_pool_at_another_version_is_not_enough(vop, tmp_path: Path) -> None:
    """oem-config-gtk needs exactly the ubiquity version of the squashfs: apt would otherwise change the installer under its own feet."""
    res = vop.check(_iso(tmp_path, version="24.04.3+mint19"), _squash(tmp_path))
    assert res["ok"] is False
    assert any("oem-config_%s" % VERSION in p and "mint19" in p for p in res["problems"]), res["problems"]


def test_no_pool_no_dists_or_no_disk_metadata_is_not_enough(vop, tmp_path: Path) -> None:
    sq = _squash(tmp_path)
    assert vop.check(_iso(tmp_path / "a", debs=False), sq)["ok"] is False
    res = vop.check(_iso(tmp_path / "b", dists=False), sq)
    assert res["ok"] is False and any("dists/" in p for p in res["problems"])
    res = vop.check(_iso(tmp_path / "c", disk=False), sq)
    assert res["ok"] is False and any(".disk/cd_type" in p or ".disk/info" in p for p in res["problems"])
    res = vop.check(_iso(tmp_path / "d", index=False), sq)
    assert res["ok"] is False and any("Packages index" in p for p in res["problems"])


def test_oem_config_gtks_other_dependencies_must_be_installed_or_in_the_pool(vop, tmp_path: Path) -> None:
    """A debloat step that removed aptdaemon would make oem-config-gtk uninstallable: the build must notice."""
    bare = tmp_path / "sq-bare"
    _status(bare, deps=False)
    res = vop.check(_iso(tmp_path / "a"), bare)
    assert res["ok"] is False and any("aptdaemon is neither installed" in p for p in res["problems"]), res["problems"]
    assert any("python3-aptdaemon.gtk3widgets is neither installed" in p for p in res["problems"])
    # ... unless the pool carries them
    iso = _iso(tmp_path / "b")
    pool = iso / "pool" / "main" / "a" / "aptdaemon"
    pool.mkdir(parents=True)
    for name in ("aptdaemon", "python3-aptdaemon.gtk3widgets"):
        (pool / ("%s_1.1.1-0ubuntu42_all.deb" % name)).write_bytes(b"!<arch>\n")
    assert vop.check(iso, bare)["ok"] is True


def test_a_missing_ubiquity_in_the_squashfs_is_reported(vop, tmp_path: Path) -> None:
    empty = tmp_path / "sq-empty"
    empty.mkdir()
    res = vop.check(_iso(tmp_path), empty)
    assert res["ok"] is False and "ubiquity is not installed" in res["problems"][0]


def test_an_epoch_in_a_file_name_or_status_is_ignored(vop) -> None:
    assert vop.strip_epoch("1:24.04.3+mint18") == VERSION and vop.strip_epoch("1%3a24.04.3+mint18") == VERSION
    assert vop.strip_epoch(VERSION) == VERSION


def test_a_matching_fallback_directory_saves_a_medium_without_a_usable_pool(vop, tmp_path: Path) -> None:
    fallback = tmp_path / "oem-debs"
    fallback.mkdir()
    for name in ("oem-config", "oem-config-gtk", "aptdaemon"):
        (fallback / ("%s_%s_all.deb" % (name, VERSION))).write_bytes(b"!<arch>\n")
    target = tmp_path / "iso" / "lindos" / "oem-debs"
    res = vop.check(_iso(tmp_path, debs=False), _squash(tmp_path), fallback, target)
    assert res["ok"] is True and res["via"] == "fallback" and res["problems"], "the pool problems stay visible"
    assert sorted(p.name for p in target.iterdir()) == sorted(p.name for p in fallback.iterdir()), "the closure is copied to the medium"


def test_a_fallback_must_bring_the_dependencies_the_squashfs_lacks(vop, tmp_path: Path) -> None:
    bare = tmp_path / "sq-bare"
    _status(bare, deps=False)
    fallback = tmp_path / "oem-debs"
    fallback.mkdir()
    for name in ("oem-config", "oem-config-gtk"):
        (fallback / ("%s_%s_all.deb" % (name, VERSION))).write_bytes(b"!<arch>\n")
    res = vop.check(_iso(tmp_path, debs=False), bare, fallback)
    assert res["ok"] is False and any("lacks matching" in p and "aptdaemon" in p for p in res["problems"]), res["problems"]
    for name in ("aptdaemon", "python3-aptdaemon.gtk3widgets"):
        (fallback / ("%s_1.1.1_all.deb" % name)).write_bytes(b"!<arch>\n")
    assert vop.check(_iso(tmp_path / "again", debs=False), bare, fallback)["ok"] is True


def test_a_fallback_at_the_wrong_version_or_incomplete_is_not_accepted(vop, tmp_path: Path) -> None:
    fallback = tmp_path / "oem-debs"
    fallback.mkdir()
    (fallback / ("oem-config_%s_all.deb" % VERSION)).write_bytes(b"x")          # oem-config-gtk missing
    res = vop.check(_iso(tmp_path, debs=False), _squash(tmp_path), fallback)
    assert res["ok"] is False and any("lacks matching oem-config-gtk" in p for p in res["problems"])
    assert not (tmp_path / "iso" / "lindos").exists(), "nothing is copied for a fallback that is refused"


def _cli(*args: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run([sys.executable, str(MODULE), *args], capture_output=True, text=True, encoding="utf-8", timeout=60)


def test_the_cli_reports_ok_and_uses_exit_codes(tmp_path: Path) -> None:
    iso, sq = _iso(tmp_path / "ok"), _squash(tmp_path / "ok")
    res = _cli("--iso-dir", str(iso), "--squashfs-root", str(sq), "--require")
    assert res.returncode == 0 and "oem-config check: ok via pool" in res.stdout
    bad_iso = _iso(tmp_path / "bad", debs=False)
    res = _cli("--iso-dir", str(bad_iso), "--squashfs-root", str(_squash(tmp_path / "bad")))
    assert res.returncode == 0 and "NOT ok" in res.stdout, "without --require the verdict is in the text (build-iso.sh reads it)"
    res = _cli("--iso-dir", str(bad_iso), "--squashfs-root", str(_squash(tmp_path / "bad")), "--require")
    assert res.returncode == 1 and "NOT ok" in res.stdout


def test_build_iso_turns_a_bad_verdict_into_a_failed_build_unless_overridden() -> None:
    build = BUILD_ISO.read_text(encoding="utf-8")
    body = build[build.index("verify_oem_offline() {"):build.index("# Step 8: md5sum.txt")]
    assert 'grep -q \'NOT ok\'' in body and 'REQUIRE_OEM_POOL}" = "1"' in body and "die " in body
    assert "warn " in body, "REQUIRE_OEM_POOL=0 must degrade to a warning, not to silence"
    assert "--fallback-dir" in body and "--copy-fallback-to" in body and "lindos/oem-debs" in body
