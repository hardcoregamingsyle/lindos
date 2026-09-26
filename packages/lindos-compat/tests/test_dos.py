"""lindos_compat.dos: DOSBox discovery and argv per flavour, the Wine WoW64-mode probe (SPEC-WINDOWS §28.5).

Hermetic: ``which``/``run`` are fakes; Flatpak exports live under LINDOS_HOME/LINDOS_ROOT.
"""
from __future__ import annotations

import json
import os
import subprocess
import types
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pytest

from lindos_compat import dos, prefix as prefix_mod


def which_of(*names: str) -> Callable[[str], Optional[str]]:
    table = {n: f"/usr/bin/{n}" for n in names}
    return table.get


class Runner:
    """Fake subprocess.run: answers by the argv's program and records everything."""

    def __init__(self, answers: Optional[Dict[str, object]] = None) -> None:
        self.answers = answers or {}
        self.calls: List[List[str]] = []
        self.envs: List[Dict[str, str]] = []

    def __call__(self, argv, *a, **k):
        argv = [str(x) for x in argv]
        self.calls.append(argv)
        self.envs.append(dict(k.get("env") or {}))
        key = " ".join([os.path.basename(argv[0])] + argv[1:2])
        ans = self.answers.get(key, self.answers.get(os.path.basename(argv[0]), (0, "", "")))
        if isinstance(ans, BaseException):
            raise ans
        rc, out, err = ans  # type: ignore[misc]
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err, args=argv)

    def ran(self, program: str) -> List[List[str]]:
        return [c for c in self.calls if os.path.basename(c[0]) == program or c[:2] == ["/usr/bin/wine", program]]


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    monkeypatch.setattr(prefix_mod, "WINE_CANDIDATES", ())
    dos.clear_wow64_memo()


# --------------------------------------------------------------------------- #
# find_dosbox
# --------------------------------------------------------------------------- #
def test_find_dosbox_prefers_dosbox_x(home: Path):
    run = Runner()
    assert dos.find_dosbox(which_of("dosbox-x", "dosbox", "flatpak"), run) == (["/usr/bin/dosbox-x"], "dosbox-x")
    assert run.calls == []


def test_find_dosbox_flatpak_x_via_exports(home: Path):
    exports = home / ".local/share/flatpak/exports/bin" / dos.DOSBOX_X_FLATPAK
    exports.parent.mkdir(parents=True)
    exports.write_text("#!/bin/sh\n", encoding="ascii")
    run = Runner()
    got = dos.find_dosbox(which_of("flatpak", "dosbox"), run, home=home)
    assert got == (["/usr/bin/flatpak", "run", dos.DOSBOX_X_FLATPAK], "dosbox-x")


def test_find_dosbox_flatpak_system_exports_honour_lindos_root(home: Path):
    root = Path(os.environ["LINDOS_ROOT"])
    exports = root / "var/lib/flatpak/exports/bin" / dos.STAGING_FLATPAK
    exports.parent.mkdir(parents=True)
    exports.write_text("", encoding="ascii")
    run = Runner({"flatpak info": (1, "", "not installed")})
    assert dos.find_dosbox(which_of("flatpak"), run, home=home) == (
        ["/usr/bin/flatpak", "run", dos.STAGING_FLATPAK], "staging")


def test_find_dosbox_flatpak_info(home: Path):
    def run(argv, *a, **k):
        ok = argv[-1] == dos.DOSBOX_X_FLATPAK
        return types.SimpleNamespace(returncode=0 if ok else 1, stdout="", stderr="")

    assert dos.find_dosbox(which_of("flatpak"), run, home=home)[1] == "dosbox-x"


@pytest.mark.parametrize("version,flavor", [
    ("\nDOSBox version 0.74-3, copyright 2002-2019 DOSBox Team.\n\n", "dosbox"),
    ("dosbox-staging, version 0.83.0\n", "staging"),
    ("DOSBox Staging version 0.82.2", "staging"),
])
def test_find_dosbox_tells_staging_from_074(home: Path, version: str, flavor: str):
    run = Runner({"dosbox --version": (0, version, "")})
    assert dos.find_dosbox(which_of("dosbox"), run, home=home) == (["/usr/bin/dosbox"], flavor)
    assert run.calls == [["/usr/bin/dosbox", "--version"]]


