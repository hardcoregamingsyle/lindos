"""Client side of the privileged helper (SPEC §4.6).

``run_privileged(action, payload)`` executes
``pkexec /usr/libexec/lindos/lindos-helper <action> <json>`` (``sudo -n`` when pkexec is
missing, direct call when already root) and returns a :class:`HelperResult`.

This module also owns the **action whitelist and payload schema** (:data:`ACTIONS`,
:func:`validate_payload`) that the root helper enforces, so that user-side code can validate
before asking for a password and so that the rules are unit-tested on any OS.

Payload shapes (all JSON objects)::

    apply-mode          {"mode": id, "packages": [..], "flatpaks": [..], "services_disable": [..],
                         "services_enable": [..], "sysctl": {k: v}, "governor": g,
                         "zram_percent": n, "compositor": c, "apply_system": path|null,
                         "set_system_default": bool}     ← lindos.modes.build_system_plan()
    install-browser     {"browser": "edge"|"chrome"|"firefox"}
    install-packages    {"packages": ["gimp", ...]}            names ^[a-z0-9.+-]+$
    install-flatpaks    {"flatpaks": ["org.prismlauncher.PrismLauncher", ...]}
    set-governor        {"governor": "performance"}
    set-services        {"enable": [..], "disable": [..], "mask": [..]}   (whitelisted units)
    apply-sysctl        {"sysctl": {"vm.max_map_count": "2147483642"}}
    apply-tune          {"mode": id, "offline": bool}
    set-zram            {"percent": 75}
    install-compat      {"items": ["wine", "umu", ...]}        (optional, default all)
    install-gaming      {"items": ["steam", "lutris", ...]}
    install-drivers     {"args": ["--nvidia-open"]}   or {"driver": "nvidia-open"}
    set-fan-profile     {"profile": "quiet"}
    set-sched           {"profile": "scx_lavd"|"scx_bpfland"|"scx_flash"|"scx_rustland"|"none"}
    write-system-config {"mode": id, "browser": id, "oem": bool}   (any subset, ≥ 1 key)
    enable-earlyoom     {"enable": true}
    reboot-to-windows   {"method": "bootnext", "entry": "0001", "reboot": true} or
                        {"method": "grub-reboot", "menuentry": "osprober-efi-...", "reboot": true}
                        (SPEC-WINDOWS §30.3/§30.4 — see ``lindos.dualboot``)
    firmware-setup      {"confirm": true}
    import-wifi         {"networks": [{"ssid", "security": "open"|"wpa-psk"|"sae", "psk"?,
                         "hidden": bool, "agent_owned": bool}, ...]}   sent over **stdin**
                         (``stdin_payload=True``) — a psk never belongs in argv/pkexec's log
    set-binfmt          {"enabled": true|false}   (terminal ``./setup.exe`` via binfmt_misc)
    apt-get-update      {}                                          (SPEC-UPDATE.md §36.4 — see ``lindos.update``)
    system-upgrade      {"packages": ["lindos-core=1.0.1", ...], "allow_kernel"?: bool}
                        every entry is ``name=version`` (name: ``PACKAGE_RE``; version: a Debian-ish
                        version string); a ``linux-image-*``/``linux-headers-*``/``linux-modules-*``
                        entry is refused unless ``allow_kernel`` is also true
    cleanup-old-packages {}                                          (``apt-get autoremove --purge``)
    install-local-debs  {"files": ["/abs/path/lindos-core_1.0.1_all.deb", ...]}
                        every path must be absolute, exist, and end ``.deb``; the *filename* must
                        start with ``lindos-`` (a cheap client-side proxy — the helper itself
                        re-verifies the real embedded ``Package`` field via ``dpkg-deb --field``
                        before running ``dpkg -i``, since a filename proves nothing by itself)

``install-flatpaks`` additionally accepts an optional ``"remote": {"name", "url"}`` (an
https:// Flatpak repo other than Flathub, e.g. NVIDIA's own GeForce NOW remote).

Environment: ``LINDOS_HELPER_DRYRUN=1`` runs the helper without privileges and makes it print
what it *would* do (used by tests); ``LINDOS_HELPER`` overrides the helper path.
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional

from . import paths

log = logging.getLogger("lindos.helper")

DRYRUN_ENV = "LINDOS_HELPER_DRYRUN"
HELPER_ENV = "LINDOS_HELPER"
POLKIT_ACTION_ID = "org.lindos.helper"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

ACTIONS: List[str] = [
    "apply-mode", "install-browser", "install-packages", "install-flatpaks", "set-governor",
    "set-services", "apply-sysctl", "apply-tune", "set-zram", "install-compat", "install-gaming",
    "install-drivers", "set-fan-profile", "set-sched", "write-system-config", "enable-earlyoom",
    "reboot-to-windows", "firmware-setup", "import-wifi", "set-binfmt",
    "apt-get-update", "system-upgrade", "cleanup-old-packages", "install-local-debs",
]

# --- validation vocabulary ---------------------------------------------------------------
#: a leading '-'/'--' is rejected (must start with an alnum) so a "package"/"flatpak id" can
#: never be parsed as an apt-get/flatpak command-line option (e.g. "--allow-unauthenticated").
PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9.+-]*$")
FLATPAK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SYSCTL_KEY_RE = re.compile(r"^(vm|kernel|fs|net|abi|dev)\.[a-z0-9_.-]+$")
SYSCTL_VALUE_RE = re.compile(r"^[A-Za-z0-9 ._:+-]{1,128}$")
UNIT_RE = re.compile(r"^[A-Za-z0-9@._:-]{1,128}$")
FAN_PROFILE_RE = re.compile(r"^[A-Za-z0-9 ._+-]{1,64}$")
BROWSER_IDS = ("edge", "chrome", "firefox")
GOVERNORS = ("schedutil", "performance", "powersave", "ondemand", "conservative", "userspace")
COMPOSITORS = ("picom", "xfwm", "none")
#: sched_ext / SCX schedulers ``set-sched`` accepts (SPEC-KERNEL §16); ``none`` stops any scx sched.
SCHED_PROFILES = ("scx_lavd", "scx_bpfland", "scx_flash", "scx_rustland", "none")
GAMING_ITEMS = frozenset({
    "steam", "lutris", "heroic", "prism", "sober", "vinegar", "mcpelauncher", "bottles", "all",
})
COMPAT_ITEMS = frozenset({
    "all", "wine", "wine-staging", "umu", "umu-launcher", "winetricks", "bottles", "dxvk", "vkd3d",
    "fonts", "dependencies", "proton",
})
DRIVER_ARGS = frozenset({"--nvidia-open", "--nvidia-proprietary", "--amd", "--intel"})
#: a non-Flathub Flatpak remote ``install-flatpaks`` may add first (e.g. NVIDIA's GeForce NOW repo).
REMOTE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
REMOTE_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(?::\d+)?(?:/[^\s]*)?$")

# --- dual-boot ("restart into Windows") / Wi-Fi import / binfmt (SPEC-WINDOWS §30.3-§30.4, §28.7) ---
REBOOT_METHODS = ("bootnext", "grub-reboot")
#: efibootmgr's 4-hex-digit ``Boot####`` number.
BOOTENTRY_RE = re.compile(r"^[0-9A-Fa-f]{4}$")
#: an os-prober ``--class windows`` GRUB menu id (``30_os-prober.in``); the helper re-derives and
#: re-checks the real ids from ``grub.cfg`` itself, this is only a client-side sanity check.
#: BIOS chain ids embed a GRUB device spec, which uses commas (``osprober-chain-hd0,gpt2``).
OSPROBER_ID_RE = re.compile(r"^osprober-(?:efi|chain)-[A-Za-z0-9._:/,-]{1,200}$")
WIFI_SECURITY = ("open", "wpa-psk", "sae")
WIFI_MAX_NETWORKS = 64
#: SSID is a *byte* string up to 32 octets (802.11), not 32 characters — check the UTF-8 length.
WIFI_SSID_MAX_BYTES = 32
#: WPA/WPA3 passphrase: 8-63 printable ASCII (0x20-0x7E) characters …
WIFI_PSK_PASSPHRASE_RE = re.compile(r"^[\x20-\x7E]{8,63}$")
#: … or the raw 256-bit PMK/network key as 64 hex characters.
WIFI_PSK_HEX_RE = re.compile(r"^[0-9A-Fa-f]{64}$")

#: systemd units the helper may enable/disable/mask (base name without ``.service``).
SERVICE_WHITELIST = frozenset({
    # debloat / power (SPEC §8, §11)
    "bluetooth", "cups", "cups-browsed", "ModemManager", "avahi-daemon", "mintreport",
    "apport", "whoopsie", "kerneloops", "ubuntu-report", "brltty", "speech-dispatcher",
    "NetworkManager-wait-online", "systemd-oomd", "snapd", "packagekit", "fwupd",
    "fwupd-refresh", "motd-news", "e2scrub_all", "apt-daily", "apt-daily-upgrade",
    "unattended-upgrades", "geoclue", "rsyslog", "switcheroo-control", "colord",
    "accounts-daemon", "upower", "udisks2", "irqbalance", "thermald", "tlp", "cpupower",
    "power-profiles-daemon", "earlyoom", "ananicy-cpp", "zramswap", "systemd-zram-setup@zram0",
    "fstrim", "tmp.mount", "nbfc_service", "openrgb", "gamemoded", "preload", "haveged",
    "lm-sensors", "fancontrol", "plymouth-quit-wait", "cron", "anacron", "wpa_supplicant",
    "warpinator", "blueman-mechanism", "smartmontools", "smartd", "ufw",
})
UNIT_SUFFIXES = (".service", ".socket", ".timer", ".mount", ".target", ".path")

# --- update (SPEC-UPDATE.md §36.2/§36.4): apt-get-update / system-upgrade / cleanup-old-packages /
# install-local-debs — see ``lindos.update`` for the read-only status/payload-building side. -----
#: a Debian-ish version string (epoch/upstream/revision), permissive but never empty.
SYSTEM_UPGRADE_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+:~-]*$")
#: the three real Debian kernel package name prefixes (Lindos's own tuned build included —
#: "linux-image-6.14.0-lindos" is still a kernel package and still needs ``allow_kernel``).
KERNEL_PACKAGE_NAME_RE = re.compile(r"^(?:linux-image|linux-headers|linux-modules)-")
#: cheap client-side proxy for "this .deb looks like a Lindos package" — the helper itself
#: re-verifies the *real* embedded ``Package:`` control field via ``dpkg-deb --field`` before
#: ever running ``dpkg -i`` on it, since a filename on its own proves nothing.
LINDOS_DEB_BASENAME_RE = re.compile(r"^lindos-.*\.deb$", re.IGNORECASE)


class PayloadError(ValueError):
    """Raised by :func:`validate_payload` for an unknown action or a bad payload."""


@dataclass
class HelperResult:
    """Outcome of one helper invocation."""

    ok: bool
    out: str = ""
    err: str = ""
    code: int = 0

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return self.ok

    @property
    def message(self) -> str:
        """Best human-readable summary line."""
        text = (self.err or self.out or "").strip()
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return lines[-1] if lines else ("ok" if self.ok else f"exit code {self.code}")

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "out": self.out, "err": self.err, "code": self.code}


# --- validation --------------------------------------------------------------------------
def _expect(cond: bool, message: str) -> None:
    if not cond:
        raise PayloadError(message)


def _str_list(payload: Dict[str, Any], key: str, pattern: re.Pattern, *, required: bool = False,
              allowed: Optional[Iterable[str]] = None, alias: Optional[str] = None) -> List[str]:
    value = payload.get(key)
    if value is None and alias:
        value = payload.get(alias)
    if value is None:
        _expect(not required, f"'{key}' is required")
        return []
    if isinstance(value, str):
        value = [value]
    _expect(isinstance(value, list), f"'{key}' must be a list of strings")
    out: List[str] = []
    allowed_set = set(allowed) if allowed is not None else None
    for item in value:
        _expect(isinstance(item, str), f"'{key}' must contain only strings")
        item = item.strip()
        _expect(bool(item) and bool(pattern.match(item)), f"invalid entry in '{key}': {item!r}")
        if allowed_set is not None:
            _expect(item in allowed_set, f"'{item}' is not allowed in '{key}'")
        if item not in out:
            out.append(item)
    _expect(not required or bool(out), f"'{key}' must not be empty")
    return out


def normalize_unit(unit: str) -> str:
    """Strip a known unit suffix (``bluetooth.service`` → ``bluetooth``)."""
    for suffix in UNIT_SUFFIXES:
        if unit.endswith(suffix) and unit != "tmp.mount":
            return unit[: -len(suffix)]
    return unit


def unit_allowed(unit: str) -> bool:
    return bool(UNIT_RE.match(unit)) and normalize_unit(unit) in SERVICE_WHITELIST


def _unit_list(payload: Dict[str, Any], key: str) -> List[str]:
    units = _str_list(payload, key, UNIT_RE)
    for unit in units:
        _expect(unit_allowed(unit), f"unit '{unit}' is not in the whitelist")
    return units


def _sysctl_dict(payload: Dict[str, Any], key: str = "sysctl", *, required: bool = False) -> Dict[str, str]:
    value = payload.get(key)
    if value is None:
        _expect(not required, f"'{key}' is required")
        return {}
    _expect(isinstance(value, dict), f"'{key}' must be an object")
    out: Dict[str, str] = {}
    for k, v in value.items():
        _expect(isinstance(k, str) and bool(SYSCTL_KEY_RE.match(k)), f"invalid sysctl key {k!r}")
        if isinstance(v, bool):
            v = "1" if v else "0"
        _expect(isinstance(v, (str, int, float)), f"invalid sysctl value for {k}")
        sval = str(v).strip()
        _expect(bool(SYSCTL_VALUE_RE.match(sval)), f"invalid sysctl value for {k}: {sval!r}")
        out[k] = sval
    _expect(not required or bool(out), f"'{key}' must not be empty")
    return out


def _bool(payload: Dict[str, Any], key: str, default: bool = False) -> bool:
    value = payload.get(key, default)
    _expect(isinstance(value, bool), f"'{key}' must be true/false")
    return value


def _int(payload: Dict[str, Any], key: str, lo: int, hi: int, default: Optional[int] = None,
         required: bool = False) -> Optional[int]:
    value = payload.get(key, default)
    if value is None:
        _expect(not required, f"'{key}' is required")
        return None
    _expect(isinstance(value, int) and not isinstance(value, bool), f"'{key}' must be an integer")
    _expect(lo <= value <= hi, f"'{key}' must be between {lo} and {hi}")
    return value


def _choice(payload: Dict[str, Any], key: str, choices: Iterable[str], *, required: bool = True,
            default: Optional[str] = None) -> Optional[str]:
    value = payload.get(key, default)
    if value is None:
        _expect(not required, f"'{key}' is required")
        return None
    _expect(isinstance(value, str) and value in tuple(choices), f"'{key}' must be one of {', '.join(choices)}")
    return value


def _mode_ids() -> List[str]:
    from .modes import MODE_IDS  # local import: modes imports helper at module level

    return list(MODE_IDS)


def _apply_system_path(payload: Dict[str, Any]) -> Optional[str]:
    value = payload.get("apply_system")
    if value in (None, ""):
        return None
    _expect(isinstance(value, str), "'apply_system' must be a path or null")
    norm = value.replace("\\", "/")
    # Reject '..'/'.' traversal segments outright, then re-derive the real, collapsed path and
    # check *that* for containment (not the raw string): a naive str.startswith() on the raw
    # value is bypassable with e.g. "<modes_dir>/../../../../tmp/evil/apply-system.sh", which
    # passes a prefix check as a string while resolving (once bash/os.path.isfile touch the real
    # filesystem path) to a script completely outside the modes directory — and this path is
    # bash'd as root by the helper's apply-mode action.
    _expect(".." not in norm.split("/") and "." not in norm.split("/"),
            "'apply_system' must not contain '.'/'..' path segments")
    real = posixpath.normpath(norm)
    _expect(os.path.basename(real) == "apply-system.sh", "'apply_system' must point at apply-system.sh")
    modes_roots = {paths.modes_dir().replace("\\", "/").rstrip("/"), paths.MODES_DIR.rstrip("/")}
    _expect(any(real == root_ or real.startswith(root_ + "/") for root_ in modes_roots),
            "'apply_system' must live under the modes directory")
    return value


def _remote_dict(payload: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Validate the optional ``install-flatpaks`` ``"remote": {"name", "url"}`` (SPEC-WINDOWS §30.4:
    NVIDIA's own GeForce NOW Flatpak repo is not on Flathub)."""
    value = payload.get("remote")
    if value is None:
        return None
    _expect(isinstance(value, dict), "'remote' must be an object with 'name' and 'url'")
    name = value.get("name")
    url = value.get("url")
    _expect(isinstance(name, str) and bool(REMOTE_NAME_RE.match(name)),
            "'remote.name' must be a short alphanumeric flatpak remote name")
    _expect(name.lower() != "flathub", "'remote.name' must not shadow the flathub remote")
    _expect(isinstance(url, str) and bool(REMOTE_URL_RE.match(url)), "'remote.url' must be an https:// URL")
    return {"name": name, "url": url}


