"""Tests for Wi-Fi network transfer (SPEC-WINDOWS §29.8, §27.3).

Covers: SSID from hex vs. name, hidden/auto-connect, the security-type mapping table, key material
stripped on parse (never decrypted), enterprise/WEP/OWE/ad-hoc networks listed but not moved, the
namespace-agnostic (local-name) XML matching for both v1 http/https and the v4 transitionMode
element, and that a password only ever reaches the helper through
``run_privileged(..., stdin_payload=True)`` -- never argv, never a plain file.
"""
from __future__ import annotations

import types
from pathlib import Path

from lindos_transfer import TransferError
from lindos_transfer.wifi import collect_networks, parse_profile, plan_items, run_item


def _profile_xml(*, ssid="Home", auth="WPA2PSK", enc="AES", protected=True, key="correcthorsebatterystaple",
                 namespace="http://www.microsoft.com/networking/WLAN/profile/v1", hidden=False,
                 connection_mode="auto", one_x=False, transition=False, conn_type="ESS") -> bytes:
    transition_el = (f'<transitionMode xmlns="http://www.microsoft.com/networking/WLAN/profile/v4">'
                     f'true</transitionMode>') if transition else ""
    return f"""<?xml version="1.0"?>
<WLANProfile xmlns="{namespace}">
  <name>{ssid}</name>
  <SSIDConfig><SSID><name>{ssid}</name></SSID><nonBroadcast>{"true" if hidden else "false"}</nonBroadcast></SSIDConfig>
  <connectionType>{conn_type}</connectionType>
  <connectionMode>{connection_mode}</connectionMode>
  <MSM><security>
    <authEncryption><authentication>{auth}</authentication><encryption>{enc}</encryption>
      <useOneX>{"true" if one_x else "false"}</useOneX>{transition_el}</authEncryption>
    <sharedKey><keyType>passPhrase</keyType><protected>{"true" if protected else "false"}</protected>
      <keyMaterial>{key}</keyMaterial></sharedKey>
  </security></MSM>
</WLANProfile>""".encode("utf-8")


# --------------------------------------------------------------------------- #
# parse_profile()
# --------------------------------------------------------------------------- #
def test_parse_profile_wpa2_open_key_when_allowed() -> None:
    net, why = parse_profile(_profile_xml(protected=False), allow_key=True)
    assert why is None
    assert net.ssid == "Home" and net.security == "wpa-psk" and net.psk == "correcthorsebatterystaple"


def test_parse_profile_never_returns_key_when_protected() -> None:
    net, why = parse_profile(_profile_xml(protected=True), allow_key=True)
    assert net.psk is None  # a DPAPI blob is never usable/returned even with allow_key


def test_parse_profile_offline_never_reads_key_even_if_clear() -> None:
    net, why = parse_profile(_profile_xml(protected=False), allow_key=False)
    assert net.psk is None


def test_parse_profile_https_namespace_and_v4_transitionmode() -> None:
    net, why = parse_profile(_profile_xml(auth="WPA3SAE", transition=True,
                                          namespace="https://www.microsoft.com/networking/WLAN/profile/v1"))
    assert why is None and net.security == "wpa-psk"  # transition -> compatible with WPA2 clients


def test_parse_profile_wpa3sae_without_transition_is_sae() -> None:
    net, why = parse_profile(_profile_xml(auth="WPA3SAE", transition=False))
    assert net.security == "sae"


def test_parse_profile_enterprise_and_onex_are_listed_not_moved() -> None:
    net, why = parse_profile(_profile_xml(auth="WPA2", one_x=True))
    assert net is None and "802.1X" in why


def test_parse_profile_wep_is_listed_not_moved() -> None:
    net, why = parse_profile(_profile_xml(auth="shared", enc="WEP"))
    assert net is None and "WEP" in why


def test_parse_profile_owe_is_listed_not_moved() -> None:
    net, why = parse_profile(_profile_xml(auth="owe", enc="none"))
    assert net is None and "OWE" in why


def test_parse_profile_ibss_adhoc_is_skipped() -> None:
    net, why = parse_profile(_profile_xml(conn_type="IBSS"))
    assert net is None and "ad-hoc" in why


def test_parse_profile_open_network() -> None:
    net, why = parse_profile(_profile_xml(auth="open", enc="none"), )
    assert why is None and net.security == "open"


