"""End-to-end CLI dispatch and exit codes (SPEC-VM §22, §26)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lindos_winapps import cli as cli
from lindos_winapps import winapps_conf_path


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.run_cli(["--version"])
    assert exc.value.code == 0
    assert "lindos-winapps" in capsys.readouterr().out


def test_no_command_is_usage(capsys):
    assert cli.run_cli([]) == 2


def test_setup_writes_conf_without_password(home: Path, capsys):
    rc = cli.run_cli(["setup", "--user", "alice", "--host", "10.1.2.3", "--backend", "libvirt"])
    assert rc == 0
    conf = winapps_conf_path()
    assert conf.is_file()
    text = conf.read_text(encoding="utf-8")
    assert 'RDP_USER="alice"' in text
    assert 'RDP_HOST="10.1.2.3"' in text
    for line in text.splitlines():
        if not line.strip().startswith("#"):
            assert not line.strip().startswith("RDP_PASS=")
    out = capsys.readouterr().out
    assert "NOT stored" in out


def test_check_json_unreachable(home: Path, monkeypatch, capsys):
    monkeypatch.setattr(cli.backend_mod, "check", lambda cfg, **k: {
        "backend": "libvirt", "host": "127.0.0.1", "port": 3389, "vm_name": "RDPWindows",
        "freerdp": "/usr/bin/xfreerdp3", "freerdp_present": True, "backend_running": False,
        "rdp_port_open": False, "reachable": False})
    rc = cli.run_cli(["check", "--json"])
    assert rc == 3                              # backend unreachable
    data = json.loads(capsys.readouterr().out)
    assert data["reachable"] is False


def test_check_freerdp_missing_returns_error(home: Path, monkeypatch):
    monkeypatch.setattr(cli.backend_mod, "check", lambda cfg, **k: {
        "backend": "libvirt", "host": "127.0.0.1", "port": 3389, "vm_name": "RDPWindows",
        "freerdp": None, "freerdp_present": False, "backend_running": None,
        "rdp_port_open": True, "reachable": True})
    assert cli.run_cli(["check"]) == 1


def test_list_json(home: Path, staged_catalog: Path, monkeypatch, capsys):
    monkeypatch.setattr(cli.backend_mod, "check", lambda cfg, **k: {"reachable": True})
    rc = cli.run_cli(["list", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    ids = {a["id"] for a in data["apps"]}
    assert "photoshop" in ids and "office-word" in ids


def test_install_list_remove(home: Path, staged_catalog: Path, monkeypatch, capsys):
    assert cli.run_cli(["install", "photoshop"]) == 0
    capsys.readouterr()                          # flush the install output
    monkeypatch.setattr(cli.backend_mod, "check", lambda cfg, **k: {"reachable": False})
    cli.run_cli(["list", "--json"])
    data = json.loads(capsys.readouterr().out)
    ps = next(a for a in data["apps"] if a["id"] == "photoshop")
    assert ps["installed"] is True
    assert cli.run_cli(["remove", "photoshop"]) == 0


def test_install_unknown_id(home: Path, staged_catalog: Path):
    assert cli.run_cli(["install", "nope"]) == 1


def test_run_unreachable(home: Path, staged_catalog: Path, monkeypatch):
    monkeypatch.setattr(cli.rdp_mod, "find_freerdp", lambda *a, **k: "/usr/bin/xfreerdp3")
    monkeypatch.setattr(cli.backend_mod, "rdp_port_open", lambda h, p, **k: False)
    assert cli.run_cli(["run", "photoshop"]) == 3


def test_run_no_freerdp(home: Path, staged_catalog: Path, monkeypatch):
    monkeypatch.setattr(cli.rdp_mod, "find_freerdp", lambda *a, **k: None)
    assert cli.run_cli(["run", "photoshop"]) == 1


def test_run_launches(home: Path, staged_catalog: Path, monkeypatch):
    monkeypatch.setattr(cli.rdp_mod, "find_freerdp", lambda *a, **k: "/usr/bin/xfreerdp3")
    monkeypatch.setattr(cli.backend_mod, "rdp_port_open", lambda h, p, **k: True)
    seen = {}

    def fake_launch(app, cfg, *, args=None):
        seen["id"] = app.id
        seen["args"] = args
        return 0

    monkeypatch.setattr(cli.rdp_mod, "launch", fake_launch)
    assert cli.run_cli(["run", "photoshop", "--", "/quiet"]) == 0
    assert seen["id"] == "photoshop"
    assert seen["args"] == ["/quiet"]