def test_find_dosbox_version_probe_failure_means_074(home: Path):
    run = Runner({"dosbox --version": subprocess.TimeoutExpired("dosbox", 15)})
    assert dos.find_dosbox(which_of("dosbox"), run, home=home) == (["/usr/bin/dosbox"], "dosbox")


def test_find_dosbox_none(home: Path):
    assert dos.find_dosbox(which_of(), Runner(), home=home) is None
    assert dos.find_dosbox(which_of("flatpak"), Runner({"flatpak info": (1, "", "")}), home=home) is None


def test_flatpak_app_installed_errors(home: Path):
    assert dos.flatpak_app_installed("x.y.Z", which=which_of(), run=Runner(), home=home) is False
    boom = Runner({"flatpak info": OSError("no exec")})
    assert dos.flatpak_app_installed("x.y.Z", which=which_of("flatpak"), run=boom, home=home) is False


# --------------------------------------------------------------------------- #
# dosbox_argv
# --------------------------------------------------------------------------- #
def test_argv_dosbox_x(tmp_path: Path, home: Path):
    exe = tmp_path / "GAME.EXE"
    exe.write_bytes(b"MZ")
    argv = dos.dosbox_argv(exe, which=which_of("dosbox-x"), run=Runner(), home=home)
    assert argv == ["/usr/bin/dosbox-x", "-fastlaunch", "-nopromptfolder", "-exit", str(exe.absolute())]
    assert "-noconsole" not in argv


def test_argv_staging_and_074(tmp_path: Path, home: Path):
    exe = tmp_path / "PROG.EXE"
    staging = dos.dosbox_argv(exe, which=which_of("dosbox"), run=Runner({"dosbox --version": (0, "staging", "")}),
                              home=home)
    assert staging == ["/usr/bin/dosbox", str(exe.absolute())]
    old = dos.dosbox_argv(exe, which=which_of("dosbox"), run=Runner({"dosbox --version": (0, "DOSBox 0.74", "")}),
                          home=home)
    assert old == ["/usr/bin/dosbox", str(exe.absolute()), "-exit"]


def test_argv_with_program_arguments(tmp_path: Path, home: Path):
    folder = tmp_path / "Old Games"
    exe = folder / "Commander Keen.exe"
    x = dos.dosbox_argv(exe, ["/nojoy", "two words"], which=which_of("dosbox-x"), run=Runner(), home=home)
    assert x == ["/usr/bin/dosbox-x", "-fastlaunch", "-nopromptfolder", "-c", f'mount c "{folder.absolute()}"',
                 "-c", "c:", "-c", '"Commander Keen.exe" /nojoy "two words"', "-c", "exit"]
    st = dos.dosbox_argv(exe, ["/x"], which=which_of("dosbox"), run=Runner({"dosbox --version": (0, "staging", "")}),
                         home=home)
    assert st[1:3] == ["-c", f'mount c "{folder.absolute()}"'] and st[-1] == "exit"


def test_argv_074_arguments_need_83_names(tmp_path: Path, home: Path):
    run = Runner({"dosbox --version": (0, "DOSBox version 0.74-3", "")})
    short = tmp_path / "KEEN4E.EXE"
    ok = dos.dosbox_argv(short, ["/nojoy"], which=which_of("dosbox"), run=run, home=home)
    assert ok[-4:] == ["-c", "KEEN4E.EXE /nojoy", "-c", "exit"]
    long = tmp_path / "LongGameName.exe"
    dropped = dos.dosbox_argv(long, ["/nojoy"], which=which_of("dosbox"), run=run, home=home)
    assert dropped == ["/usr/bin/dosbox", str(long.absolute()), "-exit"]


def test_argv_quote_in_folder_drops_arguments(tmp_path: Path, home: Path):
    exe = tmp_path / 'we"ird' / "A.EXE"
    argv = dos.dosbox_argv(exe, ["/x"], which=which_of("dosbox-x"), run=Runner(), home=home)
    assert argv[-2:] == ["-exit", str(exe.absolute())]


