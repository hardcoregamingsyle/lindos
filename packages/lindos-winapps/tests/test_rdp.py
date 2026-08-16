"""FreeRDP RemoteApp command building + credential handling (SPEC-VM §22, §26)."""
from __future__ import annotations

import types
from pathlib import Path

from lindos_winapps import backend as backend_mod
from lindos_winapps import rdp as rdp_mod


def _app(path=r"C:\Program Files\Adobe\Photoshop.exe", name="Adobe Photoshop"):
    return types.SimpleNamespace(rdp_path=path, name=name)


def test_find_freerdp_prefers_v3(fake_which):
    assert rdp_mod.find_freerdp(fake_which("xfreerdp3", "xfreerdp")).endswith("xfreerdp3")
    assert rdp_mod.find_freerdp(fake_which("xfreerdp")).endswith("xfreerdp")
    assert rdp_mod.find_freerdp(fake_which()) is None


def test_generation():
    assert rdp_mod.freerdp_generation("/usr/bin/xfreerdp3") == 3
    assert rdp_mod.freerdp_generation("/usr/bin/xfreerdp") == 2


def test_password_source_env_then_file_then_prompt(home: Path, monkeypatch):
    # prompt when nothing is set
    val, src = rdp_mod.password_source(env={})
    assert val is None and src == "prompt"
    # RDP_PASS env wins
    val, src = rdp_mod.password_source(env={"RDP_PASS": "s3cret"})
    assert val == "s3cret" and "RDP_PASS" in src
    # user-created file
    pf = rdp_mod.rdp_pass_file()
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text("filepass\n", encoding="utf-8")
    val, src = rdp_mod.password_source(env={})
    assert val == "filepass" and str(pf) in src


def test_build_command_gen3():
    cfg = backend_mod.default_config()
    cfg.user = "alice"
    cfg.domain = "WORKGROUP"
    cmd = rdp_mod.build_command(_app(), cfg, "/usr/bin/xfreerdp3", password="pw")
    assert cmd[0] == "/usr/bin/xfreerdp3"
    assert "/v:127.0.0.1" in cmd                 # default port omitted
    assert "/u:alice" in cmd
    assert "/d:WORKGROUP" in cmd
    assert "/p:pw" in cmd
    appspec = [c for c in cmd if c.startswith("/app:program:")]
    assert appspec and "Photoshop.exe" in appspec[0] and "name:Adobe Photoshop" in appspec[0]
    # flags from config were split into individual tokens
    assert "+clipboard" in cmd


def test_build_command_gen3_with_args():
    cfg = backend_mod.default_config()
    cmd = rdp_mod.build_command(_app(), cfg, "xfreerdp3", args=["/foo", "bar"])
    spec = [c for c in cmd if c.startswith("/app:program:")][0]
    assert "cmd:/foo bar" in spec


def test_build_command_gen2():
    cfg = backend_mod.default_config()
    cfg.user = "bob"
    cmd = rdp_mod.build_command(_app(), cfg, "/usr/bin/xfreerdp", args=["x"])
    assert any(c.startswith("/app:") and "program:" not in c for c in cmd)
    assert "/app-name:Adobe Photoshop" in cmd
    assert "/app-cmd:x" in cmd


def test_port_in_server_when_nondefault():
    cfg = backend_mod.default_config()
    cfg.port = 3390
    cmd = rdp_mod.build_command(_app(), cfg, "xfreerdp3")
    assert "/v:127.0.0.1:3390" in cmd


def test_redact_hides_password():
    red = rdp_mod.redact_command(["xfreerdp3", "/u:bob", "/p:hunter2", "/v:h"])
    assert "/p:hunter2" not in red
    assert "/p:******" in red


def test_launch_no_freerdp_returns_1(fake_which):
    cfg = backend_mod.default_config()
    rc = rdp_mod.launch(_app(), cfg, which=fake_which())
    assert rc == 1


def test_launch_runs_and_no_password_leak(home: Path, fake_which):
    cfg = backend_mod.default_config()
    calls = []

    def run(cmd, *a, **k):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0)

    rc = rdp_mod.launch(_app(), cfg, which=fake_which("xfreerdp3"), run=run,
                        env={"RDP_PASS": "topsecret"})
    assert rc == 0
    assert calls and calls[0][0].endswith("xfreerdp3")
    # the password is passed to FreeRDP (user-provided) ...
    assert any(c == "/p:topsecret" for c in calls[0])
    # ... but the redacted form used for logging never shows it
    assert "/p:topsecret" not in rdp_mod.redact_command(calls[0])
