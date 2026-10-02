"""build/chroot/77-mint-sweep.sh: the build's last pass over what still says "Linux Mint".

The hook is run for real (bash) against a *fake root* (LINDOS_SWEEP_ROOT) with the real lindos-desktop
scripts and rules copied into it; apt-get and dpkg-query are replaced by small fakes (test seams).  It checks:

  * structure: house style, ordered after every package hook and before the installer hook and cleanup,
    shellcheck clean, refuses to run against a build host;
  * behaviour: the sweep runs over the final image, Mint Welcome is verified out of autostart, the Mint wallpaper
    packs are purged only when apt says nothing else goes with them, the audit is written to the log, and every
    missing piece degrades to a warning (exit 0), never a build failure.

Needs bash (skipped otherwise).  What only a real image can show is listed in docs/BUILDING.md ("Mint sweep").
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
HOOK = REPO_ROOT / "build" / "chroot" / "77-mint-sweep.sh"
HOOK_DIR = HOOK.parent
DESKTOP = REPO_ROOT / "packages" / "lindos-desktop" / "root"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available on this host")


def _text(p: Path) -> str:
    assert p.is_file(), p
    return p.read_text(encoding="utf-8")


def _posix(p: Path) -> str:
    if os.name == "nt":
        cyg = shutil.which("cygpath")
        if cyg:
            res = subprocess.run([cyg, "-u", str(p)], capture_output=True, text=True, check=False)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
    return p.as_posix()


# --------------------------------------------------------------------------- structure
def test_hook_exists_with_house_style() -> None:
    raw = HOOK.read_bytes()
    assert raw.startswith(b"#!/bin/bash\n") and b"\r" not in raw
    t = raw.decode("utf-8")
    assert "set -Eeuo pipefail" in t and "hook_begin" in t and "hook_end" in t
    assert re.search(r'^\. "\$\(dirname "\$\(readlink -f "\$0"\)"\)/lib\.sh"$', t, flags=re.M)
    assert re.search(r"^\s*(sudo|pkexec)\s", t, flags=re.M) is None


def test_hook_runs_after_every_package_hook_and_before_the_installer_hook_and_cleanup() -> None:
    names = sorted(p.name for p in HOOK_DIR.glob("[0-9][0-9]-*.sh"))
    i = names.index(HOOK.name)
    before, after = names[:i], names[i + 1:]
    for h in ("00-repos.sh", "10-debloat.sh", "20-base.sh", "30-lindos-debs.sh", "40-theme.sh", "50-tune.sh",
              "60-compat.sh", "70-gaming.sh", "75-vm.sh", "76-mint-purge.sh"):
        assert h in before, f"{HOOK.name} must run after {h}"
    for h in ("78-installer-brand.sh", "80-cleanup.sh"):
        assert h in after, f"{HOOK.name} must run before {h}"


def test_hook_refuses_to_edit_a_build_host() -> None:
    t = _text(HOOK)
    assert 'LINDOS_CHROOT:-}" != "1"' in t and "in_chroot" in t and "die " in t


def test_hook_never_touches_identity_files_or_partitioning() -> None:
    body = "\n".join(ln for ln in _text(HOOK).splitlines() if not ln.lstrip().startswith("#"))
    for forbidden in ("DISTRIB_ID", "os-release", "sources.list", "partman", "grub-installer", "dpkg-divert", "sudo "):
        assert forbidden not in body, forbidden


def _find_shellcheck() -> Optional[str]:
    found = shutil.which("shellcheck")
    if found:
        return found
    import sysconfig
    for scheme in (None, "nt_user", "posix_user"):
        try:
            d = sysconfig.get_path("scripts", scheme) if scheme else sysconfig.get_path("scripts")
        except (KeyError, ValueError):
            continue
        for name in ("shellcheck", "shellcheck.exe"):
            cand = os.path.join(d or "", name)
            if d and os.path.isfile(cand):
                return cand
    return None


@needs_bash
def test_hook_syntax_and_shellcheck() -> None:
    assert BASH is not None
    res = subprocess.run([BASH, "-n", str(HOOK)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert res.returncode == 0, res.stderr
    sc = _find_shellcheck()
    if sc is None:
        pytest.skip("shellcheck not installed (tests/run.sh lints the hook when it is)")
    res = subprocess.run([sc, "-S", "warning", "-e", "SC1090,SC1091", "-x", str(HOOK)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=120, cwd=str(REPO_ROOT))
    assert res.returncode == 0, res.stdout + res.stderr


def test_debloat_hook_also_hides_the_mint_welcome_autostart() -> None:
    body = _text(HOOK_DIR / "10-debloat.sh")
    assert re.search(r'^hide_autostart "mintwelcome"$', body, flags=re.M)
    # 76-mint-purge.sh removes the package; the autostart hide stays for the case that purge is skipped
    assert "mintwelcome" not in body.split("PROTECTED=")[1].split("\n\n")[0].split()


# --------------------------------------------------------------------------- behaviour
MINTWELCOME = "[Desktop Entry]\nName=Welcome Screen\nComment=Introduction to Linux Mint\nExec=mintwelcome\nType=Application\n"
AUTOSTART = "[Desktop Entry]\nName=Welcome Screen\nExec=mintwelcome-launcher\nType=Application\n"
LSB = 'DISTRIB_ID=LinuxMint\nDISTRIB_RELEASE=22.2\nDISTRIB_CODENAME=zara\nDISTRIB_DESCRIPTION="Linux Mint 22.2 Zara"\n'
OS_RELEASE = 'NAME="Linux Mint"\nID=linuxmint\nID_LIKE="ubuntu debian"\nPRETTY_NAME="Linux Mint 22.2"\nVERSION_CODENAME=zara\n'


def _write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return p


def _root(tmp: Path, *, with_scripts: bool = True) -> Path:
    root = tmp / "root"
    _write(root, "usr/share/applications/mintwelcome.desktop", MINTWELCOME)
    _write(root, "etc/xdg/autostart/mintwelcome.desktop", AUTOSTART)
    _write(root, "etc/lsb-release", LSB)
    _write(root, "etc/os-release", OS_RELEASE)
    _write(root, "etc/issue", "Linux Mint 22.2 Zara \\n \\l\n")
    _write(root, "etc/skel/.config/autostart/mintwelcome.desktop", "[Desktop Entry]\nName=x\nExec=true\nHidden=true\n")
    _write(root, "etc/default/grub.d/60-lindos-distributor.cfg", 'GRUB_DISTRIBUTOR="Lindos"\n')
    _write(root, "usr/share/lindos/os-release.d/lindos.conf", _text(DESKTOP / "usr/share/lindos/os-release.d/lindos.conf"))
    _write(root, "usr/share/lindos/branding/base-sweep.json", _text(DESKTOP / "usr/share/lindos/branding/base-sweep.json"))
    if with_scripts:
        for name in ("apply-branding.sh", "rebrand-base.py"):
            _write(root, "usr/libexec/lindos/" + name, _text(DESKTOP / "usr/libexec/lindos" / name))
    return root


def _fakes(tmp: Path, *, installed: str, simulation: str) -> Dict[str, str]:
    """dpkg-query / apt-get stand-ins; apt-get logs every call into apt.log and answers -s from a file."""
    fake = tmp / "fakes"
    fake.mkdir(exist_ok=True)
    (fake / "apt.log").write_text("", encoding="utf-8")
    (fake / "sim.txt").write_bytes(simulation.encode("utf-8"))
    dq = fake / "dpkg-query"
    dq.write_bytes(b"#!/bin/sh\n" + "".join("echo 'installed %s'\n" % p for p in installed.split()).encode("utf-8"))
    ag = fake / "apt-get"
    ag.write_bytes(("#!/bin/sh\necho \"$@\" >> '%s'\ncase \" $* \" in *' -s '*) cat '%s' ;; esac\nexit 0\n"
                    % (_posix(fake / "apt.log"), _posix(fake / "sim.txt"))).encode("utf-8"))
    for p in (dq, ag):
        os.chmod(p, 0o755)
    return {"LINDOS_SWEEP_DPKG_QUERY": _posix(dq), "LINDOS_SWEEP_APT_GET": _posix(ag)}


def _shim_python(tmp: Path) -> str:
    shim = tmp / "fakes" / "python3"
    shim.parent.mkdir(exist_ok=True)
    shim.write_bytes(('#!/bin/sh\nexec "%s" "$@"\n' % Path(sys.executable).as_posix()).encode("utf-8"))
    os.chmod(shim, 0o755)
    return _posix(shim)


def _run(tmp: Path, root: Path, *, installed: str = "mint-backgrounds-xia mint-backgrounds-wilma",
         simulation: str = "Purg mint-backgrounds-xia [1.0] \nPurg mint-backgrounds-wilma [1.0] \n",
         extra_env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    assert BASH is not None
    env = dict(os.environ)
    env.update(_fakes(tmp, installed=installed, simulation=simulation))
    config = tmp / "config.env"
    config.write_bytes(b"# empty on purpose: lib.sh falls back to its own apt defaults\n")
    env.update({
        "LINDOS_SWEEP_ROOT": _posix(root),
        "LINDOS_PYTHON": _shim_python(tmp),
        "LINDOS_STAGE_DIR": _posix(tmp / "no-such-stage"),
        "LINDOS_CONFIG_ENV": _posix(config),
    })
    env.update(extra_env or {})
    return subprocess.run([BASH, "-Eeuo", "pipefail", _posix(HOOK)], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=180, check=False, env=env)


def _apt_calls(tmp: Path) -> str:
    return (tmp / "fakes" / "apt.log").read_text(encoding="utf-8")


def _snapshot(root: Path) -> Dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@needs_bash
def test_full_run_sweeps_checks_purges_wallpapers_and_audits(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "usr/share/backgrounds/linuxmint-zara").mkdir(parents=True)
    res = _run(tmp_path, root)
    assert res.returncode == 0, res.stderr
    assert "NoDisplay=true" in _text(root / "usr/share/applications/mintwelcome.desktop")
    assert "Hidden=true" in _text(root / "etc/xdg/autostart/mintwelcome.desktop")
    assert 'DISTRIB_DESCRIPTION="Lindos 1.0 (Aurora)"' in _text(root / "etc/lsb-release")
    assert "DISTRIB_ID=LinuxMint" in _text(root / "etc/lsb-release").splitlines()
    assert 'NAME="Lindos"' in _text(root / "etc/os-release") and "ID=linuxmint" in _text(root / "etc/os-release")
    assert "no visible autostart entry starts Mint Welcome" in res.stderr
    calls = _apt_calls(tmp_path).splitlines()
    assert any(c.startswith("-s purge mint-backgrounds-wilma mint-backgrounds-xia") or c.startswith("-s purge mint-backgrounds-xia mint-backgrounds-wilma") for c in calls), calls
    real = [c for c in calls if " -s " not in f" {c} " and "purge" in c]
    assert len(real) == 1 and "mint-backgrounds-xia" in real[0] and "mint-backgrounds-wilma" in real[0], calls
    assert "audit: /usr/share/backgrounds/linuxmint-zara:" in res.stderr       # the audit reaches the build log
    assert re.search(r"audit: \d+ item\(s\) still mention Linux Mint", res.stderr)
    assert "WARNING" not in res.stderr, res.stderr


@needs_bash
def test_wallpapers_stay_when_apt_says_something_else_would_go_with_them(tmp_path: Path) -> None:
    root = _root(tmp_path)
    res = _run(tmp_path, root, simulation="Purg mint-backgrounds-xia [1.0] \nPurg mint-artwork [2.0] \nPurg mint-meta-xfce [1.0] \n")
    assert res.returncode == 0, res.stderr
    assert "would also remove: mint-artwork mint-meta-xfce" in res.stderr and "keeping the Mint wallpapers" in res.stderr
    assert not [c for c in _apt_calls(tmp_path).splitlines() if " -s " not in f" {c} " and "purge" in c]


@needs_bash
def test_wallpapers_stay_when_the_simulation_gives_no_answer(tmp_path: Path) -> None:
    root = _root(tmp_path)
    res = _run(tmp_path, root, simulation="")
    assert res.returncode == 0
    assert "could not simulate purging" in res.stderr
    assert not [c for c in _apt_calls(tmp_path).splitlines() if " -s " not in f" {c} " and "purge" in c]


@needs_bash
def test_nothing_to_purge_means_apt_is_never_asked(tmp_path: Path) -> None:
    root = _root(tmp_path)
    res = _run(tmp_path, root, installed="")
    assert res.returncode == 0 and "no mint-backgrounds-* package installed" in res.stderr
    assert _apt_calls(tmp_path) == ""


@needs_bash
def test_purge_failure_is_only_a_warning(tmp_path: Path) -> None:
    root = _root(tmp_path)
    fake = tmp_path / "fakes"
    fake.mkdir()
    (fake / "sim.txt").write_bytes(b"Purg mint-backgrounds-xia [1.0] \n")
    ag = fake / "apt-get-failing"                    # the simulation works, the real purge fails
    ag.write_bytes(("#!/bin/sh\ncase \" $* \" in *' -s '*) cat '%s'; exit 0 ;; esac\nexit 100\n" % _posix(fake / "sim.txt")).encode("utf-8"))
    os.chmod(ag, 0o755)
    res = _run(tmp_path, root, installed="mint-backgrounds-xia", extra_env={"LINDOS_SWEEP_APT_GET": _posix(ag)})
    assert res.returncode == 0 and "purge of mint-backgrounds-" in res.stderr and "failed (continuing)" in res.stderr


@needs_bash
def test_a_second_run_changes_no_file(tmp_path: Path) -> None:
    root = _root(tmp_path)
    assert _run(tmp_path, root, installed="").returncode == 0
    before = _snapshot(root)
    assert _run(tmp_path, root, installed="").returncode == 0
    assert _snapshot(root) == before


@needs_bash
def test_missing_lindos_desktop_scripts_degrade_to_warnings_and_the_welcome_check_notices(tmp_path: Path) -> None:
    root = _root(tmp_path, with_scripts=False)
    res = _run(tmp_path, root, installed="")
    assert res.returncode == 0, res.stderr
    assert "apply-branding.sh missing" in res.stderr and "nothing swept" in res.stderr
    assert "etc/xdg/autostart/mintwelcome.desktop still starts Mint Welcome" in res.stderr
    assert "cannot audit" in res.stderr
    assert "NoDisplay" not in _text(root / "usr/share/applications/mintwelcome.desktop")   # nothing was touched


@needs_bash
def test_missing_skel_override_and_grub_drop_in_are_reported(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "etc/skel/.config/autostart/mintwelcome.desktop").unlink()
    (root / "etc/default/grub.d/60-lindos-distributor.cfg").unlink()
    res = _run(tmp_path, root, installed="")
    assert res.returncode == 0
    assert "mintwelcome.desktop missing" in res.stderr and "60-lindos-distributor.cfg missing" in res.stderr


@needs_bash
def test_hook_does_not_fail_the_build_on_an_empty_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    res = _run(tmp_path, root, installed="")
    assert res.returncode == 0, res.stderr
