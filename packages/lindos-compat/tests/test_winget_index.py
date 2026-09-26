"""lindos_compat.winget: the MSZIP container decoder, the V2 CDN index (source2.msix ->
Public/index.db -> search/canonical_id), the versionData/manifest hash chain, and the GitHub
fallback (SPEC-WINDOWS §28.10). Everything here uses an injectable ``fetch`` and a synthetic
in-memory index built the same way the winget research digest describes the real one -- no
network access, ever."""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import struct
import zipfile
import zlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from lindos_compat import wingetyaml
from lindos_compat import winget


# --------------------------------------------------------------------------- #
# MSZIP: a synthetic encoder that mirrors decode_mszip's container exactly,
# built from the research digest's decode algorithm (magic, u64 total @8, blocks
# of u32-size + "CK" + raw-deflate from offset 24, with the preset dictionary
# carried across blocks).
# --------------------------------------------------------------------------- #
def _encode_mszip(data: bytes, *, block_size: int = winget.MSZIP_BLOCK) -> bytes:
    out = bytearray()
    out += winget.MSZIP_MAGIC
    out.append(24)                      # header_size
    out += b"\x00\x00"                  # reserved
    out.append(winget.MSZIP_ALGORITHM)  # algorithm = MSZIP
    out += struct.pack("<Q", len(data))  # total uncompressed size
    out += struct.pack("<Q", block_size)  # nominal block size (informational)
    assert len(out) == 24
    pos = 0
    prev = b""
    while True:
        chunk = data[pos:pos + block_size]
        zdict = prev[-winget.MSZIP_BLOCK:] if prev else None
        co = zlib.compressobj(9, zlib.DEFLATED, -15, zdict=zdict) if zdict else zlib.compressobj(9, zlib.DEFLATED, -15)
        comp = co.compress(chunk) + co.flush(zlib.Z_FINISH)
        block = b"CK" + comp
        out += struct.pack("<I", len(block))
        out += block
        prev += chunk
        pos += len(chunk)
        if pos >= len(data):
            break
    return bytes(out)


def test_mszip_round_trip_single_block() -> None:
    data = b"sV: 1.0\nvD:\n- v: '1.0'\n  rP: manifests/t/Test/Package/1.0/abcd\n  s256H: " + b"a" * 64 + b"\n"
    encoded = _encode_mszip(data)
    assert winget.decode_mszip(encoded) == data


def test_mszip_round_trip_multi_block_uses_dictionary_chaining() -> None:
    # >32 KiB of repetitive text so a second block's back-references only resolve if the
    # decoder actually reseeds the zlib dictionary from the previous block's output.
    line = b"- v: '1.0.%d'\n  rP: manifests/t/Test/Package/1.0.%d/abcd\n  s256H: " + b"f" * 64 + b"\n"
    chunks = [line % (i, i) for i in range(1200)]
    data = b"sV: 1.0\nvD:\n" + b"".join(chunks)
    assert len(data) > winget.MSZIP_BLOCK  # actually spans multiple blocks
    encoded = _encode_mszip(data)
    assert winget.decode_mszip(encoded) == data


def test_mszip_bad_magic_rejected() -> None:
    with pytest.raises(winget.WingetLayoutError):
        winget.decode_mszip(b"NOPE" + b"\x00" * 40)


def test_mszip_wrong_algorithm_rejected() -> None:
    encoded = bytearray(_encode_mszip(b"hello world"))
    encoded[7] = 9  # not MSZIP_ALGORITHM
    with pytest.raises(winget.WingetLayoutError, match="algorithm"):
        winget.decode_mszip(bytes(encoded))


def test_mszip_truncated_block_rejected() -> None:
    encoded = _encode_mszip(b"hello world, this is some test data")
    with pytest.raises(winget.WingetLayoutError):
        winget.decode_mszip(encoded[:-3])


def test_mszip_size_mismatch_rejected() -> None:
    encoded = bytearray(_encode_mszip(b"hello world"))
    struct.pack_into("<Q", encoded, 8, 999999)  # claim a much larger total size
    with pytest.raises(winget.WingetLayoutError, match="shorter"):
        winget.decode_mszip(bytes(encoded))