def _validate_wifi_network(net: Any) -> Dict[str, Any]:
    """Validate one ``import-wifi`` network entry (SPEC-WINDOWS §30.4, §29.8): SSID bytes/length,
    security enum, and a psk that is either an 8-63 printable-ASCII passphrase or 64 hex chars —
    never anything else, and never required when NetworkManager will just ask the user instead."""
    _expect(isinstance(net, dict), "each entry in 'networks' must be an object")
    ssid = net.get("ssid")
    _expect(isinstance(ssid, str) and ssid != "", "'ssid' must be a non-empty string")
    # no control characters (NUL, newline, ...): the SSID is written verbatim into an INI-style
    # NetworkManager keyfile, and a newline there would inject an extra line/section.
    _expect(not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in ssid),
            "'ssid' must not contain control characters")
    ssid_bytes = ssid.encode("utf-8", "surrogatepass")
    _expect(len(ssid_bytes) <= WIFI_SSID_MAX_BYTES, f"'ssid' must be at most {WIFI_SSID_MAX_BYTES} bytes (UTF-8)")
    security = net.get("security")
    _expect(security in WIFI_SECURITY, f"'security' must be one of {', '.join(WIFI_SECURITY)}")
    out: Dict[str, Any] = {
        "ssid": ssid, "security": security,
        "hidden": _bool(net, "hidden", False),
        "agent_owned": _bool(net, "agent_owned", False),
    }
    psk = net.get("psk")
    if security == "open":
        _expect(psk in (None, ""), "an 'open' network must not carry a psk")
        out["psk"] = None
    elif out["agent_owned"] or psk in (None, ""):
        out["psk"] = None   # NetworkManager prompts the user once (psk-flags=1)
    else:
        _expect(isinstance(psk, str), "'psk' must be a string")
        _expect(bool(WIFI_PSK_PASSPHRASE_RE.match(psk)) or bool(WIFI_PSK_HEX_RE.match(psk)),
                "'psk' must be an 8-63 character printable-ASCII passphrase or 64 hex characters")
        out["psk"] = psk
    return out


