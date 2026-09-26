"""Integration tests: ``lindos-run`` dispatching every Windows format handler (SPEC-WINDOWS
§28.1-§28.9): the wiring in ``cli_run.py`` between ``formats.py``/``dos.py``/``diskimage.py``/
``msix.py`` and the runner/prefix/scan machinery.

``formats``, ``dos``, ``diskimage`` and ``msix`` are owned by other engineers and are being
written concurrently, so every test here drives ``cli_run`` against small **fakes** of their
documented SPEC-WINDOWS APIs (installed into ``sys.modules`` for the duration of the test) —
never their real, changing implementations. This keeps these tests focused on *this* file's
job (dispatch, confirmation, prefix choice, the C:\\ drive a program's own tools run in,
Start-Menu registration, exit codes) and immune to unrelated changes in those modules.

Real Wine/DOSBox/cabextract/udisks2 are never invoked: ``cli_run.run_plan`` / ``run_host`` are
replaced with small recorders, and host tools are looked up through ``fake_which`` so nothing
here depends on what is actually installed on the machine running the tests.
"""
from __future__ import annotations

import functools
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from lindos_compat import cli_run, prefix, runner


# --------------------------------------------------------------------------- #
# fake formats.py / dos.py / diskimage.py / msix.py builders
# --------------------------------------------------------------------------- #
def _fmt(fid: str, *, label: str = "Windows file", status: str = "works", handler: str = "run",
        note: str = "") -> SimpleNamespace:
    return SimpleNamespace(id=fid, label=label, suffixes=(), mime="", handler=handler, status=status, note=note)


def _det(fid: str, *, reason: str = "detected for test", **fmt_kw) -> SimpleNamespace:
    return SimpleNamespace(format=_fmt(fid, handler=fmt_kw.pop("handler", "run"), **fmt_kw), reason=reason,
                           details={})


def _plan(handler: str, *, wine_tail: Tuple[str, ...] = (), host_argv: Tuple[str, ...] = (),
          needs_prefix: bool = False, force_runner: Optional[str] = None, arch: Optional[str] = None,
          prefix_hint: Optional[str] = None, confirm: Optional[str] = None, message: str = "",
          exit_code: int = 0, details: Optional[Dict[str, object]] = None) -> SimpleNamespace:
    return SimpleNamespace(handler=handler, wine_tail=list(wine_tail), host_argv=list(host_argv),
                           needs_prefix=needs_prefix, force_runner=force_runner, arch=arch,
                           prefix_hint=prefix_hint, confirm=confirm, message=message, exit_code=exit_code,
                           details=details or {})


def install_fake_module(monkeypatch: pytest.MonkeyPatch, name: str, **attrs: object) -> types.ModuleType:
    mod = types.ModuleType(f"lindos_compat.{name}")
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, f"lindos_compat.{name}", mod)
    return mod


def install_fake_formats(monkeypatch: pytest.MonkeyPatch, *, det: SimpleNamespace, plan: SimpleNamespace,
                         **extra: object) -> types.ModuleType:
    """A ``formats`` fake that always returns ``det``/``plan`` for whatever file is opened."""

    def detect(path: Path, *, head: Optional[bytes] = None) -> SimpleNamespace:
        return det

    def plan_action(path: Path, d: SimpleNamespace, args: Any = (), **kw: object) -> SimpleNamespace:
        return plan

    kwargs: Dict[str, object] = {"detect": detect, "plan_action": plan_action}
    kwargs.update(extra)
    return install_fake_module(monkeypatch, "formats", **kwargs)


class _RecordingExec:
    """Stands in for ``cli_run.run_plan`` / ``cli_run.run_host``: records the call, returns ``rc``."""

    def __init__(self, rc: int = 0):
        self.rc = rc
        self.calls: List[Any] = []

    def as_run_plan(self, plan: Any, **kw: object) -> int:
        self.calls.append(plan)
        return self.rc

    def as_run_host(self, argv: Any, **kw: object) -> int:
        self.calls.append((list(argv), kw))
        return self.rc


@pytest.fixture()
def wired(monkeypatch: pytest.MonkeyPatch, fake_which, home: Path):
    """Wires ``choose_runner``/``build_plan`` to a fake ``which`` (wine+umu "installed") and
    replaces ``run_plan``/``run_host`` with recorders, the way real Wine/DOSBox never run here."""
    w = fake_which("wine", "wineboot", "umu-run", "xdg-open", "cabextract", "udisksctl", "pwsh")
    monkeypatch.setattr(cli_run, "choose_runner",
                        functools.partial(runner.choose_runner, which=w, bottles_installed=False))
    monkeypatch.setattr(cli_run, "build_plan", functools.partial(runner.build_plan, which=w, home=home, nvidia=False))
    monkeypatch.setattr(cli_run, "_which", w)  # the handlers' own host-tool lookups (xdg-open, pwsh, cabextract, ...)
    exec_plan = _RecordingExec()
    exec_host = _RecordingExec()
    monkeypatch.setattr(cli_run, "run_plan", exec_plan.as_run_plan)
    monkeypatch.setattr(cli_run, "run_host", exec_host.as_run_host)
    return SimpleNamespace(which=w, run_plan=exec_plan, run_host=exec_host)


