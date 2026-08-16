"""Tests for ``lindos-compat doctor`` (lindos_compat.doctor) with a fake ``which``/dpkg."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lindos_compat import cli_compat, doctor

ALL_TOOLS = ("wine", "wineboot", "winetricks", "umu-run", "vulkaninfo", "cabextract", "winbindd", "gamemoderun",
             "mangohud", "flatpak", "wrestool", "icotool", "zenity", "dpkg-query")
ALL_PKGS = {"libvulkan1", "mesa-vulkan-drivers", "libvulkan1:i386", "mesa-vulkan-drivers:i386", "fonts-liberation",
            "ttf-mscorefonts-installer", "winbind", "wine-staging-i386:i386"}


def _run_factory(version: str = "wine-9.0 (Staging)"):
    def run(argv, *a, **k):
        if argv and str(argv[0]).endswith("wine") and "--version" in argv:
            return SimpleNamespace(returncode=0, stdout=version + "\n", stderr="")
        if argv and str(argv[0]).endswith("flatpak") and "info" in argv:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return run


def test_doctor_all_missing(fake_core, home: Path, fake_which):
    rep = doctor.run_doctor(which=fake_which(), run=_run_factory(), dpkg=lambda p: False, env={}, home=home,
                            isdir=lambda p: False, isfile=lambda p: False)
    by_id = {c.id: c for c in rep.checks}
    for cid in ("core", "wine", "wine32", "winetricks", "umu", "proton-ge", "vulkan-tools", "vulkan32", "fonts",
                "corefonts", "cabextract", "winbind", "gamemode", "mangohud", "bottles", "icoutils", "zenity",
                "prefixes-dir", "display"):
        assert cid in by_id, cid
    assert by_id["core"].ok                       # fake lindos.compat is importable
    assert not by_id["wine"].ok and "install-compat.sh" in by_id["wine"].fix
    assert not by_id["umu"].ok and by_id["umu"].fix == "lindos-compat install-umu"
    assert not by_id["vulkan32"].ok and "libvulkan1:i386" in by_id["vulkan32"].fix
    assert not by_id["fonts"].ok and "fonts-liberation" in by_id["fonts"].fix
    assert not by_id["display"].ok
    assert by_id["prefixes-dir"].ok and (home / ".local/share/lindos/prefixes").is_dir()
    assert not rep.ok
    for c in rep.checks:
        if not c.ok:
            assert c.fix, f"{c.id} failed without a fix command"
        assert c.level in doctor.LEVELS
    text = doctor.format_report(rep)
    assert "\u2717" in text and "fix:" in text and "NOT ready" in text
    ascii_text = doctor.format_report(rep, use_unicode=False)
    assert "X " in ascii_text and "\u2717" not in ascii_text
    json.dumps(rep.as_dict())  # --json shape
    s = rep.summary()
    assert s["ok"] is False and s["required"]["total"] >= 3


def test_doctor_all_present(fake_core, home: Path, fake_which):
    proton = home / ".local/share/umu/compatibilitytools/GE-Proton9-20"
    proton.mkdir(parents=True)
    (proton / "proton").write_text("")
    (home / ".local/share/flatpak/exports/bin").mkdir(parents=True)
    (home / ".local/share/flatpak/exports/bin/com.usebottles.bottles").write_text("")
    rep = doctor.run_doctor(which=fake_which(*ALL_TOOLS), run=_run_factory(), dpkg=lambda p: p in ALL_PKGS,
                            env={"DISPLAY": ":0"}, home=home, isdir=lambda p: False, isfile=lambda p: False)
    by_id = {c.id: c for c in rep.checks}
    assert rep.ok
    assert by_id["wine"].ok and "wine-9.0" in by_id["wine"].detail
    assert by_id["wine32"].ok and by_id["umu"].ok and by_id["proton-ge"].ok and by_id["vulkan32"].ok
    assert by_id["fonts"].ok and by_id["corefonts"].ok and by_id["bottles"].ok and by_id["icoutils"].ok
    assert by_id["display"].ok and by_id["gamemode"].ok and by_id["mangohud"].ok and by_id["zenity"].ok
    assert "GE-Proton9-20" in by_id["proton-ge"].detail
    text = doctor.format_report(rep)
    assert "\u2713" in text and "can run on this PC" in text
    assert all(c.fix == "" for c in rep.checks if c.ok)


def test_doctor_partial(fake_core, home: Path, fake_which):
    rep = doctor.run_doctor(which=fake_which("wine", "dpkg-query"), run=_run_factory("wine-8.0"),
                            dpkg=lambda p: p == "fonts-liberation", env={"DISPLAY": ":1"}, home=home,
                            isdir=lambda p: False, isfile=lambda p: False)
    by_id = {c.id: c for c in rep.checks}
    assert rep.ok                                # required: core, wine, prefixes-dir, display
    assert not by_id["umu"].ok and not by_id["vulkan32"].ok
    assert "recommended items" in doctor.format_report(rep)


def test_dpkg_installed_probe():
    calls = []

    def run(argv, *a, **k):
        calls.append(argv)
        pkg = argv[-1]
        return SimpleNamespace(returncode=0 if pkg == "libvulkan1:i386" else 1,
                               stdout="installed" if pkg == "libvulkan1:i386" else "", stderr="")

    which = lambda n: "/usr/bin/dpkg-query" if n == "dpkg-query" else None  # noqa: E731
    assert doctor.dpkg_installed("libvulkan1:i386", which=which, run=run)
    assert not doctor.dpkg_installed("mesa-vulkan-drivers:i386", which=which, run=run)
    assert calls[0][:2] == ["/usr/bin/dpkg-query", "-W"]
    assert not doctor.dpkg_installed("x", which=lambda n: None, run=run)


def test_cli_doctor_json(fake_core, home: Path, monkeypatch, capsys, fake_which):
    def fake_run_doctor(**kw):
        return doctor.run_doctor(which=fake_which(*ALL_TOOLS), run=_run_factory(), dpkg=lambda p: p in ALL_PKGS,
                                 env={"DISPLAY": ":0"}, home=home, isdir=lambda p: False, isfile=lambda p: False)

    monkeypatch.setattr(cli_compat, "run_doctor", fake_run_doctor)
    rc = cli_compat.main(["doctor", "--json"])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert rc == 0 and data["ok"] is True and isinstance(data["checks"], list)
    rc = cli_compat.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0 and "\u2713" in out

    def failing(**kw):
        return doctor.run_doctor(which=fake_which(), run=_run_factory(), dpkg=lambda p: False, env={}, home=home,
                                 isdir=lambda p: False, isfile=lambda p: False)

    monkeypatch.setattr(cli_compat, "run_doctor", failing)
    assert cli_compat.main(["doctor", "--ascii"]) == 1
    assert "X " in capsys.readouterr().out


def test_cli_usage(capsys):
    assert cli_compat.main([]) == 2
    with pytest.raises(SystemExit) as exc:
        cli_compat.main(["bogus"])
    assert exc.value.code == 2
    p = cli_compat.build_parser()
    ns = p.parse_args(["recipes", "apply", "office-2016", "--prefix", "office"])
    assert ns.cmd == "recipes" and ns.sub == "apply" and ns.id == "office-2016" and ns.prefix == "office"
    ns = p.parse_args(["prefixes", "winecfg", "foo"])
    assert ns.sub == "winecfg" and ns.slug == "foo"
    ns = p.parse_args(["install-umu", "--system"])
    assert ns.system
    ns = p.parse_args(["proton", "list"])
    assert ns.proton_args == ["list"]


def test_cli_recipes_list_and_show(fake_core, home: Path, monkeypatch, capsys, repo_recipes_dir: Path):
    monkeypatch.setenv("LINDOS_RECIPES_DIR", str(repo_recipes_dir))
    assert cli_compat.main(["recipes", "list"]) == 0
    out = capsys.readouterr().out
    assert "riot-client" in out and "does NOT work" in out and "notepad-plus-plus" in out
    assert cli_compat.main(["recipes", "list", "--json", "--status", "broken"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {d["id"] for d in data} >= {"premiere", "office-365", "riot-client", "roblox-player", "autocad"}
    assert cli_compat.main(["recipes", "show", "office-2016"]) == 0
    assert "Winetricks: msxml6 riched20 corefonts" in capsys.readouterr().out
    assert cli_compat.main(["recipes", "show", "nope"]) == 1
    # apply refuses broken with the alternative printed
    assert cli_compat.main(["recipes", "apply", "roblox-player"]) == 1
    assert "Sober" in capsys.readouterr().out
    assert cli_compat.main(["recipes", "apply", "office-2016", "--dry-run"]) == 0


def test_cli_prefixes_list_empty(fake_core, home: Path, capsys):
    assert cli_compat.main(["prefixes", "list"]) == 0
    assert "No C:\\ drives yet" in capsys.readouterr().out
    assert cli_compat.main(["prefixes", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert cli_compat.main(["prefixes", "remove", "nope", "--yes"]) == 1
