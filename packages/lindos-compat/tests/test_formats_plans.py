"""lindos_compat.formats.plan_action: one plan per format id (SPEC-WINDOWS §28.3), hermetic.

No Wine, DOSBox, pwsh, udisks or cabextract is run: ``which`` is a fake and the Wine WoW64 mode
is passed in (or probed through a fake ``run``).
"""
from __future__ import annotations

import codecs
import json
import struct
import sys
import types
from pathlib import Path
from typing import Callable, Dict, Optional

import pytest

import lindos_compat
from lindos_compat import dos, formats, prefix as prefix_mod

FORBIDDEN_ARGV_TOKENS = ("spoof", "hwid", "smbios", "kvm=off", "hv-vendor-id", "vendor_id", "attest", "vanguard",
                         "tpm", "secureboot", "secure-boot", "bypass", "patchguard", "hvci", "acpitable")


def which_of(*names: str) -> Callable[[str], Optional[str]]:
    table = {n: f"/usr/bin/{n}" for n in names}
    return table.get


NOTHING = which_of()


def det(fid: str, **details: object) -> formats.Detection:
    return formats.Detection(format=formats.by_id(fid), reason="test", details=dict(details))


def plan(path: Path, fid: str, args=(), *, which=NOTHING, details: Optional[Dict[str, object]] = None,
         **kw) -> formats.ActionPlan:
    return formats.plan_action(path, det(fid, **(details or {})), args, which=which, **kw)


def win(p: Path) -> str:
    return formats.to_windows_path(p)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """No host Wine (WineHQ /opt fallback) and no W-B msix module unless a test installs a fake."""
    monkeypatch.setattr(prefix_mod, "WINE_CANDIDATES", ())
    monkeypatch.delattr(lindos_compat, "msix", raising=False)
    monkeypatch.setitem(sys.modules, "lindos_compat.msix", None)
    dos.clear_wow64_memo()


def install_fake_msix(monkeypatch: pytest.MonkeyPatch, **attrs: object) -> None:
    mod = types.ModuleType("lindos_compat.msix")
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, "lindos_compat.msix", mod)
    monkeypatch.setattr(lindos_compat, "msix", mod, raising=False)


def touch(tmp: Path, name: str, data: bytes = b"x") -> Path:
    p = tmp / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def assert_clean(p: formats.ActionPlan) -> None:
    blob = " ".join(p.wine_tail + p.host_argv).lower()
    for tok in FORBIDDEN_ARGV_TOKENS:
        assert tok not in blob, (tok, p.wine_tail, p.host_argv)
    for tok in p.wine_tail + p.host_argv:
        assert isinstance(tok, str)
    json.dumps(p.as_dict())


# --------------------------------------------------------------------------- #
# Wine programs
# --------------------------------------------------------------------------- #
def test_exe(tmp_path: Path):
    exe = touch(tmp_path, "My App/setup.exe")
    p = plan(exe, "exe", ["/S", "D=C:\\x y"])
    assert p.handler == "run" and p.wine_tail == [win(exe), "/S", "D=C:\\x y"]
    assert p.needs_prefix and p.force_runner is None and p.exit_code == 0 and p.arch is None
    assert p.wine_tail[0].startswith("Z:\\") and p.wine_tail[0].endswith("\\My App\\setup.exe")
    assert_clean(p)


def test_exe_inside_prefix_gets_c_drive(tmp_path: Path):
    pfx = tmp_path / "pfx"
    exe = touch(pfx, "drive_c/Program Files/App/app.exe")
    p = plan(exe, "exe", prefix=pfx)
    assert p.wine_tail == ["C:\\Program Files\\App\\app.exe"]


