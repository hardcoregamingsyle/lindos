"""lindos-proton — version parsing, tag→dir naming, asset selection, sha512sum parsing,
inventory/symlink sharing and the update/remove flows with injected (mocked) network."""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.append(_HERE)  # appended, not prepended: never shadow other packages' `conftest`
from gaming_testlib import load_bin, symlinks_supported  # noqa: E402

proton = load_bin("lindos-proton")


# --------------------------------------------------------------------------- versions / names
@pytest.mark.parametrize("name,expected", [
    ("GE-Proton9-20", (9, 20)),
    ("GE-Proton10-1", (10, 1)),
    ("GE-Proton11-5", (11, 5)),
    (" GE-Proton8-32 ", (8, 32)),
    ("Proton-6.21-GE-2", (6, 21, 2)),
    ("Proton-7.0rc3-GE-1", None),
    ("GE-Proton9-20.tar.gz", None),
    ("proton_9.0", None),
    ("Proton Experimental", None),
    ("", None),
    (".lindos-proton-tmpabc", None),
])
def test_parse_version(name, expected):
    assert proton.parse_version(name) == expected
    assert proton.is_ge_name(name) is (expected is not None)


def test_version_ordering():
    names = ["GE-Proton9-20", "GE-Proton10-1", "GE-Proton9-3", "GE-Proton8-32", "GE-Proton11-5"]
    ordered = sorted(names, key=proton.parse_version, reverse=True)
    assert ordered == ["GE-Proton11-5", "GE-Proton10-1", "GE-Proton9-20", "GE-Proton9-3", "GE-Proton8-32"]


def test_tag_to_dir_and_asset_names():
    assert proton.dir_name_for_tag("GE-Proton9-20") == "GE-Proton9-20"
    assert proton.dir_name_for_tag(" GE-Proton11-5\n") == "GE-Proton11-5"
    assert proton.asset_names_for_tag("GE-Proton11-5", "x86_64") == ("GE-Proton11-5-x86_64.tar.gz", "GE-Proton11-5-x86_64.sha512sum")
    cands = proton.asset_candidates("GE-Proton9-20", "x86_64")
    assert cands == [
        ("GE-Proton9-20-x86_64.tar.gz", "GE-Proton9-20-x86_64.sha512sum"),
        ("GE-Proton9-20.tar.gz", "GE-Proton9-20.sha512sum"),
    ]
    assert proton.host_arch() in ("x86_64", "aarch64")


def test_human_size():
    assert proton.human_size(512) == "512 B"
    assert proton.human_size(1536) == "1.5 KiB"
    assert proton.human_size(450 * 1024 * 1024).endswith("MiB")
    assert proton.human_size(3 * 1024 ** 3).endswith("GiB")


# --------------------------------------------------------------------------- sha512sum parsing
GOOD_HEX = "ab" * 64


def test_parse_sha512sum_gnu_formats():
    text = (
        "# comment\n"
        f"{GOOD_HEX}  GE-Proton9-20.tar.gz\n"
        f"{'cd' * 64} *GE-Proton9-20-x86_64.tar.gz\n"
        "\n"
        "not a checksum line\n"
        f"{'EF' * 64}  ./sub/dir/other.tar.gz\n"
        "0123  too-short.tar.gz\n"
    )
    sums = proton.parse_sha512sum(text)
    assert sums == {
        "GE-Proton9-20.tar.gz": GOOD_HEX,
        "GE-Proton9-20-x86_64.tar.gz": "cd" * 64,
        "other.tar.gz": "ef" * 64,
    }


def test_parse_sha512sum_empty_and_garbage():
    assert proton.parse_sha512sum("") == {}
    assert proton.parse_sha512sum("zz\nyy  file\n") == {}


def test_sha512_file_matches_hashlib(tmp_path: Path):
    p = tmp_path / "blob.bin"
    data = os.urandom(3 * 1024 * 1024 + 17)
    p.write_bytes(data)
    seen = []
    digest = proton.sha512_file(p, progress=lambda done, total: seen.append((done, total)))
    assert digest == hashlib.sha512(data).hexdigest()
    assert seen and seen[-1] == (len(data), len(data))