def _system_upgrade_packages(payload: Dict[str, Any], *, allow_kernel: bool) -> List[str]:
    """Validate ``system-upgrade``'s ``"packages"`` list: every entry is ``name=version`` (name:
    :data:`PACKAGE_RE`; version: :data:`SYSTEM_UPGRADE_VERSION_RE`) — never a bare package name
    with no version, and never a bare "upgrade everything". A kernel package
    (``linux-image-*``/``linux-headers-*``/``linux-modules-*``) is refused unless *allow_kernel*."""
    value = payload.get("packages")
    _expect(isinstance(value, list) and bool(value), "'packages' must be a non-empty list of 'name=version' entries")
    out: List[str] = []
    for item in value:
        _expect(isinstance(item, str), "'packages' must contain only strings")
        item = item.strip()
        _expect("=" in item, f"invalid entry in 'packages' (need 'name=version'): {item!r}")
        name, _, version = item.partition("=")
        _expect(bool(PACKAGE_RE.match(name)), f"invalid package name in 'packages': {name!r}")
        _expect(bool(version) and bool(SYSTEM_UPGRADE_VERSION_RE.match(version)),
                f"invalid version in 'packages': {item!r}")
        if KERNEL_PACKAGE_NAME_RE.match(name):
            _expect(allow_kernel, f"'{name}' is a kernel package (linux-image-*/linux-headers-*/"
                                  "linux-modules-*); refusing unless 'allow_kernel' is also true")
        entry = f"{name}={version}"
        if entry not in out:
            out.append(entry)
    return out


