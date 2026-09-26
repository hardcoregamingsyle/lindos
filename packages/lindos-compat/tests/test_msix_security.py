"""msix.py: hostile input -- zip-slip, percent-decoding tricks, symlinks, case duplicates, zip bombs,
XML entity attacks, encrypted Store packages, damaged files.  Nothing may be written outside the prefix
and nothing may crash with anything but MsixError (classify() never raises).
"""
from __future__ import annotations

import importlib.util
import stat
import struct
import sys
import zipfile
from pathlib import Path

import pytest

from lindos_compat import msix


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
FULL = f"{B.NAME}_{B.VERSION}_x64__{PID}"


@pytest.fixture(autouse=True)
def _sandbox(home: Path) -> Path:
    return home


@pytest.fixture()
def prefix(tmp_path: Path) -> Path:
    return tmp_path / "work" / "prefixes" / f"{B.NAME}_{PID}"


def _hostile(tmp_path: Path, raw_entries, fname: str = "hostile.msix", **kw) -> Path:
    return B.write(tmp_path / "work" / "in" / fname, B.make_package(raw_entries=raw_entries, **kw))


def _written_outside(tmp_path: Path, prefix: Path) -> list:
    """Every file created under tmp_path except the input folder and the prefix."""
    allowed = (tmp_path / "work" / "in", prefix)
    out = []
    for p in tmp_path.rglob("*"):
        if p.is_file() and not any(a == p or a in p.parents for a in allowed) and "home" not in p.parts:
            out.append(p)
    return out


# --------------------------------------------------------------------------- #
# (e) malicious entry names
# --------------------------------------------------------------------------- #
HOSTILE_NAMES = [
    ("..%2Fevil.dll", "'..'"),
    ("%2E%2E%2Fevil.dll", "'..'"),
    ("%2e%2e/%2e%2e/evil.dll", "'..'"),
    ("%5C..%5Cevil.dll", "absolute"),
    ("Assets%5C..%5C..%5Cevil.dll", "'..'"),
    ("..\\evil.dll", "'..'"),
    ("Assets\\..\\..\\evil.dll", "'..'"),
    ("/tmp/evil.dll", "absolute"),
    ("%2Ftmp%2Fevil.dll", "absolute"),
    ("C:/evil.dll", "drive letter"),
    ("C%3A%5CWindows%5Cevil.dll", "drive letter"),
    ("Assets/C%3Aevil.dll", "reserved character"),
    ("a%00b.dll", "control character"),
    ("evil.dll%0A", "control character"),
    ("CON.txt", "device name"),
    ("Assets/aux", "device name"),
    ("trailing.dot.", "dot or a space"),
    ("trailing%20", "dot or a space"),
    ("a//b.dll", "empty"),
    ("a/./b.dll", "'.'"),
    ("bad%FF.dll", "UTF-8"),
    ("what%3F.dll", "reserved character"),
    ("pipe%7C.dll", "reserved character"),
    ("x" * 300, "too long"),
    ("d/" * 140 + "f.dll", "longer than"),
]


@pytest.mark.parametrize("raw, words", HOSTILE_NAMES)
def test_hostile_entry_names_are_refused(tmp_path: Path, prefix: Path, raw: str, words: str) -> None:
    path = _hostile(tmp_path, [(raw, b"MZ evil")])
    assert msix.classify(path) == "package"
    info = msix.inspect(path)
    assert info.status == "unsupported" and words in info.reason, info.reason
    with pytest.raises(msix.MsixError, match="Unsafe file path|refuses"):
        msix.install(path, prefix)
    assert _written_outside(tmp_path, prefix) == []
    assert not (prefix / "drive_c" / "Program Files" / "WindowsApps" / FULL).exists()


def test_raw_nul_in_entry_name(tmp_path: Path, prefix: Path) -> None:
    path = _hostile(tmp_path, [("evil.dll\x00.png", b"MZ")])
    info = msix.inspect(path)
    assert info.status == "unsupported" and "NUL" in info.reason
    with pytest.raises(msix.MsixError):
        msix.install(path, prefix)


def test_percent_names_are_decoded_exactly_once(tmp_path: Path, prefix: Path) -> None:
    path = _hostile(tmp_path, [("%252E%252E%252Fliteral.txt", b"literal")])
    inst = msix.install(path, prefix)
    assert (inst.install_dir / "%2E%2E%2Fliteral.txt").read_bytes() == b"literal"
    assert _written_outside(tmp_path, prefix) == []