# --------------------------------------------------------------------------- release asset selection
def _release(tag: str, names: list[str], **extra) -> dict:
    return {
        "tag_name": tag,
        "published_at": "2026-08-01T00:00:00Z",
        "html_url": f"https://github.com/GloriousEggroll/proton-ge-custom/releases/tag/{tag}",
        "assets": [{"name": n, "browser_download_url": f"https://example.invalid/{tag}/{n}", "size": 1000 + i}
                   for i, n in enumerate(names)],
        **extra,
    }


def test_pick_assets_classic_layout():
    rel = _release("GE-Proton9-20", ["GE-Proton9-20.sha512sum", "GE-Proton9-20.tar.gz"])
    info = proton.pick_assets(rel, arch="x86_64")
    assert info["tag"] == "GE-Proton9-20"
    assert info["tar_name"] == "GE-Proton9-20.tar.gz"
    assert info["sum_name"] == "GE-Proton9-20.sha512sum"
    assert info["tar_url"].endswith("/GE-Proton9-20.tar.gz")
    assert info["tar_size"] == 1001


def test_pick_assets_per_arch_layout_prefers_x86_64_over_aarch64():
    # GE-Proton11+ publishes aarch64 first in the asset list; we must never pick it on x86_64
    rel = _release("GE-Proton11-5", [
        "GE-Proton11-5-aarch64.sha512sum", "GE-Proton11-5-aarch64.tar.gz",
        "GE-Proton11-5-x86_64.sha512sum", "GE-Proton11-5-x86_64.tar.gz",
    ])
    info = proton.pick_assets(rel, arch="x86_64")
    assert info["tar_name"] == "GE-Proton11-5-x86_64.tar.gz"
    assert info["sum_name"] == "GE-Proton11-5-x86_64.sha512sum"
    info_arm = proton.pick_assets(rel, arch="aarch64")
    assert info_arm["tar_name"] == "GE-Proton11-5-aarch64.tar.gz"
    assert info_arm["sum_name"] == "GE-Proton11-5-aarch64.sha512sum"


def test_pick_assets_fallback_by_stem_and_errors():
    rel = _release("GE-Proton12-1", ["GE-Proton12-1-aarch64.tar.gz", "GE-Proton12-1-linux-x86_64.tar.gz",
                                     "GE-Proton12-1-linux-x86_64.sha512sum"])
    info = proton.pick_assets(rel, arch="x86_64")
    assert info["tar_name"] == "GE-Proton12-1-linux-x86_64.tar.gz"
    assert info["sum_name"] == "GE-Proton12-1-linux-x86_64.sha512sum"
    with pytest.raises(ValueError):
        proton.pick_assets(_release("GE-Proton12-1", ["GE-Proton12-1-aarch64.tar.gz"]), arch="x86_64")
    with pytest.raises(ValueError):
        proton.pick_assets({"assets": []})
    no_sum = proton.pick_assets(_release("GE-Proton9-1", ["GE-Proton9-1.tar.gz"]), arch="x86_64")
    assert no_sum["sum_name"] is None and no_sum["sum_url"] is None
    # arch synonyms: arm64 == aarch64, amd64 == x86_64
    assert proton._foreign_arch("GE-Proton12-1-arm64.tar.gz", "aarch64") is False
    assert proton._foreign_arch("GE-Proton12-1-amd64.tar.gz", "x86_64") is False
    assert proton._foreign_arch("GE-Proton12-1-amd64.tar.gz", "aarch64") is True
    assert proton._foreign_arch("GE-Proton12-1.tar.gz", "x86_64") is False


# --------------------------------------------------------------------------- locations
def test_resolve_locations_without_steam(fake_home: Path, monkeypatch):
    monkeypatch.setattr(proton, "steam_installed", lambda: False)
    locs = proton.resolve_locations(fake_home)
    assert locs.steam_present is False
    assert locs.primary == fake_home / ".local" / "share" / "umu" / "compatibilitytools"
    assert locs.others == []


