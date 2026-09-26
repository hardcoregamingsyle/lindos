"""Synthetic MSIX / APPX / bundle / upload / .appinstaller builders for the ``test_msix_*`` tests.

Every fixture is generated here, in memory, from scratch -- nothing is copied from a real (Store)
package.  The other ``test_msix_*`` files load this module by path (``--import-mode=importlib``
means test files are not importable by name).  The tests at the bottom check the builders
themselves (bundle Offset arithmetic, SDK percent-encoding).
"""
from __future__ import annotations

import importlib.util
import io
import struct
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union
from xml.sax.saxutils import escape

NAME = "Contoso.PhotoEditor"
PUBLISHER = "CN=Contoso Software, O=Contoso Ltd, C=US"
VERSION = "1.2.3.0"
BUNDLE_VERSION = "2026.926.1.0"
EXE_REL = "VFS\\ProgramFilesX64\\Contoso App\\Contoso [Beta].exe"
UNSIGNED = "OID.2.25.311729368913984317654407730594956997722=1"
STORE_OID_DER = bytes.fromhex("060a2b0601040182374c0301")

NS_F = "http://schemas.microsoft.com/appx/manifest/foundation/windows10"
NS_B2013 = "http://schemas.microsoft.com/appx/2013/bundle"
NS_B2017 = "http://schemas.microsoft.com/appx/2017/bundle"
NS_B2018 = "http://schemas.microsoft.com/appx/2018/bundle"

PKG_NS_DECL = (
    'xmlns:uap="http://schemas.microsoft.com/appx/manifest/uap/windows10" '
    'xmlns:uap10="http://schemas.microsoft.com/appx/manifest/uap/windows10/10" '
    'xmlns:uap11="http://schemas.microsoft.com/appx/manifest/uap/windows10/11" '
    'xmlns:rescap="http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities" '
    'xmlns:desktop4="http://schemas.microsoft.com/appx/manifest/desktop/windows10/4" '
    'xmlns:previewsecurity="http://schemas.microsoft.com/appx/manifest/preview/windows10/security" '
    'xmlns:previewsecurity2="http://schemas.microsoft.com/appx/manifest/preview/windows10/security/2" '
    'IgnorableNamespaces="uap uap10 uap11 rescap desktop4 previewsecurity previewsecurity2"'
)

CLASSIC = 'uap10:RuntimeBehavior="packagedClassicApp" uap10:TrustLevel="mediumIL"'
FULLTRUST_EP = 'EntryPoint="Windows.FullTrustApplication"'

# MSIX SDK Encoding.cpp percent-encoding table (plus every non-ASCII UTF-8 byte)
SDK_ENCODE = {" ": "%20", "!": "%21", "#": "%23", "$": "%24", "%": "%25", "&": "%26", "'": "%27", "(": "%28",
              ")": "%29", "+": "%2B", ",": "%2C", ";": "%3B", "=": "%3D", "@": "%40", "[": "%5B", "]": "%5D",
              "^": "%5E", "`": "%60", "{": "%7B", "}": "%7D"}


