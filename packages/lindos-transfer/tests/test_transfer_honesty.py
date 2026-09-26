"""Honesty-rule tests that apply to the whole package, not one module (SPEC-WINDOWS §27, §27.1;
mirrors the FORBIDDEN_TOKENS scans of ``lindos-vm``/``lindos-gaming``, extended to every new file
here per SPEC-WINDOWS §27.1)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]                      # packages/lindos-transfer
LIB = PKG / "root" / "usr" / "lib" / "lindos-transfer" / "lindos_transfer"
BIN = PKG / "root" / "usr" / "bin" / "lindos-transfer"
APP_MAP = PKG / "root" / "usr" / "share" / "lindos" / "transfer" / "app-map.json"

#: Same vocabulary as packages/lindos-vm/tests/test_no_spoof.py and lindos-gaming's FORBIDDEN_TOKENS
#: (SPEC-WINDOWS §27.1): no anti-cheat circumvention, no spoofing of any kind.
FORBIDDEN_TOKENS = ("spoof", "hwid", "attestation", "smbios", "vm-detect", "vmdetect", "kvm=off",
                   "hv-vendor-id", "acpitable", "hide_hypervisor", "hidden_hypervisor",
                   "fake_tpm", "user-agent spoof", "useragent spoof")

PY_FILES = sorted(LIB.glob("*.py")) + [BIN]


@pytest.mark.parametrize("path", PY_FILES, ids=lambda p: p.name)
def test_no_evasion_tokens_in_source(path: Path) -> None:
    text = path.read_text(encoding="utf-8").lower()
    for tok in FORBIDDEN_TOKENS:
        assert tok not in text, f"HONESTY VIOLATION: {tok!r} found in {path.name}"


def test_no_evasion_tokens_in_app_map() -> None:
    text = APP_MAP.read_text(encoding="utf-8").lower()
    for tok in FORBIDDEN_TOKENS:
        assert tok not in text, f"HONESTY VIOLATION: {tok!r} found in app-map.json"


# --------------------------------------------------------------------------- #
# never claim a bypass of DRM/licensing/activation
# --------------------------------------------------------------------------- #
LICENSE_BYPASS_WORDS = ("crack", "keygen", "activation bypass", "bypass activation", "pirat")


@pytest.mark.parametrize("path", PY_FILES, ids=lambda p: p.name)
def test_no_license_bypass_language(path: Path) -> None:
    text = path.read_text(encoding="utf-8").lower()
    for word in LICENSE_BYPASS_WORDS:
        assert word not in text, f"{word!r} found in {path.name}"


# --------------------------------------------------------------------------- #
# secrets are never written into the app-map (it's just an install map, no data) -- a legitimate
# product name like "1Password" or "LastPass" is fine; an actual credential-shaped KEY is not.
# --------------------------------------------------------------------------- #
def test_app_map_never_contains_credential_fields() -> None:
    data = json.loads(APP_MAP.read_text(encoding="utf-8"))
    forbidden_keys = {"password", "passwd", "api_key", "apikey", "secret", "token", "credential"}

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                assert key.lower() not in forbidden_keys, f"credential-shaped key {key!r} in app-map.json"
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)


# --------------------------------------------------------------------------- #
# encoding / line-ending hygiene for every Python source file (repo convention)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", PY_FILES, ids=lambda p: p.name)
def test_source_files_are_lf_utf8_no_bom(path: Path) -> None:
    data = path.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf"), f"{path.name} must not have a BOM"
    assert b"\r" not in data, f"{path.name} must use LF line endings"
    data.decode("utf-8")  # must not raise


def test_app_map_is_lf_utf8_no_bom() -> None:
    data = APP_MAP.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in data
    data.decode("utf-8")


# --------------------------------------------------------------------------- #
# never shell=True, matching CONTINUATION.md / SPEC-WINDOWS §29 code-style rule
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", PY_FILES, ids=lambda p: p.name)
def test_never_uses_shell_true(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"shell\s*=\s*True", text), f"shell=True found in {path.name}"


# --------------------------------------------------------------------------- #
# no secret ever appears in an argv-building call (the two carriers that could leak one)
# --------------------------------------------------------------------------- #
def test_wifi_module_never_builds_argv_with_a_password() -> None:
    text = (LIB / "wifi.py").read_text(encoding="utf-8")
    # the only place a password may travel is the helper payload dict, sent over stdin
    assert "stdin_payload=True" in text
    assert re.search(r"subprocess\.(run|Popen|call)\([^)]*psk", text) is None
