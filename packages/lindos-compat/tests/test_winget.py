"""lindos_compat.winget: download (hash verification, HTTPS-only), zip extraction (zip-slip safe),
the end-to-end install() flow (dry-run/agreements/dependencies/portable/ledger), list_installed,
and the small honest-reporting helpers (SPEC-WINDOWS §28.10, §27). No network, no Wine, no root:
every network and process call is injected."""
from __future__ import annotations

import hashlib
import io
import json
import struct
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from lindos_compat import winget

pytestmark = pytest.mark.usefixtures("home")


# --------------------------------------------------------------------------- #
# a fake streaming response for download()
# --------------------------------------------------------------------------- #
class FakeStream:
    def __init__(self, body: bytes, *, url: Optional[str] = None, status: int = 200,
                headers: Optional[Dict[str, str]] = None) -> None:
        self._buf = io.BytesIO(body)
        self.url = url
        self.status = status
        self.headers = dict(headers or {})
        self.closed = False

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def geturl(self) -> Optional[str]:
        return self.url

    def close(self) -> None:
        self.closed = True


def _installer(url: str, body: bytes, *, base_type: str = "exe") -> Dict[str, Any]:
    return {"InstallerUrl": url, "InstallerSha256": hashlib.sha256(body).hexdigest(), "BaseInstallerType": base_type}


# --------------------------------------------------------------------------- #
# download()
# --------------------------------------------------------------------------- #
def test_download_success_verifies_and_renames(tmp_path: Path) -> None:
    body = b"installer-bytes-content"
    inst = _installer("https://pub.test/Setup.exe", body)

    def fetch_stream(url: str) -> FakeStream:
        assert url == inst["InstallerUrl"]
        return FakeStream(body, url=url)

    path = winget.download(inst, tmp_path, fetch_stream=fetch_stream)
    assert path.read_bytes() == body
    assert path.suffix == ".exe"
    assert not list(tmp_path.glob("*.part"))


def test_download_hash_mismatch_deletes_and_reports_both_hashes(tmp_path: Path) -> None:
    body = b"actual bytes on the wire"
    wrong = "a" * 64
    inst = {"InstallerUrl": "https://pub.test/a.exe", "InstallerSha256": wrong}
    dest = tmp_path / "dl"
    with pytest.raises(winget.WingetHashError) as ei:
        winget.download(inst, dest, fetch_stream=lambda u: FakeStream(body, url=u))
    assert wrong in str(ei.value)
    assert hashlib.sha256(body).hexdigest() in str(ei.value)
    assert not list(dest.glob("*"))  # the .part file was deleted, nothing left behind


def test_download_refuses_plain_http(tmp_path: Path) -> None:
    inst = {"InstallerUrl": "http://pub.test/a.exe", "InstallerSha256": "a" * 64}
    with pytest.raises(winget.WingetUnsupported, match="refuses"):
        winget.download(inst, tmp_path, fetch_stream=lambda u: FakeStream(b"x", url=u))


def test_download_refuses_missing_hash(tmp_path: Path) -> None:
    inst = {"InstallerUrl": "https://pub.test/a.exe"}
    with pytest.raises(winget.WingetError, match="SHA-256"):
        winget.download(inst, tmp_path, fetch_stream=lambda u: FakeStream(b"x", url=u))


def test_download_refuses_non_https_redirect(tmp_path: Path) -> None:
    inst = {"InstallerUrl": "https://pub.test/a.exe", "InstallerSha256": "a" * 64}
    with pytest.raises(winget.WingetUnsupported, match="HTTPS"):
        winget.download(inst, tmp_path, fetch_stream=lambda u: FakeStream(b"data", url="http://evil.test/a.exe"))


def test_download_empty_body_refused(tmp_path: Path) -> None:
    inst = {"InstallerUrl": "https://pub.test/a.exe", "InstallerSha256": hashlib.sha256(b"").hexdigest()}
    with pytest.raises(winget.WingetError, match="empty"):
        winget.download(inst, tmp_path, fetch_stream=lambda u: FakeStream(b"", url=u))


def test_download_over_max_bytes_refused(tmp_path: Path) -> None:
    body = b"x" * 1000
    inst = _installer("https://pub.test/a.exe", body)
    with pytest.raises(winget.WingetError, match="larger"):
        winget.download(inst, tmp_path, fetch_stream=lambda u: FakeStream(body, url=u), max_bytes=100)


