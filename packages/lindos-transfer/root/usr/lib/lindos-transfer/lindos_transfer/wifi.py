"""Wi-Fi networks (SPEC-WINDOWS §29.8, §30.4).

* **Offline** (Windows partition): ``ProgramData\\Microsoft\\Wlansvc\\Profiles\\Interfaces\\{GUID}\\*.xml``
  gives the network name (SSID), whether it is hidden and its security type.  The password in
  those files is a Windows (DPAPI) blob: Lindos strips it on parse and never decrypts, stores or
  logs it.  NetworkManager asks for the password once (``psk-flags=1``).
* **Transfer folder**: when the user ticked the kit's explicit "include Wi-Fi passwords" option
  (Microsoft's own ``netsh wlan export profile key=clear``; manifest ``wifi.with_keys``), the
  password is used **only** for XML whose ``<protected>`` is ``false``.
* Everything goes to the root helper action ``import-wifi`` with the payload on **stdin** (never on
  a command line, where other programs could read it), which writes root-only (0600)
  NetworkManager keyfiles.  Enterprise/802.1X, WEP, OWE and ad-hoc networks are listed, not moved.

XML is matched by *local name* (Windows has written both ``http://`` and ``https://`` namespaces;
``transitionMode`` lives in the ``.../profile/v4`` namespace).  DTDs/entities are refused.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Tuple

from . import TransferError, secrets
from .report import ItemResult
from .sources import ci_path, is_link

if TYPE_CHECKING:  # pragma: no cover
    from .plan import Context

__all__ = ["WLANSVC_REL", "NM_DIR", "SECURITY_TYPES", "WifiNetwork", "parse_profile", "collect_networks",
           "helper_payload", "plan_items", "run_item"]

log = logging.getLogger("lindos-transfer.wifi")

WLANSVC_REL = ("ProgramData", "Microsoft", "Wlansvc", "Profiles", "Interfaces")
NM_DIR = "/etc/NetworkManager/system-connections"
#: Security values the helper's ``import-wifi`` action accepts.
SECURITY_TYPES = ("open", "wpa-psk", "sae")
MAX_XML = 256 << 10
_ENTERPRISE = {"wpa", "wpa2", "wpa3", "wpa3ent", "wpa3ent192"}
_PSK_TEXT = re.compile(r"^[\x20-\x7e]{8,63}$")
_PSK_HEX = re.compile(r"^[0-9A-Fa-f]{64}$")


@dataclass
class WifiNetwork:
    ssid: str
    security: str
    hidden: bool = False
    autoconnect: bool = True
    psk: Optional[str] = field(default=None, repr=False)      # never printed, logged or reported
    source: str = ""

    def payload(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"ssid": self.ssid, "security": self.security, "hidden": self.hidden,
                               "agent_owned": self.security != "open" and not self.psk}
        if self.psk and self.security != "open":
            out["psk"] = self.psk
        return out

    def public(self) -> Dict[str, Any]:
        """Safe-to-show view (no password)."""
        return {"ssid": self.ssid, "security": self.security, "hidden": self.hidden,
                "password_included": bool(self.psk)}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(el: Optional[ET.Element], *names: str) -> Optional[ET.Element]:
    for name in names:
        if el is None:
            return None
        el = next((c for c in el if _local(c.tag) == name), None)
    return el


def _text(el: Optional[ET.Element]) -> str:
    return (el.text or "").strip() if el is not None else ""


def parse_profile(data: bytes, *, allow_key: bool = False) -> Tuple[Optional[WifiNetwork], Optional[str]]:
    """Parse one WLANProfile XML.  Returns ``(network, None)`` or ``(None, reason it is not moved)``.

    With *allow_key* False (offline Windows profiles) the key material is dropped unread.
    """
    if len(data) > MAX_XML:
        return None, "file too large to be a Wi-Fi profile"
    head = data[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in data.lower():
        return None, "unexpected XML content"
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None, "damaged Wi-Fi profile"
    if _local(root.tag) != "WLANProfile":
        return None, "not a Windows Wi-Fi profile"
    security_el = _child(root, "MSM", "security")
    shared = _child(security_el, "sharedKey")
    key: Optional[str] = None
    protected = True
    if shared is not None:
        protected = _text(_child(shared, "protected")).lower() != "false"
        for el in list(shared):
            if _local(el.tag) in secrets.WLAN_SECRET_ELEMENTS:
                if allow_key and not protected:
                    key = (el.text or "").strip()
                el.text = None
                shared.remove(el)
    ssid_el = _child(root, "SSIDConfig", "SSID")
    ssid = ""
    hex_ssid = _text(_child(ssid_el, "hex"))
    if hex_ssid and re.fullmatch(r"(?:[0-9A-Fa-f]{2})+", hex_ssid):
        try:
            ssid = bytes.fromhex(hex_ssid).decode("utf-8")
        except UnicodeDecodeError:
            ssid = ""
    if not ssid:
        ssid = _text(_child(ssid_el, "name")) or _text(_child(root, "name"))
    if not ssid or len(ssid.encode("utf-8")) > 32 or any(ord(c) < 32 for c in ssid):
        return None, "network name cannot be used"
    if _text(_child(root, "connectionType")).upper() == "IBSS":
        return None, f"'{ssid}': ad-hoc (computer-to-computer) network - not moved"
    hidden = _text(_child(root, "SSIDConfig", "nonBroadcast")).lower() == "true"
    auto = _text(_child(root, "connectionMode")).lower() != "manual"
    auth_el = _child(security_el, "authEncryption")
    auth = _text(_child(auth_el, "authentication")).lower()
    enc = _text(_child(auth_el, "encryption")).lower()
    onex = _text(_child(auth_el, "useOneX")).lower() == "true"
    transition = auth_el is not None and any(
        _local(c.tag) == "transitionMode" and (c.text or "").strip().lower() == "true" for c in auth_el.iter())
    if onex or auth in _ENTERPRISE:
        return None, f"'{ssid}': work/school (802.1X) network - set it up by hand (it needs your account details)"
    if auth in ("open", "shared") and enc == "wep":
        return None, f"'{ssid}': old WEP security (unsafe) - add it by hand if you still need it"
    if auth == "owe":
        return None, f"'{ssid}': Enhanced Open (OWE) network - add it by hand from the network menu"
    if auth == "open" and enc in ("none", ""):
        security = "open"
    elif auth in ("wpapsk", "wpa2psk"):
        security = "wpa-psk"
    elif auth == "wpa3sae":
        security = "wpa-psk" if transition else "sae"
    else:
        return None, f"'{ssid}': unsupported security type ({auth or 'unknown'})"
    net = WifiNetwork(ssid=ssid, security=security, hidden=hidden, autoconnect=auto)
    if key and security != "open":
        if (_PSK_TEXT.match(key) or (security == "wpa-psk" and _PSK_HEX.match(key))):
            net.psk = key
    return net, None


def _xml_files(folder: Path, *, recursive: bool) -> Iterable[Path]:
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return []
    out: List[Path] = []
    for e in entries:
        if is_link(e):
            continue
        if e.is_dir() and recursive:
            out.extend(_xml_files(e, recursive=False))
        elif e.is_file() and e.name.lower().endswith(".xml"):
            out.append(e)
    return out


def wifi_source(ctx: "Context") -> Tuple[Optional[Path], bool]:
    """(folder with profile XML, passwords allowed) for the source."""
    src = ctx.source
    if src.is_bundle:
        wifi = src.manifest.get("wifi") or {}
        rel = wifi.get("dir")
        folder = src.path(str(rel)) if rel else None
        return folder, bool(wifi.get("with_keys"))
    return src.path(list(WLANSVC_REL)), False


def collect_networks(ctx: "Context", folder: Optional[Path] = None) -> Tuple[List[WifiNetwork], List[Dict[str, str]]]:
    """Networks to import and the ones left behind (with reasons)."""
    base, allow_key = wifi_source(ctx)
    folder = folder or base
    nets: List[WifiNetwork] = []
    skipped: List[Dict[str, str]] = []
    if folder is None or not folder.is_dir():
        return nets, skipped
    seen: Dict[Tuple[str, str], WifiNetwork] = {}
    for path in _xml_files(folder, recursive=not ctx.source.is_bundle):
        try:
            data = secrets.read_bytes(path, MAX_XML)
        except OSError as exc:
            skipped.append({"path": str(path), "reason": f"cannot be read ({exc.strerror or exc})"})
            continue
        net, why = parse_profile(data, allow_key=allow_key)
        del data
        if net is None:
            skipped.append({"path": str(path), "reason": why or "not moved"})
            continue
        net.source = str(path)
        key = (net.ssid, net.security)
        if key in seen:
            if net.psk and not seen[key].psk:
                seen[key].psk = net.psk
            continue
        seen[key] = net
        nets.append(net)
    return nets, skipped


def helper_payload(networks: Iterable[WifiNetwork]) -> Dict[str, Any]:
    return {"networks": [n.payload() for n in networks]}


def plan_items(ctx: "Context") -> Tuple[List[Dict[str, Any]], List[Dict[str, str]], List[str]]:
    from .plan import LABELS, make_item

    folder, allow_key = wifi_source(ctx)
    nets, skipped = collect_networks(ctx)
    if not nets:
        return [], skipped, []
    names = ", ".join(n.ssid for n in nets[:20]) + (" ..." if len(nets) > 20 else "")
    with_pw = sum(1 for n in nets if n.psk)
    notes = [f"{len(nets)} network(s): {names}"]
    if with_pw:
        notes.append(f"Passwords included for {with_pw} network(s) (from your Windows export).")
    if with_pw < len(nets):
        notes.append("Lindos asks for the other passwords the first time you connect.")
    notes.append("Adding networks needs your admin password.")
    return [make_item("wifi", "wifi", LABELS["wifi"], folder, NM_DIR, files=len(nets), notes=notes)], skipped, []


def _scrub(text: str, nets: Iterable[WifiNetwork]) -> str:
    for n in nets:
        if n.psk:
            text = text.replace(n.psk, "********")
    return text


def run_item(item: Dict[str, Any], ctx: "Context") -> ItemResult:
    nets, skipped = collect_networks(ctx, Path(item["src"]) if item.get("src") else None)
    res = ItemResult(id=item["id"], category=item["category"], label=item.get("label", ""), dest=NM_DIR,
                     skipped=skipped)
    if not nets:
        res.status = "skipped"
        res.notes.append("no Wi-Fi networks to add")
        return res
    res.notes.append("Networks: " + ", ".join(n.ssid for n in nets))
    if any(n.psk for n in nets):
        res.notes.append("Wi-Fi passwords came from your Windows export.")
    if ctx.dry_run:
        res.files = len(nets)
        return res
    helper = ctx.helper()
    payload = helper_payload(nets)
    try:
        result = helper.run_privileged("import-wifi", payload, stdin_payload=True)
    except TypeError as exc:
        # An older lindos-core cannot pass the payload on stdin; never fall back to the command line.
        raise TransferError("This Lindos core is too old to add Wi-Fi networks safely (update lindos-core).") from exc
    finally:
        payload = {}
    if getattr(result, "ok", False):
        res.files = len(nets)
        return res
    message = _scrub(str(getattr(result, "message", "") or "the helper failed"), nets)
    res.status = "failed"
    res.errors.append({"path": NM_DIR, "reason": message})
    return res
