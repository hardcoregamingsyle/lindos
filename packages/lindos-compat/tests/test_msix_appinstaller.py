"""msix.py: .appinstaller parsing (never downloads), PublisherId vectors and the small launch helpers."""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import socket
import sys
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
NS_2017 = "http://schemas.microsoft.com/appx/appinstaller/2017"


@pytest.fixture(autouse=True)
def _no_network(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in this module may open a connection."""
    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def _ai(tmp_path: Path, fname: str = "Contoso.appinstaller", **kw) -> Path:
    return B.write(tmp_path / fname, B.appinstaller(**kw))


# --------------------------------------------------------------------------- #
# (g) .appinstaller
# --------------------------------------------------------------------------- #
def test_main_bundle(tmp_path: Path) -> None:
    ai = msix.parse_appinstaller(_ai(tmp_path))
    assert {k: ai[k] for k in ("uri", "kind", "name", "version", "publisher", "host")} == {
        "uri": "https://downloads.contoso.example/app/Contoso.msixbundle", "kind": "bundle", "name": B.NAME,
        "version": B.BUNDLE_VERSION, "publisher": B.PUBLISHER, "host": "downloads.contoso.example"}
    assert ai["scheme"] == "https" and ai["arch"] == "neutral" and ai["resource_id"] == "~"
    assert ai["appinstaller_uri"].endswith("x.appinstaller")
    assert msix.appinstaller_refusal(ai) == ""


@pytest.mark.parametrize("ns", [NS_2017, NS_2017 + "/2", "http://schemas.microsoft.com/appx/appinstaller/2018",
                                "http://schemas.microsoft.com/appx/appinstaller/2021"])
def test_main_package_in_every_namespace(tmp_path: Path, ns: str) -> None:
    ai = msix.parse_appinstaller(_ai(tmp_path, ns=ns, main="MainPackage", version=B.VERSION, arch="x64",
                                     uri="https://cdn.contoso.example/Contoso_x64.msix"))
    assert (ai["kind"], ai["arch"], ai["version"], ai["host"]) == ("package", "x64", B.VERSION, "cdn.contoso.example")


def test_bom_and_classify(tmp_path: Path) -> None:
    path = _ai(tmp_path, bom=True)
    assert msix.classify(path) == "appinstaller"
    assert msix.parse_appinstaller(path)["name"] == B.NAME
    commented = B.write(tmp_path / "c.xml", b'<?xml version="1.0"?>\n<!-- hi -->\n' + B.appinstaller()[38:])
    assert msix.classify(commented) == "appinstaller"
    utf16 = B.write(tmp_path / "u.appinstaller", B.appinstaller().decode("utf-8").encode("utf-16"))
    assert msix.classify(utf16) == "appinstaller"
    assert msix.parse_appinstaller(utf16)["kind"] == "bundle"


@pytest.mark.parametrize("kwargs, words", [
    ({"ns": "http://example.invalid/appinstaller"}, "not an App Installer file"),
    ({"extra_main": f'<MainPackage Name="{B.NAME}" Publisher="CN=x" Version="1.0.0.0" Uri="https://a.example/x"/>'},
     "exactly one"),
    ({"main": "OptionalPackages"}, "exactly one"),
    ({"doctype": '<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]>'}, "DOCTYPE"),
    ({"name": "../../evil"}, "invalid name"),
    ({"version": "1.2"}, "version"),
    ({"uri": ""}, "download address"),
    ({"uri": "https://[::1/x"}, "download address"),
])
def test_malformed_appinstaller(tmp_path: Path, kwargs: dict, words: str) -> None:
    with pytest.raises(msix.MsixError, match=words):
        msix.parse_appinstaller(_ai(tmp_path, **kwargs))


@pytest.mark.parametrize("uri, scheme, words", [
    ("http://downloads.contoso.example/x.msixbundle", "http", "secure (https)"),
    ("\\\\fileserver\\apps\\x.msixbundle", "unc", "network share"),
    ("file:///C:/apps/x.msixbundle", "file", "local file"),
    ("ftp://downloads.contoso.example/x.msixbundle", "ftp", "secure (https)"),
])
def test_only_https_is_allowed(tmp_path: Path, uri: str, scheme: str, words: str) -> None:
    path = _ai(tmp_path, uri=uri)
    ai = msix.parse_appinstaller(path)
    assert ai["scheme"] == scheme
    assert words in msix.appinstaller_refusal(ai)
    info = msix.inspect(path)
    assert info.kind == "appinstaller" and info.status == "unsupported" and words in info.reason
    if scheme == "unc":
        assert ai["host"] == "fileserver"


def test_inspect_appinstaller_and_refuse_install(tmp_path: Path) -> None:
    path = _ai(tmp_path)
    info = msix.inspect(path)
    assert info.kind == "appinstaller" and info.status == "partial"
    assert "downloads.contoso.example" in info.reason and "asks before downloading" in info.reason
    assert info.package_full_name == f"{B.NAME}_{B.BUNDLE_VERSION}_neutral_~_{PID}"
    assert any("updates" in w for w in info.warnings)
    with pytest.raises(msix.MsixError, match="only points to a download"):
        msix.install(path, tmp_path / "prefixes" / "app")


def test_downloaded_bundle_must_match(tmp_path: Path) -> None:
    ai = msix.parse_appinstaller(_ai(tmp_path))
    bundle = msix.inspect(B.write(tmp_path / "dl" / "Contoso.msixbundle", B.standard_bundle()))
    msix.check_appinstaller_target(ai, bundle)                    # bundle version matches the MainBundle
    for change in ({"name": "Fabrikam.Other"}, {"publisher": "CN=Mallory"}, {"bundle_version": "2026.1.0.0"}):
        with pytest.raises(msix.MsixError, match="not the one the App Installer file promised"):
            msix.check_appinstaller_target(ai, dataclasses.replace(bundle, **change))


def test_downloaded_package_must_match(tmp_path: Path) -> None:
    ai = msix.parse_appinstaller(_ai(tmp_path, main="MainPackage", version=B.VERSION, arch="x64"))
    info = msix.inspect(B.write(tmp_path / "dl" / "Contoso.msix", B.make_package()))
    msix.check_appinstaller_target(ai, info)
    with pytest.raises(msix.MsixError, match="architecture x86"):
        msix.check_appinstaller_target(ai, dataclasses.replace(info, arch="x86"))
    with pytest.raises(msix.MsixError, match="version"):
        msix.check_appinstaller_target(ai, dataclasses.replace(info, version="9.9.9.9"))


def test_cli_appinstaller(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert msix.main(["appinstaller", str(_ai(tmp_path)), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["host"] == "downloads.contoso.example" and data["refusal"] == ""
    http = _ai(tmp_path, "h.appinstaller", uri="http://downloads.contoso.example/x.msixbundle")
    assert msix.main(["appinstaller", str(http)]) == msix.EXIT_UNSUPPORTED
    assert "https" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# (k) PublisherId
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("publisher, expected", [
    ("CN=Microsoft Corporation, O=Microsoft Corporation, L=Redmond, S=Washington, C=US", "8wekyb3d8bbwe"),
    ("CN=728D3E49-73AE-4BBD-BC7C-5885A138F109", "3v3sf0k6w2rec"),
])
def test_publisher_id_vectors(publisher: str, expected: str) -> None:
    assert msix.publisher_id(publisher) == expected


def test_publisher_id_shape() -> None:
    for pub in ("CN=a", f"CN=Contoso, {B.UNSIGNED}", "CN=\u00c9diteur, O=Soci\u00e9t\u00e9"):
        pid = msix.publisher_id(pub)
        assert len(pid) == 13 and set(pid) <= set("0123456789abcdefghjkmnpqrstvwxyz")
    assert msix.publisher_id("CN=a") != msix.publisher_id("CN=A")   # Publisher compares case-sensitively


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text, argv", [
    ("", []),
    ("a b  c", ["a", "b", "c"]),
    ('"a b" c', ["a b", "c"]),
    ('a\\\\b "c\\" d"', ["a\\\\b", 'c" d']),
    ('x\\\\"y z"', ["x\\y z"]),
    ('"a""b"', ['a"b']),
    ('""', [""]),
    ("C:\\Program Files\\x", ["C:\\Program", "Files\\x"]),
    ('--path="C:\\Program Files\\x" -v', ["--path=C:\\Program Files\\x", "-v"]),
])
def test_split_command_line(text: str, argv: list) -> None:
    assert msix.split_command_line(text) == argv


@pytest.mark.parametrize("text, expected", [
    ("$(package.effectivePath)\\bin", "C:\\Pkg\\bin"),
    ("$(PACKAGE.INSTALLEDPATH)", "C:\\Pkg"),
    ("$(package.mutablePath)", "C:\\Pkg"),
    ("$(system.path)", "C:\\windows\\system32"),
    ("$(windows.path)", "C:\\windows"),
    ("$(env:ProgramFiles)", "C:\\Program Files"),
    ("$(env:APPDATA)", "%APPDATA%"),
    ("costs $$5", "costs $5"),
    ("$(unknown.macro)", "$(unknown.macro)"),
    ("", ""),
])
def test_expand_macros(text: str, expected: str) -> None:
    assert msix.expand_macros(text, "C:\\Pkg") == expected
