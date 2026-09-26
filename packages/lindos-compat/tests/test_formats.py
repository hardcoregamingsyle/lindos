"""lindos_compat.formats: registry (SPEC-WINDOWS §28.2/§28.3), API shapes and the text helpers
(.reg preview, .url allowlist, .inf driver-vs-software, .NET Framework probe, .pif)."""
from __future__ import annotations

import codecs
import json
import os
import re
from pathlib import Path

import pytest

from lindos_compat import formats

LIB = Path(formats.__file__).resolve().parent

# SPEC-WINDOWS §28.3: binding ids -> (handler, status)
SPEC_TABLE = {
    "exe": ("run", "works"), "dotnet-exe": ("run", "partial"), "win16-exe": ("win16", "partial"),
    "dos-exe": ("dos", "works"), "dos-com": ("dos", "works"), "arm-exe": ("explain", "unsupported"),
    "dll": ("explain", "unsupported"), "msi": ("msiexec-install", "works"), "msp": ("msiexec-patch", "partial"),
    "mst": ("explain", "partial"), "msix": ("msix", "partial"), "msix-bundle": ("msix", "partial"),
    "msix-upload": ("msix", "partial"), "msix-encrypted": ("explain", "unsupported"),
    "msixvc": ("explain", "unsupported"), "appinstaller": ("appinstaller", "partial"), "bat": ("run", "works"),
    "ps1": ("pwsh", "partial"), "vbs": ("wscript", "partial"), "reg": ("regedit", "works"), "lnk": ("run", "works"),
    "url": ("open-url", "works"), "scr": ("screensaver", "works"), "cpl": ("control-panel", "partial"),
    "inf": ("inf-install", "partial"), "cab": ("extract", "works"), "msu": ("explain", "unsupported"),
    "iso": ("mount", "works"), "clickonce": ("clickonce", "partial"),
}
SPEC_SUFFIXES = {
    "dos-com": {".com", ".pif"}, "dll": {".dll", ".ocx", ".sys", ".efi"}, "msix": {".msix", ".appx"},
    "msix-bundle": {".msixbundle", ".appxbundle"}, "msix-upload": {".msixupload", ".appxupload"},
    "msix-encrypted": {".emsix", ".eappx", ".emsixbundle", ".eappxbundle"}, "bat": {".bat", ".cmd"},
    "vbs": {".vbs", ".vbe", ".wsf"}, "iso": {".iso", ".img"}, "clickonce": {".application", ".appref-ms"},
}

# Honesty tokens that must never appear in Lindos compat sources (SPEC §27.1; same spirit as the
# lindos-vm / lindos-winapps / lindos-gaming scans).
FORBIDDEN_TOKENS = ("spoof", "hwid", "smbios", "kvm=off", "hv-vendor-id", "hv_vendor_id", "acpitable",
                    "attestation", "vm-detect", "vmdetect", "bypass", "patchguard")


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
def test_constants():
    assert formats.STATUSES == ("works", "partial", "unsupported")
    assert formats.EXIT_UNSUPPORTED == 3
    assert formats.HANDLERS == ("run", "msiexec-install", "msiexec-patch", "msix", "appinstaller", "dos", "win16",
                                "wscript", "pwsh", "regedit", "open-url", "screensaver", "control-panel",
                                "inf-install", "extract", "mount", "clickonce", "explain")


def test_table_matches_spec_exactly():
    ids = [f.id for f in formats.FORMATS]
    assert len(ids) == len(set(ids)), "duplicate ids"
    assert set(ids) == set(SPEC_TABLE)
    for f in formats.FORMATS:
        assert (f.handler, f.status) == SPEC_TABLE[f.id], f.id
        assert f.handler in formats.HANDLERS and f.status in formats.STATUSES
        assert f.note and "\n" not in f.note and len(f.note) < 160, f.id
        assert f.label and f.mime and "/" in f.mime
        for s in f.suffixes:
            assert s.startswith(".") and s == s.lower(), (f.id, s)
    for fid, suffixes in SPEC_SUFFIXES.items():
        assert set(formats.by_id(fid).suffixes) == suffixes, fid


