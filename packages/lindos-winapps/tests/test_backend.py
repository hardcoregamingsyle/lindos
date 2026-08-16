"""Backend config + reachability, all guarded/injectable (SPEC-VM §22, §26)."""
from __future__ import annotations

import sys
from pathlib import Path

from lindos_winapps import backend as backend_mod
from lindos_winapps import winapps_conf_path


def test_defaults():
    cfg = backend_mod.default_config()
    assert cfg.backend == "libvirt"
    assert cfg.vm_name == "RDPWindows"
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 3389


def test_render_conf_never_stores_password():
    cfg = backend_mod.default_config()
    cfg.user = "alice"
    text = backend_mod.render_conf(cfg)
    # The password must never be written as an assignable value.
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not stripped.startswith("RDP_PASS="), line
    assert 'BACKEND="libvirt"' in text
    assert 'RDP_USER="alice"' in text
    assert 'VM_NAME="RDPWindows"' in text
    assert "RDP_PASS" in text          # mentioned, but only as documentation in comments


def test_write_and_reload(home: Path):
    cfg = backend_mod.default_config()
    cfg.user = "bob"
    cfg.host = "192.168.122.2"
    path = backend_mod.write_config(cfg)
    assert path == winapps_conf_path()
    assert path.parent == home / ".config" / "lindos" / "winapps"
    if not sys.platform.startswith("win"):
        assert (path.stat().st_mode & 0o777) == 0o600
    reloaded = backend_mod.load_config()
    assert reloaded.user == "bob"
    assert reloaded.host == "192.168.122.2"
    assert reloaded.backend == "libvirt"


def test_load_config_missing_returns_defaults(home: Path):
    cfg = backend_mod.load_config()
    assert cfg.backend == "libvirt" and cfg.vm_name == "RDPWindows"


def test_parse_conf_and_aliases():
    m = backend_mod.parse_conf(
        "# comment\n\nexport WAFLAVOR='podman'\nRDP_IP=\"10.0.0.5\"\nRDP_USER=carol\n"
        "RDP_PASS=\"secret\"\nCUSTOM=abc\n")
    assert m["WAFLAVOR"] == "podman"
    assert m["RDP_IP"] == "10.0.0.5"
    cfg = backend_mod._config_from_map(m)
    assert cfg.backend == "podman"        # WAFLAVOR alias honoured
    assert cfg.host == "10.0.0.5"         # RDP_IP alias honoured
    assert cfg.user == "carol"
    assert cfg.extra.get("CUSTOM") == "abc"
    assert "RDP_PASS" not in cfg.extra    # password never retained


def test_invalid_backend_falls_back():
    cfg = backend_mod._config_from_map({"BACKEND": "vmware"})
    assert cfg.backend == "libvirt"


def test_domain_running(fake_which, fake_run):
    # no virsh -> unknown (None)
    assert backend_mod.domain_running("RDPWindows", which=fake_which(), run=fake_run) is None
    which = fake_which("virsh")
    fake_run.set("domstate", stdout="running\n", rc=0)
    assert backend_mod.domain_running("RDPWindows", which=which, run=fake_run) is True
    fake_run.set("domstate", stdout="shut off\n", rc=0)
    assert backend_mod.domain_running("RDPWindows", which=which, run=fake_run) is False
    fake_run.set("domstate", stdout="", rc=1)
    assert backend_mod.domain_running("RDPWindows", which=which, run=fake_run) is False


def test_container_running(fake_which, fake_run):
    assert backend_mod.container_running(which=fake_which(), run=fake_run) is None
    which = fake_which("podman")
    fake_run.set("ps", stdout="WinApps\n", rc=0)
    assert backend_mod.container_running(which=which, run=fake_run) is True
    fake_run.set("ps", stdout="", rc=0)
    assert backend_mod.container_running(which=which, run=fake_run) is False


def test_rdp_port_open_injectable():
    assert backend_mod.rdp_port_open("h", 3389, connect=lambda h, p, t: True) is True
    assert backend_mod.rdp_port_open("h", 3389, connect=lambda h, p, t: False) is False
    cfg = backend_mod.default_config()
    assert backend_mod.backend_reachable(cfg, connect=lambda h, p, t: True) is True


def test_check_report(fake_which, fake_run):
    cfg = backend_mod.default_config()
    which = fake_which("virsh", "xfreerdp3")
    fake_run.set("domstate", stdout="running\n", rc=0)
    rep = backend_mod.check(cfg, which=which, run=fake_run, connect=lambda h, p, t: True)
    assert rep["freerdp_present"] is True
    assert rep["freerdp"].endswith("xfreerdp3")
    assert rep["backend_running"] is True
    assert rep["rdp_port_open"] is True
    assert rep["reachable"] is True
    # nothing available -> not reachable, freerdp missing
    rep2 = backend_mod.check(cfg, which=fake_which(), run=fake_run, connect=lambda h, p, t: False)
    assert rep2["freerdp_present"] is False
    assert rep2["reachable"] is False
    assert rep2["backend_running"] is None
