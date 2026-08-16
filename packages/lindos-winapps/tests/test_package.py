"""Package-level sanity + honesty (SPEC-VM §20, §22, §26)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent          # packages/lindos-winapps
ROOT = PKG_ROOT / "root"
BIN = ROOT / "usr" / "bin"
LIB = ROOT / "usr" / "lib" / "lindos-winapps" / "lindos_winapps"
SHARE = ROOT / "usr" / "share" / "lindos" / "winapps"
APPS = ROOT / "usr" / "share" / "applications"
ICONS = ROOT / "usr" / "share" / "icons" / "hicolor" / "scalable" / "apps"
DEBIAN = PKG_ROOT / "DEBIAN"


def _control() -> dict:
    fields = {}
    for line in (DEBIAN / "control").read_text(encoding="utf-8").splitlines():
        if line[:1].isspace() or not line.strip():
            continue
        k, v = line.split(":", 1)
        fields[k.strip()] = v.strip()
    return fields


def test_control_fields():
    c = _control()
    assert c["Package"] == "lindos-winapps"
    assert c["Version"] == "1.0.0"
    assert c["Architecture"] == "all"
    assert c["Maintainer"] == "Lindos Team <team@lindos.dev>"
    for dep in ("python3", "lindos-core", "freerdp3-x11 | freerdp2-x11"):
        assert dep in c["Depends"], dep
    assert "lindos-vm | libvirt-daemon-system" in c["Recommends"]
    assert "podman" in c["Suggests"]


def test_maintainer_scripts_posix_sh():
    for name in ("postinst", "postrm"):
        text = (DEBIAN / name).read_text(encoding="utf-8")
        assert text.startswith("#!/bin/sh\n")
        assert "set -e" in text
        assert "\r" not in text


def test_lib_layout_has_required_modules():
    for mod in ("__init__", "backend", "apps", "rdp", "cli"):
        assert (LIB / f"{mod}.py").is_file(), mod


def test_bin_launcher():
    text = (BIN / "lindos-winapps").read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env python3\n")
    assert "/usr/lib/lindos-winapps" in text and "lindos_winapps" in text


def test_top_desktop_file():
    text = (APPS / "lindos-winapps.desktop").read_text(encoding="utf-8")
    assert text.startswith("[Desktop Entry]\n")
    fields = dict(l.split("=", 1) for l in text.splitlines() if "=" in l and not l.startswith("["))
    assert fields["Name"] == "Windows Apps (Lindos)"
    assert fields["Exec"] == "lindos-winapps list"
    assert fields["TryExec"] == "lindos-winapps"
    assert fields["Icon"] == "lindos-winapp"
    assert (ICONS / "lindos-winapp.svg").is_file()


def test_catalog_shape():
    raw = json.loads((SHARE / "apps.json").read_text(encoding="utf-8"))
    ids = {a["id"] for a in raw["apps"]}
    required = {"photoshop", "illustrator", "premiere", "office-word", "office-excel",
               "office-powerpoint", "office-outlook", "explorer"}
    assert required <= ids
    for app in raw["apps"]:
        assert app["name"] and app["rdp_path"] and app.get("note")


# --------------------------------------------------------------------------- #
# Honesty (SPEC-VM §20): winapps ships NO VM-detection/anti-cheat evasion and
# stores NO credentials / license bypass.
# --------------------------------------------------------------------------- #
FORBIDDEN_TOKENS = (
    "kvm=off", "hv-vendor-id", "hv_vendor_id", "smbios", "acpitable", "-acpitable",
    "hidden state", "spoof", "hwid", "attestation", "vm-detect", "vmdetect",
)


def _text_files():
    for dirpath, _dirs, files in os.walk(ROOT):
        for fn in files:
            p = Path(dirpath) / fn
            try:
                yield p, p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue


def test_no_spoofing_or_evasion_tokens():
    for path, text in _text_files():
        low = text.lower()
        for tok in FORBIDDEN_TOKENS:
            assert tok not in low, f"{path} contains forbidden token '{tok}'"


def test_no_stored_password_or_license_bypass():
    for path, text in _text_files():
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"') or stripped.startswith("*"):
                continue
            # An assignment giving RDP_PASS an actual value must never be shipped.
            assert not stripped.startswith("RDP_PASS=") or stripped in ("RDP_PASS=", ""), \
                f"{path}: {line}"


def test_bin_runs_help():
    env = dict(os.environ)
    core_lib = PKG_ROOT.parent / "lindos-core" / "root" / "usr" / "lib" / "python3" / "dist-packages"
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(core_lib), env.get("PYTHONPATH", "")) if p)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run([sys.executable, str(BIN / "lindos-winapps"), "--help"],
                          capture_output=True, text=True, env=env, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    assert "setup" in proc.stdout and "run" in proc.stdout
