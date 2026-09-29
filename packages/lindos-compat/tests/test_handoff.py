"""MSIX hand-off from Windows installers (SPEC-WINDOWS §28.4a): ``handoff.py`` + its wiring in ``cli_run``.

The scenario behind it: a Windows "installer" that is really a bootstrapper downloads a big ``.msix`` into
``%TEMP%`` and asks Windows to install it.  Wine cannot, so it used to end in "There is no Windows program
configured to open this type of file" and a file manager on the temp folder.  Everything here is hermetic:
synthetic packages built by ``test_msix_builders``, no Wine, no Linux calls; ``run_plan`` is a recorder that
plays the installer (it may drop a package into the C:\\ drive's Temp folder, or record a hand-off).
"""
from __future__ import annotations

import functools
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

import pytest

from lindos_compat import cli_run, handoff, msix, prefix, runner
from lindos_compat.gui import Feedback


def _builders():
    name = "lindos_test_msix_builders"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("test_msix_builders.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return sys.modules[name]


B = _builders()
PID = msix.publisher_id(B.PUBLISHER)
FAMILY_SLUG = "contoso-photoeditor-" + PID.lower()
TEMP_REL = "drive_c/users/user/Temp"


def _desktop_pkg(dest: Path) -> Path:
    return B.write(dest, B.make_package())


def _uwp_pkg(dest: Path) -> Path:
    man = B.manifest(apps=[B.app_xml(exe_rel="Xodo.exe", attrs='EntryPoint="App.App"')],
                     families=("Windows.Universal",), caps="")
    return B.write(dest, B.make_package(files={"Xodo.exe": B.exe()}, man=man))


def _framework_pkg(dest: Path) -> Path:
    return B.write(dest, B.make_package(man=B.manifest(props_extra="<Framework>true</Framework>", apps=[])))


# --------------------------------------------------------------------------- #
# the association registered inside the C:\ drive
# --------------------------------------------------------------------------- #
def _parse_reg(text: str) -> Dict[str, Dict[str, str]]:
    """A tiny .reg reader (keys -> {value name: string}), un-escaping the way regedit does."""
    keys: Dict[str, Dict[str, str]] = {}
    current: Optional[Dict[str, str]] = None
    for line in text.splitlines():
        if line.startswith("[") and line.endswith("]"):
            current = keys.setdefault(line[1:-1], {})
        elif current is not None and (m := re.match(r'^(@|"[^"]+")="((?:[^"\\]|\\.)*)"$', line)):
            name = "" if m.group(1) == "@" else m.group(1).strip('"')
            current[name] = m.group(2).replace('\\"', '"').replace("\\\\", "\\")
    return keys


def test_every_package_type_and_the_uri_scheme_map_to_the_recorder() -> None:
    keys = _parse_reg(handoff.registration_reg())
    for suffix in (".msix", ".appx", ".msixbundle", ".appxbundle", ".msixupload", ".appxupload", ".emsix",
                   ".eappx", ".emsixbundle", ".eappxbundle", ".appinstaller"):
        assert keys[f"HKEY_CLASSES_ROOT\\{suffix}"][""] == handoff.PROG_ID, suffix
    assert set(handoff.PACKAGE_SUFFIXES) == {k.split("\\", 1)[1] for k in keys if k.split("\\", 1)[1].startswith(".")}
    cmd = handoff.handler_command()
    for verb in ("open", "runas"):  # a bootstrapper may ShellExecute with either verb
        assert keys[f"HKEY_CLASSES_ROOT\\{handoff.PROG_ID}\\shell\\{verb}\\command"][""] == cmd
    scheme = keys["HKEY_CLASSES_ROOT\\ms-appinstaller"]
    assert scheme["URL Protocol"] == "" and scheme[""] == "URL:ms-appinstaller"
    assert keys["HKEY_CLASSES_ROOT\\ms-appinstaller\\shell\\open\\command"][""] == handoff.uri_handler_command()


def test_the_handler_only_records_it_never_launches_anything() -> None:
    cmd = handoff.handler_command()
    assert cmd == 'C:\\windows\\system32\\cmd.exe /d /c echo "%1">>"C:\\ProgramData\\Lindos\\handoff.log"'
    assert cmd.count("%1") == 1 and '"%1"' in cmd            # quoted: spaces and & in a path stay harmless
    for forbidden in ("lindos-run", "xdg-open", "explorer", "start ", "winebrowser", "msix.exe"):
        assert forbidden not in cmd


def test_a_link_never_reaches_cmd_exe() -> None:
    cmd = handoff.uri_handler_command()
    assert "%" not in cmd                                   # a link may hold " or & - it is never substituted
    assert cmd == 'C:\\windows\\system32\\cmd.exe /d /c echo ms-appinstaller:>>"C:\\ProgramData\\Lindos\\handoff.log"'
    keys = _parse_reg(handoff.registration_reg())
    assert "%1" not in keys["HKEY_CLASSES_ROOT\\ms-appinstaller\\shell\\open\\command"][""]


def test_prepare_registration_writes_a_utf16_reg_and_the_queue_folder(tmp_path: Path) -> None:
    pfx = tmp_path / "prefixes" / "claude"
    reg = handoff.prepare_registration(pfx)
    raw = reg.read_bytes()
    assert raw[:2] == b"\xff\xfe"                               # UTF-16LE BOM, like every regedit export
    text = raw.decode("utf-16")
    assert text.startswith("Windows Registry Editor Version 5.00\r\n") and "\r\n" in text
    assert _parse_reg(text) == _parse_reg(handoff.registration_reg())
    assert reg.parent == pfx / ".lindos-handoff"                # outside drive_c: not part of the "C:\ drive"
    assert (pfx / "drive_c" / "ProgramData" / "Lindos").is_dir()  # the recorder appends to a file in here
    assert handoff.prepare_registration(pfx) == reg             # idempotent


def test_marker_roundtrip_and_stale_versions_register_again(tmp_path: Path) -> None:
    pfx = tmp_path / "p"
    prefix.write_marker(pfx, {"slug": "p", "runner": "wine"})
    assert not handoff.is_registered(prefix.read_marker(pfx))
    assert handoff.mark_registered(pfx)
    marker = prefix.read_marker(pfx)
    assert marker["runner"] == "wine" and marker["handoff"] == handoff.HANDOFF_VERSION
    assert handoff.is_registered(marker)
    assert not handoff.is_registered({"handoff": handoff.HANDOFF_VERSION - 1})
    assert not handoff.is_registered({"handoff": "junk"}) and not handoff.is_registered({"handoff": None})


# --------------------------------------------------------------------------- #
# the queue and the before/after look
# --------------------------------------------------------------------------- #
@pytest.fixture()
def pfx(tmp_path: Path, home: Path) -> Path:
    p = tmp_path / "prefixes" / "claude"
    (p / TEMP_REL).mkdir(parents=True)
    (p / "drive_c" / "ProgramData" / "Lindos").mkdir(parents=True)
    return p


def test_read_queue_cleans_dedupes_and_consumes(pfx: Path) -> None:
    q = handoff.queue_path(pfx)
    q.write_bytes(b'\xef\xbb\xbf"C:\\users\\user\\Temp\\A.msix"\r\n\r\n"C:\\users\\user\\Temp\\A.msix" \r\n'
                  b"ms-appinstaller:?source=https://cdn.example/app.msix\r\n")
    assert handoff.read_queue(pfx) == ["C:\\users\\user\\Temp\\A.msix",
                                       "ms-appinstaller:?source=https://cdn.example/app.msix"]
    assert not q.exists()                                       # consumed: never offered twice
    assert handoff.read_queue(pfx) == []


def test_read_queue_is_bounded_and_ignores_a_symlink(pfx: Path, tmp_path: Path) -> None:
    q = handoff.queue_path(pfx)
    q.write_text("\n".join(f"C:\\x\\{i}.msix" for i in range(500)), encoding="utf-8")
    assert len(handoff.read_queue(pfx, consume=False)) == handoff.QUEUE_MAX_ENTRIES
    q.unlink()
    secret = tmp_path / "secret.txt"
    secret.write_text("C:\\users\\user\\Temp\\A.msix\n", encoding="utf-8")
    try:
        q.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert handoff.read_queue(pfx) == []                        # a symlinked queue is never followed


def test_leftover_packages_are_the_new_ones_only(pfx: Path) -> None:
    temp = pfx / TEMP_REL
    old = _desktop_pkg(temp / "Old.msix")
    downloads = pfx / "drive_c/users/user/Downloads"
    downloads.mkdir(parents=True)
    before = handoff.snapshot_packages(pfx)
    assert str(old) in before
    fresh = _desktop_pkg(temp / "sub" / "Claude_1.0.0.0_x64.msix")
    in_downloads = _desktop_pkg(downloads / "Tool.msixbundle")
    (temp / "notes.txt").write_text("not a package")
    found = handoff.find_packages(pfx, before)
    assert {c.path for c in found} == {fresh, in_downloads}     # Old.msix was already there: not offered again
    assert all(c.source == "temp" for c in found)
    # a package that the installer rewrote counts as new
    os.utime(old, (old.stat().st_atime, old.stat().st_mtime + 100))
    assert old in {c.path for c in handoff.find_packages(pfx, before)}


def test_without_a_before_snapshot_only_recent_files_count(pfx: Path) -> None:
    temp = pfx / TEMP_REL
    stale = _desktop_pkg(temp / "Stale.msix")
    os.utime(stale, (1_000_000, 1_000_000))
    recent = _desktop_pkg(temp / "Recent.msix")
    found = handoff.find_packages(pfx, None, started=recent.stat().st_mtime - 1)
    assert [c.path for c in found] == [recent]


def test_queued_windows_paths_map_into_the_c_drive_and_dedupe_with_the_scan(pfx: Path) -> None:
    pkg = _desktop_pkg(pfx / TEMP_REL / "Claude.msix")
    handoff.queue_path(pfx).write_text('"C:\\users\\user\\Temp\\Claude.msix"\r\n', encoding="utf-8")
    found = handoff.find_packages(pfx, {})
    assert len(found) == 1 and found[0].path == pkg.resolve() and found[0].source == "handoff"


def test_queue_entries_that_leave_the_c_drive_or_are_not_packages_are_ignored(pfx: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere" / "Evil.msix"
    outside.parent.mkdir()
    outside.write_bytes(B.make_package())
    (pfx / TEMP_REL / "readme.txt").write_text("x")
    handoff.queue_path(pfx).write_text(
        "C:\\..\\..\\..\\elsewhere\\Evil.msix\r\n"                # traversal
        "C:\\users\\user\\Temp\\readme.txt\r\n"                   # not a package type
        "C:\\users\\user\\Temp\\missing.msix\r\n"                 # not there
        "\\\\server\\share\\x.msix\r\n", encoding="utf-8")        # UNC
    assert handoff.find_packages(pfx, {}) == []


def test_an_ms_appinstaller_link_is_kept_as_a_link_never_fetched(pfx: Path) -> None:
    handoff.queue_path(pfx).write_text(
        "ms-appinstaller:?source=https://downloads.contoso.example/app/Contoso.appinstaller\r\n", encoding="utf-8")
    [cand] = handoff.find_packages(pfx, {})
    assert cand.path is None
    verdict = handoff.assess(cand, msix)
    assert verdict.role == "uri" and verdict.host == "downloads.contoso.example" and not verdict.installable


def test_sniff_bootstrapper_sees_ascii_and_utf16_spellings(tmp_path: Path, make_pe) -> None:
    plain = tmp_path / "plain.exe"
    plain.write_bytes(make_pe())
    assert handoff.sniff_bootstrapper(plain) == []
    a = tmp_path / "a.exe"
    a.write_bytes(make_pe() + b"...Add-AppxPackage -Path ...")
    assert "Add-AppxPackage" in handoff.sniff_bootstrapper(a)
    w = tmp_path / "w.exe"
    w.write_bytes(make_pe() + "Claude.MSIX".encode("utf-16-le") + "ms-appinstaller:".encode("utf-16-le"))
    assert {"ms-appinstaller", ".msix"} <= set(handoff.sniff_bootstrapper(w))
    text = tmp_path / "notpe.exe"
    text.write_bytes(b"Add-AppxPackage")                         # not a PE file: no hint
    assert handoff.sniff_bootstrapper(text) == [] and handoff.sniff_bootstrapper(tmp_path / "absent.exe") == []


# --------------------------------------------------------------------------- #
# what is it, and the ONE explanation
# --------------------------------------------------------------------------- #
def _assess(path: Path) -> handoff.Verdict:
    return handoff.assess(handoff.Candidate(path=path, raw=str(path), size=path.stat().st_size), msix)


def test_assess_desktop_uwp_component_encrypted_damaged_appinstaller(tmp_path: Path) -> None:
    app = _assess(_desktop_pkg(tmp_path / "App.msix"))
    assert (app.role, app.installable, app.title, app.version) == ("app", True, "Contoso Photo Editor", "1.2.3.0")
    uwp = _assess(_uwp_pkg(tmp_path / "Uwp.msix"))
    assert uwp.role == "unsupported" and not uwp.installable and "UWP" in uwp.reason
    comp = _assess(_framework_pkg(tmp_path / "Fw.appx"))
    assert comp.role == "component" and not comp.installable
    enc = _assess(B.write(tmp_path / "Store.emsix", B.make_encrypted()))
    assert enc.role == "encrypted" and "Store" in enc.reason and not enc.installable
    data = B.make_package()
    cut = _assess(B.write(tmp_path / "Cut.msix", data[: len(data) // 2]))
    assert cut.role == "damaged" and not cut.installable
    junk = _assess(B.write(tmp_path / "Junk.msix", b"not a zip at all"))
    assert junk.role == "damaged"
    ai = _assess(B.write(tmp_path / "x.appinstaller", B.appinstaller()))
    assert ai.role == "appinstaller" and ai.installable and ai.title == B.NAME


def test_explanation_is_one_plain_message_with_ways_forward(tmp_path: Path) -> None:
    uwp = _assess(_uwp_pkg(tmp_path / "Xodo_1.0.msix"))
    text = handoff.explain_text("Xodo-Setup.exe", [uwp])
    assert text.startswith("'Xodo-Setup.exe' downloaded a Windows app package")
    assert "Xodo_1.0.msix" in text and "UWP" in text
    assert "lindos-compat winget search" in text and "lindos-vm" in text and "lindos-winapps" in text
    assert "web version" in text and "Nothing was installed or opened" in str(text)
    assert str(uwp.path) in text                                # says where the file is, never opens it
    assert "Wine" not in text.split("What you can do instead")[1]
    assert ".." not in text and "`" not in text                 # a dialog, not markdown
    link = handoff.assess(handoff.Candidate(path=None, raw="ms-appinstaller:?source=https://cdn.example/a.msix"), msix)
    only_link = handoff.explain_text("Setup.exe", [link])
    assert "cdn.example" in only_link and "2023" in only_link and "never downloads" in only_link
    many = [uwp] * 5
    listed = [ln for ln in handoff.explain_text("S.exe", many).splitlines() if ln.startswith("  - ") and "Xodo_1.0.msix" in ln]
    assert len(listed) == handoff.MAX_REPORTED
    assert "and 2 more package(s)" in handoff.explain_text("S.exe", many)


def test_damaged_download_hints_at_an_interrupted_installer(tmp_path: Path) -> None:
    v = _assess(B.write(tmp_path / "Claude.msix", B.make_package()[:100]))
    assert "incomplete or damaged" in handoff.explain_text("Claude.exe", [v])


# --------------------------------------------------------------------------- #
# lindos-run end to end: the installer is a recorder that plays the bootstrapper
# --------------------------------------------------------------------------- #
class RecFeedback(Feedback):
    """Records every dialog; ``answer`` is what a click on the question returns."""

    def __init__(self) -> None:
        super().__init__(False)
        self.events: List[tuple] = []
        self.answer: Optional[bool] = True

    def progress(self, text: str) -> None:
        self.events.append(("progress", text))

    def question(self, text: str, **kw: Any) -> Optional[bool]:
        self.events.append(("question", text))
        return self.answer

    def explain(self, text: str) -> None:
        self.events.append(("explain", text))

    def error(self, text: str) -> None:
        self.events.append(("error", text))

    def warning(self, text: str) -> None:
        self.events.append(("warning", text))

    def notify(self, summary: str, body: str = "", icon: str = "lindos-exe") -> bool:
        self.events.append(("notify", summary + " " + body))
        return True

    def texts(self, kind: str) -> List[str]:
        return [t for k, t in self.events if k == kind]


@pytest.fixture()
def rig(monkeypatch: pytest.MonkeyPatch, fake_core, fake_which, fake_run, home: Path, tmp_path: Path, make_pe):
    w = fake_which("wine", "wineboot", "wineserver", "umu-run", "xdg-open", "exo-open")
    monkeypatch.setattr(cli_run, "choose_runner", functools.partial(runner.choose_runner, which=w, bottles_installed=False))
    monkeypatch.setattr(cli_run, "build_plan", functools.partial(runner.build_plan, which=w, home=home, nvidia=False))
    monkeypatch.setattr(cli_run, "ensure_prefix", functools.partial(prefix.ensure_prefix, which=w, run=fake_run))
    monkeypatch.setattr(cli_run, "_which", w)
    fb = RecFeedback()
    monkeypatch.setattr(cli_run, "Feedback", lambda enabled: fb)
    opened: List[Any] = []
    monkeypatch.setattr(cli_run, "_open_folder", lambda folder: opened.append(folder) or True)  # must stay unused
    monkeypatch.setattr(cli_run, "_spawn", lambda argv, **kw: opened.append(argv) or True)
    monkeypatch.setattr(cli_run, "_popen", lambda *a, **kw: opened.append(a) or None)
    downloads: List[str] = []
    monkeypatch.setattr(cli_run, "download_https", lambda *a, **kw: downloads.append(str(a)) or (_ for _ in ()).throw(
        AssertionError("nothing may be downloaded")))

    state = SimpleNamespace(regs=[], installers=[], behaviour=lambda plan: None, rc=0, fb=fb, opened=opened,
                            downloads=downloads)

    def fake_run_plan(plan: Any, **kw: Any) -> int:
        if plan.argv[1] == "regedit":
            state.regs.append(plan)
            return 0
        state.installers.append(plan)
        state.behaviour(plan)
        return state.rc

    monkeypatch.setattr(cli_run, "run_plan", fake_run_plan)
    installer = tmp_path / "in" / "Claude-Setup-x64.exe"
    installer.parent.mkdir()
    installer.write_bytes(make_pe() + b"Add-AppxPackage")
    state.installer = installer
    return state


def _leaves(name: str, builder: Callable[[Path], Path]) -> Callable[[Any], None]:
    def behaviour(plan: Any) -> None:
        builder(plan.prefix / TEMP_REL / name)
    return behaviour


def _is_family_prefix_installed(home: Path) -> bool:
    return (home / ".local/share/lindos/prefixes" / FAMILY_SLUG / "drive_c/Program Files/WindowsApps").is_dir()


def test_a_desktop_package_left_by_the_installer_is_offered_and_installed(rig, home: Path, capsys) -> None:
    rig.behaviour = _leaves("Claude_1.2.3.0_x64.msix", _desktop_pkg)
    rig.rc = 1                                                   # the bootstrapper itself reported a failure
    rc = cli_run.main([str(rig.installer)])
    assert rc == cli_run.EXIT_OK
    [question] = rig.fb.texts("question")
    assert "Claude-Setup-x64.exe' downloaded this app and asked Windows to install it" in question
    assert "Install Contoso Photo Editor?" in question and "Disk space: about" in question
    assert _is_family_prefix_installed(home)
    assert "Installed: Contoso Photo Editor" in capsys.readouterr().out
    db = json.loads((home / ".local/share/lindos/apps.json").read_text(encoding="utf-8"))
    assert next(iter(db.values()))["source"] == "msix"
    assert not rig.fb.texts("error") and not rig.fb.texts("explain")   # no raw "exit code 1" dialog on top
    assert rig.opened == []                                      # no file manager, no browser, no launcher
    assert rig.downloads == []


def test_the_associations_are_imported_once_before_the_installer_and_never_again(rig, home: Path) -> None:
    rc1 = cli_run.main([str(rig.installer)])
    assert rc1 == cli_run.EXIT_OK and len(rig.regs) == 1 and len(rig.installers) == 1
    reg_plan = rig.regs[0]
    assert reg_plan.argv[1:3] == ["regedit", "/S"] and reg_plan.argv[3].endswith("handoff.reg")
    assert reg_plan.env["WINEPREFIX"] == rig.installers[0].env["WINEPREFIX"]
    assert prefix.read_marker(reg_plan.prefix)["handoff"] == handoff.HANDOFF_VERSION
    assert (reg_plan.prefix / ".lindos-handoff" / "handoff.reg").is_file()
    cli_run.main([str(rig.installer)])
    assert len(rig.regs) == 1 and len(rig.installers) == 2      # idempotent and cheap: a marker, no extra process


def test_a_failed_registration_is_not_fatal_and_is_retried_next_time(rig, home: Path, monkeypatch) -> None:
    recorder = cli_run.run_plan
    attempts = {"regedit": 0}

    def flaky(plan: Any, **kw: Any) -> int:
        if plan.argv[1] == "regedit":
            attempts["regedit"] += 1
            if attempts["regedit"] == 1:
                return 1                                            # e.g. wineserver was busy
        return recorder(plan, **kw)

    monkeypatch.setattr(cli_run, "run_plan", flaky)
    assert cli_run.main([str(rig.installer)]) == cli_run.EXIT_OK      # the installer still ran
    assert "handoff" not in prefix.read_marker(rig.installers[0].prefix)
    assert cli_run.main([str(rig.installer)]) == cli_run.EXIT_OK
    assert prefix.read_marker(rig.installers[0].prefix)["handoff"] == handoff.HANDOFF_VERSION
    assert attempts["regedit"] == 2


def test_games_and_proton_prefixes_are_left_alone(rig, home: Path, tmp_path: Path, make_pe) -> None:
    game = tmp_path / "in" / "SuperGame.exe"
    game.write_bytes(make_pe())
    assert cli_run.main([str(game)]) == cli_run.EXIT_OK          # kind "game" -> Proton (umu)
    assert rig.regs == []
    assert rig.installers and rig.installers[0].runner == "umu"


def test_the_hand_off_queue_and_the_scan_find_the_same_package_once(rig, home: Path) -> None:
    def behaviour(plan: Any) -> None:
        _desktop_pkg(plan.prefix / TEMP_REL / "Claude.msix")
        # what the registered association does when the bootstrapper ShellExecutes the package
        handoff.queue_path(plan.prefix).write_text('"C:\\users\\user\\Temp\\Claude.msix"\r\n', encoding="utf-8")

    rig.behaviour = behaviour
    assert cli_run.main([str(rig.installer)]) == cli_run.EXIT_OK
    assert len(rig.fb.texts("question")) == 1
    assert not handoff.queue_path(rig.installers[0].prefix).exists()


def test_a_uwp_only_package_gets_one_plain_explanation_and_no_file_manager(rig, home: Path, caplog) -> None:
    rig.behaviour = _leaves("Xodo_9.9.msix", _uwp_pkg)
    rig.rc = 1
    rc = cli_run.main([str(rig.installer)])
    assert rc == cli_run.EXIT_UNSUPPORTED
    [text] = rig.fb.texts("explain")
    assert "Claude-Setup-x64.exe" in text and "Xodo_9.9.msix" in text and "UWP" in text
    assert "lindos-compat winget search" in text and "lindos-vm" in text
    assert not rig.fb.texts("error") and not rig.fb.texts("question")
    assert rig.opened == [] and rig.downloads == []
    assert not _is_family_prefix_installed(home)


def test_a_store_encrypted_or_broken_download_is_explained_not_tried(rig, home: Path) -> None:
    rig.behaviour = lambda plan: B.write(plan.prefix / TEMP_REL / "Store.emsix", B.make_encrypted())
    assert cli_run.main([str(rig.installer)]) == cli_run.EXIT_UNSUPPORTED
    [text] = rig.fb.texts("explain")
    assert "encrypted Microsoft Store package" in text
    rig.fb.events.clear()
    rig.behaviour = lambda plan: B.write(plan.prefix / TEMP_REL / "Half.msix", B.make_package()[:200])
    assert cli_run.main([str(rig.installer)]) == cli_run.EXIT_UNSUPPORTED
    assert "incomplete or damaged" in rig.fb.texts("explain")[0]
    assert rig.opened == []


def test_a_package_that_was_already_there_is_not_offered(rig, home: Path) -> None:
    assert cli_run.main([str(rig.installer)]) == cli_run.EXIT_OK      # creates the installer's C:\ drive
    _desktop_pkg(rig.installers[0].prefix / TEMP_REL / "Old.msix")   # a leftover from an earlier session
    rig.fb.events.clear()
    assert cli_run.main([str(rig.installer)]) == cli_run.EXIT_OK
    assert not rig.fb.texts("question") and not rig.fb.texts("explain")


def test_only_components_are_not_worth_a_message_and_the_normal_error_stays(rig, home: Path) -> None:
    rig.behaviour = _leaves("Microsoft.VCLibs.appx", _framework_pkg)
    rig.rc = 3
    assert cli_run.main([str(rig.installer)]) == cli_run.EXIT_ERROR
    assert not rig.fb.texts("explain") and rig.fb.texts("error")   # the usual "ended with exit code 3" dialog
    rig.fb.events.clear()
    rig.rc = 0
    assert cli_run.main([str(rig.installer)]) == cli_run.EXIT_OK


def test_without_a_yes_or_a_dialog_nothing_is_installed(rig, home: Path) -> None:
    rig.fb.answer = None                                          # no dialog and no terminal to ask on
    rig.behaviour = _leaves("Claude.msix", _desktop_pkg)
    assert cli_run.main([str(rig.installer)]) == cli_run.EXIT_USAGE
    assert not _is_family_prefix_installed(home)


def test_saying_no_installs_nothing_and_keeps_the_file(rig, home: Path) -> None:
    rig.fb.answer = False
    rig.behaviour = _leaves("Claude.msix", _desktop_pkg)
    assert cli_run.main([str(rig.installer)]) == cli_run.EXIT_ERROR
    assert not _is_family_prefix_installed(home)
    assert list(rig.installers[0].prefix.joinpath(TEMP_REL).glob("Claude.msix"))


def test_yes_installs_without_asking(rig, home: Path) -> None:
    rig.behaviour = _leaves("Claude.msix", _desktop_pkg)
    assert cli_run.main(["--yes", str(rig.installer)]) == cli_run.EXIT_OK
    assert rig.fb.texts("question") == [] and _is_family_prefix_installed(home)


def test_new_prefix_never_moves_aside_the_drive_that_holds_the_package(rig, home: Path) -> None:
    rig.behaviour = _leaves("Claude.msix", _desktop_pkg)
    rc = cli_run.main(["--yes", "--new-prefix", "--prefix", "claude", str(rig.installer)])
    assert rc == cli_run.EXIT_OK
    prefixes = home / ".local/share/lindos/prefixes"
    assert not [p for p in prefixes.iterdir() if "-old-" in p.name]
    assert (prefixes / "claude" / TEMP_REL / "Claude.msix").is_file()   # still there: nothing was moved away
    assert (prefixes / "claude/drive_c/Program Files/WindowsApps").is_dir()


def test_an_ms_appinstaller_link_is_explained_and_never_followed(rig, home: Path) -> None:
    rig.behaviour = lambda plan: handoff.queue_path(plan.prefix).write_text(
        "ms-appinstaller:?source=https://downloads.contoso.example/x.appinstaller\r\n", encoding="utf-8")
    assert cli_run.main([str(rig.installer)]) == cli_run.EXIT_UNSUPPORTED
    [text] = rig.fb.texts("explain")
    assert "downloads.contoso.example" in text and "ms-appinstaller" in text
    assert rig.downloads == [] and rig.opened == []


def test_a_bootstrapper_looking_installer_is_mentioned_in_the_log(rig, home: Path, caplog) -> None:
    import logging
    logger = logging.getLogger("lindos-run")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.INFO, logger="lindos-run"):
            cli_run.main([str(rig.installer)])
    finally:
        logger.removeHandler(caplog.handler)
    assert "may download a Windows app package" in caplog.text
    assert "Add-AppxPackage" in caplog.text


def test_info_reports_bootstrapper_hints(rig, home: Path, capsys) -> None:
    assert cli_run.main(["--info", str(rig.installer)]) == cli_run.EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["msix_handoff"]["hints"] == ["Add-AppxPackage"] and data["msix_handoff"]["prefix_registered"] is False


# --------------------------------------------------------------------------- #
# big packages: streaming, a friendly space check before anyone is asked, tidy leftovers
# --------------------------------------------------------------------------- #
def _big_pkg(dest: Path, megabytes: int = 12) -> Path:
    files = dict(B.desktop_files())
    files["VFS/ProgramFilesX64/Contoso App/data.bin"] = os.urandom(1 << 20) * megabytes   # deflate barely helps
    return B.write(dest, B.make_package(files))


def test_unpacked_size_is_reported_and_progress_streams_in_chunks(tmp_path: Path) -> None:
    pkg = B.write(tmp_path / "a.msix", B.make_package())
    info = msix.inspect(pkg)
    assert info.unpacked_bytes == sum(len(v) for v in B.desktop_files().values()) + len(B.manifest())
    assert json.loads(json.dumps(info.as_dict()))["unpacked_bytes"] == info.unpacked_bytes
    big = _big_pkg(tmp_path / "big.msix")
    calls: List[tuple] = []
    inst = msix.install(big, tmp_path / "pfx", on_progress=lambda d, t, n: calls.append((d, t)))
    assert len(calls) >= 12 and calls[-1][0] == calls[-1][1] == msix.inspect(big).unpacked_bytes
    assert all(a[0] <= b[0] for a, b in zip(calls, calls[1:]))     # monotonic
    assert (inst.install_dir / "VFS/ProgramFilesX64/Contoso App/data.bin").stat().st_size == 12 << 20


def test_the_progress_dialog_is_updated_once_per_percent_not_once_per_megabyte() -> None:
    seen: List[str] = []
    cb = cli_run._progress_cb(SimpleNamespace(progress=seen.append), "Claude")
    for done in range(0, 600 * (1 << 20) + 1, 1 << 20):            # a 600 MB package, reported per MB
        cb(done, 600 * (1 << 20), "file")
    assert 90 <= len(seen) <= 101 and seen[-1].endswith("100%") and len(set(seen)) == len(seen)


def test_check_space_refuses_early_in_plain_words(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    info = msix.inspect(_desktop_pkg(tmp_path / "a.msix"))
    monkeypatch.setattr(msix, "_free_bytes", lambda path: 1024)
    with pytest.raises(msix.MsixError) as exc:
        msix.check_space(info, tmp_path / "not" / "created" / "yet")   # walks up to the first folder that exists
    message = str(exc.value)
    assert "Not enough free disk space" in message and "needs about" in message and "Trash" in message
    monkeypatch.setattr(msix, "_free_bytes", lambda path: 1 << 40)
    msix.check_space(info, tmp_path / "pfx")
    info.unpacked_bytes = 0
    monkeypatch.setattr(msix, "_free_bytes", lambda path: 1)
    msix.check_space(info, tmp_path / "pfx")                          # unknown size: nothing to refuse
    monkeypatch.setattr(msix, "_free_bytes", lambda path: None)
    info.unpacked_bytes = 10 << 30
    msix.check_space(info, tmp_path / "pfx")                          # unreadable disk: install() decides


def test_lindos_run_refuses_a_too_big_package_before_asking(rig, home: Path, tmp_path: Path, monkeypatch) -> None:
    pkg = _desktop_pkg(tmp_path / "in" / "Big.msix")
    monkeypatch.setattr(msix, "_free_bytes", lambda path: 4096)
    installed: List[Any] = []
    monkeypatch.setattr(msix, "install", lambda *a, **kw: installed.append(a))
    rc = cli_run.main([str(pkg)])
    assert rc == cli_run.EXIT_ERROR
    assert installed == [] and rig.fb.texts("question") == []      # refused before the question, not after a long unpack
    [err] = rig.fb.texts("error")
    assert "Not enough free disk space" in err and "Contoso Photo Editor" in err
    assert not _is_family_prefix_installed(home)


def test_the_question_names_the_unpacked_size(rig, home: Path, tmp_path: Path) -> None:
    pkg = _desktop_pkg(tmp_path / "in" / "Photo.msix")
    assert cli_run.main([str(pkg)]) == cli_run.EXIT_OK
    [question] = rig.fb.texts("question")
    assert re.search(r"Disk space: about \d+(\.\d)? (bytes|KB|MB|GB) once unpacked", question)


def test_stale_half_unpacked_folders_are_swept_recent_ones_are_kept(tmp_path: Path) -> None:
    pfx_dir = tmp_path / "pfx"
    wa = pfx_dir / "drive_c" / "Program Files" / "WindowsApps"
    stale, fresh, other = wa / ".lindos-msix-dead", wa / ".lindos-msix-live", wa / "Keep.Me_1.0_x64__abc"
    for d in (stale, fresh, other):
        (d / "sub").mkdir(parents=True)
        (d / "sub" / "big.bin").write_bytes(b"x" * 10)
    old = 1_000_000_000
    os.utime(stale, (old, old))
    msix.install(_desktop_pkg(tmp_path / "a.msix"), pfx_dir)
    assert not stale.exists()                                       # a killed install's leftovers do not pile up
    assert fresh.exists() and other.exists()                        # a running install / other apps are untouched