def test_argv_flatpak_filesystem(tmp_path: Path, home: Path):
    exports = home / ".local/share/flatpak/exports/bin" / dos.DOSBOX_X_FLATPAK
    exports.parent.mkdir(parents=True)
    exports.write_text("", encoding="ascii")
    outside = tmp_path / "media" / "CD" / "SETUP.EXE"
    argv = dos.dosbox_argv(outside, which=which_of("flatpak"), run=Runner(), home=home)
    assert argv[:4] == ["/usr/bin/flatpak", "run", f"--filesystem={outside.parent.absolute()}:ro",
                        dos.DOSBOX_X_FLATPAK]
    assert argv[-1] == str(outside.absolute())
    inside = home / "Games" / "A.EXE"
    argv2 = dos.dosbox_argv(inside, which=which_of("flatpak"), run=Runner(), home=home)
    assert argv2[:3] == ["/usr/bin/flatpak", "run", dos.DOSBOX_X_FLATPAK]
    assert not any(a.startswith("--filesystem") for a in argv2)


def test_argv_missing_dosbox(tmp_path: Path, home: Path):
    with pytest.raises(FileNotFoundError) as exc:
        dos.dosbox_argv(tmp_path / "A.EXE", which=which_of(), run=Runner(), home=home)
    assert dos.DOSBOX_INSTALL_HINT in str(exc.value)
    assert dos.DOSBOX_INSTALL_HINT == "sudo apt install dosbox-x"


@pytest.mark.parametrize("name,ok", [("KEEN.EXE", True), ("keen4e.exe", True), ("A", True), ("GAME_1.COM", True),
                                     ("LONGNAME1.EXE", False), ("A.EXEC", False), ("MY GAME.EXE", False),
                                     ("A.B.EXE", False), ("", False)])
def test_is_83_name(name: str, ok: bool):
    assert dos.is_83_name(name) is ok


# --------------------------------------------------------------------------- #
# Wine version / WoW64 mode
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,expected", [
    ("wine-9.0 (Ubuntu 9.0~repack-4build3)", (9, 0)), ("wine-10.16 (Staging)", (10, 16)), ("wine-11.0", (11, 0)),
    ("wine-11.18", (11, 18)), ("", None), ("Wine 9", None),
])
def test_parse_wine_version(text: str, expected):
    assert dos.parse_wine_version(text) == expected


WOW64_FATAL = "wine: WINEARCH is set to 'win32' but this is not supported in wow64 mode.\n"


@pytest.mark.parametrize("version,rc,stderr,mode", [
    ("wine-9.0 (Ubuntu 9.0~repack-4build3)", 0, "", "old-wow64"),
    ("wine-11.0", 1, WOW64_FATAL, "new-wow64-16bit"),
    ("wine-10.16 (Staging)", 1, WOW64_FATAL, "new-wow64-16bit"),
    ("wine-10.15", 1, WOW64_FATAL, "new-wow64-no16bit"),
    ("wine-9.0", 1, WOW64_FATAL, "new-wow64-no16bit"),
    ("wine-strange", 1, WOW64_FATAL, "new-wow64-16bit"),
    ("wine-9.0", 1, "it looks like wine32 is missing, you should install it.\n", "unknown"),
])
def test_wow64_probe(tmp_path: Path, version: str, rc: int, stderr: str, mode: str):
    run = Runner({"wine --version": (0, version + "\n", ""), "wineboot": (rc, "", stderr),
                  "wine wineboot": (rc, "", stderr)})
    got = dos.wine_wow64_mode(which=which_of("wine", "wineboot", "wineserver"), run=run, cache_dir=tmp_path)
    assert got == mode
    probe = [c for c in run.calls if "wineboot" in os.path.basename(c[0]) or c[1:2] == ["wineboot"]]
    assert len(probe) == 1 and probe[0][-1] == "-i"
    env = run.envs[run.calls.index(probe[0])]
    assert env["WINEARCH"] == "win32" and env["WINEPREFIX"].endswith("pfx")
    assert "mscoree" in env["WINEDLLOVERRIDES"]
    assert not Path(env["WINEPREFIX"]).parent.exists(), "the temporary prefix must be removed"
    assert any(c[-1] == "-k" and c[0].endswith("wineserver") for c in run.calls), "wineserver -k after the probe"
    cached = tmp_path / "wine-wow64.json"
    assert cached.exists() is (mode != "unknown")


def test_wow64_uses_wine_wineboot_without_wineboot_binary(tmp_path: Path):
    run = Runner({"wine --version": (0, "wine-9.0", ""), "wine wineboot": (0, "", "")})
    assert dos.wine_wow64_mode(which=which_of("wine"), run=run, cache_dir=tmp_path) == "old-wow64"
    assert ["/usr/bin/wine", "wineboot", "-i"] in run.calls