# --------------------------------------------------------------------------- #
# .msp: msiexec /p ... REINSTALL=ALL REINSTALLMODE=omus, in the *product's* C:\ drive,
# never the old "/i" (SPEC-WINDOWS §28.3 - the bug this addendum fixes).
# --------------------------------------------------------------------------- #
def test_msp_patches_the_products_own_prefix_with_p_not_i(fake_core, home, wired, tmp_path, capsys, monkeypatch):
    msp = tmp_path / "Update.msp"
    msp.write_bytes(b"\xd0\xcf\x11\xe0")
    win = "C:\\dummy.msp"
    plan = _plan("msiexec-patch", wine_tail=["msiexec", "/p", win, "REINSTALL=ALL", "REINSTALLMODE=omus"],
                force_runner="wine")
    install_fake_formats(monkeypatch, det=_det("msp", handler="msiexec-patch"), plan=plan)
    rc = cli_run.main(["--prefix", "product-x", str(msp)])
    assert rc == cli_run.EXIT_OK
    assert len(wired.run_plan.calls) == 1
    executed = wired.run_plan.calls[0]
    assert executed.argv[-4:] == ["msiexec", "/p", win, "REINSTALL=ALL"] or "REINSTALLMODE=omus" in executed.argv
    assert "/p" in executed.argv and "/i" not in executed.argv
    assert executed.env["WINEPREFIX"] == str(prefix.prefix_path("product-x"))  # the product's own C:\ drive


# --------------------------------------------------------------------------- #
# .reg: a Windows-style confirmation that always lists deletions, then "wine regedit /S".
# --------------------------------------------------------------------------- #
def test_reg_merge_lists_deletions_and_needs_confirmation(fake_core, home, wired, tmp_path, monkeypatch, caplog):
    import logging

    reg = tmp_path / "settings.reg"
    reg.write_text("Windows Registry Editor Version 5.00\n", encoding="utf-8")
    win = "C:\\dummy.reg"
    plan = _plan("regedit", wine_tail=["regedit", "/S", win], force_runner="wine")
    install_fake_formats(monkeypatch, det=_det("reg", handler="regedit"), plan=plan,
                         reg_preview=lambda p: {"deletions": ["[HKEY_CURRENT_USER\\Software\\Foo] "
                                                              "(this key and everything in it)"]})
    # without --yes and no tty/GUI: refused (usage error), asking the user to confirm somehow
    rc = cli_run.main(["--prefix", "notepad", str(reg)])
    assert rc == cli_run.EXIT_USAGE
    assert wired.run_plan.calls == []

    # --yes: merges into the chosen C:\ drive; the log shows what would be deleted
    logger = logging.getLogger("lindos-run")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.INFO, logger="lindos-run"):
            rc = cli_run.main(["--prefix", "notepad", "--yes", str(reg)])
    finally:
        logger.removeHandler(caplog.handler)
    assert rc == cli_run.EXIT_OK
    assert len(wired.run_plan.calls) == 1
    executed = wired.run_plan.calls[0]
    assert executed.argv[-3:] == ["regedit", "/S", win]
    assert executed.env["WINEPREFIX"] == str(prefix.prefix_path("notepad"))


# --------------------------------------------------------------------------- #
# .url: only http/https/mailto/ftp are opened (xdg-open); everything else is explained.
# --------------------------------------------------------------------------- #
def test_url_allowed_scheme_opens_with_xdg_open(fake_core, home, wired, tmp_path, monkeypatch):
    spawned: List[List[str]] = []
    monkeypatch.setattr(cli_run, "_spawn", lambda argv, cwd=None: spawned.append([str(a) for a in argv]) or True)
    f = tmp_path / "link.url"
    f.write_text("[InternetShortcut]\nURL=https://example.com/download\n", encoding="utf-8")
    plan = _plan("open-url", host_argv=["xdg-open", "https://example.com/download"])
    install_fake_formats(monkeypatch, det=_det("url", handler="open-url"), plan=plan)
    rc = cli_run.main([str(f)])
    assert rc == cli_run.EXIT_OK
    assert spawned == [[wired.which("xdg-open"), "https://example.com/download"]]


def test_url_disallowed_scheme_is_explained_not_opened(fake_core, home, wired, tmp_path, monkeypatch, caplog):
    import logging

    f = tmp_path / "link.url"
    f.write_text("[InternetShortcut]\nURL=file:///etc/passwd\n", encoding="utf-8")
    plan = _plan("open-url", host_argv=[])
    install_fake_formats(monkeypatch, det=_det("url", handler="open-url"), plan=plan,
                         parse_url_shortcut=lambda p: ("file:///etc/passwd", False, "local file"))
    logger = logging.getLogger("lindos-run")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="lindos-run"):
            rc = cli_run.main([str(f)])
    finally:
        logger.removeHandler(caplog.handler)
    assert rc == cli_run.EXIT_UNSUPPORTED
    assert "file:" in caplog.text and "not opened" in caplog.text


# --------------------------------------------------------------------------- #
# .ps1: pwsh -NoProfile -File, only after a confirmation (Linux pwsh has no execution policy).
# --------------------------------------------------------------------------- #
def test_ps1_needs_confirmation_then_runs_pwsh(fake_core, home, wired, tmp_path, monkeypatch):
    f = tmp_path / "script.ps1"
    f.write_text("Write-Host hi\n", encoding="utf-8")
    plan = _plan("pwsh", host_argv=[])
    install_fake_formats(monkeypatch, det=_det("ps1", handler="pwsh"), plan=plan)
    rc = cli_run.main([str(f)])  # no --yes, no tty -> refused, asks to add --yes
    assert rc == cli_run.EXIT_USAGE
    assert wired.run_host.calls == []
    rc = cli_run.main(["--yes", str(f)])
    assert rc == cli_run.EXIT_OK
    assert len(wired.run_host.calls) == 1
    argv, kw = wired.run_host.calls[0]
    assert argv[0] == wired.which("pwsh") and argv[1:4] == ["-NoProfile", "-File", str(f)]