# --------------------------------------------------------------------------- #
# zip extraction (zip-slip safe)
# --------------------------------------------------------------------------- #
def _make_zip(path: Path, entries: Dict[str, bytes], *, compress: bool = False) -> Path:
    ctype = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data, compress_type=ctype)
    return path


def test_extract_zip_normal(tmp_path: Path) -> None:
    archive = _make_zip(tmp_path / "a.zip", {"app/run.exe": b"EXE", "app/readme.txt": b"hi"})
    dest = tmp_path / "out"
    files = winget._extract_zip(archive, dest)
    assert (dest / "app" / "run.exe").read_bytes() == b"EXE"
    assert sorted(files) == [("app", "readme.txt"), ("app", "run.exe")]


@pytest.mark.parametrize("bad_name", ["/etc/passwd", "../../evil.txt", "a/../../evil.txt", "C:/evil.txt",
                                      "a\\..\\..\\evil.txt"])
def test_extract_zip_rejects_path_traversal(tmp_path: Path, bad_name: str) -> None:
    archive = _make_zip(tmp_path / "a.zip", {bad_name: b"x"})
    with pytest.raises(winget.WingetError):
        winget._extract_zip(archive, tmp_path / "out")


def test_extract_zip_rejects_case_insensitive_duplicates(tmp_path: Path) -> None:
    archive = _make_zip(tmp_path / "a.zip", {"App.exe": b"1", "app.exe": b"2"})
    with pytest.raises(winget.WingetError, match="upper/lower"):
        winget._extract_zip(archive, tmp_path / "out")


def test_extract_zip_rejects_symlink(tmp_path: Path) -> None:
    path = tmp_path / "a.zip"
    zi = zipfile.ZipInfo("link.exe")
    zi.external_attr = (0o120777 << 16)  # S_IFLNK
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(zi, "/etc/passwd")
    with pytest.raises(winget.WingetError, match="symbolic link"):
        winget._extract_zip(path, tmp_path / "out")


def test_extract_zip_rejects_password_protected(tmp_path: Path) -> None:
    path = tmp_path / "a.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("run.exe", b"data")
    raw = bytearray(path.read_bytes())
    lfh = raw.find(struct.pack("<I", 0x04034B50))
    cdh = raw.find(struct.pack("<I", 0x02014B50))
    assert lfh != -1 and cdh != -1
    raw[lfh + 6] |= 0x1   # local file header general-purpose flag: bit0 = encrypted
    raw[cdh + 8] |= 0x1   # central directory record: same field, different offset
    path.write_bytes(bytes(raw))
    with pytest.raises(winget.WingetError, match="password"):
        winget._extract_zip(path, tmp_path / "out")


def test_extract_zip_rejects_too_many_files(tmp_path: Path) -> None:
    archive = _make_zip(tmp_path / "a.zip", {"a.exe": b"1", "b.exe": b"2", "c.exe": b"3"})
    with pytest.raises(winget.WingetError, match="entries"):
        winget._extract_zip(archive, tmp_path / "out", max_files=2)


def test_extract_zip_rejects_over_total_size(tmp_path: Path) -> None:
    archive = _make_zip(tmp_path / "a.zip", {"a.exe": b"x" * 1000})
    with pytest.raises(winget.WingetError, match="bytes"):
        winget._extract_zip(archive, tmp_path / "out", max_bytes=100)


def test_extract_zip_rejects_suspicious_compression_ratio(tmp_path: Path) -> None:
    archive = _make_zip(tmp_path / "a.zip", {"bomb.bin": b"\x00" * 3_000_000}, compress=True)
    with pytest.raises(winget.WingetError, match="zip bomb"):
        winget._extract_zip(archive, tmp_path / "out")
    # the same archive, with a generous ratio budget, extracts fine
    files = winget._extract_zip(archive, tmp_path / "out2", max_ratio=1_000_000)
    assert files == [("bomb.bin",)]


def test_find_ci_case_insensitive_lookup(tmp_path: Path) -> None:
    # on a case-insensitive filesystem (default on Windows) the fast .exists() path can return the
    # requested casing rather than the on-disk one, so check identity (same file), not the string.
    (tmp_path / "App" / "Sub").mkdir(parents=True)
    (tmp_path / "App" / "Sub" / "Run.EXE").write_bytes(b"x")
    found = winget._find_ci(tmp_path, "app\\sub\\run.exe")
    assert found is not None and found.name.casefold() == "run.exe" and found.read_bytes() == b"x"
    assert winget._find_ci(tmp_path, "nope\\missing.exe") is None