def test_case_duplicates_are_refused(tmp_path: Path, prefix: Path) -> None:
    path = _hostile(tmp_path, [("assets/SQUARE44x44LOGO.SCALE-200.PNG", B.png("dup"))])
    info = msix.inspect(path)
    assert info.status == "unsupported" and "upper/lower case" in info.reason
    with pytest.raises(msix.MsixError, match="upper/lower case"):
        msix.install(path, prefix)


def test_duplicate_manifest_with_other_case_is_refused(tmp_path: Path) -> None:
    path = _hostile(tmp_path, [], extra_manifests=[("appxmanifest.xml", B.manifest(name="Fabrikam.Evil"))])
    info = msix.inspect(path)
    assert info.status == "unsupported" and "upper/lower case" in info.reason


def test_symlink_entries_are_refused(tmp_path: Path, prefix: Path) -> None:
    link = ("link.dll", b"/etc/passwd", {"external_attr": (stat.S_IFLNK | 0o777) << 16, "create_system": 3})
    path = _hostile(tmp_path, [link])
    info = msix.inspect(path)
    assert info.status == "unsupported" and "symbolic link" in info.reason
    with pytest.raises(msix.MsixError, match="symbolic link"):
        msix.install(path, prefix)


def test_file_and_folder_with_the_same_name(tmp_path: Path, prefix: Path) -> None:
    path = _hostile(tmp_path, [("Assets", b"i am a file")])
    with pytest.raises(msix.MsixError, match="both a file and a folder"):
        msix.install(path, prefix)


def test_directory_entries_are_fine_but_not_with_data(tmp_path: Path, prefix: Path) -> None:
    ok = _hostile(tmp_path, [("Empty%20Folder/", b"")], fname="dirs.msix")
    assert msix.install(ok, prefix).apps
    bad = _hostile(tmp_path, [("Folder/", b"data")], fname="baddir.msix")
    assert msix.inspect(bad).status == "unsupported"


def test_unsupported_compression_is_refused(tmp_path: Path) -> None:
    path = _hostile(tmp_path, [("data.bin", b"x" * 1000, {"compress": zipfile.ZIP_BZIP2})])
    info = msix.inspect(path)
    assert info.status == "unsupported" and "compression method" in info.reason


def _set_encrypted_flag(data: bytes, name: bytes) -> bytes:
    """Set general-purpose bit 0 ("encrypted") on one entry's central-directory and local headers."""
    buf = bytearray(data)
    pos = buf.find(b"PK\x01\x02")
    while pos != -1:
        n = struct.unpack_from("<H", buf, pos + 28)[0]
        if bytes(buf[pos + 46:pos + 46 + n]) == name:
            buf[pos + 8] |= 0x1
            lfh = struct.unpack_from("<I", buf, pos + 42)[0]
            buf[lfh + 6] |= 0x1
        pos = buf.find(b"PK\x01\x02", pos + 4)
    return bytes(buf)


def test_password_protected_entries_are_refused(tmp_path: Path) -> None:
    data = B.make_package(raw_entries=[("secret.bin", b"x" * 64)])
    path = B.write(tmp_path / "work" / "in" / "pw.msix", _set_encrypted_flag(data, b"secret.bin"))
    info = msix.inspect(path)
    assert info.status == "unsupported" and "password-protected" in info.reason


# --------------------------------------------------------------------------- #
# size limits
# --------------------------------------------------------------------------- #
def test_ratio_bomb_is_refused(tmp_path: Path, prefix: Path) -> None:
    path = _hostile(tmp_path, [("bomb.bin", b"\x00" * (8 << 20))])
    info = msix.inspect(path)
    assert info.status == "unsupported" and "zip bomb" in info.reason
    with pytest.raises(msix.MsixError, match="zip bomb"):
        msix.install(path, prefix)
    assert not (prefix / "drive_c" / "Program Files" / "WindowsApps" / FULL).exists()


def test_small_compressible_files_are_fine(tmp_path: Path, prefix: Path) -> None:
    path = _hostile(tmp_path, [("zeros.bin", b"\x00" * 200_000)])
    assert msix.install(path, prefix).install_dir.joinpath("zeros.bin").stat().st_size == 200_000