def test_dotnet_messages(tmp_path: Path):
    exe = touch(tmp_path, "tool.exe")
    fw = plan(exe, "dotnet-exe", details={"clr": True, "clr_version": "v4.0.30319"})
    assert fw.handler == "run" and fw.wine_tail == [win(exe)]
    assert "wine-mono" in fw.message and ".NET Framework 4.x" in fw.message and "licence" in fw.message
    old = plan(exe, "dotnet-exe", details={"clr": True, "clr_version": "v2.0.50727"})
    assert ".NET Framework 2.0-3.5" in old.message
    core = plan(exe, "dotnet-exe", details={"apphost": True, "machine_name": "x64",
                                            "dotnet_frameworks": ["Microsoft.WindowsDesktop.App 8.0.0"]})
    assert "Microsoft.WindowsDesktop.App 8.0.0" in core.message and "(x64)" in core.message
    assert "dotnet.microsoft.com" in core.message
    x86 = plan(exe, "dotnet-exe", details={"apphost": True, "machine_name": "x86"})
    assert "(x86)" in x86.message and ".NET Desktop Runtime" in x86.message
    sc = plan(exe, "dotnet-exe", details={"apphost": True, "dotnet_self_contained": True})
    assert "own .NET runtime" in sc.message


def test_bat_scr_cpl_lnk(tmp_path: Path):
    bat = touch(tmp_path, "go.bat")
    assert plan(bat, "bat", ["a"]).wine_tail == ["cmd", "/c", win(bat), "a"]
    scr = touch(tmp_path, "bubbles.scr")
    s = plan(scr, "scr")
    assert s.handler == "screensaver" and s.wine_tail == [win(scr), "/s"]
    assert plan(scr, "scr", ["/c"]).wine_tail == [win(scr), "/c"]
    cpl = touch(tmp_path, "desk.cpl")
    c = plan(cpl, "cpl")
    assert c.handler == "control-panel" and c.wine_tail == ["control", win(cpl)] and c.force_runner == "wine"
    lnk = touch(tmp_path, "App.lnk")
    assert plan(lnk, "lnk").handler == "run"


# --------------------------------------------------------------------------- #
# Windows Installer
# --------------------------------------------------------------------------- #
def test_msi_with_transform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    msi = touch(tmp_path, "pkg/app.msi")
    mst = touch(tmp_path, "pkg/custom lang.mst")
    other = touch(tmp_path, "cwd/second.mst")
    monkeypatch.chdir(tmp_path / "cwd")
    p = plan(msi, "msi", ["transforms=custom lang.mst;second.mst;:embedded", "ALLUSERS=1", "/qb"])
    assert p.handler == "msiexec-install" and p.force_runner == "wine" and p.needs_prefix
    assert p.wine_tail[:3] == ["msiexec", "/i", win(msi)]
    assert p.wine_tail[3] == f"TRANSFORMS={win(mst)};{win(other)};:embedded"
    assert p.wine_tail[4:] == ["ALLUSERS=1", "/qb"]
    abs_unix = plan(msi, "msi", ["PATCH=/opt/fix.msp"]).wine_tail[3]
    assert abs_unix == "PATCH=Z:\\opt\\fix.msp"
    keep = plan(msi, "msi", ['TRANSFORMS="C:\\t.mst"', "TRANSFORMS=@missing.mst"]).wine_tail[3:]
    assert keep == ["TRANSFORMS=C:\\t.mst", "TRANSFORMS=@missing.mst"]
    assert_clean(p)


def test_msp_patch_not_install(tmp_path: Path):
    msp = touch(tmp_path, "fix.msp")
    p = plan(msp, "msp")
    assert p.handler == "msiexec-patch"
    assert p.wine_tail == ["msiexec", "/p", win(msp), "REINSTALL=ALL", "REINSTALLMODE=omus"]
    assert "/i" not in p.wine_tail
    assert p.needs_prefix and p.force_runner == "wine" and p.confirm and p.details["needs_product_prefix"]
    custom = plan(msp, "msp", ["reinstall=Feature1", "/qn"])
    assert custom.wine_tail == ["msiexec", "/p", win(msp), "reinstall=Feature1", "/qn", "REINSTALLMODE=omus"]


def test_mst_explains_with_command(tmp_path: Path):
    touch(tmp_path, "Office.MSI")
    mst = touch(tmp_path, "lang.mst")
    p = plan(mst, "mst")
    assert p.handler == "explain" and p.exit_code == formats.EXIT_UNSUPPORTED
    assert "TRANSFORMS=" in p.message and "Office.MSI" in p.details["command"]
    touch(tmp_path, "Second.msi")
    assert "<installer>.msi" in plan(mst, "mst").message


