"""Tests for runner choice, environment construction, prefix paths and .desktop generation."""
from __future__ import annotations

import functools
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lindos_compat import cli_run, prefix, runner, scan
from lindos_compat.icons import GENERIC_ICON


def _info(kind: str, name: str = "prog.exe", product: str = "") -> SimpleNamespace:
    return SimpleNamespace(path=f"/tmp/{name}", name=name, kind=kind, arch="x64", installer_type=None,
                           product=product, company="", sha256_prefix="")


# --------------------------------------------------------------------------- #
# choose_runner
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind,tools,expected", [
    ("game", ("umu-run", "wine"), "umu"),
    ("unknown", ("umu-run", "wine"), "umu"),
    ("game", ("wine",), "wine"),            # umu missing -> wine
    ("installer", ("umu-run", "wine"), "wine"),
    ("app", ("umu-run", "wine"), "wine"),
    ("msi", ("umu-run", "wine"), "wine"),
    ("app", ("umu-run",), "umu"),           # wine missing -> umu can still run it
    ("app", (), "wine"),                    # nothing installed: wine (doctor tells the user)
])
def test_choose_runner_rules(fake_which, kind, tools, expected):
    r, reason = runner.choose_runner(_info(kind), {}, which=fake_which(*tools), bottles_installed=False)
    assert r == expected
    assert reason


def test_choose_runner_requested_and_recipe(fake_which):
    w = fake_which("umu-run", "wine", "flatpak")
    assert runner.choose_runner(_info("app"), {}, requested="umu", which=w, bottles_installed=False)[0] == "umu"
    assert runner.choose_runner(_info("game"), {}, requested="wine", which=w, bottles_installed=False)[0] == "wine"
    # requested runner not installed -> falls back with an explanation
    r, reason = runner.choose_runner(_info("game"), {}, requested="bottles", which=w, bottles_installed=False)
    assert r == "umu" and "Bottles" in reason
    r, reason = runner.choose_runner(_info("app"), {}, requested="umu", which=fake_which("wine"), bottles_installed=False)
    assert r == "wine" and "umu-run" in reason
    # recipe wants bottles
    recipe = SimpleNamespace(runner="bottles")
    assert runner.choose_runner(_info("app"), {}, recipe=recipe, which=w, bottles_installed=True)[0] == "bottles"
    assert runner.choose_runner(_info("app"), {}, recipe=recipe, which=w, bottles_installed=False)[0] == "wine"
    # a prefix created by umu keeps umu even for an "app"
    assert runner.choose_runner(_info("app"), {}, prefix_runner="umu", which=w, bottles_installed=False)[0] == "umu"


# --------------------------------------------------------------------------- #
# env
# --------------------------------------------------------------------------- #
def test_wine_env(tmp_path: Path):
    env = runner.wine_env(tmp_path / "pfx", arch="win64", dll_overrides={"gdiplus": "native,builtin"})
    assert env["WINEPREFIX"] == str(tmp_path / "pfx")
    assert env["WINEARCH"] == "win64"
    assert env["WINEDEBUG"] == "-all"
    assert "winemenubuilder.exe=d" in env["WINEDLLOVERRIDES"]
    assert "gdiplus=native,builtin" in env["WINEDLLOVERRIDES"]
    env32 = runner.wine_env(tmp_path / "pfx", arch="win32", headless=True)
    assert env32["WINEARCH"] == "win32"
    assert "mscoree=d" in env32["WINEDLLOVERRIDES"]