def test_ratio_cap_is_configurable(tmp_path: Path, prefix: Path) -> None:
    path = _hostile(tmp_path, [("bomb.bin", b"\x00" * (2 << 20))])
    with pytest.raises(msix.MsixError, match="zip bomb"):
        msix.install(path, prefix, max_ratio=200)
    assert msix.install(path, prefix, max_ratio=5000).apps


def test_max_bytes_per_file_and_total(tmp_path: Path, prefix: Path) -> None:
    big = _hostile(tmp_path, [("big.bin", bytes(range(256)) * 400)], fname="big.msix")   # 100 KiB, 1 file
    with pytest.raises(msix.MsixError, match="limit"):
        msix.install(big, prefix, max_bytes=50_000)
    many = _hostile(tmp_path, [(f"part{i}.bin", bytes(range(256)) * 40) for i in range(10)], fname="many.msix")
    with pytest.raises(msix.MsixError, match="limit"):
        msix.install(many, prefix, max_bytes=60_000)
    with pytest.raises(msix.MsixError, match="positive"):
        msix.install(many, prefix, max_bytes=0)


# --------------------------------------------------------------------------- #
# XML attacks
# --------------------------------------------------------------------------- #
XXE = (b'<?xml version="1.0"?><!DOCTYPE Package [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
       b'<Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"><Identity Name="&xxe;"/>'
       b"</Package>")
LAUGHS = (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>'
          b"<Package>&lol2;</Package>")


@pytest.mark.parametrize("man", [XXE, LAUGHS])
def test_doctype_in_manifest_is_refused(tmp_path: Path, man: bytes) -> None:
    path = B.write(tmp_path / "x.msix", B.make_package(man=man))
    with pytest.raises(msix.MsixError, match="DOCTYPE"):
        msix.inspect(path)


def test_doctype_in_bundle_manifest_is_refused(tmp_path: Path) -> None:
    data = B.make_bundle([B.Inner("Contoso_x64.msix", B.arch_package("x64"))])
    with zipfile.ZipFile(B.write(tmp_path / "src.zip", data)) as zf:
        text = zf.read("AppxMetadata/AppxBundleManifest.xml")
    evil = text.replace(b"<Bundle ", b'<!DOCTYPE b [<!ENTITY e SYSTEM "http://example.invalid/">]><Bundle ', 1)
    out = tmp_path / "evil.msixbundle"
    with zipfile.ZipFile(tmp_path / "src.zip") as src, zipfile.ZipFile(out, "w") as dst:
        for zi in src.infolist():
            dst.writestr(zi, evil if zi.filename.endswith("AppxBundleManifest.xml") else src.read(zi))
    with pytest.raises(msix.MsixError, match="DOCTYPE"):
        msix.inspect(out)


def test_malformed_manifest_xml(tmp_path: Path) -> None:
    for man in (b"<Package", b"\xff\xfe\x00garbage", b"not xml at all"):
        with pytest.raises(msix.MsixError):
            msix.inspect(B.write(tmp_path / "m.msix", B.make_package(man=man)))


def test_huge_manifest_is_refused(tmp_path: Path) -> None:
    man = B.manifest(props_extra="<Description>" + "x" * (9 << 20) + "</Description>")
    with pytest.raises(msix.MsixError, match="large"):
        msix.inspect(B.write(tmp_path / "huge.msix", B.make_package(man=man)))


# --------------------------------------------------------------------------- #
# (f) encrypted Store packages and GDK packages
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("magic, what", [(b"EXBH", "bundle"), (b"EXPH", "package"), (b"EXSH", "package")])
def test_encrypted_package_is_explained_never_opened(tmp_path: Path, prefix: Path, magic: bytes, what: str) -> None:
    path = B.write(tmp_path / "work" / "in" / "Contoso.eappxbundle", B.make_encrypted(magic))
    assert msix.classify(path) == "encrypted"
    info = msix.inspect(path)
    assert info.kind == "encrypted" and info.status == "unsupported"
    assert "Encrypted Microsoft Store package" in info.reason and "lindos-vm" in info.reason
    assert info.store_signals == ["encrypted"]
    assert (info.name, info.version, info.arch, info.publisher_id) == (B.NAME, "1.2.3.0", "x64", "8wekyb3d8bbwe")
    assert info.package_family_name == f"{B.NAME}_8wekyb3d8bbwe"
    assert any(what in w and "never decrypts" in w for w in info.warnings)
    assert msix.trust_label(info) == "Encrypted Microsoft Store package"
    assert not msix.can_try_anyway(info)
    with pytest.raises(msix.MsixError, match="Encrypted"):
        msix.install(path, prefix)
    assert _written_outside(tmp_path, prefix) == []