def _local_deb_files(payload: Dict[str, Any]) -> List[str]:
    """Validate ``install-local-debs``'s ``"files"`` list: every path must be absolute, exist,
    end ``.deb``, and its filename must start with ``lindos-`` — a cheap, fast, client-side proxy
    only; the helper re-verifies the real ``Package:`` control field via ``dpkg-deb --field``
    before ever running ``dpkg -i`` (a filename alone proves nothing)."""
    value = payload.get("files")
    _expect(isinstance(value, list) and bool(value), "'files' must be a non-empty list of .deb paths")
    out: List[str] = []
    for item in value:
        _expect(isinstance(item, str), "'files' must contain only strings")
        path = item.strip()
        _expect(bool(path), "'files' must not contain empty strings")
        _expect(os.path.isabs(path), f"'files' entries must be absolute paths: {path!r}")
        _expect(path.lower().endswith(".deb"), f"'files' entries must end in '.deb': {path!r}")
        basename = os.path.basename(path.replace("\\", "/"))
        _expect(bool(LINDOS_DEB_BASENAME_RE.match(basename)),
                f"'files' entry does not look like a lindos-* package: {basename!r}")
        _expect(os.path.isfile(path), f"file does not exist: {path!r}")
        if path not in out:
            out.append(path)
    return out