def test_wow64_no_wine(tmp_path: Path):
    run = Runner()
    assert dos.wine_wow64_mode(which=which_of(), run=run, cache_dir=tmp_path) == "unknown"
    assert run.calls == []


def test_wow64_memo_and_cache(tmp_path: Path):
    answers = {"wine --version": (0, "wine-11.0\n", ""), "wineboot": (1, "", WOW64_FATAL)}
    first = Runner(answers)
    w = which_of("wine", "wineboot")
    assert dos.wine_wow64_mode(which=w, run=first, cache_dir=tmp_path) == "new-wow64-16bit"
    # same process: memo, only the (cheap) version query runs
    second = Runner(answers)
    assert dos.wine_wow64_mode(which=w, run=second, cache_dir=tmp_path) == "new-wow64-16bit"
    assert [c[1] for c in second.calls] == ["--version"]
    # new process (memo cleared): the cache file answers, no probe
    dos.clear_wow64_memo()
    third = Runner({"wine --version": (0, "wine-11.0\n", ""), "wineboot": (0, "", "")})
    assert dos.wine_wow64_mode(which=w, run=third, cache_dir=tmp_path) == "new-wow64-16bit"
    assert not any("wineboot" in c[0] for c in third.calls)
    data = json.loads((tmp_path / "wine-wow64.json").read_text(encoding="utf-8"))
    assert data["mode"] == "new-wow64-16bit" and data["version"] == "wine-11.0" and data["wine"] == "/usr/bin/wine"
    # another Wine version invalidates the cache
    dos.clear_wow64_memo()
    fourth = Runner({"wine --version": (0, "wine-9.0\n", ""), "wineboot": (0, "", "")})
    assert dos.wine_wow64_mode(which=w, run=fourth, cache_dir=tmp_path) == "old-wow64"


def test_wow64_unknown_is_not_cached_and_timeout(tmp_path: Path):
    w = which_of("wine", "wineboot")
    timeout = Runner({"wine --version": (0, "wine-9.0\n", ""), "wineboot": subprocess.TimeoutExpired("wineboot", 1)})
    assert dos.wine_wow64_mode(which=w, run=timeout, cache_dir=tmp_path) == "unknown"
    assert not (tmp_path / "wine-wow64.json").exists()
    dos.clear_wow64_memo()
    ok = Runner({"wine --version": (0, "wine-9.0\n", ""), "wineboot": (0, "", "")})
    assert dos.wine_wow64_mode(which=w, run=ok, cache_dir=tmp_path) == "old-wow64"


def test_wow64_corrupt_cache(tmp_path: Path):
    (tmp_path / "wine-wow64.json").write_text("{broken", encoding="utf-8")
    run = Runner({"wine --version": (0, "wine-9.0\n", ""), "wineboot": (0, "", "")})
    assert dos.wine_wow64_mode(which=which_of("wine", "wineboot"), run=run, cache_dir=tmp_path) == "old-wow64"
    (tmp_path / "wine-wow64.json").write_text(json.dumps({"wine": "/usr/bin/wine", "version": "wine-9.0",
                                                          "mode": "bogus"}), encoding="utf-8")
    dos.clear_wow64_memo()
    run2 = Runner({"wine --version": (0, "wine-9.0\n", ""), "wineboot": (0, "", "")})
    assert dos.wine_wow64_mode(which=which_of("wine", "wineboot"), run=run2, cache_dir=tmp_path) == "old-wow64"
    assert any("wineboot" in c[0] for c in run2.calls)


def test_wow64_default_cache_dir_honours_lindos_home(home: Path):
    assert dos.wow64_cache_dir() == home / ".cache" / "lindos"
    run = Runner({"wine --version": (0, "wine-9.0\n", ""), "wineboot": (0, "", "")})
    assert dos.wine_wow64_mode(which=which_of("wine", "wineboot"), run=run) == "old-wow64"
    assert (home / ".cache/lindos/wine-wow64.json").is_file()


def test_modes_constant():
    assert dos.WOW64_MODES == ("old-wow64", "new-wow64-16bit", "new-wow64-no16bit", "unknown")
    assert dos.FLAVORS == ("dosbox-x", "staging", "dosbox")