def test_unsupported_formats_are_explained():
    for f in formats.FORMATS:
        if f.status == "unsupported":
            assert f.handler == "explain", f.id
    assert formats.UNKNOWN_FORMAT.handler == "explain" and formats.UNKNOWN_FORMAT.status == "unsupported"
    assert formats.UNKNOWN_FORMAT not in formats.FORMATS


def test_ps1_and_vbs_mime_match_what_desktop_integration_actually_registers():
    """``ps1``/``vbs`` FormatSpec.mime must be MIME types the package's own desktop-integration
    files actually define or reuse -- not a string that matches nothing real (tests-hermetic:F4).

    A type is "known" when lindos-windows.xml defines it (a ``type="..."`` mime-type element) or
    documents it in its own header comment as a reused shared-mime-info type, mirroring
    ``tests/test_integration.py``'s ``test_addendum_w_run_desktop_mime_types_are_all_known``.
    This only checks ``ps1``/``vbs`` (the ids this regression covers), not the whole table.
    """
    pkg_root = Path(__file__).resolve().parent.parent  # packages/lindos-compat
    mime_xml = (pkg_root / "root" / "usr" / "share" / "mime" / "packages" / "lindos-windows.xml").read_text(
        encoding="utf-8")
    comment_end = mime_xml.index("-->")
    header, body = mime_xml[:comment_end], mime_xml[comment_end:]
    known = set(re.findall(r"\b(?:application|text)/[A-Za-z0-9_.+-]+", header))
    known |= set(re.findall(r'type="((?:application|text)/[^"]+)"', body))
    mimeapps = (pkg_root / "root" / "usr" / "share" / "lindos" / "mimeapps-lindos.list").read_text(encoding="utf-8")
    known |= set(re.findall(r"^((?:application|text)/[A-Za-z0-9_.+-]+)=", mimeapps, re.M))
    assert formats.by_id("ps1").mime == "application/x-powershell"
    assert formats.by_id("vbs").mime == "application/x-lindos-wsf"
    for fid in ("ps1", "vbs"):
        mime = formats.by_id(fid).mime
        assert mime in known, f"{fid}: mime {mime!r} is not defined/reused anywhere real"


def test_formatspec_is_frozen():
    spec = formats.by_id("exe")
    with pytest.raises(Exception):
        spec.id = "x"  # type: ignore[misc]


def test_by_id():
    assert formats.by_id("msi").handler == "msiexec-install"
    assert formats.by_id("MSI").id == "msi"
    assert formats.by_id(" reg ").id == "reg"
    assert formats.by_id("unknown") is formats.UNKNOWN_FORMAT
    assert formats.by_id("nope") is None
    assert formats.by_id("") is None


@pytest.mark.parametrize("suffix,fid", [
    (".exe", "exe"), ("EXE", "exe"), ("exe", "exe"), ("Setup.MSI", "msi"), (".img", "iso"), (".iso", "iso"),
    (".sys", "dll"), (".efi", "dll"), (".ocx", "dll"), (".appref-ms", "clickonce"), (".application", "clickonce"),
    (".appx", "msix"), (".eappxbundle", "msix-encrypted"), (".msixvc", "msixvc"), (".cmd", "bat"),
    (".pif", "dos-com"), (".wsf", "vbs"), (".msu", "msu"), (".ps1", "ps1"), (".url", "url"),
])
def test_by_suffix(suffix, fid):
    assert formats.by_suffix(suffix).id == fid


@pytest.mark.parametrize("suffix", [".txt", "", ".zip", ".", ".exe.bak"])
def test_by_suffix_unknown(suffix):
    assert formats.by_suffix(suffix) is None


def test_formats_table_shape():
    table = formats.formats_table()
    assert len(table) == len(formats.FORMATS)
    json.dumps(table)
    for row in table:
        assert set(row) == {"id", "label", "suffixes", "mime", "handler", "status", "note"}
        assert isinstance(row["suffixes"], list)