def test_parse_profile_ssid_from_hex_element() -> None:
    xml = _profile_xml().replace(b"<name>Home</name></SSID>",
                                 b"<name>Home</name><hex>486F6D65</hex></SSID>", 1)
    # replace only inside SSIDConfig's SSID/name, so both the outer <name> and SSID's stay intact
    net, why = parse_profile(xml)
    assert why is None and net.ssid == "Home"


def test_parse_profile_hidden_and_manual_connection_mode() -> None:
    net, why = parse_profile(_profile_xml(hidden=True, connection_mode="manual"))
    assert net.hidden is True and net.autoconnect is False


def test_parse_profile_rejects_dtd_and_oversized() -> None:
    net, why = parse_profile(b"<!DOCTYPE x><WLANProfile></WLANProfile>")
    assert net is None
    net2, why2 = parse_profile(b"x" * 300000)
    assert net2 is None and "large" in why2


def test_parse_profile_not_a_wlan_profile() -> None:
    net, why = parse_profile(b"<Other/>")
    assert net is None and "not a Windows Wi-Fi profile" in why


def test_parse_profile_damaged_xml() -> None:
    net, why = parse_profile(b"<WLANProfile><unterminated>")
    assert net is None and "damaged" in why


# --------------------------------------------------------------------------- #
# helper_payload(): never leaks a password into the "public" summary
# --------------------------------------------------------------------------- #
def test_network_payload_and_public_view() -> None:
    net, _why = parse_profile(_profile_xml(protected=False), allow_key=True)
    payload = net.payload()
    assert payload["psk"] == "correcthorsebatterystaple"
    public = net.public()
    assert "psk" not in public and public["password_included"] is True
    assert repr(net).find("correcthorsebatterystaple") == -1  # dataclass repr hides the field


def test_open_network_payload_has_no_psk_key() -> None:
    net, _why = parse_profile(_profile_xml(auth="open", enc="none"))
    payload = net.payload()
    assert "psk" not in payload and payload["agent_owned"] is False


def test_agent_owned_true_when_no_password_known() -> None:
    net, _why = parse_profile(_profile_xml(protected=True))  # offline: never has the key
    payload = net.payload()
    assert payload["agent_owned"] is True and "psk" not in payload


# --------------------------------------------------------------------------- #
# collect_networks(): offline dedupe, recursive scan
# --------------------------------------------------------------------------- #
def test_collect_networks_from_partition_recurses_interfaces(tmp_path: Path) -> None:
    base = tmp_path / "ProgramData" / "Microsoft" / "Wlansvc" / "Profiles" / "Interfaces" / "{GUID}"
    base.mkdir(parents=True)
    (base / "Wi-Fi-Home.xml").write_bytes(_profile_xml(ssid="Home"))
    (base / "Wi-Fi-Work.xml").write_bytes(_profile_xml(ssid="Work", one_x=True))
    ctx = types.SimpleNamespace(source=types.SimpleNamespace(is_bundle=False,
                                                             path=lambda rel: (tmp_path / "/".join(rel)
                                                                               if isinstance(rel, list) else tmp_path)))
    nets, skipped = collect_networks(ctx, base)
    assert {n.ssid for n in nets} == {"Home"}
    assert any("Work" in s["reason"] for s in skipped)


def test_collect_networks_bundle_with_keys(tmp_path: Path) -> None:
    wifi_dir = tmp_path / "wifi"
    wifi_dir.mkdir()
    (wifi_dir / "Wi-Fi-Cafe.xml").write_bytes(_profile_xml(ssid="Cafe", protected=False, key="opensesame123"))

    class BundleSource:
        is_bundle = True
        manifest = {"wifi": {"dir": "wifi", "with_keys": True}}

        def path(self, rel):
            return tmp_path / str(rel)

    ctx = types.SimpleNamespace(source=BundleSource())
    nets, skipped = collect_networks(ctx)
    assert len(nets) == 1 and nets[0].psk == "opensesame123"


def test_collect_networks_bundle_without_keys_never_reads_key() -> None:
    class BundleSource:
        is_bundle = True
        manifest = {"wifi": {"dir": "wifi", "with_keys": False}}

        def path(self, rel):
            return None

    ctx = types.SimpleNamespace(source=BundleSource())
    nets, skipped = collect_networks(ctx)
    assert nets == [] and skipped == []