def test_umu_env_with_and_without_proton(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    monkeypatch.delenv("PROTONPATH", raising=False)
    env = runner.umu_env(tmp_path / "pfx", "my-game", home=home, nvidia=False)
    assert env["GAMEID"] == "umu-my-game"
    assert env["STORE"] == "none"
    assert env["DXVK_ASYNC"] == "1"
    assert env["WINEDEBUG"] == "-all"
    assert env["PROTONPATH"] == "GE-Proton"
    assert "winemenubuilder.exe=d" in env["WINEDLLOVERRIDES"]
    assert "PROTON_ENABLE_NVAPI" not in env
    # shared prefix -> umu-default
    assert runner.umu_env(tmp_path / "pfx", prefix.SHARED_SLUG, home=home, nvidia=False)["GAMEID"] == "umu-default"
    # installed GE-Proton: highest version wins
    for name in ("GE-Proton9-5", "GE-Proton9-20", "GE-Proton8-32"):
        d = home / ".local/share/umu/compatibilitytools" / name
        d.mkdir(parents=True)
        (d / "proton").write_text("#!/bin/sh\n")
    steam_dir = home / ".steam/root/compatibilitytools.d" / "GE-Proton9-7"
    steam_dir.mkdir(parents=True)
    (steam_dir / "proton").write_text("")
    env = runner.umu_env(tmp_path / "pfx", "my-game", home=home, nvidia=True)
    assert env["PROTONPATH"].endswith("GE-Proton9-20")
    assert env["PROTON_ENABLE_NVAPI"] == "1"
    # user override wins
    monkeypatch.setenv("PROTONPATH", "/opt/proton")
    assert runner.umu_env(tmp_path / "pfx", "g", home=home, nvidia=False)["PROTONPATH"] == "/opt/proton"


# --------------------------------------------------------------------------- #
# command line
# --------------------------------------------------------------------------- #
def test_build_command_variants(fake_which):
    w = fake_which("wine", "umu-run", "gamemoderun", "mangohud", "flatpak")
    msi, bat, game, app, g = (Path("/x/Setup.msi"), Path("/x/run.bat"), Path("/x/game.exe"), Path("/x/app.exe"),
                              Path("/x/g.exe"))
    # Wine tools get Windows paths (SPEC-WINDOWS §28.1): Z:\ outside a C:\ drive
    argv, warns = runner.build_command("wine", msi, "msi", which=w)
    assert argv == ["/usr/bin/wine", "msiexec", "/i", runner.wine_path(msi)] and not warns
    assert argv[3].startswith("Z:\\") and argv[3].endswith("\\x\\Setup.msi")
    # SPEC-WINDOWS §28.3: a .msp is a *patch* - "/p ... REINSTALL=ALL REINSTALLMODE=omus", never
    # the old "/i" (which fails: msiexec refuses to install a patch as if it were a product).
    msp = Path("/x/Update.msp")
    argv, warns = runner.build_command("wine", msp, "installer", which=w)
    assert argv == ["/usr/bin/wine", "msiexec", "/p", runner.wine_path(msp), *runner.MSP_PROPERTIES] and not warns
    assert "/i" not in argv
    # an explicit "tail" (SPEC-WINDOWS §28.1: the complete argv after wine/umu-run, e.g. from
    # formats.plan_action) always wins over the file-kind guess, and "args" are appended after it.
    argv, _ = runner.build_command("wine", Path("/x/settings.reg"), "app", which=w,
                                   tail=["regedit", "/S", "Z:\\x\\settings.reg"], args=["/extra"])
    assert argv == ["/usr/bin/wine", "regedit", "/S", "Z:\\x\\settings.reg", "/extra"]
    argv, _ = runner.build_command("wine", bat, "app", which=w, args=["a", "b"])
    assert argv == ["/usr/bin/wine", "cmd", "/c", runner.wine_path(bat), "a", "b"]
    argv, _ = runner.build_command("umu", game, "game", which=w, gamemode=True, mangohud=True)
    # umu: MangoHud is enabled through the env (MANGOHUD=1), gamemoderun wraps the command
    assert argv == ["/usr/bin/gamemoderun", "/usr/bin/umu-run", str(game)]
    argv, _ = runner.build_command("wine", game, "game", which=w, gamemode=True, mangohud=True)
    assert argv[:3] == ["/usr/bin/gamemoderun", "/usr/bin/mangohud", "/usr/bin/wine"]
    argv, _ = runner.build_command("bottles", app, "app", which=w, bottle="foo", args=["-q"])
    assert argv[:2] == ["/usr/bin/flatpak", "run"] and "com.usebottles.bottles" in argv and "-b" in argv
    # missing wrappers -> warnings, not failures
    argv, warns = runner.build_command("wine", g, "game", which=fake_which("wine"), gamemode=True, mangohud=True)
    assert argv == ["/usr/bin/wine", str(g)] and len(warns) == 2
    with pytest.raises(FileNotFoundError):
        runner.build_command("umu", Path("/x/g.exe"), "game", which=fake_which("wine"))


def test_build_plan_gamemode_auto_and_config(fake_which, home: Path):
    w = fake_which("wine", "umu-run", "gamemoderun", "mangohud")
    cfg = {"gamemode_auto": True, "mangohud": False}
    plan = runner.build_plan(runner="umu", slug="cool-game", exe=Path("/g/CoolGame.exe"), kind="game", config=cfg,
                             which=w, home=home, nvidia=False)
    assert plan.gamemode and not plan.mangohud
    assert plan.argv[0].endswith("gamemoderun")
    assert plan.env["GAMEID"] == "umu-cool-game"
    assert plan.prefix == home / ".local/share/lindos/prefixes/cool-game"
    assert plan.log_path == home / ".local/state/lindos/run-cool-game.log"
    assert plan.cwd == str(Path("/g"))
    # apps get no gamemode unless asked; config mangohud on -> MANGOHUD=1 for umu / mangohud wrapper for wine
    plan = runner.build_plan(runner="wine", slug="tool", exe=Path("/t/tool.exe"), kind="app",
                             config={"gamemode_auto": True, "mangohud": True}, which=w, home=home)
    assert not plan.gamemode and plan.mangohud and plan.argv[0].endswith("mangohud")
    plan = runner.build_plan(runner="wine", slug="tool", exe=Path("/t/tool.exe"), kind="app",
                             config={"gamemode_auto": False}, which=w, home=home, gamemode_flag=True,
                             extra_env={"WINEESYNC": "1"}, dll_overrides={"msxml6": "n,b"}, arch="win32")
    assert plan.gamemode and plan.env["WINEESYNC"] == "1" and plan.env["WINEARCH"] == "win32"
    assert "msxml6=n,b" in plan.env["WINEDLLOVERRIDES"]
    d = plan.as_dict()
    json.dumps(d)  # JSON-able for --dry-run
    assert d["runner"] == "wine"


# --------------------------------------------------------------------------- #
# prefixes
# --------------------------------------------------------------------------- #
def test_prefix_path_and_slugs(fake_core, home: Path):
    assert prefix.prefixes_dir() == home / ".local/share/lindos/prefixes"
    assert prefix.prefix_path("Notepad++ (x64)") == home / ".local/share/lindos/prefixes/notepad-x64"
    assert prefix.safe_slug("../../etc") == "etc"
    assert prefix.safe_slug("") == "app"
    info = SimpleNamespace(product="Notepad++", name="npp.8.6.Installer.x64.exe", path="/d/npp.8.6.Installer.x64.exe")
    assert prefix.derive_slug(info) == "notepad"
    info = SimpleNamespace(product="", name="FooSetup-x64-v1.2.3.exe", path="/d/FooSetup-x64-v1.2.3.exe")
    assert prefix.derive_slug(info) == "foo"
    info = SimpleNamespace(product="", name="setup.exe", path="/d/setup.exe")
    assert prefix.derive_slug(info) == "setup"


def test_ensure_prefix_runs_wineboot_once(fake_core, home: Path, fake_which, fake_run):
    w = fake_which("wine", "wineboot", "wineserver")
    state = prefix.ensure_prefix("demo", runner="wine", which=w, run=fake_run)
    assert state.created and state.initialized
    assert state.path == home / ".local/share/lindos/prefixes/demo"
    assert fake_run.has("wineboot", "-u")
    env = fake_run.envs[0]
    assert env["WINEPREFIX"] == str(state.path) and env["WINEARCH"] == "win64" and env["WINEDEBUG"] == "-all"
    assert "winemenubuilder.exe=d" in env["WINEDLLOVERRIDES"]
    marker = prefix.read_marker(state.path)
    assert marker["runner"] == "wine" and marker["arch"] == "win64"
    n = len(fake_run.calls)
    state2 = prefix.ensure_prefix("demo", runner="wine", which=w, run=fake_run)
    assert not state2.created and state2.initialized
    assert len(fake_run.calls) == n  # no second wineboot
    # umu prefixes are only created (Proton initialises them)
    st = prefix.ensure_prefix("game", runner="umu", which=w, run=fake_run)
    assert st.created and not st.initialized and st.path.is_dir()
    assert prefix.read_marker(st.path)["runner"] == "umu"
    # missing wine -> warning, no crash
    st = prefix.ensure_prefix("nowine", runner="wine", which=fake_which(), run=fake_run)
    assert st.warnings and not st.initialized
    listing = {p["slug"]: p for p in prefix.list_prefixes()}
    assert set(listing) >= {"demo", "game"}
    assert listing["demo"]["initialized"] is True
    assert prefix.find_prefix_for_path(state.path / "drive_c" / "x.exe") == "demo"
    assert prefix.remove_prefix("demo") and not state.path.exists()
    assert not prefix.remove_prefix("demo")


# --------------------------------------------------------------------------- #
# .desktop generation + apps DB
# --------------------------------------------------------------------------- #
def test_desktop_file_generation(fake_core, home: Path):
    exe = home / ".local/share/lindos/prefixes/foo/drive_c/Program Files/Foo Corp/foo.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    path = scan.write_desktop_file("foo", name="Foo Editor", exe=exe, prefix_slug="foo", runner="wine", home=home)
    assert path == home / ".local/share/applications/lindos-foo.desktop"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("[Desktop Entry]\n")
    assert "Name=Foo Editor\n" in text
    assert f"Exec=lindos-run --prefix foo {scan.desktop_quote(str(exe))}\n" in text
    assert "Categories=Wine;X-Lindos;\n" in text
    assert "StartupWMClass=foo.exe\n" in text
    assert f"Icon={GENERIC_ICON}\n" in text
    assert "Terminal=false" in text and "Type=Application" in text
    # quoting: the path is wrapped in double quotes; a '%' must be doubled, backslashes escaped
    assert scan.desktop_quote("/a b/c%d.exe") == '"/a b/c%%d.exe"'
    assert scan.desktop_quote('a"b') == '"a\\\\"b"'
    # register + unregister through the apps DB
    app = scan.FoundApp(name="Foo Editor", exe=exe, source=str(exe))
    slug = scan.register_app(app, prefix_slug="foo", runner="wine", desktop_file=path)
    assert slug == "foo-editor"
    db = fake_core.apps_db_load()
    rec = db["foo-editor"]
    assert rec["exe"] == str(exe) and rec["prefix"] == "foo" and rec["runner"] == "wine" and rec["kind"] == "app"
    assert rec["installed_at"] and rec["name"] == "Foo Editor"
    assert scan.find_registered(exe)["slug"] == "foo-editor"
    # same exe again reuses the slug; different exe with same name gets -2
    assert scan.unique_app_slug("Foo Editor", exe, db) == "foo-editor"
    assert scan.unique_app_slug("Foo Editor", exe.with_name("other.exe"), db) == "foo-editor-2"
    assert scan.unregister_app("foo-editor", home=home)
    assert not path.exists() and "foo-editor" not in fake_core.apps_db_load()


def test_snapshot_diff_and_discover(fake_core, home: Path, build_lnk):
    pfx = home / ".local/share/lindos/prefixes/foo"
    drive_c = pfx / "drive_c"
    (drive_c / "Program Files").mkdir(parents=True)
    before = scan.snapshot(pfx, home=home)
    exe = drive_c / "Program Files" / "Foo" / "foo.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    unins = drive_c / "Program Files" / "Foo" / "unins000.exe"
    unins.write_bytes(b"MZ")
    sm = drive_c / "users" / "user" / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Foo"
    sm.mkdir(parents=True)
    (sm / "Foo Editor.lnk").write_bytes(build_lnk(local_base_path="C:\\Program Files\\Foo\\foo.exe",
                                                  working_dir="C:\\Program Files\\Foo", arguments="/gui"))
    (sm / "Uninstall Foo.lnk").write_bytes(build_lnk(local_base_path="C:\\Program Files\\Foo\\unins000.exe"))
    after = scan.snapshot(pfx, home=home)
    new = scan.diff_snapshots(before, after)
    assert exe in new and (sm / "Foo Editor.lnk") in new
    apps = scan.discover_apps(new, pfx, product_hint="Foo")
    assert len(apps) == 1
    app = apps[0]
    assert app.name == "Foo Editor" and app.exe == exe and app.args == "/gui"
    assert app.win_exe == "C:\\Program Files\\Foo\\foo.exe"
    assert app.workdir == "C:\\Program Files\\Foo"
    # no shortcuts -> falls back to the most plausible .exe, skipping uninstallers
    apps = scan.discover_apps([exe, unins], pfx, product_hint="foo")
    assert [a.exe for a in apps] == [exe]


# --------------------------------------------------------------------------- #
# lindos-run CLI
# --------------------------------------------------------------------------- #
def test_cli_flags_match_spec():
    p = cli_run.build_parser()
    ns = p.parse_args(["--runner", "umu", "--prefix", "x", "--new-prefix", "--gamemode", "--mangohud", "--info",
                       "--shared", "a.exe", "/S"])
    assert ns.runner == "umu" and ns.prefix == "x" and ns.new_prefix and ns.gamemode and ns.mangohud and ns.info
    assert ns.shared and ns.file == "a.exe" and ns.args == ["/S"]
    with pytest.raises(SystemExit) as exc:
        p.parse_args(["--runner", "vm", "a.exe"])
    assert exc.value.code == 2


def test_cli_info_prints_json(fake_core, home: Path, tmp_path: Path, capsys, monkeypatch, make_pe):
    exe = tmp_path / "npp.8.6.Installer.x64.exe"
    exe.write_bytes(make_pe())
    fake_core.PRODUCT_OVERRIDES[exe.name] = "Notepad++"
    rc = cli_run.main(["--info", str(exe)])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["kind"] == "installer" and data["arch"] == "x64" and data["product"] == "Notepad++"
    assert data["suggested_prefix"] == "notepad"
    assert data["suggested_runner"] in ("wine", "umu")
    assert data["prefix_path"].endswith("notepad")
    assert data["log"].endswith("run-notepad.log")


def test_cli_missing_file_is_usage_error(fake_core, home: Path, tmp_path: Path):
    assert cli_run.main([str(tmp_path / "nope.exe")]) == 2


def test_cli_dry_run_plan(fake_core, home: Path, tmp_path: Path, capsys, monkeypatch, fake_which, make_pe):
    w = fake_which("wine", "umu-run", "gamemoderun")
    monkeypatch.setattr(cli_run, "_which", w)
    monkeypatch.setattr(cli_run, "choose_runner", functools.partial(runner.choose_runner, which=w, bottles_installed=False))
    monkeypatch.setattr(cli_run, "build_plan", functools.partial(runner.build_plan, which=w, home=home, nvidia=False))
    exe = tmp_path / "SuperGame.exe"
    exe.write_bytes(make_pe())
    rc = cli_run.main(["--dry-run", "--mangohud", str(exe), "-windowed"])
    assert rc == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["runner"] == "umu"
    assert plan["env"]["GAMEID"] == "umu-supergame"
    assert plan["env"]["MANGOHUD"] == "1"
    assert plan["argv"][0].endswith("gamemoderun")           # gamemode_auto (default) and kind == game
    assert plan["argv"][-1] == "-windowed"
    assert plan["prefix"].endswith("supergame")
    # msi under wine
    msi = tmp_path / "Tool.msi"
    msi.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 1024)
    rc = cli_run.main(["--dry-run", str(msi)])
    plan = json.loads(capsys.readouterr().out)
    assert rc == 0 and plan["runner"] == "wine" and plan["argv"][1:3] == ["msiexec", "/i"]
    assert plan["argv"][3].endswith("Tool.msi") and "/" not in plan["argv"][3]   # a Windows path
    # .bat under wine cmd /c, forced runner
    bat = tmp_path / "run.bat"
    bat.write_text("@echo off\n")
    rc = cli_run.main(["--dry-run", "--runner", "wine", "--prefix", "shared-tools", str(bat)])
    plan = json.loads(capsys.readouterr().out)
    assert rc == 0 and plan["argv"][1:3] == ["cmd", "/c"] and plan["prefix"].endswith("shared-tools")