# --------------------------------------------------------------------------- #
# .scr / .cpl: "wine <file> /s" and "wine control <file>".
# --------------------------------------------------------------------------- #
def test_screensaver_runs_with_slash_s(fake_core, home, wired, tmp_path, monkeypatch):
    f = tmp_path / "Bubbles.scr"
    f.write_bytes(b"MZ")
    plan = _plan("screensaver", wine_tail=["C:\\dummy.scr", "/s"], force_runner="wine")
    install_fake_formats(monkeypatch, det=_det("scr", handler="screensaver"), plan=plan)
    rc = cli_run.main(["--shared", str(f)])
    assert rc == cli_run.EXIT_OK
    executed = wired.run_plan.calls[0]
    assert executed.argv[-2:] == ["C:\\dummy.scr", "/s"]


def test_control_panel_item_runs_via_control(fake_core, home, wired, tmp_path, monkeypatch):
    f = tmp_path / "Item.cpl"
    f.write_bytes(b"MZ")
    plan = _plan("control-panel", wine_tail=["control", "C:\\dummy.cpl"], force_runner="wine")
    install_fake_formats(monkeypatch, det=_det("cpl", handler="control-panel"), plan=plan)
    rc = cli_run.main(["--shared", str(f)])
    assert rc == cli_run.EXIT_OK
    executed = wired.run_plan.calls[0]
    assert executed.argv[-2:] == ["control", "C:\\dummy.cpl"]


# --------------------------------------------------------------------------- #
# .inf: a software INF installs; a driver INF is explained (Linux has its own drivers);
# an over-long path is refused (Wine's InstallHinfSection copies it into a MAX_PATH buffer).
# --------------------------------------------------------------------------- #
def test_inf_software_installs(fake_core, home, wired, tmp_path, monkeypatch):
    f = tmp_path / "install.inf"
    f.write_text("[DefaultInstall]\n", encoding="utf-8")
    tail = ["rundll32", "setupapi.dll,InstallHinfSection", "DefaultInstall", "132", "C:\\dummy.inf"]
    install_fake_formats(monkeypatch, det=_det("inf", handler="inf-install"),
                         plan=_plan("inf-install", wine_tail=tail, force_runner="wine"),
                         inf_kind=lambda p: "software")
    rc = cli_run.main(["--shared", str(f)])
    assert rc == cli_run.EXIT_OK
    assert wired.run_plan.calls[0].argv[-5:] == tail


def test_inf_driver_is_explained_not_installed(fake_core, home, wired, tmp_path, monkeypatch, caplog):
    import logging

    f = tmp_path / "driver.inf"
    f.write_text("[Manufacturer]\n", encoding="utf-8")
    install_fake_formats(monkeypatch, det=_det("inf", handler="inf-install"),
                         plan=_plan("inf-install", wine_tail=["rundll32", "x", "C:\\driver.inf"],
                                   force_runner="wine"),
                         inf_kind=lambda p: "driver")
    logger = logging.getLogger("lindos-run")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="lindos-run"):
            rc = cli_run.main(["--shared", str(f)])
    finally:
        logger.removeHandler(caplog.handler)
    assert rc == cli_run.EXIT_UNSUPPORTED
    assert "lindos-drivers detect" in caplog.text
    assert wired.run_plan.calls == []


def test_inf_path_too_long_is_refused(fake_core, home, wired, tmp_path, monkeypatch):
    f = tmp_path / "install.inf"
    f.write_text("[DefaultInstall]\n", encoding="utf-8")
    long_win_path = "C:\\" + ("a" * 260) + ".inf"
    install_fake_formats(monkeypatch, det=_det("inf", handler="inf-install"),
                         plan=_plan("inf-install", wine_tail=["rundll32", "x", long_win_path], force_runner="wine"),
                         inf_kind=lambda p: "software")
    rc = cli_run.main(["--shared", str(f)])
    assert rc == cli_run.EXIT_UNSUPPORTED
    assert wired.run_plan.calls == []


# --------------------------------------------------------------------------- #
# .cab: cabextract into a new folder (zip-slip safe: unsafe members refuse extraction).
# --------------------------------------------------------------------------- #
def test_cab_extracts_into_a_new_folder(fake_core, home, wired, tmp_path, monkeypatch):
    f = tmp_path / "driver-pack.cab"
    f.write_bytes(b"MSCF")
    monkeypatch.setattr(cli_run, "_run",
                        lambda argv, **kw: SimpleNamespace(returncode=0,
                                                           stdout="  1234|2020-01-01|readme.txt\n"
                                                                  "  5678|2020-01-01|sub\\data.bin\n",
                                                           stderr=""))
    install_fake_formats(monkeypatch, det=_det("cab", handler="extract"), plan=_plan("extract"))
    rc = cli_run.main([str(f)])
    assert rc == cli_run.EXIT_OK
    assert len(wired.run_host.calls) == 1
    argv, _kw = wired.run_host.calls[0]
    assert argv[0] == wired.which("cabextract") and argv[1] == "-d"
    dest = Path(argv[2])
    assert dest.is_dir() and dest.parent == tmp_path