def test_encrypted_header_bounds_are_checked(tmp_path: Path) -> None:
    fname = "Contoso.PhotoEditor_1.2.3.0_x64__8wekyb3d8bbwe.eappx"
    lying = B.make_encrypted(b"EXPH", name_bytes=7)                           # byte count doesn't match
    info = msix.inspect(B.write(tmp_path / fname, lying))
    assert info.name == B.NAME and info.package_full_name == fname[:-6]       # from the file name
    for blob in (b"EXPH", b"EXBH" + b"\xff" * 10, b"EXSH" + struct.pack("<HH", 0xFFFF, 0xFFFE) + b"A\x00" * 5,
                 B.make_encrypted(b"EXBH", full_name="not a package name at all!!", keys=1)):
        info = msix.inspect(B.write(tmp_path / "weird.emsix", blob))
        assert info.kind == "encrypted" and info.status == "unsupported" and info.name == ""


def test_msixvc_is_explained(tmp_path: Path, prefix: Path) -> None:
    path = B.write(tmp_path / "Contoso.Game_1.0.0.0_x64__8wekyb3d8bbwe.msixvc", b"\x00\x01opaque" * 100)
    assert msix.classify(path) == "msixvc"
    info = msix.inspect(path)
    assert info.status == "unsupported" and "Game Pass" in info.reason
    assert info.name == "Contoso.Game" and "xbox-gdk-game" in info.store_signals
    with pytest.raises(msix.MsixError, match="Xbox"):
        msix.install(path, prefix)


# --------------------------------------------------------------------------- #
# damaged / foreign files: MsixError from inspect, "unknown" from classify
# --------------------------------------------------------------------------- #
def test_both_manifests_is_corrupt(tmp_path: Path) -> None:
    path = _hostile(tmp_path, [], extra_manifests=[("AppxMetadata/AppxBundleManifest.xml", b"<Bundle/>")])
    assert msix.classify(path) == "unknown"
    with pytest.raises(msix.MsixError, match="both an app manifest and a bundle manifest"):
        msix.inspect(path)


@pytest.mark.parametrize("blob", [b"PK\x03\x04" + b"\x00" * 50, b"PK\x03\x04garbage" * 40, b"", b"MZ\x90\x00",
                                  b"\x00" * 10])
def test_damaged_and_foreign_files(tmp_path: Path, blob: bytes) -> None:
    path = B.write(tmp_path / "x.msix", blob)
    assert msix.classify(path) == "unknown"
    with pytest.raises(msix.MsixError):
        msix.inspect(path)


def test_plain_zip_is_not_a_package(tmp_path: Path) -> None:
    path = B.write(tmp_path / "x.msix", B.make_upload([("readme.txt", b"hello")]))
    assert msix.classify(path) == "unknown"
    with pytest.raises(msix.MsixError, match="no AppxManifest.xml"):
        msix.inspect(path)


def test_classify_never_raises(tmp_path: Path) -> None:
    assert msix.classify(tmp_path / "missing.msix") == "unknown"
    assert msix.classify(tmp_path) == "unknown"
    assert msix.classify(Path("")) == "unknown"
    with pytest.raises(msix.MsixError, match="no such file"):
        msix.inspect(tmp_path / "missing.msix")


def test_truncated_package_is_damaged(tmp_path: Path, prefix: Path) -> None:
    data = B.make_package()
    path = B.write(tmp_path / "work" / "in" / "cut.msix", data[: len(data) // 2])
    assert msix.classify(path) == "unknown"
    with pytest.raises(msix.MsixError):
        msix.install(path, prefix)


def test_corrupted_payload_crc_is_caught(tmp_path: Path, prefix: Path) -> None:
    data = bytearray(B.make_package(files={"payload.bin": b"A" * 5000, "a.exe": B.exe()},
                                    man=B.manifest(apps=[B.app_xml(exe_rel="a.exe")]),
                                    compress=zipfile.ZIP_STORED))
    at = bytes(data).find(b"A" * 5000)
    data[at + 10] = ord("B")
    path = B.write(tmp_path / "work" / "in" / "crc.msix", bytes(data))
    with pytest.raises(msix.MsixError, match="damaged"):
        msix.install(path, prefix)
    wa = prefix / "drive_c" / "Program Files" / "WindowsApps"
    assert not any(wa.iterdir())