# --------------------------------------------------------------------------- #
# MSIX family
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fid,name", [("msix", "a.msix"), ("msix-bundle", "a.msixbundle"),
                                      ("msix-upload", "a.msixupload")])
def test_msix_handlers(tmp_path: Path, fid: str, name: str):
    p = plan(touch(tmp_path, name), fid)
    assert p.handler == "msix" and p.needs_prefix and p.exit_code == 0
    assert p.confirm and name in p.confirm and p.wine_tail == [] and p.host_argv == []


def test_msix_encrypted_uses_file_name(tmp_path: Path):
    p = plan(touch(tmp_path, "Vendor.PhotoApp_2.3.4.0_x64__8wekyb3d8bbwe.eappxbundle", b"EXBH"), "msix-encrypted")
    assert p.handler == "explain" and p.exit_code == 3
    assert p.details == {"name": "Vendor.PhotoApp", "version": "2.3.4.0"}
    assert "Vendor.PhotoApp 2.3.4.0" in p.message and "Store" in p.message


def test_msix_encrypted_uses_msix_inspect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    install_fake_msix(monkeypatch, inspect=lambda path: types.SimpleNamespace(name="Contoso.App", version="1.0.0.0"))
    p = plan(touch(tmp_path, "x.eappx", b"EXPH"), "msix-encrypted")
    assert "Contoso.App 1.0.0.0" in p.message


def test_msixvc(tmp_path: Path):
    p = plan(touch(tmp_path, "game.msixvc"), "msixvc")
    assert p.handler == "explain" and p.exit_code == 3 and "Xbox" in p.message


def test_appinstaller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    f = touch(tmp_path, "App.appinstaller")
    generic = plan(f, "appinstaller")
    assert generic.handler == "appinstaller" and generic.needs_prefix and "Download" in generic.confirm
    install_fake_msix(monkeypatch, parse_appinstaller=lambda path: {
        "uri": "https://cdn.contoso.com/App.msixbundle", "kind": "bundle", "name": "Contoso.App",
        "version": "1.0.0.0", "publisher": "CN=Contoso", "host": "cdn.contoso.com"})
    p = plan(f, "appinstaller")
    assert "cdn.contoso.com" in p.confirm and "CN=Contoso" in p.confirm and "Contoso.App" in p.confirm
    assert p.details["appinstaller"]["kind"] == "bundle"

    def bad(path):
        raise ValueError("no MainPackage")

    install_fake_msix(monkeypatch, parse_appinstaller=bad)
    e = plan(f, "appinstaller")
    assert e.handler == "explain" and e.exit_code == 1 and "no MainPackage" in e.message


# --------------------------------------------------------------------------- #
# scripts
# --------------------------------------------------------------------------- #
def test_ps1(tmp_path: Path):
    script = touch(tmp_path, "Set Up.ps1")
    p = plan(script, "ps1", ["-Force"], which=which_of("pwsh"))
    assert p.handler == "pwsh" and p.host_argv == ["/usr/bin/pwsh", "-NoProfile", "-File",
                                                   str(script.absolute()), "-Force"]
    assert p.confirm and "trust" in p.confirm and "Windows-only" in p.message and not p.needs_prefix
    lts = plan(script, "ps1", which=which_of("pwsh-lts"))
    assert lts.host_argv[0] == "/usr/bin/pwsh-lts"
    missing = plan(script, "ps1")
    assert missing.exit_code == 1 and "ubuntu/24.04/packages-microsoft-prod.deb" in missing.message
    assert missing.host_argv == []
    assert_clean(p)


@pytest.mark.parametrize("name,note", [("a.vbs", "Script Host"), ("a.vbe", "Encoded"), ("a.wsf", "partly")])
def test_vbs(tmp_path: Path, name: str, note: str):
    f = touch(tmp_path, name)
    p = plan(f, "vbs", ["arg"])
    assert p.handler == "wscript" and p.wine_tail == ["wscript", win(f), "arg"]
    assert p.force_runner == "wine" and p.needs_prefix and note in p.message