def test_cab_refuses_zip_slip_members(fake_core, home, wired, tmp_path, monkeypatch):
    f = tmp_path / "evil.cab"
    f.write_bytes(b"MSCF")
    monkeypatch.setattr(cli_run, "_run",
                        lambda argv, **kw: SimpleNamespace(returncode=0,
                                                           stdout="  10|2020-01-01|..\\..\\evil.txt\n", stderr=""))
    install_fake_formats(monkeypatch, det=_det("cab", handler="extract"), plan=_plan("extract"))
    rc = cli_run.main([str(f)])
    assert rc == cli_run.EXIT_ERROR
    assert wired.run_host.calls == []


# --------------------------------------------------------------------------- #
# .iso/.img: read-only loop mount, AutoPlay-style "Run setup?" - never automatic.
# --------------------------------------------------------------------------- #
def _install_fake_diskimage(monkeypatch, *, mount: Path, setup: Optional[Path], autorun: Optional[dict] = None):
    return install_fake_module(
        monkeypatch, "diskimage",
        loop_setup=lambda image, which=None: "/dev/loop7",
        mount_loop=lambda device, run=None: mount,
        find_autorun=lambda m: autorun or {},
        find_setup=lambda m: setup,
        detach_command=lambda device: [["udisksctl", "unmount", "-b", device],
                                       ["udisksctl", "loop-delete", "-b", device]],
    )


def test_iso_mounts_and_never_auto_runs_setup_without_confirmation(fake_core, home, wired, tmp_path, monkeypatch,
                                                                   capsys):
    iso = tmp_path / "game.iso"
    iso.write_bytes(b"\x00" * 32800 + b"CD001")
    mount = tmp_path / "mnt"
    mount.mkdir()
    setup = mount / "setup.exe"
    setup.write_bytes(b"MZ")

    def detect(path: Path, *, head=None):
        return _det("iso", handler="mount") if path == iso else _det("exe", handler="run")

    def plan_action(path, det, args=(), **kw):
        return _plan("mount", host_argv=["udisksctl", "loop-setup", "-r", "-f", str(iso)]) \
            if det.format.handler == "mount" else _plan("run", wine_tail=[str(path)], force_runner="wine")

    install_fake_module(monkeypatch, "formats", detect=detect, plan_action=plan_action)
    _install_fake_diskimage(monkeypatch, mount=mount, setup=setup)
    rc = cli_run.main([str(iso)])  # no --yes: the default must never auto-run
    assert rc == cli_run.EXIT_OK
    assert wired.run_plan.calls == []  # setup.exe was never started
    out = capsys.readouterr().out
    assert "is open (read-only)" in out and "To eject it later" in out


def test_iso_setup_runs_only_after_yes(fake_core, home, wired, tmp_path, monkeypatch):
    iso = tmp_path / "game.iso"
    iso.write_bytes(b"\x00" * 32800 + b"CD001")
    mount = tmp_path / "mnt"
    mount.mkdir()
    setup = mount / "setup.exe"
    setup.write_bytes(b"MZ")

    def detect(path: Path, *, head=None):
        return _det("iso", handler="mount") if path == iso else _det("exe", handler="run")

    def plan_action(path, det, args=(), **kw):
        return _plan("mount", host_argv=["udisksctl", "loop-setup", "-r", "-f", str(iso)]) \
            if det.format.handler == "mount" else _plan("run", wine_tail=[str(path)], force_runner="wine")

    install_fake_module(monkeypatch, "formats", detect=detect, plan_action=plan_action)
    _install_fake_diskimage(monkeypatch, mount=mount, setup=setup)
    rc = cli_run.main(["--yes", str(iso)])
    assert rc == cli_run.EXIT_OK
    assert len(wired.run_plan.calls) == 1  # setup.exe *was* started, through the normal "run" flow
    assert wired.run_plan.calls[0].exe == setup


def test_iso_without_setup_just_opens(fake_core, home, wired, tmp_path, monkeypatch, capsys):
    iso = tmp_path / "data.iso"
    iso.write_bytes(b"\x00" * 32800 + b"CD001")
    mount = tmp_path / "mnt"
    mount.mkdir()
    install_fake_formats(monkeypatch, det=_det("iso", handler="mount"),
                         plan=_plan("mount", host_argv=["udisksctl", "loop-setup", "-r", "-f", str(iso)]))
    _install_fake_diskimage(monkeypatch, mount=mount, setup=None)
    rc = cli_run.main([str(iso)])
    assert rc == cli_run.EXIT_OK
    assert wired.run_plan.calls == []


# --------------------------------------------------------------------------- #
# DOS: DOSBox argv straight from the plan, no C:\ drive at all.
# --------------------------------------------------------------------------- #
def test_dos_program_runs_via_dosbox_with_no_prefix(fake_core, home, wired, tmp_path, monkeypatch):
    f = tmp_path / "GAME.EXE"
    f.write_bytes(b"MZ")
    argv = ["dosbox-x", "-fastlaunch", "-nopromptfolder", "-exit", str(f)]
    install_fake_formats(monkeypatch, det=_det("dos-exe", handler="dos"), plan=_plan("dos", host_argv=argv))
    rc = cli_run.main([str(f)])
    assert rc == cli_run.EXIT_OK
    assert len(wired.run_host.calls) == 1
    got_argv, kw = wired.run_host.calls[0]
    assert got_argv == argv
    # DOS never gets its own C:\ drive (no prefixes directory created for it)
    assert not (home / ".local/share/lindos/prefixes").is_dir() \
        or list((home / ".local/share/lindos/prefixes").iterdir()) == []