def test_resolve_locations_with_steam_dir(fake_home: Path, monkeypatch):
    monkeypatch.setattr(proton, "steam_installed", lambda: False)
    (fake_home / ".local" / "share" / "Steam").mkdir(parents=True)
    locs = proton.resolve_locations(fake_home)
    assert locs.steam_present is True
    assert locs.primary == fake_home / ".local" / "share" / "Steam" / "compatibilitytools.d"
    assert locs.others == [fake_home / ".local" / "share" / "umu" / "compatibilitytools"]


def test_resolve_locations_prefers_dot_steam_root(fake_home: Path, monkeypatch):
    monkeypatch.setattr(proton, "steam_installed", lambda: True)
    (fake_home / ".steam" / "root").mkdir(parents=True)
    locs = proton.resolve_locations(fake_home)
    assert locs.primary == fake_home / ".steam" / "root" / "compatibilitytools.d"


def test_scan_and_inventory(fake_home: Path, monkeypatch):
    monkeypatch.setattr(proton, "steam_installed", lambda: False)
    umu = fake_home / ".local" / "share" / "umu" / "compatibilitytools"
    for name in ("GE-Proton9-20", "GE-Proton10-3", "not-a-proton", ".lindos-proton-tmp"):
        (umu / name).mkdir(parents=True)
    (umu / "GE-Proton8-1.tar.gz").write_bytes(b"x")  # files are ignored
    locs = proton.resolve_locations(fake_home)
    inv = proton.inventory(locs)
    assert [r["tag"] for r in inv] == ["GE-Proton10-3", "GE-Proton9-20"]
    assert inv[0]["installed_in"][0]["kind"] == "real"
    assert proton.newest_installed(locs) == "GE-Proton10-3"


def test_share_links_creates_symlinks_both_ways(fake_home: Path, monkeypatch, tmp_path: Path):
    if not symlinks_supported(tmp_path):
        pytest.skip("symlinks not supported on this host")
    monkeypatch.setattr(proton, "steam_installed", lambda: True)
    steam = fake_home / ".steam" / "root" / "compatibilitytools.d"
    umu = fake_home / ".local" / "share" / "umu" / "compatibilitytools"
    (steam / "GE-Proton9-20").mkdir(parents=True)
    (umu / "GE-Proton10-1").mkdir(parents=True)
    locs = proton.resolve_locations(fake_home)
    actions = proton.share_links(locs)
    kinds = {(a["action"], Path(a["path"]).name) for a in actions}
    assert ("link", "GE-Proton9-20") in kinds and ("link", "GE-Proton10-1") in kinds
    assert (umu / "GE-Proton9-20").is_symlink() and (steam / "GE-Proton10-1").is_symlink()
    # idempotent
    assert proton.share_links(locs) == []
    # broken link gets removed once the real directory disappears
    os.rmdir(umu / "GE-Proton10-1")
    actions = proton.share_links(locs)
    assert any(a["action"] == "unlink-broken" for a in actions)
    assert not (steam / "GE-Proton10-1").exists() and not (steam / "GE-Proton10-1").is_symlink()


# --------------------------------------------------------------------------- update / remove (mocked network)
def _make_tarball(tag: str, top: str | None = None) -> bytes:
    top = top or tag
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel, content in ((f"{top}/version", b"1.0 " + tag.encode() + b"\n"),
                             (f"{top}/proton", b"#!/usr/bin/env python3\n"),
                             (f"{top}/files/bin/wine", b"ELF")):
            info = tarfile.TarInfo(rel)
            info.size = len(content)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