def test_detection_and_plan_dicts():
    det = formats.Detection(format=formats.by_id("exe"), reason="PE32 x86 GUI", details={"machine": 0x14C})
    assert det.info() == {"id": "exe", "label": "Windows program", "status": "works", "handler": "run",
                          "note": formats.by_id("exe").note, "reason": "PE32 x86 GUI"}
    assert det.as_dict()["details"] == {"machine": 0x14C}
    plan = formats.ActionPlan(handler="run", wine_tail=["Z:\\a.exe"], host_argv=[], needs_prefix=True,
                              force_runner=None, arch=None, prefix_hint=None, confirm=None, message="", exit_code=0)
    d = plan.as_dict()
    assert d["wine_tail"] == ["Z:\\a.exe"] and d["exit_code"] == 0 and d["details"] == {}
    json.dumps(d)


# --------------------------------------------------------------------------- #
# Windows paths
# --------------------------------------------------------------------------- #
def test_to_windows_path_z_drive():
    assert formats.to_windows_path(Path("/home/alice/Downloads/app.msi")) == "Z:\\home\\alice\\Downloads\\app.msi"
    assert formats.to_windows_path("/home/alice/My Apps/x y.exe") == "Z:\\home\\alice\\My Apps\\x y.exe"
    assert formats.to_windows_path("/tmp/a/../b/./c.exe") == "Z:\\tmp\\b\\c.exe"
    assert formats.to_windows_path("//x/y.exe") == "Z:\\x\\y.exe"