# --------------------------------------------------------------------------- #
# Win16: mode-aware C:\ drive choice (SPEC-WINDOWS §28.5), decided from the plan's details
# (as formats.plan_action would set them after probing Wine's WoW64 mode).
# --------------------------------------------------------------------------- #
def test_win16_old_wow64_gets_a_dedicated_prefix(fake_core, home, wired, tmp_path, monkeypatch):
    f = tmp_path / "PROGRAM.EXE"
    f.write_bytes(b"MZ")
    install_fake_formats(monkeypatch, det=_det("win16-exe", handler="win16"),
                         plan=_plan("win16", wine_tail=[str(f)], force_runner="wine", prefix_hint="win16",
                                   details={"wine_mode": "old-wow64"}))
    rc = cli_run.main([str(f)])
    assert rc == cli_run.EXIT_OK
    executed = wired.run_plan.calls[0]
    assert executed.slug == "win16"
    assert executed.env.get("WINEARCH") == "win32"


def test_win16_new_wow64_without_16bit_support_is_explained(fake_core, home, wired, tmp_path, monkeypatch, caplog):
    import logging

    f = tmp_path / "PROGRAM.EXE"
    f.write_bytes(b"MZ")
    install_fake_formats(monkeypatch, det=_det("win16-exe", handler="win16"),
                         plan=_plan("win16", wine_tail=[str(f)], force_runner="wine",
                                   details={"wine_mode": "new-wow64-no16bit"}))
    logger = logging.getLogger("lindos-run")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="lindos-run"):
            rc = cli_run.main([str(f)])
    finally:
        logger.removeHandler(caplog.handler)
    assert rc == cli_run.EXIT_UNSUPPORTED
    assert "16-bit" in caplog.text and "WineHQ" in caplog.text
    assert wired.run_plan.calls == []


# --------------------------------------------------------------------------- #
# ClickOnce: only when the C:\ drive already has .NET Framework (never auto-installed:
# its licence is tied to a licensed Windows).
# --------------------------------------------------------------------------- #
def test_clickonce_without_dotnet_framework_is_explained(fake_core, home, wired, tmp_path, monkeypatch, caplog):
    import logging

    f = tmp_path / "App.application"
    f.write_text("<application/>", encoding="utf-8")
    install_fake_formats(monkeypatch, det=_det("clickonce", handler="clickonce"),
                         plan=_plan("clickonce", wine_tail=["rundll32", "dfshim.dll,ShOpenVerbApplication", "url"],
                                   force_runner="wine"),
                         prefix_has_dotnet_framework=lambda pfx: False)
    logger = logging.getLogger("lindos-run")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="lindos-run"):
            rc = cli_run.main(["--shared", str(f)])
    finally:
        logger.removeHandler(caplog.handler)
    assert rc == cli_run.EXIT_UNSUPPORTED
    assert ".NET Framework" in caplog.text and "licence" in caplog.text
    assert wired.run_plan.calls == []


def test_clickonce_with_dotnet_framework_runs(fake_core, home, wired, tmp_path, monkeypatch):
    f = tmp_path / "App.application"
    f.write_text("<application/>", encoding="utf-8")
    tail = ["rundll32", "dfshim.dll,ShOpenVerbApplication", "C:\\App.application"]
    install_fake_formats(monkeypatch, det=_det("clickonce", handler="clickonce"),
                         plan=_plan("clickonce", wine_tail=tail, force_runner="wine"),
                         prefix_has_dotnet_framework=lambda pfx: True)
    rc = cli_run.main(["--shared", str(f)])
    assert rc == cli_run.EXIT_OK
    assert wired.run_plan.calls[0].argv[-3:] == tail


# --------------------------------------------------------------------------- #
# "explain" handlers never try anyway: exit code 3, nothing runs.
# --------------------------------------------------------------------------- #
def test_explain_handler_is_exit_unsupported_and_never_runs_anything(fake_core, home, wired, tmp_path, monkeypatch,
                                                                      caplog):
    import logging

    f = tmp_path / "driver.sys"
    f.write_bytes(b"MZ")
    install_fake_formats(monkeypatch, det=_det("dll", handler="explain", status="unsupported",
                                               note="a library/driver, not a program"),
                         plan=_plan("explain", message="a library/driver, not a program",
                                   exit_code=cli_run.EXIT_UNSUPPORTED))
    logger = logging.getLogger("lindos-run")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="lindos-run"):
            rc = cli_run.main([str(f)])
    finally:
        logger.removeHandler(caplog.handler)
    assert rc == cli_run.EXIT_UNSUPPORTED
    assert "not a program" in caplog.text
    assert wired.run_plan.calls == [] and wired.run_host.calls == []


# --------------------------------------------------------------------------- #
# --info always carries a "format" block (id/label/status/handler/note/reason).
# --------------------------------------------------------------------------- #
def test_info_carries_the_format_block(fake_core, home, wired, tmp_path, monkeypatch, capsys):
    f = tmp_path / "thing.reg"
    f.write_text("Windows Registry Editor Version 5.00\n", encoding="utf-8")
    install_fake_formats(monkeypatch, det=_det("reg", handler="regedit", label="Registration file",
                                               status="works", note="merges settings", reason="suffix .reg"),
                         plan=_plan("regedit", wine_tail=["regedit", "/S", "C:\\thing.reg"]),
                         reg_preview=lambda p: {"deletions": []})
    rc = cli_run.main(["--info", str(f)])
    assert rc == cli_run.EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["format"] == {"id": "reg", "label": "Registration file", "status": "works", "handler": "regedit",
                              "note": "merges settings", "reason": "suffix .reg"}