def test_mszip_oversized_rejected() -> None:
    encoded = bytearray(_encode_mszip(b"hello world"))
    struct.pack_into("<Q", encoded, 8, winget.MAX_VERSION_DATA + 1)
    with pytest.raises(winget.WingetLayoutError, match="large"):
        winget.decode_mszip(bytes(encoded), max_size=winget.MAX_VERSION_DATA)


def test_mszip_too_short_rejected() -> None:
    with pytest.raises(winget.WingetLayoutError):
        winget.decode_mszip(b"\x0a\x51\xe5\xc0\x18\x00\x00\x02")


# --------------------------------------------------------------------------- #
# a synthetic Public/index.db (V2 schema) and source2.msix wrapper
# --------------------------------------------------------------------------- #
def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build_index_db(packages: List[Dict[str, Any]], *, major: str = "2", minor: str = "0") -> bytes:
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    con.executemany("INSERT INTO metadata (name, value) VALUES (?, ?)",
                    [("majorVersion", major), ("minorVersion", minor), ("databaseIdentifier", "test")])
    con.execute("CREATE TABLE packages (id TEXT, name TEXT, moniker TEXT, latest_version TEXT, "
               "arp_min_version TEXT, arp_max_version TEXT, hash BLOB)")
    con.execute("CREATE TABLE tags2 (tag TEXT)")
    con.execute("CREATE TABLE tags2_map (tag INTEGER, package INTEGER)")
    con.execute("CREATE TABLE commands2 (command TEXT)")
    con.execute("CREATE TABLE commands2_map (command INTEGER, package INTEGER)")
    con.execute("CREATE TABLE pfns2 (pfn TEXT, package INTEGER)")
    con.execute("CREATE TABLE productcodes2 (productcode TEXT, package INTEGER)")
    con.execute("CREATE TABLE upgradecodes2 (upgradecode TEXT, package INTEGER)")
    for p in packages:
        con.execute("INSERT INTO packages (rowid, id, name, moniker, latest_version, hash) VALUES (?,?,?,?,?,?)",
                    (p["rowid"], p["id"], p.get("name", ""), p.get("moniker", ""),
                     p.get("latest_version", ""), p.get("hash", b"")))
        for tag in p.get("tags", []):
            cur = con.execute("INSERT INTO tags2 (tag) VALUES (?)", (tag,))
            con.execute("INSERT INTO tags2_map (tag, package) VALUES (?, ?)", (cur.lastrowid, p["rowid"]))
        for cmd in p.get("commands", []):
            cur = con.execute("INSERT INTO commands2 (command) VALUES (?)", (cmd,))
            con.execute("INSERT INTO commands2_map (command, package) VALUES (?, ?)", (cur.lastrowid, p["rowid"]))
        if p.get("product_code"):
            con.execute("INSERT INTO productcodes2 (productcode, package) VALUES (?, ?)",
                        (p["product_code"], p["rowid"]))
    con.commit()
    return con.serialize()