def test_reg(tmp_path: Path):
    f = touch(tmp_path, "tweak.reg", ("Windows Registry Editor Version 5.00\r\n\r\n[HKEY_CURRENT_USER\\A]\r\n"
                                      "\"x\"=-\r\n[-HKEY_LOCAL_MACHINE\\Software\\B]\r\n").encode("utf-16-le"))
    f.write_bytes(codecs.BOM_UTF16_LE + f.read_bytes())
    p = plan(f, "reg")
    assert p.handler == "regedit" and p.wine_tail == ["regedit", "/S", win(f)]
    assert p.force_runner == "wine" and p.needs_prefix
    assert "DELETES 1 key(s)" in p.confirm and "HKEY_LOCAL_MACHINE\\Software\\B" in p.confirm
    assert "HKEY_CURRENT_USER\\A\\x" in p.confirm
    assert p.details["reg"]["encoding"] == "utf-16-le"
    bad = plan(touch(tmp_path, "bad.reg", b"[HKEY_CURRENT_USER\\A]\n"), "reg")
    assert bad.handler == "explain" and bad.exit_code == 1 and "REGEDIT4" in bad.message
    gone = plan(tmp_path / "gone.reg", "reg")
    assert gone.handler == "explain" and gone.exit_code == 1


def test_url(tmp_path: Path):
    ok = touch(tmp_path, "site.url", b"[InternetShortcut]\r\nURL=https://example.org/\r\n")
    p = plan(ok, "url", which=which_of("xdg-open"))
    assert p.handler == "open-url" and p.host_argv == ["/usr/bin/xdg-open", "https://example.org/"]
    assert not p.needs_prefix and p.exit_code == 0
    no_opener = plan(ok, "url")
    assert no_opener.exit_code == 1 and "xdg-utils" in no_opener.message
    evil = touch(tmp_path, "evil.url", b"[InternetShortcut]\r\nURL=javascript:alert(document.cookie)\r\n")
    e = plan(evil, "url", which=which_of("xdg-open"))
    assert e.handler == "explain" and e.exit_code == 3 and e.host_argv == [] and "script" in e.message
    f = touch(tmp_path, "file.url", b"[InternetShortcut]\r\nURL=file:///etc/passwd\r\n")
    assert plan(f, "url", which=which_of("xdg-open")).host_argv == []


# --------------------------------------------------------------------------- #
# .inf
# --------------------------------------------------------------------------- #
def test_inf_software_unquoted_tokens(tmp_path: Path):
    f = touch(tmp_path, "My Fonts/install fonts.inf",
              b"[Version]\r\nSignature=\"$Windows NT$\"\r\n[DefaultInstall]\r\nCopyFiles=F\r\n[F]\r\na.ttf\r\n")
    p = plan(f, "inf")
    assert p.handler == "inf-install" and p.force_runner == "wine" and p.needs_prefix and p.confirm
    assert p.wine_tail[:4] == ["rundll32", "setupapi.dll,InstallHinfSection", "DefaultInstall", "132"]
    assert " ".join(p.wine_tail[4:]) == win(f)
    assert all(" " not in t and '"' not in t for t in p.wine_tail[4:])
    assert_clean(p)


def test_inf_driver_and_unknown(tmp_path: Path):
    drv = touch(tmp_path, "net.inf", b"[Version]\nClass=Net\n[Manufacturer]\nX=Y\n")
    d = plan(drv, "inf")
    assert d.handler == "explain" and d.exit_code == 3 and "lindos-drivers detect" in d.message
    assert d.details["suggest"] == ["lindos-drivers", "detect"]
    none = plan(touch(tmp_path, "x.inf", b"[Version]\n[Other]\n"), "inf")
    assert none.handler == "explain" and "DefaultInstall" in none.message
    auto = plan(touch(tmp_path, "autorun.inf", b"[AutoRun]\nopen=setup.exe /x\n"), "inf")
    assert auto.handler == "explain" and "setup.exe /x" in auto.message


def test_inf_path_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    body = b"[Version]\n[DefaultInstall]\nAddReg=R\n"
    real = formats.inf_details
    monkeypatch.setattr(formats, "inf_details", lambda path: {"kind": "software", "reasons": [], "sections": [],
                                                              "autorun": {}})
    deep = tmp_path / ("d" * 120) / ("e" * 120) / "x.inf"  # not created: Windows hosts cap paths at 260
    long_plan = plan(deep, "inf")
    assert long_plan.handler == "explain" and long_plan.exit_code == 1 and "260" in long_plan.message
    monkeypatch.setattr(formats, "inf_details", real)
    spaced = touch(tmp_path, "two  spaces.inf", body)
    s = plan(spaced, "inf")
    assert s.handler == "explain" and s.exit_code == 1 and "double spaces" in s.message