def load(name: str = "lindos_test_msix_builders"):
    """This module, importable from sibling test files (import-mode=importlib)."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def attr(text: str) -> str:
    return escape(text, {'"': "&quot;"})


def encode_name(name: str) -> str:
    """Decoded package path -> ZIP entry name, as MakeAppx writes it."""
    out: List[str] = []
    for ch in name.replace("\\", "/"):
        if ch in SDK_ENCODE:
            out.append(SDK_ENCODE[ch])
        elif ord(ch) < 128:
            out.append(ch)
        else:
            out.extend("%%%02X" % b for b in ch.encode("utf-8"))
    return "".join(out)


def png(tag: str) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + tag.encode("ascii")


def exe(tag: str = "program") -> bytes:
    return b"MZ" + b"\x00" * 62 + tag.encode("ascii")


# --------------------------------------------------------------------------- #
# manifests
# --------------------------------------------------------------------------- #
def app_xml(app_id: str = "App", exe_rel: Optional[str] = EXE_REL, attrs: str = CLASSIC,
            display: str = "Contoso Photo Editor", logo44: str = "Assets\\Square44x44Logo.png",
            logo150: str = "Assets\\Square150x150Logo.png", ve_extra: str = "", inner: str = "") -> str:
    exe_attr = f' Executable="{attr(exe_rel)}"' if exe_rel else ""
    return (f'<Application Id="{app_id}"{exe_attr} {attrs}>'
            f'<uap:VisualElements DisplayName="{attr(display)}" Description="d" BackgroundColor="transparent" '
            f'Square150x150Logo="{attr(logo150)}" Square44x44Logo="{attr(logo44)}"{ve_extra}/>'
            f"{inner}</Application>")


def manifest(*, name: str = NAME, publisher: str = PUBLISHER, version: str = VERSION, arch: Optional[str] = "x64",
             resource_id: Optional[str] = None, display: str = "Contoso Photo Editor",
             pub_display: str = "Contoso Ltd", apps: Optional[Sequence[str]] = None, deps: str = "",
             caps: str = '<rescap:Capability Name="runFullTrust"/>', props_extra: str = "",
             families: Sequence[str] = ("Windows.Desktop",), order: Optional[Sequence[str]] = None,
             bom: bool = False, prolog: str = '<?xml version="1.0" encoding="utf-8"?>') -> bytes:
    arch_attr = f' ProcessorArchitecture="{arch}"' if arch else ""
    rid_attr = f' ResourceId="{resource_id}"' if resource_id is not None else ""
    fam = "".join(f'<TargetDeviceFamily Name="{f}" MinVersion="10.0.17763.0" MaxVersionTested="10.0.22621.0"/>'
                  for f in families)
    sections = {
        "Identity": f'<Identity Name="{attr(name)}" Publisher="{attr(publisher)}" Version="{version}"'
                    f"{arch_attr}{rid_attr}/>",
        "Properties": (f"<Properties><DisplayName>{escape(display)}</DisplayName>"
                       f"<PublisherDisplayName>{escape(pub_display)}</PublisherDisplayName>"
                       f"<Logo>Assets\\StoreLogo.png</Logo>{props_extra}</Properties>"),
        "Resources": '<Resources><Resource Language="en-us"/></Resources>',
        "Dependencies": f"<Dependencies>{fam}{deps}</Dependencies>",
        "Capabilities": f"<Capabilities>{caps}</Capabilities>",
        "Applications": "<Applications>" + "".join(apps if apps is not None else [app_xml()]) + "</Applications>",
    }
    order = order or ("Identity", "Properties", "Resources", "Dependencies", "Capabilities", "Applications")
    body = "".join(sections[k] for k in order)
    text = f'{prolog}<Package xmlns="{NS_F}" {PKG_NS_DECL}>{body}</Package>'
    data = text.encode("utf-8")
    return b"\xef\xbb\xbf" + data if bom else data


def win8_manifest(app_attrs: str = 'Executable="App.exe" EntryPoint="App.App"') -> bytes:
    return (f'<?xml version="1.0" encoding="utf-8"?>'
            f'<Package xmlns="http://schemas.microsoft.com/appx/2010/manifest">'
            f'<Identity Name="{NAME}" Publisher="{attr(PUBLISHER)}" Version="{VERSION}" ProcessorArchitecture="x86"/>'
            f"<Properties><DisplayName>Old App</DisplayName><PublisherDisplayName>Contoso</PublisherDisplayName>"
            f"<Logo>Assets\\Logo.png</Logo></Properties>"
            f'<Prerequisites><OSMinVersion>6.2.1</OSMinVersion><OSMaxVersionTested>6.2.1</OSMaxVersionTested>'
            f"</Prerequisites><Resources><Resource Language=\"en-us\"/></Resources>"
            f'<Applications><Application Id="App" {app_attrs}>'
            f'<VisualElements DisplayName="Old App" Logo="Assets\\Logo.png" SmallLogo="Assets\\SmallLogo.png" '
            f'Description="d" ForegroundText="light" BackgroundColor="#000000"/></Application></Applications>'
            f"</Package>").encode("utf-8")


# --------------------------------------------------------------------------- #
# ZIP writing
# --------------------------------------------------------------------------- #
def add(zf: zipfile.ZipFile, raw_name: str, data: bytes, *, compress: int = zipfile.ZIP_DEFLATED,
        external_attr: Optional[int] = None, create_system: Optional[int] = None) -> zipfile.ZipInfo:
    """Write ``data`` under the *raw* entry name (backslashes, NULs and all are kept verbatim)."""
    zi = zipfile.ZipInfo("placeholder", date_time=(2026, 9, 26, 12, 0, 0))
    zi.filename = raw_name          # set after construction: ZipInfo() would rewrite os.sep / cut at NUL
    zi.compress_type = compress
    if external_attr is not None:
        zi.external_attr = external_attr
    if create_system is not None:
        zi.create_system = create_system
    zf.writestr(zi, data)
    return zf.getinfo(raw_name) if "\x00" not in raw_name else zi


def signature(store: bool = False) -> bytes:
    body = b"\x30\x82\x01\x00" + b"fake pkcs7 signed data" + (STORE_OID_DER if store else b"") + b"\x00" * 16
    return b"PKCX" + body


def desktop_files() -> Dict[str, bytes]:
    """Payload of the reference packagedClassicApp (spaces and '[' in names, VFS, logo variants)."""
    return {
        "VFS/ProgramFilesX64/Contoso App/Contoso [Beta].exe": exe("x64 editor"),
        "VFS/ProgramFilesX64/Contoso App/helper lib.dll": exe("dll"),
        "VFS/ProgramFilesX64/Contoso App/100% done & ready.txt": b"text",
        "VFS/SystemX86/contoso32.dll": exe("sys32"),
        "VFS/Windows/System32/contoso64.dll": exe("sys64"),
        "VFS/AppData/Contoso/settings.ini": b"[settings]\n",
        "Assets/Square44x44Logo.scale-200.png": png("44-scale200"),
        "Assets/Square44x44Logo.scale-400.png": png("44-scale400"),
        "Assets/Square44x44Logo.targetsize-256_altform-unplated.png": png("44-256-unplated"),
        "Assets/Square44x44Logo.targetsize-48.png": png("44-48"),
        "Assets/contrast-high/Square44x44Logo.targetsize-256_altform-unplated.png": png("44-hc"),
        "Assets/Square150x150Logo.scale-200.png": png("150-scale200"),
        "Assets/StoreLogo.png": png("store"),
    }


EntrySpec = Union[Tuple[str, bytes], Tuple[str, bytes, dict]]


def make_package(files: Optional[Dict[str, bytes]] = None, man: Optional[bytes] = None, *, signed: bool = True,
                 store_signer: bool = False, code_integrity: bool = False, compress: int = zipfile.ZIP_DEFLATED,
                 raw_entries: Iterable[EntrySpec] = (), manifest_name: str = "AppxManifest.xml",
                 extra_manifests: Iterable[Tuple[str, bytes]] = ()) -> bytes:
    """An .msix/.appx as bytes.  ``files`` use decoded names (encoded like MakeAppx); ``raw_entries``
    are written verbatim (for hostile names)."""
    files = desktop_files() if files is None else files
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            add(zf, encode_name(name), data, compress=compress)
        for spec in raw_entries:
            raw, data = spec[0], spec[1]
            kwargs = spec[2] if len(spec) > 2 else {}  # type: ignore[misc]
            add(zf, raw, data, **kwargs)
        add(zf, manifest_name, man if man is not None else manifest())
        for extra_name, extra_data in extra_manifests:
            add(zf, extra_name, extra_data)
        add(zf, "AppxBlockMap.xml", b'<?xml version="1.0"?><BlockMap xmlns="http://schemas.microsoft.com/appx/2010/'
                                    b'blockmap" HashMethod="http://www.w3.org/2001/04/xmlenc#sha256"/>')
        add(zf, "[Content_Types].xml", b'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
                                       b'package/2006/content-types"/>')
        if code_integrity:
            add(zf, "AppxMetadata/CodeIntegrity.cat", b"0\x82catalog")
        if signed:
            add(zf, "AppxSignature.p7x", signature(store_signer))
    return buf.getvalue()


def write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def arch_package(arch: str, *, version: str = VERSION, name: str = NAME, publisher: str = PUBLISHER,
                 exe_tag: Optional[str] = None, logos: bool = True) -> bytes:
    """A small desktop package built for ``arch`` (its exe content names the arch)."""
    files = {"VFS/ProgramFilesX64/Contoso App/Contoso [Beta].exe": exe(exe_tag or f"{arch} editor")}
    if logos:
        files["Assets/Square44x44Logo.scale-200.png"] = png(f"{arch}-44")
    return make_package(files, manifest(arch=arch, version=version, name=name, publisher=publisher))


def resource_package(scale: int = 200, *, version: str = VERSION) -> bytes:
    files = {f"Assets/Square44x44Logo.scale-{scale}.png": png(f"res-{scale}"),
             f"Assets/Square150x150Logo.scale-{scale}.png": png(f"res150-{scale}")}
    man = manifest(arch="neutral", resource_id=f"split.scale-{scale}", version=version, apps=[],
                   props_extra="<ResourcePackage>true</ResourcePackage>", caps="")
    return make_package(files, man)


@dataclass
class Inner:
    file_name: str
    data: bytes
    type: Optional[str] = "application"
    version: str = VERSION
    arch: Optional[str] = "x64"
    resource_id: str = ""
    offset: Union[str, int, None] = "auto"     # "auto" = real offset; None = attribute omitted
    size: Union[str, int, None] = "auto"
    compress: int = zipfile.ZIP_STORED
    b5_stub: Optional[bool] = None           # not None -> <b5:Package IsStub="...">
    family: Optional[str] = "Windows.Desktop"
    embed: bool = True                       # False -> flat bundle (package is a sibling file)


def make_bundle(inners: Sequence[Inner], *, name: str = NAME, publisher: str = PUBLISHER,
                version: str = BUNDLE_VERSION, root_ns: str = NS_B2013, schema: str = "5.0",
                signed: bool = True) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        placed: Dict[str, Tuple[int, int]] = {}
        for inner in inners:
            if not inner.embed:
                continue
            enc = encode_name(inner.file_name)
            zi = add(zf, enc, inner.data, compress=inner.compress)
            placed[inner.file_name] = (zi.header_offset + 30 + len(enc.encode("utf-8")) + len(zi.extra),
                                       zi.compress_size)
        packages = []
        for inner in inners:
            real_off, real_size = placed.get(inner.file_name, (None, len(inner.data)))
            attrs = []
            if inner.type is not None:
                attrs.append(f'Type="{inner.type}"')
            attrs.append(f'Version="{inner.version}"')
            if inner.arch is not None:
                attrs.append(f'Architecture="{inner.arch}"')
            if inner.resource_id:
                attrs.append(f'ResourceId="{inner.resource_id}"')
            attrs.append(f'FileName="{attr(inner.file_name)}"')
            offset = real_off if inner.offset == "auto" else inner.offset
            if offset is not None:
                attrs.append(f'Offset="{offset}"')
            size = real_size if inner.size == "auto" else inner.size
            if size is not None:
                attrs.append(f'Size="{size}"')
            tag = "Package"
            if inner.b5_stub is not None:
                tag = "b5:Package"
                attrs.append(f'IsStub="{"true" if inner.b5_stub else "false"}"')
            if inner.type == "application":
                res = '<Resources><Resource Language="EN-US"/></Resources>'
            else:
                res = '<Resources><Resource Scale="200"/></Resources>'
            fam = (f'<b4:Dependencies><b4:TargetDeviceFamily Name="{inner.family}" MinVersion="10.0.17763.0" '
                   f'MaxVersionTested="10.0.22621.0"/></b4:Dependencies>' if inner.family else "")
            packages.append(f"<{tag} {' '.join(attrs)}>{res}{fam}</{tag}>")
        text = (f'<?xml version="1.0" encoding="UTF-8"?>'
                f'<Bundle xmlns="{root_ns}" xmlns:b4="{NS_B2018}" xmlns:b5="http://schemas.microsoft.com/appx/2019/'
                f'bundle" SchemaVersion="{schema}" IgnorableNamespaces="b4 b5">'
                f'<Identity Name="{attr(name)}" Publisher="{attr(publisher)}" Version="{version}"/>'
                f'<Packages>{"".join(packages)}</Packages></Bundle>')
        add(zf, "AppxMetadata/AppxBundleManifest.xml", text.encode("utf-8"))
        add(zf, "AppxBlockMap.xml", b"<BlockMap/>")
        add(zf, "[Content_Types].xml", b"<Types/>")
        if signed:
            add(zf, "AppxSignature.p7x", signature())
    return buf.getvalue()


def standard_bundle(**kw) -> bytes:
    """x86 + x64 + arm64 application packages and one scale-200 resource package (no Type attribute)."""
    return make_bundle([
        Inner("Contoso_x86.msix", arch_package("x86"), arch="x86"),
        Inner("Contoso_x64.msix", arch_package("x64"), arch="x64"),
        Inner("Contoso_arm64.msix", arch_package("arm64"), arch="arm64"),
        Inner("Contoso_scale-200.msix", resource_package(200), type=None, arch="neutral",
              resource_id="split.scale-200", family=None),
    ], **kw)


def make_upload(members: Sequence[Tuple[str, bytes]], *, compress: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members:
            add(zf, encode_name(name), data, compress=compress)
    return buf.getvalue()


def make_encrypted(magic: bytes = b"EXBH", full_name: str = "Contoso.PhotoEditor_1.2.3.0_x64__8wekyb3d8bbwe",
                   keys: int = 2, name_bytes: Optional[int] = None, trailer: bytes = b"") -> bytes:
    """EAPPX-like header: fixed fields, key-ID array, then WORD chars + WORD bytes + UTF-16LE full name."""
    head = magic + struct.pack("<HHQ", 1, 0, 0x1234) + b"\x00" * 12
    head += struct.pack("<H", keys) + b"\xab" * (32 * keys)
    encoded = full_name.encode("utf-16-le")
    head += struct.pack("<HH", len(full_name), len(encoded) if name_bytes is None else name_bytes) + encoded
    return head + (trailer or bytes(range(256)) * 8)


def appinstaller(*, ns: str = "http://schemas.microsoft.com/appx/appinstaller/2017/2", main: str = "MainBundle",
                 name: str = NAME, publisher: str = PUBLISHER, version: str = BUNDLE_VERSION,
                 uri: str = "https://downloads.contoso.example/app/Contoso.msixbundle", arch: Optional[str] = None,
                 extra_main: str = "", bom: bool = False, doctype: str = "") -> bytes:
    arch_attr = f' ProcessorArchitecture="{arch}"' if arch else ""
    text = (f'<?xml version="1.0" encoding="utf-8"?>{doctype}'
            f'<AppInstaller xmlns="{ns}" Version="1.0.0.0" Uri="https://downloads.contoso.example/app/x.appinstaller">'
            f'<{main} Name="{attr(name)}" Publisher="{attr(publisher)}" Version="{version}"{arch_attr} '
            f'Uri="{attr(uri)}"/>{extra_main}'
            f'<UpdateSettings><OnLaunch HoursBetweenUpdateChecks="0"/></UpdateSettings></AppInstaller>')
    data = text.encode("utf-8")
    return b"\xef\xbb\xbf" + data if bom else data


# --------------------------------------------------------------------------- #
# builder self-tests
# --------------------------------------------------------------------------- #
def test_encode_name_follows_sdk_table() -> None:
    assert encode_name("Contoso App/Contoso [Beta].exe") == "Contoso%20App/Contoso%20%5BBeta%5D.exe"
    assert encode_name("a\\b%c") == "a/b%25c"
    assert encode_name("café.txt") == "caf%C3%A9.txt"


def test_bundle_offsets_point_at_stored_inner_data() -> None:
    import re

    data = standard_bundle()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        text = zf.read("AppxMetadata/AppxBundleManifest.xml").decode("utf-8")
        for m in re.finditer(r'FileName="([^"]+)" Offset="(\d+)" Size="(\d+)"', text):
            fname, offset, size = m.group(1), int(m.group(2)), int(m.group(3))
            zi = zf.getinfo(encode_name(fname))
            lfh = data[zi.header_offset:zi.header_offset + 30]
            n, x = struct.unpack("<HH", lfh[26:30])
            assert offset == zi.header_offset + 30 + n + x
            assert data[offset:offset + size] == zf.read(zi)


def test_load_returns_module() -> None:
    mod = load()
    assert mod.encode_name(" ") == "%20"