def _wrap_msix(db_bytes: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Public/index.db", db_bytes)
        zf.writestr("AppxManifest.xml", "<Package/>")
    return buf.getvalue()


class FakeFetcher:
    """A ``fetch`` callable serving canned responses by exact URL, recording every call."""

    def __init__(self) -> None:
        self.routes: Dict[str, winget.Response] = {}
        self.calls: List[str] = []

    def set(self, url: str, *, status: int = 200, body: bytes = b"", headers: Optional[Dict[str, str]] = None) -> None:
        self.routes[url] = winget.Response(status, body, dict(headers or {}), url)

    def __call__(self, url: str, headers: Dict[str, str]) -> winget.Response:
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError(f"unexpected fetch: {url}")
        return self.routes[url]


@pytest.fixture()
def edge_hash() -> bytes:
    return hashlib.sha256(b"edge-versiondata-fingerprint").digest()


@pytest.fixture()
def fetcher_with_index(tmp_path: Path, edge_hash: bytes) -> FakeFetcher:
    db = _build_index_db([
        {"rowid": 1, "id": "Microsoft.Edge", "name": "Microsoft Edge", "moniker": "edge",
         "latest_version": "154.0.4258.37", "hash": edge_hash, "tags": ["browser"], "commands": ["msedge"]},
        {"rowid": 2, "id": "Mozilla.Firefox", "name": "Mozilla Firefox", "moniker": "firefox",
         "latest_version": "156.0.1", "hash": hashlib.sha256(b"firefox").digest(), "tags": ["browser"]},
        {"rowid": 3, "id": "Mozilla.Firefox.ach", "name": "Firefox (Sorani)", "moniker": "",
         "latest_version": "156.0.1", "hash": hashlib.sha256(b"firefox-ach").digest()},
        {"rowid": 4, "id": "7zip.7zip", "name": "7-Zip", "moniker": "7zip",
         "latest_version": "26.03", "hash": hashlib.sha256(b"7zip").digest(),
         "product_code": "{23170F69-40C1-2702-2603-000001000000}"},
    ])
    f = FakeFetcher()
    f.set(winget.INDEX_URL, body=_wrap_msix(db), headers={"etag": '"v1"'})
    return f


# --------------------------------------------------------------------------- #
# load_index / caching / schema checks
# --------------------------------------------------------------------------- #
def test_load_index_downloads_and_caches(tmp_path: Path, fetcher_with_index: FakeFetcher) -> None:
    con = winget.load_index(fetch=fetcher_with_index, cache_dir=tmp_path)
    row = con.execute("SELECT id FROM packages WHERE id = 'Microsoft.Edge'").fetchone()
    assert row == ("Microsoft.Edge",)
    con.close()
    assert (tmp_path / "index" / "index.db").is_file()
    meta = json.loads((tmp_path / "index" / "index.json").read_text())
    assert meta["etag"] == '"v1"'


def test_load_index_reuses_fresh_cache_without_fetching(tmp_path: Path, fetcher_with_index: FakeFetcher) -> None:
    winget.load_index(fetch=fetcher_with_index, cache_dir=tmp_path).close()
    assert fetcher_with_index.calls == [winget.INDEX_URL]

    def boom(_url: str, _headers: Dict[str, str]) -> winget.Response:
        raise AssertionError("should not fetch: the cache is still fresh")

    con = winget.load_index(fetch=boom, cache_dir=tmp_path, max_age_s=900)
    assert con.execute("SELECT count(*) FROM packages").fetchone()[0] == 4
    con.close()


def test_load_index_conditional_get_not_modified(tmp_path: Path, fetcher_with_index: FakeFetcher) -> None:
    winget.load_index(fetch=fetcher_with_index, cache_dir=tmp_path).close()

    def not_modified(url: str, headers: Dict[str, str]) -> winget.Response:
        assert headers.get("If-None-Match") == '"v1"'
        return winget.Response(304, b"", {}, url)

    con = winget.load_index(fetch=not_modified, cache_dir=tmp_path, max_age_s=0)
    assert con.execute("SELECT count(*) FROM packages").fetchone()[0] == 4
    con.close()


def test_load_index_rejects_wrong_schema(tmp_path: Path) -> None:
    db = _build_index_db([{"rowid": 1, "id": "A.B", "hash": b""}], major="3", minor="0")
    f = FakeFetcher()
    f.set(winget.INDEX_URL, body=_wrap_msix(db))
    with pytest.raises(winget.WingetLayoutError, match="format"):
        winget.load_index(fetch=f, cache_dir=tmp_path)


def test_load_index_missing_member_rejected(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("nothing/here.txt", "x")
    f = FakeFetcher()
    f.set(winget.INDEX_URL, body=buf.getvalue())
    with pytest.raises(winget.WingetLayoutError, match="index.db"):
        winget.load_index(fetch=f, cache_dir=tmp_path)


def test_load_index_falls_back_to_cache_when_offline(tmp_path: Path, fetcher_with_index: FakeFetcher) -> None:
    winget.load_index(fetch=fetcher_with_index, cache_dir=tmp_path).close()

    def offline(_url: str, _headers: Dict[str, str]) -> bytes:
        raise winget.WingetError("network is down")

    con = winget.load_index(fetch=offline, cache_dir=tmp_path, max_age_s=0)
    assert con.execute("SELECT count(*) FROM packages").fetchone()[0] == 4
    con.close()


def test_load_index_no_cache_and_offline_raises(tmp_path: Path) -> None:
    def offline(_url: str, _headers: Dict[str, str]) -> bytes:
        raise winget.WingetError("network is down")

    with pytest.raises(winget.WingetError):
        winget.load_index(fetch=offline, cache_dir=tmp_path)


def test_index_info(tmp_path: Path, fetcher_with_index: FakeFetcher) -> None:
    con = winget.load_index(fetch=fetcher_with_index, cache_dir=tmp_path)
    info = winget.index_info(con, tmp_path)
    con.close()
    assert info["packages"] == 4
    assert info["url"] == winget.INDEX_URL


# --------------------------------------------------------------------------- #
# search / canonical_id
# --------------------------------------------------------------------------- #
def test_search_matches_id_name_moniker(tmp_path: Path, fetcher_with_index: FakeFetcher) -> None:
    con = winget.load_index(fetch=fetcher_with_index, cache_dir=tmp_path)
    results = winget.search("firefox", index=con)
    con.close()
    ids = [r["id"] for r in results]
    assert "Mozilla.Firefox" in ids
    # the plain package ranks above its language sub-package for the same query
    assert ids.index("Mozilla.Firefox") < ids.index("Mozilla.Firefox.ach")


def test_search_matches_tag_and_command(tmp_path: Path, fetcher_with_index: FakeFetcher) -> None:
    con = winget.load_index(fetch=fetcher_with_index, cache_dir=tmp_path)
    by_tag = {r["id"] for r in winget.search("browser", index=con)}
    by_cmd = {r["id"] for r in winget.search("msedge", index=con)}
    con.close()
    assert {"Microsoft.Edge", "Mozilla.Firefox"} <= by_tag
    assert by_cmd == {"Microsoft.Edge"}


def test_search_is_case_insensitive_unicode(tmp_path: Path, fetcher_with_index: FakeFetcher) -> None:
    con = winget.load_index(fetch=fetcher_with_index, cache_dir=tmp_path)
    results = winget.search("EDGE", index=con)
    con.close()
    assert any(r["id"] == "Microsoft.Edge" for r in results)


def test_search_exact_product_code_ranks_first(tmp_path: Path, fetcher_with_index: FakeFetcher) -> None:
    con = winget.load_index(fetch=fetcher_with_index, cache_dir=tmp_path)
    results = winget.search("{23170F69-40C1-2702-2603-000001000000}", index=con)
    con.close()
    assert results and results[0]["id"] == "7zip.7zip"
    assert "ProductCode" in results[0]["match"]


def test_search_requires_nonempty_query(tmp_path: Path, fetcher_with_index: FakeFetcher) -> None:
    con = winget.load_index(fetch=fetcher_with_index, cache_dir=tmp_path)
    with pytest.raises(winget.WingetError):
        winget.search("   ", index=con)
    con.close()


def test_search_limit_is_honoured(tmp_path: Path, fetcher_with_index: FakeFetcher) -> None:
    con = winget.load_index(fetch=fetcher_with_index, cache_dir=tmp_path)
    results = winget.search("e", index=con, limit=1)
    con.close()
    assert len(results) == 1


def test_canonical_id_exact_and_case_insensitive(tmp_path: Path, fetcher_with_index: FakeFetcher) -> None:
    con = winget.load_index(fetch=fetcher_with_index, cache_dir=tmp_path)
    assert winget.canonical_id("Microsoft.Edge", index=con) == "Microsoft.Edge"
    assert winget.canonical_id("microsoft.edge", index=con) == "Microsoft.Edge"
    con.close()


def test_canonical_id_unknown_raises_not_found(tmp_path: Path, fetcher_with_index: FakeFetcher) -> None:
    con = winget.load_index(fetch=fetcher_with_index, cache_dir=tmp_path)
    with pytest.raises(winget.WingetNotFound):
        winget.canonical_id("Nope.NotThere", index=con)
    con.close()


def test_canonical_id_refuses_store_id() -> None:
    with pytest.raises(winget.WingetUnsupported, match="Microsoft Store"):
        winget.canonical_id("9NBLGGH4NNS1")


def test_is_store_id() -> None:
    assert winget.is_store_id("9NBLGGH4NNS1")
    assert winget.is_store_id("XP89DCGQ3K6VLD")
    assert not winget.is_store_id("Mozilla.Firefox")


# --------------------------------------------------------------------------- #
# manifest_dir path building (winget-pkgs layout rule)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("pid, expected", [
    ("Microsoft.Edge", "manifests/m/Microsoft/Edge/"),
    ("7zip.7zip", "manifests/7/7zip/7zip/"),
    ("Microsoft.VisualStudio.2022.Community", "manifests/m/Microsoft/VisualStudio/2022/Community/"),
    ("Microsoft.VCRedist.2015+.x64", "manifests/m/Microsoft/VCRedist/2015+/x64/"),
])
def test_manifest_dir_path_rule(pid: str, expected: str) -> None:
    assert winget.manifest_dir(pid) == expected
    assert winget.manifest_dir(pid, "1.2.3") == expected + "1.2.3/"