def validate_payload(action: str, payload: Any) -> Dict[str, Any]:
    """Validate and normalise *payload* for *action*.

    Returns a new dict containing only known keys with canonical types.  Raises
    :class:`PayloadError` (a ``ValueError``) on any problem.  Unknown keys are dropped.
    """
    _expect(action in ACTIONS, f"unknown action {action!r}")
    _expect(isinstance(payload, dict), "payload must be a JSON object")
    p: Dict[str, Any] = payload
    out: Dict[str, Any] = {}

    if action == "apply-mode":
        out["mode"] = _choice(p, "mode", _mode_ids())
        out["packages"] = _str_list(p, "packages", PACKAGE_RE)
        out["flatpaks"] = _str_list(p, "flatpaks", FLATPAK_RE)
        out["services_disable"] = _unit_list(p, "services_disable")
        out["services_enable"] = _unit_list(p, "services_enable")
        out["sysctl"] = _sysctl_dict(p)
        out["governor"] = _choice(p, "governor", GOVERNORS, required=False, default="schedutil")
        out["zram_percent"] = _int(p, "zram_percent", 0, 200, default=50)
        out["compositor"] = _choice(p, "compositor", COMPOSITORS, required=False, default="picom")
        out["apply_system"] = _apply_system_path(p)
        out["set_system_default"] = _bool(p, "set_system_default", False)
        out["offline"] = _bool(p, "offline", False)
        out["schema"] = _int(p, "schema", 1, 99, default=1)
    elif action == "install-browser":
        out["browser"] = _choice(p, "browser", BROWSER_IDS)
    elif action == "install-packages":
        out["packages"] = _str_list(p, "packages", PACKAGE_RE, required=True)
    elif action == "install-flatpaks":
        out["flatpaks"] = _str_list(p, "flatpaks", FLATPAK_RE, required=True, alias="ids")
        remote = _remote_dict(p)
        if remote is not None:
            out["remote"] = remote
    elif action == "set-governor":
        out["governor"] = _choice(p, "governor", GOVERNORS)
    elif action == "set-services":
        out["enable"] = _unit_list(p, "enable")
        out["disable"] = _unit_list(p, "disable")
        out["mask"] = _unit_list(p, "mask")
        _expect(bool(out["enable"] or out["disable"] or out["mask"]),
                "set-services needs at least one of enable/disable/mask")
    elif action == "apply-sysctl":
        out["sysctl"] = _sysctl_dict(p, required=True)
    elif action == "apply-tune":
        out["mode"] = _choice(p, "mode", _mode_ids())
        out["offline"] = _bool(p, "offline", False)
    elif action == "set-zram":
        out["percent"] = _int(p, "percent", 0, 200, required=True)
    elif action == "install-compat":
        out["items"] = _str_list(p, "items", re.compile(r"^[a-z0-9-]+$"), allowed=COMPAT_ITEMS)
    elif action == "install-gaming":
        out["items"] = _str_list(p, "items", re.compile(r"^[a-z0-9-]+$"), required=True, allowed=GAMING_ITEMS)
    elif action == "install-drivers":
        args = _str_list(p, "args", re.compile(r"^--[a-z-]+$"), allowed=DRIVER_ARGS)
        driver = p.get("driver")
        if driver is not None:
            _expect(isinstance(driver, str) and f"--{driver}" in DRIVER_ARGS,
                    "'driver' must be one of nvidia-open, nvidia-proprietary, amd, intel")
            if f"--{driver}" not in args:
                args.append(f"--{driver}")
        out["args"] = args
    elif action == "set-fan-profile":
        profile = p.get("profile")
        _expect(isinstance(profile, str) and bool(FAN_PROFILE_RE.match(profile.strip())),
                "'profile' must be a short profile name")
        out["profile"] = profile.strip()
    elif action == "set-sched":
        out["profile"] = _choice(p, "profile", SCHED_PROFILES)
    elif action == "write-system-config":
        if "mode" in p:
            out["mode"] = _choice(p, "mode", _mode_ids())
        if "browser" in p:
            out["browser"] = _choice(p, "browser", BROWSER_IDS)
        if "oem" in p:
            out["oem"] = _bool(p, "oem", False)
        _expect(bool(out), "write-system-config needs at least one of mode/browser/oem")
    elif action == "enable-earlyoom":
        out["enable"] = _bool(p, "enable", True)
    elif action == "reboot-to-windows":
        method = _choice(p, "method", REBOOT_METHODS)
        out["method"] = method
        if method == "bootnext":
            entry = p.get("entry")
            _expect(isinstance(entry, str) and bool(BOOTENTRY_RE.match(entry)),
                    "'entry' must be a 4-digit hex efibootmgr boot number")
            out["entry"] = entry.upper()
        else:
            menuentry = p.get("menuentry")
            _expect(isinstance(menuentry, str) and bool(OSPROBER_ID_RE.match(menuentry)),
                    "'menuentry' must be an os-prober Windows menu id (osprober-efi-... / osprober-chain-...)")
            out["menuentry"] = menuentry
        out["reboot"] = _bool(p, "reboot", True)
    elif action == "firmware-setup":
        _expect(p.get("confirm") is True, "'confirm' must be true")
        out["confirm"] = True
    elif action == "import-wifi":
        networks = p.get("networks")
        _expect(isinstance(networks, list) and bool(networks), "'networks' must be a non-empty list")
        _expect(len(networks) <= WIFI_MAX_NETWORKS, f"'networks' must have at most {WIFI_MAX_NETWORKS} entries")
        out["networks"] = [_validate_wifi_network(n) for n in networks]
    elif action == "set-binfmt":
        enabled = p.get("enabled")
        _expect(isinstance(enabled, bool), "'enabled' must be true/false")
        out["enabled"] = enabled
    elif action == "apt-get-update":
        pass  # no fields — a plain 'apt-get update', nothing else
    elif action == "system-upgrade":
        allow_kernel = _bool(p, "allow_kernel", False)
        out["packages"] = _system_upgrade_packages(p, allow_kernel=allow_kernel)
        out["allow_kernel"] = allow_kernel
    elif action == "cleanup-old-packages":
        pass  # no fields — a plain 'apt-get autoremove --purge'
    elif action == "install-local-debs":
        out["files"] = _local_deb_files(p)
    return out