# --------------------------------------------------------------------------- #
# install(): dry-run / agreement gate / cancellation
# --------------------------------------------------------------------------- #
def _msi_manifest(**overrides: Any) -> Dict[str, Any]:
    body = overrides.pop("_body", b"msi-bytes")
    manifest: Dict[str, Any] = {
        "PackageIdentifier": "Test.Package", "PackageVersion": "1.2.3", "ManifestType": "merged",
        "PackageName": "Test Package", "Publisher": "Test Publisher", "License": "MIT",
        "Installers": [{"Architecture": "x64", "InstallerType": "msi", "InstallerUrl": "https://pub.test/setup.msi",
                        "InstallerSha256": hashlib.sha256(body).hexdigest()}],
    }
    manifest.update(overrides)
    return manifest, body


def _fetch_stream_for(bodies: Dict[str, bytes]) -> Callable[[str], FakeStream]:
    def fetch_stream(url: str) -> FakeStream:
        return FakeStream(bodies[url], url=url)
    return fetch_stream


def test_install_dry_run_does_not_download_or_run(tmp_path: Path) -> None:
    manifest, body = _msi_manifest()
    calls: List[Any] = []
    res = winget.install("Test.Package", manifest=manifest, dry_run=True, cache_dir=tmp_path,
                         fetch_stream=lambda u: calls.append(u) or FakeStream(body, url=u),
                         run_lindos=lambda argv, env: calls.append(argv) or 0)
    assert res["status"] == "dry-run" and res["ok"] is True
    assert not calls


def test_install_needs_agreement_without_accept_or_confirm(tmp_path: Path) -> None:
    manifest, body = _msi_manifest()
    res = winget.install("Test.Package", manifest=manifest, cache_dir=tmp_path,
                         fetch_stream=_fetch_stream_for({"https://pub.test/setup.msi": body}))
    assert res["status"] == "needs-agreement" and res["ok"] is False


def test_install_cancelled_when_confirm_declines(tmp_path: Path) -> None:
    manifest, body = _msi_manifest()
    res = winget.install("Test.Package", manifest=manifest, cache_dir=tmp_path, confirm=lambda q, d=False: False,
                         fetch_stream=_fetch_stream_for({"https://pub.test/setup.msi": body}))
    assert res["status"] == "cancelled" and res["ok"] is False


# --------------------------------------------------------------------------- #
# install(): the real path (msi via a fake lindos-run), exit codes, the ledger
# --------------------------------------------------------------------------- #
def _run_lindos_writing(exit_code: int, rc: int = 0) -> Callable[[List[str], Dict[str, str]], int]:
    calls: List[Any] = []

    def run_lindos(argv: List[str], env: Dict[str, str]) -> int:
        calls.append((list(argv), dict(env)))
        result_file = env.get("LINDOS_RUN_RESULT")
        if result_file:
            Path(result_file).write_text(json.dumps({"exit_code": exit_code}), encoding="utf-8")
        return rc

    run_lindos.calls = calls  # type: ignore[attr-defined]
    return run_lindos


def test_install_msi_success_records_ledger(tmp_path: Path, fake_core: Any) -> None:
    manifest, body = _msi_manifest()
    run_lindos = _run_lindos_writing(0)
    res = winget.install("Test.Package", manifest=manifest, cache_dir=tmp_path, accept_package_agreements=True,
                         fetch_stream=_fetch_stream_for({"https://pub.test/setup.msi": body}),
                         run_lindos=run_lindos, which=lambda name: None)
    assert res["ok"] is True and res["status"] == "installed"
    assert run_lindos.calls  # type: ignore[attr-defined]
    argv, env = run_lindos.calls[0]  # type: ignore[attr-defined]
    # lindos-run itself dispatches .msi to msiexec (formats.py); winget hands it the raw installer
    # file plus tokens (SPEC-WINDOWS §33.1: "winget install" calls "lindos-run --prefix ... <file> <tokens>").
    assert argv[:2] == ["/usr/bin/lindos-run", "--prefix"]
    assert argv[3] == "--" and argv[4].endswith("setup.msi")
    assert "/quiet" in argv and "/norestart" in argv
    installed = winget.list_installed(cache_dir=tmp_path)
    assert any(it["id"] == "Test.Package" and it["version"] == "1.2.3" for it in installed)