# --------------------------------------------------------------------------- #
# plan_items() / run_item(): the helper call, stdin_payload, never argv
# --------------------------------------------------------------------------- #
def _ctx_with_networks(tmp_path: Path, fake_helper):
    base = tmp_path / "ProgramData" / "Microsoft" / "Wlansvc" / "Profiles" / "Interfaces" / "{GUID}"
    base.mkdir(parents=True)
    (base / "Wi-Fi-Home.xml").write_bytes(_profile_xml(ssid="Home", protected=True))
    root = tmp_path

    class Source:
        is_bundle = False

        def path(self, rel):
            parts = rel if isinstance(rel, list) else [rel]
            return root.joinpath(*parts)

    ctx = types.SimpleNamespace(source=Source(), dry_run=False)
    ctx.helper = lambda: fake_helper
    return ctx


def test_plan_items_lists_networks_and_notes_password_status(tmp_path: Path, fake_helper) -> None:
    ctx = _ctx_with_networks(tmp_path, fake_helper)
    items, skipped, warnings = plan_items(ctx)
    assert len(items) == 1
    assert items[0]["files"] == 1
    assert any("Home" in n for n in items[0]["notes"])


def test_run_item_calls_helper_with_stdin_payload_true_never_argv(tmp_path: Path, fake_helper) -> None:
    ctx = _ctx_with_networks(tmp_path, fake_helper)
    items, _skipped, _warnings = plan_items(ctx)
    res = run_item(items[0], ctx)
    assert res.status == "done" and res.files == 1
    assert len(fake_helper.calls) == 1
    action, payload = fake_helper.calls[0]
    assert action == "import-wifi"
    assert payload["networks"][0]["ssid"] == "Home"
    assert "psk" not in payload["networks"][0]  # the offline profile was protected: no key to send
    # the call itself must have used stdin_payload=True -- FakeHelper.run_privileged only accepts
    # (and therefore only reaches this assertion) if wifi.py actually passed that keyword.


def test_run_item_scrubs_password_from_a_failed_helper_message(tmp_path: Path, fake_helper) -> None:
    # only a *bundle* source (the user's explicit "include Wi-Fi passwords" export) ever carries a
    # real key -- that is the only case where there is a secret to scrub in the first place.
    wifi_dir = tmp_path / "wifi"
    wifi_dir.mkdir()
    (wifi_dir / "Wi-Fi-Cafe.xml").write_bytes(_profile_xml(ssid="Cafe", protected=False, key="opensesame123"))

    class BundleSource:
        is_bundle = True
        manifest = {"wifi": {"dir": "wifi", "with_keys": True}}

        def path(self, rel):
            return tmp_path / str(rel)

    ctx = types.SimpleNamespace(source=BundleSource(), dry_run=False, helper=lambda: fake_helper)
    items, _skipped, _warnings = plan_items(ctx)
    assert items and items[0]["files"] == 1
    fake_helper.result_for = lambda action, payload: types.SimpleNamespace(
        ok=False, message="helper rejected key opensesame123")
    res = run_item(items[0], ctx)
    assert res.status == "failed"
    assert "opensesame123" not in str(res.errors)


def test_run_item_no_networks_is_skipped(tmp_path: Path, fake_helper) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    class Source:
        is_bundle = False

        def path(self, rel):
            return empty

    ctx = types.SimpleNamespace(source=Source(), dry_run=False, helper=lambda: fake_helper)
    item = {"id": "wifi", "category": "wifi", "label": "Wi-Fi networks", "src": str(empty)}
    res = run_item(item, ctx)
    assert res.status == "skipped"


def test_run_item_helper_too_old_raises_honest_error(tmp_path: Path) -> None:
    class OldHelper:
        def run_privileged(self, action, payload):  # no stdin_payload kwarg at all
            raise TypeError("unexpected keyword argument 'stdin_payload'")

    ctx = _ctx_with_networks(tmp_path, OldHelper())
    items, _s, _w = plan_items(ctx)
    try:
        run_item(items[0], ctx)
        raised = False
    except TransferError as exc:
        raised = True
        assert "too old" in str(exc)
    assert raised