# --------------------------------------------------------------------------- #
# archives / images / updates
# --------------------------------------------------------------------------- #
def test_cab(tmp_path: Path):
    cab = touch(tmp_path, "drivers.cab", b"MSCF")
    p = plan(cab, "cab", which=which_of("cabextract", "xdg-open"))
    dest = tmp_path / "drivers"
    assert p.handler == "extract" and p.host_argv == ["/usr/bin/cabextract", "-d", str(dest.absolute()),
                                                      str(cab.absolute())]
    assert p.details["dest"] == str(dest.absolute()) and p.details["open"] == ["/usr/bin/xdg-open", str(dest.absolute())]
    dest.mkdir()
    again = plan(cab, "cab", which=which_of("cabextract"))
    assert again.details["dest"].endswith("drivers (2)") and again.details["open"][0] == "xdg-open"
    seven = plan(cab, "cab", which=which_of("7z"))
    assert seven.host_argv[:2] == ["/usr/bin/7z", "x"] and seven.host_argv[2].startswith("-o")
    none = plan(cab, "cab")
    assert none.exit_code == 1 and "cabextract" in none.message


def test_cab_dest_falls_back_to_downloads(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch):
    cab = touch(tmp_path, "ro/x.cab", b"MSCF")
    (home / "Downloads").mkdir()
    monkeypatch.setattr(formats.os, "access", lambda path, mode: False)
    p = plan(cab, "cab", which=which_of("cabextract"), home=home)
    assert p.details["dest"] == str((home / "Downloads" / "x").absolute())


def test_msu(tmp_path: Path):
    p = plan(touch(tmp_path, "windows10.0-kb5031356-x64.msu", b"MSCF"), "msu")
    assert p.handler == "explain" and p.exit_code == 3 and "Windows Update" in p.message
    assert p.details["extract"][0] == "cabextract"


def test_iso(tmp_path: Path):
    iso = touch(tmp_path, "Win 11.iso")
    p = plan(iso, "iso", which=which_of("udisksctl"))
    assert p.handler == "mount" and p.host_argv == ["/usr/bin/udisksctl", "loop-setup", "-r", "-f",
                                                    str(iso.absolute())]
    assert not p.needs_prefix and "asks before" in p.message and p.details["image"] == str(iso.absolute())
    missing = plan(iso, "iso")
    assert missing.exit_code == 1 and "udisks2" in missing.message


# --------------------------------------------------------------------------- #
# ClickOnce
# --------------------------------------------------------------------------- #
def dotnet_prefix(tmp: Path) -> Path:
    pfx = tmp / "dotnet48"
    (pfx / "drive_c/windows/system32").mkdir(parents=True)
    (pfx / "drive_c/windows/system32/dfshim.dll").write_bytes(b"MZ")
    (pfx / "system.reg").write_text("WINE REGISTRY Version 2\n\n[Software\\\\Microsoft\\\\NET Framework Setup\\\\"
                                    "NDP\\\\v4\\\\Full] 1\n\"Release\"=dword:00080ff4\n", encoding="utf-8")
    return pfx


def test_clickonce_without_dotnet_is_explained(tmp_path: Path):
    app = touch(tmp_path, "Tool.application", b"<assembly/>")
    p = plan(app, "clickonce")
    assert p.handler == "explain" and p.exit_code == 3
    assert "licence" in p.message and "does not install" in p.message
    bare = tmp_path / "bare"
    bare.mkdir()
    assert plan(app, "clickonce", prefix=bare).handler == "explain"