# --------------------------------------------------------------------------- #
# MSIX: inspect -> honest refusal for unsupported/encrypted, or install into a per-app
# C:\ drive named after the package family, register each app, notify.
# --------------------------------------------------------------------------- #
def _fake_msix_app(**over: object) -> SimpleNamespace:
    base = dict(id="App", display_name="Contoso App", executable="App.exe",
               entry_point="Windows.FullTrustApplication", app_class="win32", parameters="", working_dir="",
               logo="", list_entry=True, console=False)
    base.update(over)
    return SimpleNamespace(**base)


def _fake_msix_info(*, apps: List[SimpleNamespace], status: str = "partial", framework: bool = False,
                    resource_package: bool = False, reason: str = "", kind: str = "package",
                    bundle_version: str = "", version: str = "1.0.0.0", arch: str = "x64") -> SimpleNamespace:
    return SimpleNamespace(path="", kind=kind, name="Contoso.App", publisher="CN=Contoso", publisher_display="Contoso",
                           version=version, arch=arch, resource_id="", display_name="Contoso App",
                           publisher_id="ABCDEFGH12345", package_full_name="Contoso.App_1.0.0.0_x64__abcdefgh1234g",
                           package_family_name="Contoso.App_abcdefgh1234g", apps=apps, dependencies=[],
                           framework=framework, resource_package=resource_package, signed=True,
                           unsigned_marker=False, store_signals=[], status=status, reason=reason,
                           selected_package=None, warnings=[], bundle_version=bundle_version,
                           as_dict=lambda: {"name": "Contoso.App"})


def _fake_check_appinstaller_target(error_cls: type) -> Callable[[Dict[str, str], SimpleNamespace], None]:
    """Mirrors the real ``msix.check_appinstaller_target`` closely enough for these dispatch tests:
    bundles are matched against ``info.bundle_version`` (falling back to ``info.version``), packages
    are also checked for architecture -- the exact bug class covered by ``sec-compat:F1``."""

    def check(ai: Dict[str, str], info: SimpleNamespace) -> None:
        problems: List[str] = []
        if (getattr(info, "name", "") or "").casefold() != (ai.get("name") or "").casefold():
            problems.append(f"name {getattr(info, 'name', '')!r} instead of {ai.get('name')!r}")
        if str(getattr(info, "publisher", "") or "") != str(ai.get("publisher") or ""):
            problems.append("a different publisher")
        if ai.get("kind") == "package":
            expected_version = ai.get("version", "")
            expected_arch = ai.get("arch") or "neutral"
            actual_version = getattr(info, "version", "")
            actual_arch = getattr(info, "arch", "")
            if actual_arch != expected_arch:
                problems.append(f"architecture {actual_arch} instead of {expected_arch}")
        else:
            expected_version = ai.get("version", "")
            actual_version = getattr(info, "bundle_version", "") or getattr(info, "version", "")
        if str(actual_version) != str(expected_version):
            problems.append(f"version {actual_version} instead of {expected_version}")
        if problems:
            raise error_cls("The downloaded package is not the one the App Installer file promised ("
                            + "; ".join(problems) + "). Lindos refuses to install it.")

    return check


def _install_fake_msix(monkeypatch, *, classify_result: str = "package", info: SimpleNamespace,
                      install_fn: Optional[Callable[..., SimpleNamespace]] = None,
                      parse_appinstaller_fn: Optional[Callable[[Path], Dict[str, str]]] = None):
    class FakeMsixError(Exception):
        pass

    def default_install(path: Path, dest_prefix: Path, on_progress=None) -> SimpleNamespace:
        install_dir = Path(dest_prefix) / "drive_c/Program Files/WindowsApps/Contoso.App_1.0.0.0_x64__abcdefgh1234g"
        install_dir.mkdir(parents=True, exist_ok=True)
        exe = install_dir / "App.exe"
        exe.write_bytes(b"MZ")
        return SimpleNamespace(info=info, install_dir=install_dir, apps=[(info.apps[0], exe, None)],
                               notes=[], reg_files=[])

    return install_fake_module(
        monkeypatch, "msix",
        MsixError=FakeMsixError,
        classify=lambda path: classify_result,
        inspect=lambda path: info,
        install=install_fn or default_install,
        parse_appinstaller=parse_appinstaller_fn or (lambda path: {}),
        check_appinstaller_target=_fake_check_appinstaller_target(FakeMsixError),
    )


def test_msix_win32_package_installs_into_package_family_prefix_and_registers_app(fake_core, home, wired, tmp_path,
                                                                                 monkeypatch, capsys):
    f = tmp_path / "ContosoApp.msix"
    f.write_bytes(b"PK\x03\x04")
    info = _fake_msix_info(apps=[_fake_msix_app()])
    _install_fake_msix(monkeypatch, info=info)
    rc = cli_run.main(["--yes", str(f)])
    assert rc == cli_run.EXIT_OK
    out = capsys.readouterr().out
    assert "Installed: Contoso App" in out
    expected_prefix = prefix.prefix_path("Contoso.App_abcdefgh1234g")
    assert expected_prefix.is_dir()
    db = json.loads((home / ".local/share/lindos/apps.json").read_text(encoding="utf-8"))
    rec = next(iter(db.values()))
    assert rec["source"] == "msix" and rec["pfn"] == "Contoso.App_abcdefgh1234g"
    assert rec["prefix"] == expected_prefix.name == "contoso-app-abcdefgh1234g"


