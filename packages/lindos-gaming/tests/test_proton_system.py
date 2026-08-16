"""lindos-proton system-wide install + the system/user Proton resolution order (SPEC-VM Sec. 23).

Covers ``install --system`` (pkexec escalation when not root, real install when root),
``list --system``, ``path`` and the pure ``resolve_proton`` ordering:

    UMU_PROTONPATH / profile  >  newest system GE-Proton  >  newest user GE-Proton
"""
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
    sys.path.append(_HERE)  # appended, not prepended: never shadow other packages' conftest
from gaming_testlib import load_bin  # noqa: E402

proton = load_bin("lindos-proton")


# --------------------------------------------------------------------------- helpers
def _make_tarball(tag: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel, content in ((f"{tag}/version", b"1.0\n"), (f"{tag}/proton", b"#!/usr/bin/env python3\n")):
            info = tarfile.TarInfo(rel)
            info.size = len(content)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


class Net:
    """Minimal injectable fetch_json/download serving one per-arch release from memory."""

    def __init__(self, tag: str):
        self.tag = tag
        self.tar_name = f"{tag}-x86_64.tar.gz"
        self.sum_name = f"{tag}-x86_64.sha512sum"
        self.tar_bytes = _make_tarball(tag)
        digest = hashlib.sha512(self.tar_bytes).hexdigest()
        self.sum_text = f"{digest}  {self.tar_name}\n"

    def fetch_json(self, url: str) -> dict:
        return {
            "tag_name": self.tag, "published_at": "2026-08-01T00:00:00Z", "html_url": "",
            "assets": [
                {"name": self.tar_name, "browser_download_url": f"https://x/{self.tar_name}", "size": len(self.tar_bytes)},
                {"name": self.sum_name, "browser_download_url": f"https://x/{self.sum_name}", "size": len(self.sum_text)},
            ],
        }

    def download(self, url: str, dest: Path, progress=None) -> int:
        name = url.rsplit("/", 1)[1]
        data = self.tar_bytes if name == self.tar_name else self.sum_text.encode()
        dest.write_bytes(data)
        if progress:
            progress(len(data), len(data))
        return len(data)


def _mkdir(base: Path, *tags: str) -> None:
    for t in tags:
        d = base / t
        d.mkdir(parents=True, exist_ok=True)
        (d / "proton").write_text("#!/bin/sh\n", encoding="utf-8")


# --------------------------------------------------------------------------- system dir helpers
def test_system_proton_dir_is_lindos_root_aware(fake_home: Path):
    d = proton.system_proton_dir()
    assert d.as_posix().endswith("/usr/share/lindos/proton")
    assert str(d).startswith(os.environ["LINDOS_ROOT"])


def test_newest_in_dir(tmp_path: Path):
    assert proton.newest_in_dir(tmp_path / "missing") is None
    _mkdir(tmp_path, "GE-Proton9-20", "GE-Proton10-3", "GE-Proton9-3", "not-a-proton")
    tag, path = proton.newest_in_dir(tmp_path)
    assert tag == "GE-Proton10-3" and path == tmp_path / "GE-Proton10-3"


# --------------------------------------------------------------------------- resolution order
def test_resolve_order_system_beats_user(fake_home: Path, monkeypatch):
    monkeypatch.setattr(proton, "steam_installed", lambda: False)
    monkeypatch.delenv("UMU_PROTONPATH", raising=False)
    _mkdir(proton.system_proton_dir(), "GE-Proton10-1")
    _mkdir(fake_home / ".local" / "share" / "umu" / "compatibilitytools", "GE-Proton9-20")
    res = proton.resolve_proton(fake_home)
    assert res["source"] == "system" and res["tag"] == "GE-Proton10-1"


def test_resolve_order_umu_env_wins(fake_home: Path, monkeypatch):
    monkeypatch.setattr(proton, "steam_installed", lambda: False)
    _mkdir(proton.system_proton_dir(), "GE-Proton10-1")
    monkeypatch.setenv("UMU_PROTONPATH", "/opt/custom-proton")
    res = proton.resolve_proton(fake_home)
    assert res["source"] == "UMU_PROTONPATH" and res["path"] == "/opt/custom-proton"


def test_resolve_order_profile_concrete_then_sentinel(fake_home: Path, monkeypatch):
    monkeypatch.setattr(proton, "steam_installed", lambda: False)
    monkeypatch.delenv("UMU_PROTONPATH", raising=False)
    _mkdir(proton.system_proton_dir(), "GE-Proton10-1")
    # a concrete profile pin beats the system newest
    res = proton.resolve_proton(fake_home, profile_proton="GE-Proton8-3")
    assert res["source"] == "profile" and res["tag"] == "GE-Proton8-3"
    # a "latest" sentinel does not pin: falls through to system newest
    for sentinel in ("GE-Proton-latest", "latest", "GE-Proton"):
        res = proton.resolve_proton(fake_home, profile_proton=sentinel)
        assert res["source"] == "system" and res["tag"] == "GE-Proton10-1", sentinel


def test_resolve_order_user_then_none(fake_home: Path, monkeypatch):
    monkeypatch.setattr(proton, "steam_installed", lambda: False)
    monkeypatch.delenv("UMU_PROTONPATH", raising=False)
    _mkdir(fake_home / ".local" / "share" / "umu" / "compatibilitytools", "GE-Proton9-20")
    res = proton.resolve_proton(fake_home)
    assert res["source"] == "user" and res["tag"] == "GE-Proton9-20"
    # nothing anywhere
    import shutil
    shutil.rmtree(fake_home / ".local" / "share" / "umu" / "compatibilitytools")
    res = proton.resolve_proton(fake_home)
    assert res["source"] == "none" and res["path"] is None


# --------------------------------------------------------------------------- path command
def test_cmd_path_json_and_text(fake_home: Path, monkeypatch, capsys):
    monkeypatch.setattr(proton, "steam_installed", lambda: False)
    monkeypatch.delenv("UMU_PROTONPATH", raising=False)
    _mkdir(proton.system_proton_dir(), "GE-Proton11-5")
    rc = proton.main(["path", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["source"] == "system" and out["tag"] == "GE-Proton11-5"
    rc = proton.main(["path"])
    assert rc == 0 and capsys.readouterr().out.strip().endswith("GE-Proton11-5")


def test_cmd_path_reads_profile(fake_home: Path, monkeypatch, capsys, tmp_path: Path):
    monkeypatch.setattr(proton, "steam_installed", lambda: False)
    monkeypatch.delenv("UMU_PROTONPATH", raising=False)
    prof = tmp_path / "game.json"
    prof.write_text(json.dumps({"proton": "GE-Proton8-3"}), encoding="utf-8")
    rc = proton.main(["path", "--json", "--profile", str(prof)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["source"] == "profile" and out["tag"] == "GE-Proton8-3"


def test_cmd_path_none_exits_error(fake_home: Path, monkeypatch, capsys):
    monkeypatch.setattr(proton, "steam_installed", lambda: False)
    monkeypatch.delenv("UMU_PROTONPATH", raising=False)
    rc = proton.main(["path", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == proton.EXIT_ERROR and out["source"] == "none"


# --------------------------------------------------------------------------- list --system
def test_cmd_list_system(fake_home: Path, capsys):
    _mkdir(proton.system_proton_dir(), "GE-Proton9-20", "GE-Proton11-5")
    rc = proton.main(["list", "--system", "--json"],
                     fetch_json=lambda url: (_ for _ in ()).throw(AssertionError("no network")))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["newest_installed"] == "GE-Proton11-5"
    assert [e["tag"] for e in out["installed"]] == ["GE-Proton11-5", "GE-Proton9-20"]
    assert out["system_dir"].endswith("proton") or out["system_dir"].endswith("proton/")


# --------------------------------------------------------------------------- install --system
def test_install_requires_system_flag(fake_home: Path, capsys):
    rc = proton.main(["install", "--json"], fetch_json=lambda u: {}, download=lambda *a, **k: 0)
    out = json.loads(capsys.readouterr().out)
    assert rc == proton.EXIT_USAGE and "system" in out["error"].lower()


def test_install_system_escalates_when_not_root(fake_home: Path, monkeypatch, capsys):
    monkeypatch.setattr(proton, "is_root", lambda: False)
    monkeypatch.setattr(proton.shutil, "which", lambda name: None)  # no pkexec on this host
    rc = proton.main(["install", "--system", "GE-Proton11-5", "--json"],
                     fetch_json=lambda u: {}, download=lambda *a, **k: 0)
    out = json.loads(capsys.readouterr().out)
    assert rc == proton.EXIT_ERROR and out["status"] == "needs-root"
    assert out["command"][0] == "pkexec"
    assert "install" in out["command"] and "--system" in out["command"] and "GE-Proton11-5" in out["command"]


def test_install_system_installs_when_root(fake_home: Path, monkeypatch, capsys):
    monkeypatch.setattr(proton, "is_root", lambda: True)
    monkeypatch.setattr(proton, "host_arch", lambda: "x86_64")
    net = Net("GE-Proton11-5")
    rc = proton.main(["install", "--system", "--json"], fetch_json=net.fetch_json, download=net.download)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0, out
    assert out["status"] == "installed" and out["scope"] == "system" and out["verified"] is True
    dest = proton.system_proton_dir() / "GE-Proton11-5"
    assert dest.is_dir() and (dest / "proton").is_file()
    assert not any(p.name.startswith(".lindos-proton-") for p in dest.parent.iterdir())
    # second run: already present
    rc = proton.main(["install", "--system", "--json"], fetch_json=net.fetch_json, download=net.download)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["status"] == "up-to-date"


def test_install_system_rejects_bad_tag(fake_home: Path, monkeypatch, capsys):
    monkeypatch.setattr(proton, "is_root", lambda: True)
    rc = proton.main(["install", "--system", "not-a-proton", "--json"],
                     fetch_json=lambda u: {}, download=lambda *a, **k: 0)
    out = json.loads(capsys.readouterr().out)
    assert rc == proton.EXIT_USAGE and "GE-Proton" in out["error"]