def test_install_maps_exit_code_to_restart_outcome(tmp_path: Path, fake_core: Any) -> None:
    manifest, body = _msi_manifest()
    run_lindos = _run_lindos_writing(3010)
    res = winget.install("Test.Package", manifest=manifest, cache_dir=tmp_path, accept_package_agreements=True,
                         fetch_stream=_fetch_stream_for({"https://pub.test/setup.msi": body}), run_lindos=run_lindos)
    assert res["status"] == "installed-restart" and res["ok"] is True


def test_install_maps_cancelled_exit_code(tmp_path: Path, fake_core: Any) -> None:
    manifest, body = _msi_manifest()
    run_lindos = _run_lindos_writing(1602)
    res = winget.install("Test.Package", manifest=manifest, cache_dir=tmp_path, accept_package_agreements=True,
                         fetch_stream=_fetch_stream_for({"https://pub.test/setup.msi": body}), run_lindos=run_lindos)
    assert res["status"] == "cancelled" and res["ok"] is False


def test_install_reports_unsupported_when_lindos_run_explains(tmp_path: Path, fake_core: Any) -> None:
    manifest, body = _msi_manifest()

    def run_lindos(argv: List[str], env: Dict[str, str]) -> int:
        return 3  # lindos-run's EXIT_UNSUPPORTED, and no result file written

    res = winget.install("Test.Package", manifest=manifest, cache_dir=tmp_path, accept_package_agreements=True,
                         fetch_stream=_fetch_stream_for({"https://pub.test/setup.msi": body}), run_lindos=run_lindos)
    assert res["status"] == "unsupported" and res["ok"] is False


def test_install_records_already_installed_note_on_reinstall(tmp_path: Path, fake_core: Any) -> None:
    manifest, body = _msi_manifest()
    run_lindos = _run_lindos_writing(0)
    kw = dict(manifest=manifest, cache_dir=tmp_path, accept_package_agreements=True,
             fetch_stream=_fetch_stream_for({"https://pub.test/setup.msi": body}), run_lindos=run_lindos)
    winget.install("Test.Package", **kw)
    res2 = winget.install("Test.Package", **kw)
    assert any("already installed" in n for n in res2["notes"])


# --------------------------------------------------------------------------- #
# install(): dependencies
# --------------------------------------------------------------------------- #
def test_install_dependency_declined_is_skipped_but_main_still_installs(tmp_path: Path, fake_core: Any) -> None:
    manifest, body = _msi_manifest(Dependencies={"PackageDependencies": [{"PackageIdentifier": "Some.Dep"}]})
    said: List[str] = []

    def confirm(question: str, default: bool = False) -> bool:
        return "needs Some.Dep" not in question  # decline the dependency, accept everything else

    run_lindos = _run_lindos_writing(0)
    res = winget.install("Test.Package", manifest=manifest, cache_dir=tmp_path, confirm=confirm,
                         out=said.append, fetch_stream=_fetch_stream_for({"https://pub.test/setup.msi": body}),
                         run_lindos=run_lindos)
    assert res["ok"] is True and res["status"] == "installed"
    assert any("Skipping Some.Dep" in line for line in said)
    assert res["dependency_results"] == []


def test_install_dependency_loop_is_not_reinstalled() -> None:
    # a dependency chain that loops back to the package itself must not recurse forever.
    manifest, body = _msi_manifest(PackageIdentifier="A.A",
                                   Dependencies={"PackageDependencies": [{"PackageIdentifier": "A.A"}]})
    run_lindos = _run_lindos_writing(0)
    res = winget.install("A.A", manifest=manifest, accept_package_agreements=True,
                         fetch_stream=_fetch_stream_for({"https://pub.test/setup.msi": body}), run_lindos=run_lindos)
    assert res["ok"] is True
    assert res["dependency_results"] == []  # the loop was skipped, not installed twice