class FakeNet:
    """Injectable fetch_json/download pair serving one release from memory."""

    def __init__(self, tag: str, per_arch: bool = True, top: str | None = None, corrupt: bool = False,
                 offline: bool = False, no_sum: bool = False):
        self.tag = tag
        self.offline = offline
        tar_name, sum_name = (f"{tag}-x86_64.tar.gz", f"{tag}-x86_64.sha512sum") if per_arch else (f"{tag}.tar.gz", f"{tag}.sha512sum")
        self.tar_name, self.sum_name = tar_name, sum_name
        self.tar_bytes = _make_tarball(tag, top)
        digest = hashlib.sha512(self.tar_bytes).hexdigest()
        if corrupt:
            digest = "0" * 128
        self.sum_text = f"{digest}  {tar_name}\n"
        names = [tar_name] + ([] if no_sum else [sum_name])
        if per_arch:
            names = [f"{tag}-aarch64.sha512sum", f"{tag}-aarch64.tar.gz"] + names
        self.release = _release(tag, names)
        self.calls: list[str] = []

    def fetch_json(self, url: str) -> dict:
        self.calls.append(url)
        if self.offline:
            raise proton.OfflineError("network unreachable (fake)")
        assert url in (proton.API_LATEST, proton.API_TAG.format(tag=self.tag)), url
        return self.release

    def download(self, url: str, dest: Path, progress=None) -> int:
        self.calls.append(url)
        if self.offline:
            raise proton.OfflineError("network unreachable (fake)")
        name = url.rsplit("/", 1)[1]
        if name == self.tar_name:
            data = self.tar_bytes
        elif name == self.sum_name:
            data = self.sum_text.encode()
        else:
            raise RuntimeError(f"HTTP 404 downloading {url}")
        dest.write_bytes(data)
        if progress:
            progress(len(data), len(data))
        return len(data)


def _run(argv, net: FakeNet) -> int:
    return proton.main(argv, fetch_json=net.fetch_json, download=net.download)


def test_update_installs_into_umu_when_no_steam(fake_home: Path, monkeypatch, capsys):
    monkeypatch.setattr(proton, "steam_installed", lambda: False)
    monkeypatch.setattr(proton, "host_arch", lambda: "x86_64")
    net = FakeNet("GE-Proton11-5")
    rc = _run(["update", "--json"], net)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0, out
    assert out["status"] == "installed" and out["tag"] == "GE-Proton11-5" and out["verified"] is True
    dest = fake_home / ".local" / "share" / "umu" / "compatibilitytools" / "GE-Proton11-5"
    assert dest.is_dir() and (dest / "proton").is_file() and (dest / "files" / "bin" / "wine").is_file()
    assert out["steam_present"] is False
    assert not any(p.name.startswith(".lindos-proton-") for p in dest.parent.iterdir()), "temp dir cleaned"
    # second run: up to date, no download
    rc = _run(["update", "--json"], net)
    out2 = json.loads(capsys.readouterr().out)
    assert rc == 0 and out2["status"] == "up-to-date"
    assert not any(u.endswith(".tar.gz") for u in net.calls[3:])


def test_update_installs_into_steam_and_links_umu(fake_home: Path, monkeypatch, capsys, tmp_path: Path):
    monkeypatch.setattr(proton, "steam_installed", lambda: True)
    monkeypatch.setattr(proton, "host_arch", lambda: "x86_64")
    (fake_home / ".steam" / "root").mkdir(parents=True)
    net = FakeNet("GE-Proton9-20", per_arch=False)
    rc = _run(["update", "--json", "--tag", "GE-Proton9-20"], net)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0, out
    steam_dir = fake_home / ".steam" / "root" / "compatibilitytools.d" / "GE-Proton9-20"
    assert steam_dir.is_dir()
    assert net.calls[0] == proton.API_TAG.format(tag="GE-Proton9-20")
    if symlinks_supported(tmp_path):
        umu_link = fake_home / ".local" / "share" / "umu" / "compatibilitytools" / "GE-Proton9-20"
        assert umu_link.is_symlink()
        assert any(a["action"] == "link" for a in out["links"])