def test_to_windows_path_relative(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    out = formats.to_windows_path("setup.exe")
    assert out.startswith("Z:\\") and out.endswith("\\setup.exe")
    assert "/" not in out


def test_to_windows_path_inside_prefix(tmp_path: Path):
    prefix = tmp_path / "pfx"
    exe = prefix / "drive_c" / "Program Files" / "App" / "app.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    assert formats.to_windows_path(exe, prefix) == "C:\\Program Files\\App\\app.exe"
    other = tmp_path / "elsewhere.exe"
    other.write_bytes(b"MZ")
    assert formats.to_windows_path(other, prefix).startswith("Z:\\")


def test_rundll32_path_tokens():
    assert formats.rundll32_path_tokens("Z:\\a\\b.inf") == ["Z:\\a\\b.inf"]
    toks = formats.rundll32_path_tokens("Z:\\My Drivers\\setup me.inf")
    assert toks == ["Z:\\My", "Drivers\\setup", "me.inf"]
    assert " ".join(toks) == "Z:\\My Drivers\\setup me.inf"
    assert all(t and '"' not in t for t in toks)
    for bad in ("", "Z:\\a  b.inf", " Z:\\a.inf", "Z:\\a.inf ", 'Z:\\"a".inf', "Z:\\a\tb.inf", "Z:\\a\nb"):
        with pytest.raises(ValueError):
            formats.rundll32_path_tokens(bad)


# --------------------------------------------------------------------------- #
# .reg preview
# --------------------------------------------------------------------------- #
REG4 = (
    "REGEDIT4\r\n"
    "\r\n"
    "; a comment [-HKEY_IGNORED]\r\n"
    "[HKEY_CURRENT_USER\\Software\\Vendor\\App]\r\n"
    '"Name"="Value"\r\n'
    '"Blob"=hex:01,02,03,\\\r\n'
    "  04,05\r\n"
    '"Old"=-\r\n'
    '@=-\r\n'
    '"Quoted \\"x\\""=-\r\n'
    "[-HKEY_LOCAL_MACHINE\\Software\\Vendor\\Old]\r\n"
    '"IgnoredInDeletedKey"=-\r\n'
    "[hkey_current_user\\software\\vendor\\app]\r\n"
    '"Again"=dword:00000001\r\n'
)


def test_reg_preview_regedit4_ansi(tmp_path: Path):
    f = tmp_path / "settings.reg"
    f.write_bytes(REG4.encode("cp1252"))
    p = formats.reg_preview(f)
    assert p["header"] == "REGEDIT4" and p["valid"] is True and p["encoding"] == "ansi"
    assert p["adds"] == ["HKEY_CURRENT_USER\\Software\\Vendor\\App"]  # case-insensitive dedupe
    assert p["deleted_keys"] == ["HKEY_LOCAL_MACHINE\\Software\\Vendor\\Old"]
    assert p["deleted_values"] == ["HKEY_CURRENT_USER\\Software\\Vendor\\App\\Old",
                                   "HKEY_CURRENT_USER\\Software\\Vendor\\App\\(Default)",
                                   'HKEY_CURRENT_USER\\Software\\Vendor\\App\\Quoted "x"']
    assert p["value_count"] == 3  # Name, Blob (continued), Again
    assert p["truncated"] is False
    json.dumps(p)


def test_reg_preview_utf16_v5(tmp_path: Path):
    text = ("Windows Registry Editor Version 5.00\r\n\r\n"
            "[HKEY_CURRENT_USER\\Software\\Ünïcode]\r\n\"K\"=\"v\"\r\n[-HKEY_CURRENT_USER\\Software\\Gone]\r\n")
    f = tmp_path / "u.reg"
    f.write_bytes(codecs.BOM_UTF16_LE + text.encode("utf-16-le"))
    p = formats.reg_preview(f)
    assert p["header"] == "Windows Registry Editor Version 5.00"
    assert p["encoding"] == "utf-16-le"
    assert p["adds"] == ["HKEY_CURRENT_USER\\Software\\Ünïcode"]
    assert p["deleted_keys"] == ["HKEY_CURRENT_USER\\Software\\Gone"]


def test_reg_preview_utf8_bom_and_invalid(tmp_path: Path):
    f = tmp_path / "b.reg"
    f.write_bytes(codecs.BOM_UTF8 + b"Windows Registry Editor Version 5.00\n[HKEY_CLASSES_ROOT\\.x]\n@=\"y\"\n")
    p = formats.reg_preview(f)
    assert p["valid"] and p["encoding"] == "utf-8" and p["adds"] == ["HKEY_CLASSES_ROOT\\.x"]
    bad = tmp_path / "bad.reg"
    bad.write_text("[HKEY_CURRENT_USER\\x]\n\"a\"=\"b\"\n", encoding="ascii")
    q = formats.reg_preview(bad)
    assert q["header"] == "" and q["valid"] is False
    empty = tmp_path / "empty.reg"
    empty.write_bytes(b"")
    assert formats.reg_preview(empty)["valid"] is False
    with pytest.raises(OSError):
        formats.reg_preview(tmp_path / "missing.reg")


def test_reg_preview_truncates_huge_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(formats, "_TEXT_LIMIT", 64)
    f = tmp_path / "big.reg"
    f.write_text("REGEDIT4\n\n" + "[HKEY_CURRENT_USER\\k]\n" * 20, encoding="ascii")
    p = formats.reg_preview(f)
    assert p["truncated"] is True and p["valid"]


def test_reg_confirm_text_lists_deletions():
    preview = {"adds": [f"HKCU\\K{i}" for i in range(15)], "deleted_keys": ["HKLM\\Software\\Victim"],
               "deleted_values": ["HKCU\\A\\B"], "truncated": True}
    text = formats.reg_confirm_text("x.reg", preview, limit=10)
    assert "\u201cx.reg\u201d" in text
    assert "DELETES 1 key(s)" in text and "HKLM\\Software\\Victim" in text
    assert "DELETES 1 value(s)" in text and "HKCU\\A\\B" in text
    assert "... and 5 more" in text and "only its beginning" in text
    plain = formats.reg_confirm_text("y.reg", {"adds": ["HKCU\\A"], "deleted_keys": [], "deleted_values": []})
    assert "DELETES" not in plain and "trust" in plain


# --------------------------------------------------------------------------- #
# .url allowlist
# --------------------------------------------------------------------------- #
def _url_file(tmp_path: Path, body: str, name: str = "link.url", encoding: str = "ascii") -> Path:
    f = tmp_path / name
    data = body.encode(encoding)
    if encoding == "utf-16-le":
        data = codecs.BOM_UTF16_LE + data
    f.write_bytes(data)
    return f


@pytest.mark.parametrize("url,reason", [
    ("https://www.example.org/page?x=1", "web link"),
    ("http://example.org", "web link"),
    ("HTTPS://EXAMPLE.ORG/", "web link"),
    ("mailto:someone@example.org", "e-mail link"),
    ("ftp://ftp.example.org/pub/", "FTP link"),
])
def test_url_allowed(tmp_path: Path, url: str, reason: str):
    f = _url_file(tmp_path, f"[InternetShortcut]\r\nURL={url}\r\nIconIndex=0\r\n")
    assert formats.parse_url_shortcut(f) == (url, True, reason)


@pytest.mark.parametrize("url,needle", [
    ("javascript:alert(1)", "script code"),
    ("JavaScript:alert(1)", "script code"),
    ("vbscript:msgbox(1)", "script code"),
    ("file:///C:/Windows/System32/calc.exe", "file:"),
    ("file://server/share/x.exe", "file:"),
    ("data:text/html;base64,PHNjcmlwdD4=", "data:"),
    ("steam://rungameid/570", "Steam"),
    ("ms-settings:privacy", "ms-settings:"),
    ("\\\\server\\share\\evil.exe", "not a valid web link"),
    ("C:\\Windows\\notepad.exe", "only opens"),
    ("http:///nohost", "no valid site name"),
    ("mailto:", "no address"),
    ("https://exa\x07mple.org", "control characters"),
])
def test_url_refused(tmp_path: Path, url: str, needle: str):
    f = _url_file(tmp_path, f"[InternetShortcut]\nURL={url}\n", encoding="latin-1")
    got_url, allowed, reason = formats.parse_url_shortcut(f)
    assert allowed is False
    assert needle.lower() in reason.lower(), reason


def test_url_edge_cases(tmp_path: Path):
    none = _url_file(tmp_path, "[InternetShortcut]\nIconFile=x.ico\n", name="a.url")
    assert formats.parse_url_shortcut(none)[1:] == (False, "This Internet shortcut has no web address (URL=) in it.")
    other_section = _url_file(tmp_path, "[Other]\nURL=https://x.org\n", name="b.url")
    assert formats.parse_url_shortcut(other_section)[1] is False
    wide = _url_file(tmp_path, "[{000214A0-0000-0000-C000-000000000046}]\nProp3=19,2\n"
                               "[InternetShortcut]\nURL=https://example.org/\n", name="c.url", encoding="utf-16-le")
    assert formats.parse_url_shortcut(wide) == ("https://example.org/", True, "web link")
    utf7 = codecs.encode("https://example.org/caf\u00e9", "utf-7").decode("ascii")
    both = _url_file(tmp_path, f"[InternetShortcut]\nURL=https://example.org/caf?\n"
                               f"[InternetShortcut.W]\nURL={utf7}\n", name="d.url")
    assert formats.parse_url_shortcut(both)[0] == "https://example.org/caf\u00e9"
    long = _url_file(tmp_path, "[InternetShortcut]\nURL=https://x.org/" + "a" * 9000 + "\n", name="e.url")
    assert formats.parse_url_shortcut(long)[1] is False
    assert formats.parse_url_shortcut(tmp_path / "missing.url")[1] is False
    garbage = tmp_path / "g.url"
    garbage.write_bytes(os.urandom(512))
    url, allowed, reason = formats.parse_url_shortcut(garbage)
    assert isinstance(reason, str)


# --------------------------------------------------------------------------- #
# .inf
# --------------------------------------------------------------------------- #
SOFTWARE_INF = """; a software INF
[Version]
Signature="$Windows NT$"

[DefaultInstall]
CopyFiles=MyFiles
AddReg=MyReg   ; comment with a.sys mention in a comment is ignored

[MyFiles]
app.dll

[DestinationDirs]
MyFiles=11

[MyReg]
HKLM,Software\\Vendor,Installed,,1

[Strings]
Desc="driver.sys is only text here"
"""

DRIVER_INF = """[Version]
Signature="$WINDOWS NT$"
Class=Net
ClassGuid={4d36e972-e325-11ce-bfc1-08002be10318}
Provider=%V%

[Manufacturer]
%V%=Models,NTamd64

[Models.NTamd64]
%Dev%=Install, PCI\\VEN_8086&DEV_1234

[Install.NT]
CopyFiles=Drv

[Drv]
e1000.sys
"""


@pytest.mark.parametrize("body,kind", [
    (SOFTWARE_INF, "software"),
    ("[Version]\nSignature=\"$Chicago$\"\n[DefaultInstall.NTamd64]\nAddReg=R\n[R]\nHKCU,x,y,,1\n", "software"),
    ("[Version]\n[DefaultInstall]\nAddService=MySvc,,SvcSect\n[SvcSect]\nServiceType=0x10\n"
     "ServiceBinary=%11%\\svc.exe\n", "software"),
    (DRIVER_INF, "driver"),
    ("[Version]\nClass=Printer\n[DefaultInstall]\nAddReg=R\n", "driver"),
    ("[Version]\n[DefaultInstall]\nCopyFiles=F\n[F]\nfoo.sys\n", "driver"),
    ("[Version]\n[DefaultInstall]\nCopyFiles=F\n[DestinationDirs]\nF=12\n[F]\nfoo.dat\n", "driver"),
    ("[Version]\n[DefaultInstall]\n[DefaultInstall.Services]\nAddService=K,,KS\n[KS]\n"
     "ServiceType=%SERVICE_KERNEL_DRIVER%\n[Strings]\nSERVICE_KERNEL_DRIVER=1\n", "driver"),
    ("[Version]\n[SourceDisksFiles]\nmydrv.sys=1\n[DefaultInstall]\n", "driver"),
    ("[Version]\nSignature=\"$Windows NT$\"\n[Other]\nx=1\n", "unknown"),
    ("[Version]\n[DefaultInstall.Services]\nAddService=X\n", "unknown"),
    ("[AutoRun]\nopen=setup.exe\nicon=setup.ico\n", "unknown"),
    ("", "unknown"),
])
def test_inf_kind(tmp_path: Path, body: str, kind: str):
    f = tmp_path / "x.inf"
    f.write_text(body, encoding="cp1252")
    assert formats.inf_kind(f) == kind


def test_inf_details_utf16_and_autorun(tmp_path: Path):
    f = tmp_path / "u.inf"
    f.write_bytes(codecs.BOM_UTF16_LE + DRIVER_INF.encode("utf-16-le"))
    d = formats.inf_details(f)
    assert d["kind"] == "driver"
    assert any("Manufacturer" in r for r in d["reasons"])
    assert "manufacturer" in d["sections"]
    a = tmp_path / "autorun.inf"
    a.write_text("[autorun]\nopen=SETUP.EXE /auto\nlabel=My CD\n", encoding="ascii")
    ad = formats.inf_details(a)
    assert ad["kind"] == "unknown" and ad["autorun"] == {"open": "SETUP.EXE /auto", "label": "My CD"}
    assert formats.inf_kind(tmp_path / "missing.inf") == "unknown"
    garbage = tmp_path / "g.inf"
    garbage.write_bytes(os.urandom(4096))
    assert formats.inf_kind(garbage) in ("software", "driver", "unknown")


# --------------------------------------------------------------------------- #
# .NET Framework in a C:\ drive (ClickOnce)
# --------------------------------------------------------------------------- #
def _prefix(tmp_path: Path, reg_body: str, native: bool) -> Path:
    pfx = tmp_path / "pfx"
    (pfx / "drive_c" / "windows" / "system32").mkdir(parents=True)
    (pfx / "system.reg").write_text("WINE REGISTRY Version 2\n;; All keys relative to \\\\Machine\n\n#arch=win64\n\n"
                                    + reg_body, encoding="utf-8")
    if native:
        (pfx / "drive_c" / "windows" / "system32" / "dfshim.dll").write_bytes(b"MZ")
    return pfx


NDP = "[Software\\\\Microsoft\\\\NET Framework Setup\\\\NDP\\\\v4\\\\Full] 1700000000\n#time=1d\n"


def test_prefix_has_dotnet_framework(tmp_path: Path):
    body = NDP + '"Install"=dword:00000001\n"Release"=dword:00080ff4\n"Version"="4.8.03761"\n\n'
    assert formats.prefix_has_dotnet_framework(_prefix(tmp_path, body, native=True)) is True


def test_prefix_dotnet_needs_native_files(tmp_path: Path):
    body = NDP + '"Release"=dword:00080ff4\n'
    assert formats.prefix_has_dotnet_framework(_prefix(tmp_path, body, native=False)) is False


@pytest.mark.parametrize("body,expected", [
    ("[Software\\\\Wow6432Node\\\\Microsoft\\\\NET Framework Setup\\\\NDP\\\\v4\\\\Full] 1\n"
     '"Release"=dword:0006040e\n', True),
    (NDP + '"Install"=dword:00000001\n', True),
    (NDP + '"Release"=dword:00000000\n', False),
    (NDP + '"Install"=dword:00000000\n', False),
    ("[Software\\\\Microsoft\\\\NET Framework Setup\\\\NDP\\\\v3.5] 1\n\"Install\"=dword:00000001\n", False),
    (NDP.replace("Full", "Client") + '"Release"=dword:00080ff4\n', False),
    ("", False),
])
def test_prefix_dotnet_registry_variants(tmp_path: Path, body: str, expected: bool):
    assert formats.prefix_has_dotnet_framework(_prefix(tmp_path, body, native=True)) is expected


def test_prefix_dotnet_missing_prefix(tmp_path: Path):
    assert formats.prefix_has_dotnet_framework(tmp_path / "nope") is False


# --------------------------------------------------------------------------- #
# .pif
# --------------------------------------------------------------------------- #
def make_pif(program: str = "C:\\GAMES\\KEEN.EXE", params: str = "/nosound", title: str = "Keen") -> bytes:
    buf = bytearray(0x171 + 0x200)
    buf[0x02:0x02 + len(title)] = title.encode("ascii")
    buf[0x24:0x24 + len(program)] = program.encode("ascii")
    buf[0x65:0x65 + 8] = b"C:\\GAMES"
    buf[0xA5:0xA5 + len(params)] = params.encode("ascii")
    buf[0x171:0x171 + 15] = b"MICROSOFT PIFEX"
    return bytes(buf)


def test_parse_pif(tmp_path: Path):
    f = tmp_path / "keen.pif"
    f.write_bytes(make_pif())
    assert formats.parse_pif(f) == {"title": "Keen", "program": "C:\\GAMES\\KEEN.EXE", "workdir": "C:\\GAMES",
                                    "params": "/nosound"}
    g = tmp_path / "raw.pif"
    g.write_bytes(b"\xb4\x09\xcd\x21" * 10)
    assert formats.parse_pif(g) is None
    assert formats.parse_pif(tmp_path / "missing.pif") is None


# --------------------------------------------------------------------------- #
# CLI + honesty scan
# --------------------------------------------------------------------------- #
def test_main_table_and_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    assert formats.main(["--table", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in rows] == [f.id for f in formats.FORMATS]
    f = tmp_path / "x.reg"
    f.write_text("REGEDIT4\n\n[HKEY_CURRENT_USER\\x]\n", encoding="ascii")
    assert formats.main(["--json", "--plan", str(f)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["id"] == "reg" and out["plan"]["handler"] == "regedit"
    t = tmp_path / "notes.txt"
    t.write_text("hello", encoding="ascii")
    assert formats.main([str(t)]) == formats.EXIT_UNSUPPORTED
    assert "unknown" in capsys.readouterr().out


@pytest.mark.parametrize("module", ["formats.py", "dos.py", "diskimage.py", "binfmt.py"])
def test_sources_have_no_evasion_tokens(module: str):
    text = (LIB / module).read_text(encoding="utf-8").lower()
    for tok in FORBIDDEN_TOKENS:
        assert tok not in text, f"{module} contains forbidden token {tok!r}"
    raw = (LIB / module).read_bytes()
    assert b"\r\n" not in raw and not raw.startswith(codecs.BOM_UTF8)