def test_manifest_dir_rejects_bad_version() -> None:
    with pytest.raises(winget.WingetNotFound):
        winget.manifest_dir("Microsoft.Edge", "../../etc")


# --------------------------------------------------------------------------- #
# the full CDN hash chain: index -> versionData.mszyml -> merged manifest
# --------------------------------------------------------------------------- #
def _version_data_yaml(entries: List[Dict[str, str]]) -> bytes:
    lines = ["sV: 1.0", "vD:"]
    for e in entries:
        lines.append(f"- v: '{e['v']}'")
        lines.append(f"  rP: {e['rP']}")
        lines.append(f"  s256H: {e['s256H']}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _cdn_setup(tmp_path: Path, *, versions: List[str], manifest_yaml: bytes) -> FakeFetcher:
    manifest_hash = _sha256(manifest_yaml)
    rp = "manifests/m/Microsoft/Edge/" + versions[0] + "/abcd"
    entries = [{"v": v, "rP": rp if v == versions[0] else f"manifests/m/Microsoft/Edge/{v}/abcd",
               "s256H": manifest_hash if v == versions[0] else "0" * 64} for v in versions]
    vdata = _version_data_yaml(entries)
    vdata_encoded = _encode_mszip(vdata)
    pkg_hash = hashlib.sha256(vdata_encoded).digest()
    db = _build_index_db([{"rowid": 1, "id": "Microsoft.Edge", "name": "Microsoft Edge", "moniker": "edge",
                           "latest_version": versions[0], "hash": pkg_hash}])
    f = FakeFetcher()
    f.set(winget.INDEX_URL, body=_wrap_msix(db))
    f.set(f"{winget.CDN_BASE}packages/Microsoft.Edge/{pkg_hash.hex()[:8]}/versionData.mszyml", body=vdata_encoded)
    f.set(winget.CDN_BASE + rp, body=manifest_yaml)
    return f


_EDGE_MANIFEST = (
    b"PackageIdentifier: Microsoft.Edge\n"
    b"PackageVersion: 154.0.4258.37\n"
    b"ManifestType: merged\n"
    b"InstallerType: exe\n"
    b"Installers:\n"
    b"- Architecture: x64\n"
    b"  InstallerUrl: https://edge.example.test/setup.exe\n"
    b"  InstallerSha256: " + b"a" * 64 + b"\n"
)


def test_list_versions_via_cdn(tmp_path: Path) -> None:
    f = _cdn_setup(tmp_path, versions=["154.0.4258.37", "153.0.1.1"], manifest_yaml=_EDGE_MANIFEST)
    versions = winget.list_versions("Microsoft.Edge", fetch=f, cache_dir=tmp_path)
    assert versions[0] == "154.0.4258.37"


def test_load_manifest_via_cdn_verifies_hash_chain(tmp_path: Path) -> None:
    f = _cdn_setup(tmp_path, versions=["154.0.4258.37"], manifest_yaml=_EDGE_MANIFEST)
    manifest = winget.load_manifest("Microsoft.Edge", fetch=f, cache_dir=tmp_path)
    assert manifest["PackageIdentifier"] == "Microsoft.Edge"
    assert manifest["LindosSource"]["channel"] == "cdn"


def test_load_manifest_rejects_tampered_manifest_body(tmp_path: Path) -> None:
    f = _cdn_setup(tmp_path, versions=["154.0.4258.37"], manifest_yaml=_EDGE_MANIFEST)
    rp = "manifests/m/Microsoft/Edge/154.0.4258.37/abcd"
    f.routes[winget.CDN_BASE + rp] = winget.Response(200, _EDGE_MANIFEST + b"# tampered\n", {}, winget.CDN_BASE + rp)
    with pytest.raises(winget.WingetHashError):
        winget.load_manifest("Microsoft.Edge", fetch=f, cache_dir=tmp_path)


def test_load_manifest_rejects_tampered_version_data(tmp_path: Path) -> None:
    f = _cdn_setup(tmp_path, versions=["154.0.4258.37"], manifest_yaml=_EDGE_MANIFEST)
    url = next(u for u in f.routes if u.endswith("versionData.mszyml"))
    bad = bytearray(f.routes[url].body)
    bad[-1] ^= 0xFF
    f.routes[url] = winget.Response(200, bytes(bad), {}, url)
    with pytest.raises(winget.WingetHashError):
        winget.load_manifest("Microsoft.Edge", fetch=f, cache_dir=tmp_path)


def test_load_manifest_rejects_identifier_mismatch(tmp_path: Path) -> None:
    wrong = _EDGE_MANIFEST.replace(b"Microsoft.Edge", b"Someone.Else", 1)
    f = _cdn_setup(tmp_path, versions=["154.0.4258.37"], manifest_yaml=wrong)
    with pytest.raises(winget.WingetError, match="describes"):
        winget.load_manifest("Microsoft.Edge", fetch=f, cache_dir=tmp_path)


def test_load_manifest_falls_back_to_github_on_layout_error(tmp_path: Path) -> None:
    # a broken CDN index (wrong schema) must make load_manifest try GitHub instead of raising.
    db = _build_index_db([{"rowid": 1, "id": "Microsoft.Edge", "hash": b""}], major="3", minor="0")
    f = FakeFetcher()
    f.set(winget.INDEX_URL, body=_wrap_msix(db))
    tree_url = winget.GITHUB_TREES_URL + "master:manifests/m/Microsoft/Edge?recursive=1"
    edge_singleton = _EDGE_MANIFEST.replace(b"ManifestType: merged", b"ManifestType: singleton")
    tree = {"truncated": False, "tree": [
        {"path": "154.0.4258.37/Microsoft.Edge.yaml", "type": "blob",
         "sha": hashlib.sha1(b"blob %d\x00" % len(edge_singleton) + edge_singleton).hexdigest()},
    ]}
    f.set(tree_url, body=json.dumps(tree).encode("utf-8"))
    raw_url = winget.GITHUB_RAW_BASE + "manifests/m/Microsoft/Edge/154.0.4258.37/Microsoft.Edge.yaml"
    f.set(raw_url, body=edge_singleton)
    manifest = winget.load_manifest("Microsoft.Edge", fetch=f, cache_dir=tmp_path)
    assert manifest["LindosSource"]["channel"] == "github"
    assert manifest["PackageIdentifier"] == "Microsoft.Edge"


def test_github_rate_limit_message(tmp_path: Path) -> None:
    f = FakeFetcher()
    tree_url = winget.GITHUB_TREES_URL + "master:manifests/m/Microsoft/Edge?recursive=1"
    f.set(tree_url, status=403, headers={"x-ratelimit-reset": "9999999999"})
    with pytest.raises(winget.WingetError, match="60 requests"):
        winget._github_listing("Microsoft.Edge", f)


def test_github_unknown_package_is_not_found() -> None:
    f = FakeFetcher()
    tree_url = winget.GITHUB_TREES_URL + "master:manifests/x/Xyz/Abc?recursive=1"
    f.set(tree_url, status=404)
    with pytest.raises(winget.WingetNotFound):
        winget._github_listing("Xyz.Abc", f)