def test_split_windows_args():
    assert cli_run.split_windows_args('"C:\\Program Files\\x y" /flag -o "a b"') == ["C:\\Program Files\\x y", "/flag", "-o", "a b"]
    assert cli_run.split_windows_args("") == []


def test_cli_installer_flow_creates_start_menu_entry(fake_core, home: Path, tmp_path: Path, monkeypatch, fake_which,
                                                     fake_run, build_lnk, capsys, make_pe):
    """End-to-end: run an installer -> post-run scan -> .desktop + apps DB record (no Wine involved)."""
    w = fake_which("wine", "wineboot", "wineserver")
    monkeypatch.setattr(cli_run, "choose_runner", functools.partial(runner.choose_runner, which=w, bottles_installed=False))
    monkeypatch.setattr(cli_run, "build_plan", functools.partial(runner.build_plan, which=w, home=home, nvidia=False))
    monkeypatch.setattr(cli_run, "ensure_prefix", functools.partial(prefix.ensure_prefix, which=w, run=fake_run))
    monkeypatch.setattr(cli_run, "_which", w)
    installer = tmp_path / "FooSetup-x64.exe"
    installer.write_bytes(make_pe())
    pfx = home / ".local/share/lindos/prefixes/foo"

    registrations = []

    def fake_run_plan(plan, **kw):
        if plan.argv[1] == "regedit":  # the app-package hand-off associations (imported once per C:\ drive)
            registrations.append(plan)
            return 0
        # pretend the installer put files on the C:\ drive
        assert plan.env["WINEPREFIX"] == str(pfx)
        assert plan.argv[0] == "/usr/bin/wine" and plan.argv[1] == str(installer)
        drive_c = plan.prefix / "drive_c"
        exe = drive_c / "Program Files" / "Foo Corp" / "Foo.exe"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(make_pe())
        (exe.parent / "unins000.exe").write_bytes(make_pe())
        sm = drive_c / "users" / "user" / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Foo Corp"
        sm.mkdir(parents=True, exist_ok=True)
        (sm / "Foo Editor.lnk").write_bytes(build_lnk(local_base_path="C:\\Program Files\\Foo Corp\\Foo.exe",
                                                      working_dir="C:\\Program Files\\Foo Corp"))
        (sm / "Uninstall Foo.lnk").write_bytes(build_lnk(local_base_path="C:\\Program Files\\Foo Corp\\unins000.exe"))
        plan.log_path.parent.mkdir(parents=True, exist_ok=True)
        plan.log_path.write_text("installer output\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_run, "run_plan", fake_run_plan)
    rc = cli_run.main([str(installer)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "Installed: Foo Editor" in out
    # prefix was initialised with wineboot and remembered
    assert (pfx / "system.reg").exists() and prefix.read_marker(pfx)["runner"] == "wine"
    # the MSIX hand-off associations went into that C:\ drive exactly once, before the installer ran
    assert len(registrations) == 1 and registrations[0].argv[2] == "/S"
    assert prefix.read_marker(pfx)["handoff"] == 1
    # Start Menu entry
    desktop = home / ".local/share/applications/lindos-foo-editor.desktop"
    assert desktop.exists()
    text = desktop.read_text(encoding="utf-8")
    exe = pfx / "drive_c" / "Program Files" / "Foo Corp" / "Foo.exe"
    assert f"Exec=lindos-run --prefix foo {scan.desktop_quote(str(exe))}" in text
    assert "Name=Foo Editor" in text and "Categories=Wine;X-Lindos;" in text and "StartupWMClass=Foo.exe" in text
    assert f"Path={exe.parent}" in text                # Windows working dir mapped onto the C:\ drive
    assert "Icon=lindos-exe" in text                    # no icoutils here -> generic icon
    # apps DB
    db = fake_core.apps_db_load()
    rec = db["foo-editor"]
    assert rec["exe"] == str(exe) and rec["prefix"] == "foo" and rec["runner"] == "wine"
    assert rec["win_exe"] == "C:\\Program Files\\Foo Corp\\Foo.exe"
    assert "unins" not in " ".join(db)                 # uninstaller skipped
    # running the registered program later: prefix comes from the DB, args from the shortcut
    monkeypatch.setattr(cli_run, "run_plan", lambda plan, **kw: 0)
    rc = cli_run.main(["--dry-run", str(exe)])
    plan = json.loads(capsys.readouterr().out)
    assert rc == 0 and plan["prefix"] == str(pfx) and plan["runner"] == "wine"
