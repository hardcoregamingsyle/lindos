"""The --in-installer modes of install-browser.sh, install-compat.sh and install-gaming.sh.

The installer hook runs each script twice inside 'chroot /target': a download phase that may be killed
(--download-only) and a dpkg phase from the downloaded files that never is (--no-download).  These tests
run the real scripts with fake apt/dpkg/curl commands (the harness of test_target_config.py) and check the
phase contract: what each phase does and does not do, the apt options that keep the medium's cdrom: source
out of it, exit codes, and that the existing --from-chroot semantics of the build hooks are unchanged.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import List

import pytest
from installer_testlib import BASH, BROWSER_SH, COMPAT_SH, GAMING_SH, Sandbox, needs_bash

pytestmark = needs_bash


def _script(sb: Sandbox, src: Path) -> Path:
    """A copy of the script whose fixed log path points into the sandbox (so no host /var/log is touched)."""
    text = src.read_text(encoding="utf-8")
    text = re.sub(r'^LOG_FILE="/var/log/lindos/[a-z-]+\.log"$', 'LOG_FILE="%s/script.log"' % sb.root.as_posix(), text, flags=re.M)
    dst = sb.root / src.name
    dst.write_text(text, encoding="utf-8", newline="\n")
    dst.chmod(0o755)
    return dst


def _run(sb: Sandbox, src: Path, *args: str, **env: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run([BASH, str(_script(sb, src)), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120, env=sb.env(LINDOS_ROOT=(sb.root / "root").as_posix(), **env),
                          stdin=subprocess.DEVNULL)


def _apt_lines(out: str) -> List[str]:
    """The apt-get commands the scripts ran or (--dry-run) would run - not their log lines about them."""
    return [ln for ln in out.splitlines() if re.search(r"(?:\(dry-run\)|DRY-RUN:|would run:|run:) (?:env \S+ )?apt-get ", ln)]


NO_CDROM = "Dir::Etc::SourceList=/dev/null"


# --------------------------------------------------------------------------- install-browser.sh
def test_browser_phase_flags_need_the_installer_mode(sandbox: Sandbox) -> None:
    for flag in ("--download-only", "--no-download"):
        res = _run(sandbox, BROWSER_SH, "chrome", flag, "--dry-run")
        assert res.returncode == 2 and "need --in-installer" in res.stderr, (flag, res.stderr)
    res = _run(sandbox, BROWSER_SH, "chrome", "--in-installer", "--download-only", "--no-download", "--dry-run")
    assert res.returncode == 2 and "exclude each other" in res.stderr


def test_browser_dry_run_download_phase_only_downloads(sandbox: Sandbox) -> None:
    res = _run(sandbox, BROWSER_SH, "chrome", "--in-installer", "--download-only", "--dry-run")
    assert res.returncode == 0, res.stderr
    apt = _apt_lines(res.stdout)
    assert len(apt) == 1 and " -d " in apt[0] and NO_CDROM in apt[0] and "google-chrome-stable" in apt[0], apt
    assert "--no-download" not in res.stdout
    assert "downloaded (--download-only: nothing installed)" in res.stdout


def test_browser_dry_run_install_phase_never_touches_the_network(sandbox: Sandbox) -> None:
    res = _run(sandbox, BROWSER_SH, "chrome", "--in-installer", "--no-download", "--dry-run")
    assert res.returncode == 0, res.stderr
    apt = _apt_lines(res.stdout)
    assert len(apt) == 1 and "--no-download" in apt[0] and NO_CDROM in apt[0] and " -d " not in apt[0], apt
    assert "would fetch" not in res.stdout and "apt source" not in res.stdout


def test_browser_default_installer_mode_does_both_phases_in_order(sandbox: Sandbox) -> None:
    res = _run(sandbox, BROWSER_SH, "chrome", "--in-installer", "--dry-run")
    apt = _apt_lines(res.stdout)
    assert res.returncode == 0 and len(apt) == 2 and " -d " in apt[0] and "--no-download" in apt[1], apt


def _stage(sb: Sandbox) -> None:
    root = sb.root / "root"
    (root / "etc/apt/keyrings").mkdir(parents=True)
    (root / "etc/apt/sources.list.d").mkdir(parents=True)
    (root / "etc/apt/keyrings/google-chrome.gpg").write_bytes(b"key")
    (root / "etc/apt/sources.list.d/google-chrome.list").write_text(
        "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main\n",
        encoding="utf-8", newline="\n")


def test_browser_uses_the_staged_repository_without_refetching_or_refreshing(sandbox: Sandbox) -> None:
    _stage(sandbox)
    res = _run(sandbox, BROWSER_SH, "chrome", "--in-installer", "--download-only")
    assert res.returncode == 0, res.stdout + res.stderr
    curl = sandbox.calls_of("curl")
    assert curl and all("-fsSL" not in c for c in curl), "the staged key is not downloaded again"
    apt = sandbox.calls_of("apt-get")
    assert len(apt) == 1 and " -d " in apt[0] and "update" not in apt[0], "the caller refreshed the lists already"
    res = _run(sandbox, BROWSER_SH, "chrome", "--in-installer", "--no-download")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "--no-download" in sandbox.calls_of("apt-get")[-1]
    assert len(sandbox.calls_of("curl")) == len(curl), "the install phase makes no network probe at all"


def test_browser_stages_and_refreshes_a_repository_it_had_to_write(sandbox: Sandbox) -> None:
    res = _run(sandbox, BROWSER_SH, "chrome", "--in-installer", "--download-only")
    assert res.returncode == 0, res.stdout + res.stderr
    root = sandbox.root / "root"
    assert (root / "etc/apt/keyrings/google-chrome.gpg").is_file() and (root / "etc/apt/sources.list.d/google-chrome.list").is_file()
    apt = sandbox.calls_of("apt-get")
    assert any("update" in c and "Dir::Etc::sourcelist=" in c and "sourceparts=-" in c for c in apt), apt


def test_browser_exit_codes_offline_and_unstaged(sandbox: Sandbox) -> None:
    res = _run(sandbox, BROWSER_SH, "chrome", "--in-installer", "--download-only", FAKE_NET_RC="4")
    assert res.returncode == 3 and "offline" in (res.stdout + res.stderr)
    res = _run(sandbox, BROWSER_SH, "chrome", "--in-installer", "--no-download")
    assert res.returncode == 1 and "not staged" in (res.stdout + res.stderr)


def test_browser_without_installer_mode_is_unchanged(sandbox: Sandbox) -> None:
    res = _run(sandbox, BROWSER_SH, "chrome", "--dry-run")
    assert res.returncode == 0
    apt = _apt_lines(res.stdout)
    assert len(apt) == 2 and "update" in apt[0] and NO_CDROM not in res.stdout and "-d " not in apt[1] and "--no-download" not in res.stdout


# --------------------------------------------------------------------------- install-compat.sh
def test_compat_phase_flags_need_the_installer_mode(sandbox: Sandbox) -> None:
    res = _run(sandbox, COMPAT_SH, "--download-only", "--dry-run", "winetricks")
    assert res.returncode == 2 and "need --in-installer" in (res.stdout + res.stderr)
    res = _run(sandbox, COMPAT_SH, "--in-installer", "--download-only", "--no-download", "--dry-run", "winetricks")
    assert res.returncode == 2


def test_compat_download_phase_downloads_only_and_skips_the_medium(sandbox: Sandbox) -> None:
    res = _run(sandbox, COMPAT_SH, "--in-installer", "--download-only", "--dry-run", "winetricks")
    assert res.returncode == 0, res.stdout + res.stderr
    apt = _apt_lines(res.stdout + res.stderr)
    assert apt and all(" -d " in a and NO_CDROM in a for a in apt), apt
    assert not any("--no-download" in a for a in apt) and not any(" update" in a for a in apt)


def test_compat_install_phase_uses_only_the_downloaded_files(sandbox: Sandbox) -> None:
    res = _run(sandbox, COMPAT_SH, "--in-installer", "--no-download", "--dry-run", "winetricks")
    apt = _apt_lines(res.stdout + res.stderr)
    assert res.returncode == 0 and len(apt) == 1 and "--no-download" in apt[0] and " -d " not in apt[0] and " update" not in apt[0], apt


def test_compat_umu_is_a_download_phase_job(sandbox: Sandbox) -> None:
    res = _run(sandbox, COMPAT_SH, "--in-installer", "--no-download", "--dry-run", "umu")
    assert res.returncode == 1 and "umu-launcher was not fetched by the download phase" in (res.stdout + res.stderr)


def test_compat_flatpak_and_per_user_items_are_left_to_the_caller(sandbox: Sandbox) -> None:
    res = _run(sandbox, COMPAT_SH, "--in-installer", "--dry-run", "bottles", "proton")
    out = res.stdout + res.stderr
    assert res.returncode == 0 and "the caller handles Flatpak" in out and "per-user" in out
    assert "flatpak" not in " ".join(sandbox.call_log())


def test_compat_from_chroot_keeps_its_build_hook_semantics(sandbox: Sandbox) -> None:
    res = _run(sandbox, COMPAT_SH, "--from-chroot", "--dry-run", "--no-update", "winetricks")
    apt = _apt_lines(res.stdout + res.stderr)
    assert res.returncode == 0 and apt and all(NO_CDROM not in a and " -d " not in a and "--no-download" not in a for a in apt), apt


# --------------------------------------------------------------------------- install-gaming.sh
def test_gaming_phase_flags_need_the_installer_mode(sandbox: Sandbox) -> None:
    res = _run(sandbox, GAMING_SH, "--no-download", "--dry-run", "lutris")
    assert res.returncode == 2 and "need --in-installer" in (res.stdout + res.stderr)
    res = _run(sandbox, GAMING_SH, "--in-installer", "--download-only", "--no-download", "--dry-run", "lutris")
    assert res.returncode == 2


def test_gaming_download_phase_downloads_only(sandbox: Sandbox) -> None:
    res = _run(sandbox, GAMING_SH, "--in-installer", "--download-only", "--dry-run", "lutris")
    assert res.returncode == 0, res.stdout + res.stderr
    apt = _apt_lines(res.stdout + res.stderr)
    assert len(apt) == 1 and " -d " in apt[0] and NO_CDROM in apt[0] and "lutris" in apt[0] and "--no-download" not in apt[0], apt


def test_gaming_install_phase_is_offline(sandbox: Sandbox) -> None:
    res = _run(sandbox, GAMING_SH, "--in-installer", "--no-download", "--dry-run", "lutris", FAKE_NET_RC="4")
    assert res.returncode == 0, "the dpkg phase never asks whether the network is up: " + res.stdout + res.stderr
    apt = _apt_lines(res.stdout + res.stderr)
    assert len(apt) == 1 and "--no-download" in apt[0] and " -d " not in apt[0], apt


def test_gaming_download_phase_offline_exits_3(sandbox: Sandbox) -> None:
    res = _run(sandbox, GAMING_SH, "--in-installer", "--download-only", "--dry-run", "lutris", FAKE_NET_RC="4")
    assert res.returncode == 3 and "offline" in (res.stdout + res.stderr)


def test_gaming_flatpak_items_are_skipped_for_the_caller(sandbox: Sandbox) -> None:
    res = _run(sandbox, GAMING_SH, "--in-installer", "--dry-run", "sober", "bottles")
    out = res.stdout + res.stderr
    assert res.returncode == 0 and "the caller handles Flatpak" in out and "skipped=[sober bottles]" in out


def test_gaming_all_means_the_apt_items_in_installer_mode(sandbox: Sandbox) -> None:
    res = _run(sandbox, GAMING_SH, "--in-installer", "--dry-run", "all")
    out = res.stdout + res.stderr
    assert "chroot mode: apt items only" in out
    for flatpak_only in ("sober", "vinegar", "mcpelauncher", "bottles"):
        assert f"=== {flatpak_only} ===" not in out


def test_gaming_steam_download_then_install_phases(sandbox: Sandbox) -> None:
    dl = _run(sandbox, GAMING_SH, "--in-installer", "--download-only", "--dry-run", "steam")
    assert dl.returncode == 0, dl.stdout + dl.stderr
    apt = _apt_lines(dl.stdout + dl.stderr)
    assert any("steam-launcher" in a and " -d " in a for a in apt) and not any("--no-download" in a for a in apt), apt
    inst = _run(sandbox, GAMING_SH, "--in-installer", "--no-download", "--dry-run", "steam")
    assert inst.returncode == 0, inst.stdout + inst.stderr
    apt = _apt_lines(inst.stdout + inst.stderr)
    assert any("steam-launcher" in a and "--no-download" in a for a in apt) and not any(" -d " in a for a in apt), apt
    assert "apt-get update" not in inst.stdout + inst.stderr and "fetch" not in " ".join(sandbox.calls_of("curl")[-1:])


def test_gaming_from_chroot_keeps_its_build_hook_semantics(sandbox: Sandbox) -> None:
    res = _run(sandbox, GAMING_SH, "--from-chroot", "--dry-run", "lutris")
    apt = _apt_lines(res.stdout + res.stderr)
    assert res.returncode == 0 and apt and all(NO_CDROM not in a and " -d " not in a and "--no-download" not in a for a in apt), apt


@pytest.mark.parametrize("script", [BROWSER_SH, COMPAT_SH, GAMING_SH], ids=lambda p: p.name)
def test_scripts_document_the_installer_mode(script: Path) -> None:
    text = script.read_text(encoding="utf-8")
    for flag in ("--in-installer", "--download-only", "--no-download"):
        assert flag in text.split("set -Eeuo pipefail", 1)[0], (script.name, flag)
    assert "--from-chroot" in text or script.name == "install-browser.sh"