# --- invocation --------------------------------------------------------------------------
def helper_path() -> str:
    """Path of the privileged helper (``LINDOS_HELPER`` env override, else SPEC path)."""
    return os.environ.get(HELPER_ENV) or paths.helper()


def is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid and geteuid() == 0)


def dry_run_enabled() -> bool:
    return os.environ.get(DRYRUN_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def build_command(action: str, payload: Dict[str, Any], *, stdin_payload: bool = False) -> List[str]:
    """Return the argv used to invoke the helper for *action* (no execution).

    With *stdin_payload*, the payload is **not** put on the command line at all: the argv ends
    in ``-`` (the helper's "read the JSON from stdin" marker) and the caller is responsible for
    writing the JSON to the child's stdin (:func:`run_privileged` does this). Use this for any
    payload that can carry a secret (Wi-Fi passwords): argv is visible to every user via
    ``/proc/<pid>/cmdline`` and is often logged by pkexec/sudo, stdin is not.
    """
    helper = helper_path()
    if dry_run_enabled() or os.name != "posix":
        base = [sys.executable, helper, action]
    elif is_root():
        base = [helper, action]
    else:
        pkexec = shutil.which("pkexec")
        if pkexec:
            base = [pkexec, helper, action]
        else:
            sudo = shutil.which("sudo")
            if not sudo:
                return []
            base = [sudo, "-n", helper, action]
    if stdin_payload:
        return base + ["-"]
    return base + [json.dumps(payload, separators=(",", ":"), sort_keys=True)]


def _pump(stream, sink: List[str], log_cb: Optional[Callable[[str], None]]) -> None:
    try:
        for line in iter(stream.readline, ""):
            sink.append(line)
            if log_cb is not None:
                try:
                    log_cb(line.rstrip("\n"))
                except Exception:  # never let a logging callback break the call
                    pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def run_privileged(action: str, payload: Optional[Dict[str, Any]] = None,
                   log: Optional[Callable[[str], None]] = None,
                   timeout: Optional[float] = None,
                   stdin_payload: bool = False) -> HelperResult:
    """Run helper *action* with *payload* through pkexec (or sudo -n / direct when root).

    *log* (optional callable) receives every output line as it is produced.  With
    *stdin_payload*, the (validated) JSON is written to the helper's stdin instead of argv (see
    :func:`build_command`) — always pass this for a payload that can carry a secret, such as
    ``import-wifi``'s Wi-Fi passwords. Never raises: problems are reported through
    :class:`HelperResult` (``code`` 127 = helper/pkexec not available, 126 = authentication
    cancelled/failed, 124 = timeout).
    """
    payload = dict(payload or {})
    try:
        payload = validate_payload(action, payload)
    except PayloadError as exc:
        return HelperResult(False, "", f"invalid payload for {action}: {exc}", EXIT_USAGE)

    helper = helper_path()
    if not os.path.isfile(helper):
        return HelperResult(False, "", f"helper not found: {helper}", 127)
    cmd = build_command(action, payload, stdin_payload=stdin_payload)
    if not cmd:
        return HelperResult(False, "", "no privilege escalation tool (pkexec/sudo) available", 127)

    env = dict(os.environ)
    env.setdefault("LC_ALL", "C.UTF-8")
    try:
        proc = subprocess.Popen(cmd, stdin=(subprocess.PIPE if stdin_payload else subprocess.DEVNULL),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", env=env)
    except (OSError, ValueError) as exc:
        return HelperResult(False, "", f"cannot start helper: {exc}", 127)

    if stdin_payload:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        try:
            assert proc.stdin is not None
            proc.stdin.write(body)
            proc.stdin.close()
        except (OSError, ValueError) as exc:
            proc.kill()
            proc.wait()
            return HelperResult(False, "", f"cannot write payload to the helper's stdin: {exc}", 127)

    out_lines: List[str] = []
    err_lines: List[str] = []
    t_out = threading.Thread(target=_pump, args=(proc.stdout, out_lines, log), daemon=True)
    t_err = threading.Thread(target=_pump, args=(proc.stderr, err_lines, log), daemon=True)
    t_out.start()
    t_err.start()
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        code = 124
        err_lines.append(f"helper timed out after {timeout}s\n")
    t_out.join()
    t_err.join()
    out = "".join(out_lines)
    err = "".join(err_lines)
    if code == 126 and not err.strip():
        err = "authentication cancelled or not authorised"
    return HelperResult(code == 0, out, err, code)


# --- convenience wrappers (one per action) ------------------------------------------------
def apply_mode(plan: Dict[str, Any], log: Optional[Callable[[str], None]] = None) -> HelperResult:
    return run_privileged("apply-mode", plan, log)


def install_browser(browser: str, log: Optional[Callable[[str], None]] = None) -> HelperResult:
    return run_privileged("install-browser", {"browser": browser}, log)


def install_packages(packages: Iterable[str], log: Optional[Callable[[str], None]] = None) -> HelperResult:
    return run_privileged("install-packages", {"packages": list(packages)}, log)


def install_flatpaks(ids: Iterable[str], remote: Optional[Dict[str, str]] = None,
                     log: Optional[Callable[[str], None]] = None) -> HelperResult:
    payload: Dict[str, Any] = {"flatpaks": list(ids)}
    if remote:
        payload["remote"] = remote
    return run_privileged("install-flatpaks", payload, log)


def set_governor(governor: str, log: Optional[Callable[[str], None]] = None) -> HelperResult:
    return run_privileged("set-governor", {"governor": governor}, log)


def set_services(enable: Iterable[str] = (), disable: Iterable[str] = (), mask: Iterable[str] = (),
                 log: Optional[Callable[[str], None]] = None) -> HelperResult:
    return run_privileged("set-services", {"enable": list(enable), "disable": list(disable),
                                           "mask": list(mask)}, log)


def apply_sysctl(values: Dict[str, Any], log: Optional[Callable[[str], None]] = None) -> HelperResult:
    return run_privileged("apply-sysctl", {"sysctl": dict(values)}, log)


def apply_tune(mode: str, offline: bool = False, log: Optional[Callable[[str], None]] = None) -> HelperResult:
    return run_privileged("apply-tune", {"mode": mode, "offline": offline}, log)


def set_zram(percent: int, log: Optional[Callable[[str], None]] = None) -> HelperResult:
    return run_privileged("set-zram", {"percent": int(percent)}, log)


def install_compat(items: Iterable[str] = (), log: Optional[Callable[[str], None]] = None) -> HelperResult:
    return run_privileged("install-compat", {"items": list(items)}, log)


def install_gaming(items: Iterable[str], log: Optional[Callable[[str], None]] = None) -> HelperResult:
    return run_privileged("install-gaming", {"items": list(items)}, log)


def install_drivers(args: Iterable[str], log: Optional[Callable[[str], None]] = None) -> HelperResult:
    return run_privileged("install-drivers", {"args": list(args)}, log)


def set_fan_profile(profile: str, log: Optional[Callable[[str], None]] = None) -> HelperResult:
    return run_privileged("set-fan-profile", {"profile": profile}, log)


def set_sched(profile: str, log: Optional[Callable[[str], None]] = None) -> HelperResult:
    return run_privileged("set-sched", {"profile": profile}, log)


def write_system_config(log: Optional[Callable[[str], None]] = None, **fields: Any) -> HelperResult:
    return run_privileged("write-system-config", dict(fields), log)


def enable_earlyoom(enable: bool = True, log: Optional[Callable[[str], None]] = None) -> HelperResult:
    return run_privileged("enable-earlyoom", {"enable": bool(enable)}, log)


def reboot_to_windows(method: str, *, entry: Optional[str] = None, menuentry: Optional[str] = None,
                      reboot: bool = True, log: Optional[Callable[[str], None]] = None) -> HelperResult:
    """``method`` is ``"bootnext"`` (with *entry*, a 4-hex boot number) or ``"grub-reboot"`` (with
    *menuentry*, an os-prober id) — see :func:`lindos.dualboot.reboot_payload`, which builds this
    payload from a :func:`lindos.dualboot.status` result."""
    payload: Dict[str, Any] = {"method": method, "reboot": bool(reboot)}
    if entry is not None:
        payload["entry"] = entry
    if menuentry is not None:
        payload["menuentry"] = menuentry
    return run_privileged("reboot-to-windows", payload, log)


def firmware_setup(log: Optional[Callable[[str], None]] = None) -> HelperResult:
    return run_privileged("firmware-setup", {"confirm": True}, log)


def import_wifi(networks: Iterable[Dict[str, Any]], log: Optional[Callable[[str], None]] = None) -> HelperResult:
    """Import offline Wi-Fi profiles as NetworkManager keyfiles. Always sent over stdin: a
    passphrase must never appear in argv (visible to every user via ``/proc/<pid>/cmdline``)."""
    return run_privileged("import-wifi", {"networks": list(networks)}, log, stdin_payload=True)


def set_binfmt(enabled: bool, log: Optional[Callable[[str], None]] = None) -> HelperResult:
    return run_privileged("set-binfmt", {"enabled": bool(enabled)}, log)


__all__ = [
    "ACTIONS", "DRYRUN_ENV", "HELPER_ENV", "POLKIT_ACTION_ID", "EXIT_OK", "EXIT_ERROR", "EXIT_USAGE",
    "PACKAGE_RE", "FLATPAK_RE", "SYSCTL_KEY_RE", "SYSCTL_VALUE_RE", "UNIT_RE", "GOVERNORS",
    "COMPOSITORS", "SCHED_PROFILES", "GAMING_ITEMS", "COMPAT_ITEMS", "DRIVER_ARGS", "SERVICE_WHITELIST",
    "REMOTE_NAME_RE", "REMOTE_URL_RE", "REBOOT_METHODS", "BOOTENTRY_RE", "OSPROBER_ID_RE",
    "WIFI_SECURITY", "WIFI_MAX_NETWORKS", "WIFI_SSID_MAX_BYTES", "WIFI_PSK_PASSPHRASE_RE", "WIFI_PSK_HEX_RE",
    "SYSTEM_UPGRADE_VERSION_RE", "KERNEL_PACKAGE_NAME_RE", "LINDOS_DEB_BASENAME_RE",
    "PayloadError", "HelperResult", "validate_payload", "normalize_unit", "unit_allowed",
    "helper_path", "is_root", "dry_run_enabled", "build_command", "run_privileged",
    "apply_mode", "install_browser", "install_packages", "install_flatpaks", "set_governor",
    "set_services", "apply_sysctl", "apply_tune", "set_zram", "install_compat", "install_gaming",
    "install_drivers", "set_fan_profile", "set_sched", "write_system_config", "enable_earlyoom",
    "reboot_to_windows", "firmware_setup", "import_wifi", "set_binfmt",
]