def test_update_rejects_bad_checksum(fake_home: Path, monkeypatch, capsys):
    monkeypatch.setattr(proton, "steam_installed", lambda: False)
    monkeypatch.setattr(proton, "host_arch", lambda: "x86_64")
    net = FakeNet("GE-Proton11-5", corrupt=True)
    rc = _run(["update", "--json"], net)
    out = json.loads(capsys.readouterr().out)
    assert rc == proton.EXIT_ERROR and out["status"] == "error" and "sha512" in out["error"]
    assert not (fake_home / ".local" / "share" / "umu" / "compatibilitytools" / "GE-Proton11-5").exists()


def test_update_refuses_without_checksum_unless_no_verify(fake_home: Path, monkeypatch, capsys):
    monkeypatch.setattr(proton, "steam_installed", lambda: False)
    monkeypatch.setattr(proton, "host_arch", lambda: "x86_64")
    net = FakeNet("GE-Proton11-5", no_sum=True)
    rc = _run(["update", "--json"], net)
    out = json.loads(capsys.readouterr().out)
    assert rc == proton.EXIT_ERROR and "sha512sum" in out["error"]
    rc = _run(["update", "--json", "--no-verify"], net)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["status"] == "installed" and out["verified"] is False


def test_update_offline_exit_code(fake_home: Path, monkeypatch, capsys):
    monkeypatch.setattr(proton, "steam_installed", lambda: False)
    net = FakeNet("GE-Proton11-5", offline=True)
    rc = _run(["update", "--json"], net)
    out = json.loads(capsys.readouterr().out)
    assert rc == proton.EXIT_OFFLINE and out["status"] == "offline"
    rc = _run(["update"], net)
    text = capsys.readouterr().out
    assert rc == proton.EXIT_OFFLINE and "internet" in text.lower()


def test_update_handles_unexpected_top_dir(fake_home: Path, monkeypatch, capsys):
    monkeypatch.setattr(proton, "steam_installed", lambda: False)
    monkeypatch.setattr(proton, "host_arch", lambda: "x86_64")
    net = FakeNet("GE-Proton11-5", top="GE-Proton11-5-renamed")
    rc = _run(["update", "--json"], net)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and Path(out["path"]).name == "GE-Proton11-5-renamed"


def test_extract_tarball_rejects_path_traversal(tmp_path: Path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("../evil")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(buf.getvalue())
    with pytest.raises(RuntimeError):
        proton.extract_tarball(bad, tmp_path / "out")


def test_list_and_remove(fake_home: Path, monkeypatch, capsys):
    monkeypatch.setattr(proton, "steam_installed", lambda: False)
    umu = fake_home / ".local" / "share" / "umu" / "compatibilitytools"
    (umu / "GE-Proton9-20").mkdir(parents=True)
    (umu / "GE-Proton10-3").mkdir(parents=True)
    rc = proton.main(["list", "--json"], fetch_json=lambda url: (_ for _ in ()).throw(AssertionError("no network")))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["newest_installed"] == "GE-Proton10-3"
    assert [r["tag"] for r in out["installed"]] == ["GE-Proton10-3", "GE-Proton9-20"]
    assert out["steam_present"] is False and out["latest_available"] is None
    # --check with an offline network reports the error but still exits 0
    def offline(url):
        raise proton.OfflineError("down")
    rc = proton.main(["list", "--json", "--check"], fetch_json=offline)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["latest_available"] is None and "offline" in out["latest_error"]
    # --check online
    rc = proton.main(["list", "--json", "--check"], fetch_json=lambda url: _release("GE-Proton11-5", ["GE-Proton11-5-x86_64.tar.gz"]))
    out = json.loads(capsys.readouterr().out)
    assert out["latest_available"] == "GE-Proton11-5"
    # remove
    rc = proton.main(["remove", "GE-Proton9-20", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["status"] == "removed" and not (umu / "GE-Proton9-20").exists()
    rc = proton.main(["remove", "GE-Proton9-20", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == proton.EXIT_ERROR and out["status"] == "not-found"
    rc = proton.main(["remove", "../etc", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == proton.EXIT_USAGE and out["status"] == "error"


def test_no_command_prints_help(capsys):
    rc = proton.main([])
    assert rc == proton.EXIT_USAGE
    assert "usage" in capsys.readouterr().out.lower()