def test_clickonce_with_dotnet(tmp_path: Path):
    pfx = dotnet_prefix(tmp_path)
    app = touch(tmp_path, "My Tool.application", b"<assembly/>")
    p = plan(app, "clickonce", prefix=pfx)
    assert p.handler == "clickonce" and p.force_runner == "wine" and p.needs_prefix
    assert p.wine_tail[:2] == ["rundll32", "dfshim.dll,ShOpenVerbApplication"]
    assert " ".join(p.wine_tail[2:]) == win(app)
    ref = tmp_path / "Tool.appref-ms"
    ref.write_bytes(codecs.BOM_UTF16_LE + "https://apps.example.org/Tool.application#Tool.application, "
                    "Culture=neutral, PublicKeyToken=abc, processorArchitecture=msil".encode("utf-16-le"))
    r = plan(ref, "clickonce", prefix=pfx)
    assert r.wine_tail == ["rundll32", "dfshim.dll,ShOpenVerbApplication", "https://apps.example.org/Tool.application"]
    bad = tmp_path / "Bad.appref-ms"
    bad.write_text("file:///C:/evil.application#x", encoding="utf-8")
    b = plan(bad, "clickonce", prefix=pfx)
    assert b.handler == "explain" and b.exit_code == 1


# --------------------------------------------------------------------------- #
# DOS
# --------------------------------------------------------------------------- #
def test_dos_exe(tmp_path: Path):
    exe = touch(tmp_path, "GAME.EXE", b"MZ")
    p = plan(exe, "dos-exe", which=which_of("dosbox-x"))
    assert p.handler == "dos" and p.host_argv == ["/usr/bin/dosbox-x", "-fastlaunch", "-nopromptfolder", "-exit",
                                                  str(exe.absolute())]
    assert not p.needs_prefix and p.wine_tail == [] and p.exit_code == 0
    le = plan(exe, "dos-exe", which=which_of("dosbox-x"), details={"partial_note": "DOS-extender program"})
    assert le.message == "DOS-extender program"
    missing = plan(exe, "dos-exe")
    assert missing.handler == "dos" and missing.exit_code == 1 and dos.DOSBOX_INSTALL_HINT in missing.message


def test_dos_com_and_pif(tmp_path: Path):
    com = touch(tmp_path, "HELLO.COM", b"\xcd\x20")
    assert plan(com, "dos-com", which=which_of("dosbox"), run=lambda *a, **k: types.SimpleNamespace(
        returncode=0, stdout="DOSBox version 0.74-3", stderr="")).host_argv == ["/usr/bin/dosbox", str(com.absolute()),
                                                                                "-exit"]
    game = touch(tmp_path, "keen/KEEN4E.EXE", b"MZ")
    pif = touch(tmp_path, "keen/keen.pif")
    p = plan(pif, "dos-com", which=which_of("dosbox-x"),
             details={"pif": {"program": "C:\\KEEN\\keen4e.exe", "params": "/nojoy", "title": "", "workdir": ""}})
    assert p.host_argv[-1] == "exit" and f'mount c "{game.parent.absolute()}"' in p.host_argv
    assert "KEEN4E.EXE /nojoy" in p.host_argv  # the real on-disk name, not the PIF's spelling
    lost = plan(pif, "dos-com", which=which_of("dosbox-x"),
                details={"pif": {"program": "C:\\X\\MISSING.EXE", "params": ""}})
    assert lost.handler == "explain" and lost.exit_code == 1 and "MISSING.EXE" in lost.message


# --------------------------------------------------------------------------- #
# Win16
# --------------------------------------------------------------------------- #
def test_win16_old_wow64(tmp_path: Path):
    exe = touch(tmp_path, "SOL.EXE")
    p = plan(exe, "win16-exe", ["/x"], wine_mode="old-wow64")
    assert p.handler == "win16" and p.wine_tail == [win(exe), "/x"]
    assert p.arch == "win32" and p.prefix_hint == "win16" and p.force_runner == "wine" and p.needs_prefix
    assert "win16" in p.message and "wine32" not in p.message


def test_win16_new_wow64(tmp_path: Path):
    exe = touch(tmp_path, "SOL.EXE")
    p = plan(exe, "win16-exe", wine_mode="new-wow64-16bit")
    assert p.handler == "win16" and p.arch is None and p.prefix_hint is None and p.force_runner == "wine"
    old = plan(exe, "win16-exe", wine_mode="new-wow64-no16bit")
    assert old.handler == "explain" and old.exit_code == 3 and "10.16" in old.message