def test_msix_needs_confirmation_without_yes(fake_core, home, wired, tmp_path, monkeypatch):
    f = tmp_path / "ContosoApp.msix"
    f.write_bytes(b"PK\x03\x04")
    info = _fake_msix_info(apps=[_fake_msix_app()])
    _install_fake_msix(monkeypatch, info=info)
    rc = cli_run.main([str(f)])  # no --yes, no tty
    assert rc == cli_run.EXIT_USAGE
    assert not prefix.prefix_path("Contoso.App_abcdefgh1234g").is_dir()  # nothing installed either


def test_msix_uwp_only_package_is_explained(fake_core, home, wired, tmp_path, monkeypatch, caplog):
    import logging

    f = tmp_path / "UwpOnly.msix"
    f.write_bytes(b"PK\x03\x04")
    info = _fake_msix_info(apps=[_fake_msix_app(app_class="uwp")], status="unsupported",
                           reason="UWP/WinUI app - Wine has no UWP app model")
    _install_fake_msix(monkeypatch, info=info)
    logger = logging.getLogger("lindos-run")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="lindos-run"):
            rc = cli_run.main(["--yes", str(f)])
    finally:
        logger.removeHandler(caplog.handler)
    assert rc == cli_run.EXIT_UNSUPPORTED
    assert "UWP" in caplog.text


def test_msix_encrypted_package_is_explained_honestly(fake_core, home, wired, tmp_path, monkeypatch, caplog):
    import logging

    f = tmp_path / "App.emsix"
    f.write_bytes(b"EXPH")
    info = SimpleNamespace(kind="encrypted", reason="Store-encrypted (DRM)")
    _install_fake_msix(monkeypatch, classify_result="encrypted", info=info)
    logger = logging.getLogger("lindos-run")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="lindos-run"):
            rc = cli_run.main(["--yes", str(f)])
    finally:
        logger.removeHandler(caplog.handler)
    assert rc == cli_run.EXIT_UNSUPPORTED
    assert "encrypted" in caplog.text.lower()


# --------------------------------------------------------------------------- #
# .appinstaller: HTTPS-only, explicit consent, identity must match after the download.
# --------------------------------------------------------------------------- #
class _FakeHttpResponse:
    def __init__(self, body: bytes, url: str):
        self._body = body
        self._url = url
        self.headers: Dict[str, str] = {}
        self._read = False

    def geturl(self) -> str:
        return self._url

    def read(self, n: int = -1) -> bytes:
        if self._read:
            return b""
        self._read = True
        return self._body

    def close(self) -> None:
        pass


def test_appinstaller_refuses_non_https(fake_core, home, wired, tmp_path, monkeypatch):
    f = tmp_path / "App.appinstaller"
    f.write_text("<AppInstaller/>", encoding="utf-8")
    _install_fake_msix(monkeypatch, info=_fake_msix_info(apps=[_fake_msix_app()]),
                      parse_appinstaller_fn=lambda p: {"uri": "http://cdn.example.com/App.msix", "kind": "package",
                                                       "name": "Contoso App", "version": "1.0.0.0",
                                                       "publisher": "CN=Contoso", "host": "cdn.example.com"})
    install_fake_formats(monkeypatch, det=_det("appinstaller", handler="appinstaller"), plan=_plan("appinstaller"))
    rc = cli_run.main(["--yes", str(f)])
    assert rc == cli_run.EXIT_UNSUPPORTED


def test_appinstaller_happy_path_downloads_verifies_and_installs(fake_core, home, wired, tmp_path, monkeypatch,
                                                                 capsys):
    f = tmp_path / "App.appinstaller"
    f.write_text("<AppInstaller/>", encoding="utf-8")
    uri = "https://cdn.example.com/pkg/ContosoApp.msix"
    d = {"uri": uri, "kind": "package", "name": "Contoso.App", "version": "1.0.0.0", "publisher": "CN=Contoso",
        "host": "cdn.example.com", "arch": "x64"}
    info = _fake_msix_info(apps=[_fake_msix_app()])
    monkeypatch.setattr(cli_run, "_fetch", lambda url: _FakeHttpResponse(b"fake-msix-bytes", url))
    _install_fake_msix(monkeypatch, info=info, parse_appinstaller_fn=lambda p: d)
    install_fake_formats(monkeypatch, det=_det("appinstaller", handler="appinstaller"), plan=_plan("appinstaller"))
    rc = cli_run.main(["--yes", str(f)])
    assert rc == cli_run.EXIT_OK
    assert "Installed: Contoso App" in capsys.readouterr().out
    # the quarantine directory is always cleaned up afterwards
    quarantine = home / ".cache/lindos/quarantine"
    assert not quarantine.is_dir() or list(quarantine.iterdir()) == []