# --------------------------------------------------------------------------- #
# install(): portable (zip) packages register a Start Menu entry
# --------------------------------------------------------------------------- #
def test_install_portable_extracts_and_registers_app(tmp_path: Path, fake_core: Any, fake_which: Any,
                                                      make_pe: Any) -> None:
    exe_body = make_pe(subsystem=2)  # a GUI program
    zip_bytes = io.BytesIO()
    with zipfile.ZipFile(zip_bytes, "w") as zf:
        zf.writestr("app/Run.exe", exe_body)
    zip_body = zip_bytes.getvalue()
    manifest = {
        "PackageIdentifier": "Portable.App", "PackageVersion": "2.0", "ManifestType": "merged",
        "PackageName": "Portable App",
        "Installers": [{"Architecture": "x64", "InstallerType": "zip", "NestedInstallerType": "portable",
                        "NestedInstallerFiles": [{"RelativeFilePath": "app\\Run.exe", "PortableCommandAlias": "run"}],
                        "InstallerUrl": "https://pub.test/portable.zip",
                        "InstallerSha256": hashlib.sha256(zip_body).hexdigest()}],
    }
    res = winget.install("Portable.App", manifest=manifest, cache_dir=tmp_path, accept_package_agreements=True,
                         which=fake_which(), fetch_stream=_fetch_stream_for({"https://pub.test/portable.zip": zip_body}))
    assert res["ok"] is True and res["status"] == "installed"
    assert res["apps"], "a Start Menu entry should have been registered"
    db_path = winget.expand_user_path(winget.path_const("APPS_DB"))
    db = json.loads(db_path.read_text(encoding="utf-8"))
    rec = db[res["apps"][0]]
    assert rec["source"] == "winget" and rec["winget_id"] == "Portable.App"


# --------------------------------------------------------------------------- #
# list_installed merges the ledger with APPS_DB and reports update availability
# --------------------------------------------------------------------------- #
def test_list_installed_reports_update_available(tmp_path: Path, fake_core: Any) -> None:
    manifest, body = _msi_manifest()
    run_lindos = _run_lindos_writing(0)
    winget.install("Test.Package", manifest=manifest, cache_dir=tmp_path, accept_package_agreements=True,
                  fetch_stream=_fetch_stream_for({"https://pub.test/setup.msi": body}), run_lindos=run_lindos)
    # build a small cached index that reports a newer version, without any network access
    idx_dir = tmp_path / "index"
    idx_dir.mkdir(parents=True, exist_ok=True)
    import sqlite3
    con = sqlite3.connect(str(idx_dir / "index.db"))
    con.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    con.executemany("INSERT INTO metadata VALUES (?, ?)", [("majorVersion", "2"), ("minorVersion", "0")])
    con.execute("CREATE TABLE packages (id TEXT, name TEXT, moniker TEXT, latest_version TEXT, "
               "arp_min_version TEXT, arp_max_version TEXT, hash BLOB)")
    con.execute("INSERT INTO packages (id, name, moniker, latest_version) "
               "VALUES ('Test.Package', 'Test Package', '', '9.9.9')")
    con.commit()
    con.close()
    items = winget.list_installed(cache_dir=tmp_path)
    rec = next(it for it in items if it["id"] == "Test.Package")
    assert rec["latest"] == "9.9.9" and rec["update_available"] is True


def test_list_installed_empty_when_nothing_installed(tmp_path: Path) -> None:
    assert winget.list_installed(cache_dir=tmp_path) == []


# --------------------------------------------------------------------------- #
# honest-reporting helpers
# --------------------------------------------------------------------------- #
def test_format_plan_mentions_package_and_source() -> None:
    manifest, body = _msi_manifest(License="MIT", LicenseUrl="https://pub.test/license")
    inst = winget.select_installer(manifest)
    from lindos_compat.winget import _installer_summary, _manifest_summary, _dependencies, installer_notes
    plan = {
        **_manifest_summary(manifest), "installer": _installer_summary(inst), "prefix": "test-package",
        "notes": installer_notes(manifest, inst), "dependencies": _dependencies(inst),
    }
    text = winget.format_plan(plan)
    assert "Test Package" in text and "1.2.3" in text and "MIT" in text


def test_installer_notes_warn_about_elevation_and_msix() -> None:
    inst = {"EffectiveInstallerType": "msix", "ElevationRequirement": "elevationRequired",
            "InstallerSwitches": {}, "Scope": "", "MinimumOSVersion": "", "ArchiveBinariesDependOnPath": "false"}
    notes = winget.installer_notes({}, inst)
    assert any("administrator rights" in n for n in notes)
    assert any("MSIX" in n for n in notes)


def test_system_locale_reads_lang_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_MESSAGES", raising=False)
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    assert winget.system_locale() == "de-DE"
    monkeypatch.setenv("LANG", "C")
    assert winget.system_locale() == "en-US"


def test_refuse_store_id_message_offers_alternatives() -> None:
    with pytest.raises(winget.WingetUnsupported, match="apps.microsoft.com"):
        winget.install("9NBLGGH4NNS1")