def test_win16_unknown_and_probe(tmp_path: Path):
    exe = touch(tmp_path, "SOL.EXE")
    p = plan(exe, "win16-exe")  # no Wine at all -> probe says "unknown"
    assert p.details["wine_mode"] == "unknown" and p.arch == "win32" and "wine32:i386" in p.message

    def run(argv, *a, **k):
        if "--version" in argv:
            return types.SimpleNamespace(returncode=0, stdout="wine-11.0\n", stderr="")
        return types.SimpleNamespace(returncode=1, stdout="",
                                     stderr="wine: WINEARCH is set to 'win32' but this is not supported in wow64 mode.")

    probed = plan(exe, "win16-exe", which=which_of("wine"), run=run)
    assert probed.details["wine_mode"] == "new-wow64-16bit" and probed.arch is None


# --------------------------------------------------------------------------- #
# explained formats
# --------------------------------------------------------------------------- #
def test_arm_and_dll(tmp_path: Path):
    a = plan(touch(tmp_path, "arm.exe"), "arm-exe", details={"machine_name": "arm64"})
    assert a.handler == "explain" and a.exit_code == 3 and "ARM" in a.message and "x64" in a.message
    lib = plan(touch(tmp_path, "x.dll"), "dll")
    assert lib.exit_code == 3 and "library" in lib.message
    drv = plan(touch(tmp_path, "e1000.sys"), "dll", details={"driver": True})
    assert "lindos-drivers detect" in drv.message
    efi = plan(touch(tmp_path, "boot.efi"), "dll", details={"efi": True})
    assert "UEFI" in efi.message
    boot = plan(touch(tmp_path, "winload.exe"), "dll", details={"boot_application": True})
    assert "start-up" in boot.message
    ne = plan(touch(tmp_path, "c.dll"), "dll", details={"new_exe": "NE"})
    assert "16-bit" in ne.message


def test_unknown(tmp_path: Path):
    f = touch(tmp_path, "notes.txt", b"hello")
    d = formats.detect(f)
    p = formats.plan_action(f, d)
    assert p.handler == "explain" and p.exit_code == 3 and "notes.txt" in p.message
    elf = touch(tmp_path, "tool.exe", b"\x7fELF" + b"\x00" * 60)
    pe = formats.plan_action(elf, formats.detect(elf))
    assert "Linux program" in pe.message and "directly" in pe.message
    gone = tmp_path / "gone.exe"
    g = formats.plan_action(gone, formats.detect(gone))
    assert g.exit_code == 1 and "does not exist" in g.message


# --------------------------------------------------------------------------- #
# whole table
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("spec", formats.FORMATS, ids=lambda s: s.id)
def test_every_format_has_a_consistent_plan(tmp_path: Path, spec: formats.FormatSpec):
    name = "file" + (spec.suffixes[0] if spec.suffixes else ".bin")
    content = {"reg": b"REGEDIT4\n\n[HKEY_CURRENT_USER\\x]\n", "url": b"[InternetShortcut]\nURL=https://x.org\n",
               "inf": b"[Version]\n[DefaultInstall]\n"}.get(spec.id, b"MZ")
    f = touch(tmp_path, name, content)
    tools = which_of("dosbox-x", "pwsh", "xdg-open", "cabextract", "udisksctl")
    p = formats.plan_action(f, det(spec.id), which=tools, wine_mode="old-wow64", home=tmp_path)
    assert p.handler in formats.HANDLERS
    if spec.status == "unsupported":
        assert p.handler == "explain" and p.exit_code == formats.EXIT_UNSUPPORTED
    if p.handler == "explain":
        assert p.message and p.exit_code != 0
    else:
        assert p.exit_code == 0
        assert p.handler == spec.handler or (spec.id, p.handler) in {("clickonce", "explain")}
        assert p.wine_tail or p.host_argv or p.handler in ("msix", "appinstaller")
    if p.wine_tail:
        assert p.needs_prefix
    assert_clean(p)


def test_plan_action_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def boom(*a, **k):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(formats, "_plan_reg", boom)
    p = plan(touch(tmp_path, "x.reg"), "reg")
    assert p.handler == "explain" and p.exit_code == 1 and "simulated failure" in p.message