def test_appinstaller_identity_mismatch_after_download_is_refused(fake_core, home, wired, tmp_path, monkeypatch):
    f = tmp_path / "App.appinstaller"
    f.write_text("<AppInstaller/>", encoding="utf-8")
    uri = "https://cdn.example.com/pkg/ContosoApp.msix"
    d = {"uri": uri, "kind": "package", "name": "SomethingElse", "version": "9.9.9.9", "publisher": "CN=Evil",
        "host": "cdn.example.com", "arch": "x64"}
    info = _fake_msix_info(apps=[_fake_msix_app()])  # inspect() will report "Contoso.App", not "SomethingElse"
    monkeypatch.setattr(cli_run, "_fetch", lambda url: _FakeHttpResponse(b"fake-msix-bytes", url))
    _install_fake_msix(monkeypatch, info=info, parse_appinstaller_fn=lambda p: d)
    install_fake_formats(monkeypatch, det=_det("appinstaller", handler="appinstaller"), plan=_plan("appinstaller"))
    rc = cli_run.main(["--yes", str(f)])
    assert rc == cli_run.EXIT_ERROR
    quarantine = home / ".cache/lindos/quarantine"
    assert not quarantine.is_dir() or list(quarantine.iterdir()) == []


def test_appinstaller_package_arch_mismatch_after_download_is_refused(fake_core, home, wired, tmp_path, monkeypatch):
    """A package-kind .appinstaller promising x86 that resolves to an x64 download must be refused
    (sec-compat:F1: the identity check must verify architecture, not just name/publisher/version)."""
    f = tmp_path / "App.appinstaller"
    f.write_text("<AppInstaller/>", encoding="utf-8")
    uri = "https://cdn.example.com/pkg/ContosoApp.msix"
    d = {"uri": uri, "kind": "package", "name": "Contoso.App", "version": "1.0.0.0", "publisher": "CN=Contoso",
        "host": "cdn.example.com", "arch": "x86"}
    info = _fake_msix_info(apps=[_fake_msix_app()], arch="x64")  # download resolves to x64, not the promised x86
    monkeypatch.setattr(cli_run, "_fetch", lambda url: _FakeHttpResponse(b"fake-msix-bytes", url))
    _install_fake_msix(monkeypatch, info=info, parse_appinstaller_fn=lambda p: d)
    install_fake_formats(monkeypatch, det=_det("appinstaller", handler="appinstaller"), plan=_plan("appinstaller"))
    rc = cli_run.main(["--yes", str(f)])
    assert rc == cli_run.EXIT_ERROR
    quarantine = home / ".cache/lindos/quarantine"
    assert not quarantine.is_dir() or list(quarantine.iterdir()) == []


def test_appinstaller_bundle_version_checked_against_bundle_version_not_inner_package(fake_core, home, wired,
                                                                                     tmp_path, monkeypatch, capsys):
    """A bundle-kind .appinstaller must be checked against the bundle's own identity version
    (``info.bundle_version``), not the version of whichever inner package msix.inspect() selected --
    those two legitimately differ for real bundles (sec-compat:F1 / correct-compat:F1)."""
    f = tmp_path / "App.appinstaller"
    f.write_text("<AppInstaller/>", encoding="utf-8")
    uri = "https://cdn.example.com/pkg/ContosoApp.msixbundle"
    d = {"uri": uri, "kind": "bundle", "name": "Contoso.App", "version": "2020.1120.1332.0",
        "publisher": "CN=Contoso", "host": "cdn.example.com", "arch": "neutral"}
    # The bundle's own identity version matches what the .appinstaller promised, but the selected
    # inner package's own manifest version differs -- this must still be accepted.
    info = _fake_msix_info(apps=[_fake_msix_app()], kind="bundle", bundle_version="2020.1120.1332.0",
                           version="3.13.1730.0")
    monkeypatch.setattr(cli_run, "_fetch", lambda url: _FakeHttpResponse(b"fake-msix-bytes", url))
    _install_fake_msix(monkeypatch, info=info, classify_result="bundle", parse_appinstaller_fn=lambda p: d)
    install_fake_formats(monkeypatch, det=_det("appinstaller", handler="appinstaller"), plan=_plan("appinstaller"))
    rc = cli_run.main(["--yes", str(f)])
    assert rc == cli_run.EXIT_OK
    assert "Installed: Contoso App" in capsys.readouterr().out


def test_appinstaller_bundle_version_mismatch_is_refused(fake_core, home, wired, tmp_path, monkeypatch):
    """The reverse: a bundle whose own identity version differs from what the .appinstaller
    promised must still be refused, even if the inner package's version happens to match."""
    f = tmp_path / "App.appinstaller"
    f.write_text("<AppInstaller/>", encoding="utf-8")
    uri = "https://cdn.example.com/pkg/ContosoApp.msixbundle"
    d = {"uri": uri, "kind": "bundle", "name": "Contoso.App", "version": "2020.1120.1332.0",
        "publisher": "CN=Contoso", "host": "cdn.example.com", "arch": "neutral"}
    info = _fake_msix_info(apps=[_fake_msix_app()], kind="bundle", bundle_version="9.9.9.9",
                           version="2020.1120.1332.0")
    monkeypatch.setattr(cli_run, "_fetch", lambda url: _FakeHttpResponse(b"fake-msix-bytes", url))
    _install_fake_msix(monkeypatch, info=info, classify_result="bundle", parse_appinstaller_fn=lambda p: d)
    install_fake_formats(monkeypatch, det=_det("appinstaller", handler="appinstaller"), plan=_plan("appinstaller"))
    rc = cli_run.main(["--yes", str(f)])
    assert rc == cli_run.EXIT_ERROR
    quarantine = home / ".cache/lindos/quarantine"
    assert not quarantine.is_dir() or list(quarantine.iterdir()) == []
