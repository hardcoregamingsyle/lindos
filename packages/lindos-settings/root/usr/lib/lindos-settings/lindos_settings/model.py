"""Pure-logic model for Lindos Settings.

This module deliberately has **no GTK / gi imports** and performs **no Linux calls at import
time**, so it can be unit-tested on any OS.  It contains:

* the page registry (loader for ``/usr/share/lindos/settings/pages.json`` with an embedded
  fallback that must stay identical to the shipped JSON — the tests enforce this),
* quick-toggle definitions with injectable get/set backends,
* the sidebar / page-item search filter,
* the ``which_or_install`` resolver used by delegate pages,
* small formatting helpers (MB, uptime, initials, RAM summary),
* static data tables (launchers, accents, power-menu items) and defensive normalisers for
  data returned by lindos-core / external CLIs.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Sequence

log = logging.getLogger("lindos.settings.model")

# ---------------------------------------------------------------------------------------------
# Constants (must match SPEC §7)
# ---------------------------------------------------------------------------------------------

PAGE_ORDER: tuple[str, ...] = (
    "home",
    "system",
    "personalization",
    "apps",
    "windows-apps",
    "gaming",
    "hardware",
    "network",
    "accounts",
    "mode",
    "update",
    "updates",
    "about",
)
NATIVE_PAGES: tuple[str, ...] = ("home", "personalization", "windows-apps", "gaming", "hardware", "mode", "updates", "about")
DELEGATE_PAGES: tuple[str, ...] = ("system", "apps", "network", "accounts", "update")

PAGES_JSON = "/usr/share/lindos/settings/pages.json"
ACCENTS_JSON = "/usr/share/lindos/setup/accents.json"
COMPAT_MATRIX_JSON = "/usr/share/lindos/compat-matrix.json"
RECIPES_DIR = "/usr/share/lindos/recipes"

RAM_TARGET_MIN_MB = 350
RAM_TARGET_MAX_MB = 500

MODE_IDS: tuple[str, ...] = ("everyday", "gaming", "work", "creator", "lite")
MODE_NAMES: dict[str, str] = {
    "everyday": "Everyday",
    "gaming": "Gaming",
    "work": "Work",
    "creator": "Creator",
    "lite": "Lite",
}
MODE_ICONS: dict[str, str] = {
    "everyday": "user-home",
    "gaming": "applications-games",
    "work": "x-office-document",
    "creator": "applications-graphics",
    "lite": "battery-good",
}
MODE_DESCRIPTIONS: dict[str, str] = {
    "everyday": "Balanced defaults for browsing, media and everyday work.",
    "gaming": "Performance governor, Game Mode auto, MangoHud, gaming launchers pinned.",
    "work": "Office suite and mail pinned, balanced power profile, night light on.",
    "creator": "Bottles and creative Wine recipes ready; GIMP, Krita, Kdenlive suggested.",
    "lite": "No compositor, no animations, aggressive memory saving for ≤ 4 GB PCs.",
}

# ---------------------------------------------------------------------------------------------
# Page registry
# ---------------------------------------------------------------------------------------------


def _as_tuple(value: Any) -> tuple[str, ...]:
    """Normalise a str | list | tuple | None into a tuple of str."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value if str(v))
    return (str(value),)


def _as_argv(value: Any) -> tuple[str, ...]:
    """Normalise an exec definition (list of str) into an argv tuple.  A bare string is treated
    as a single program name (never split on whitespace: we do not want shell semantics)."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    return ()


@dataclass(frozen=True)
class SubItem:
    """One entry on a delegating page (e.g. System → Display)."""

    id: str
    label: str
    description: str = ""
    icon: tuple[str, ...] = ()
    exec: tuple[str, ...] = ()
    alternatives: tuple[tuple[str, ...], ...] = ()
    package: str = ""
    keywords: tuple[str, ...] = ()

    def search_text(self) -> str:
        return " ".join([self.label, self.description, *self.keywords]).lower()

    def all_argvs(self) -> list[tuple[str, ...]]:
        out: list[tuple[str, ...]] = []
        if self.exec:
            out.append(self.exec)
        out.extend(a for a in self.alternatives if a)
        return out


@dataclass(frozen=True)
class Page:
    """A sidebar entry.  ``kind`` is ``native`` (GTK page) or ``delegate`` (buttons that launch
    existing tools)."""

    id: str
    title: str
    icon: tuple[str, ...] = ()
    kind: str = "native"
    description: str = ""
    subitems: tuple[SubItem, ...] = ()
    keywords: tuple[str, ...] = ()

    def search_text(self) -> str:
        parts = [self.title, self.description, *self.keywords]
        parts.extend(s.search_text() for s in self.subitems)
        return " ".join(parts).lower()

    @property
    def is_native(self) -> bool:
        return self.kind == "native"


def subitem_from_dict(d: dict[str, Any]) -> SubItem:
    if not isinstance(d, dict):
        raise ValueError("subitem must be an object")
    label = str(d.get("label") or "").strip()
    if not label:
        raise ValueError("subitem without label")
    sid = str(d.get("id") or re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-"))
    alts = d.get("alternatives") or []
    if not isinstance(alts, list):
        alts = []
    return SubItem(
        id=sid,
        label=label,
        description=str(d.get("description") or ""),
        icon=_as_tuple(d.get("icon")),
        exec=_as_argv(d.get("exec")),
        alternatives=tuple(_as_argv(a) for a in alts if _as_argv(a)),
        package=str(d.get("package") or ""),
        keywords=_as_tuple(d.get("keywords")),
    )


def page_from_dict(d: dict[str, Any]) -> Page:
    if not isinstance(d, dict):
        raise ValueError("page must be an object")
    pid = str(d.get("id") or "").strip()
    title = str(d.get("title") or "").strip()
    if not pid or not title:
        raise ValueError("page needs id and title")
    kind = str(d.get("kind") or "native")
    if kind not in ("native", "delegate"):
        raise ValueError(f"page {pid}: kind must be native|delegate, got {kind!r}")
    subs = d.get("subitems") or []
    if not isinstance(subs, list):
        raise ValueError(f"page {pid}: subitems must be a list")
    return Page(
        id=pid,
        title=title,
        icon=_as_tuple(d.get("icon")),
        kind=kind,
        description=str(d.get("description") or ""),
        subitems=tuple(subitem_from_dict(s) for s in subs),
        keywords=_as_tuple(d.get("keywords")),
    )


# Embedded copy of pages.json (fallback when the file is missing/corrupt).  Keep in sync — the
# unit tests compare this with the shipped JSON.
BUILTIN_PAGES: dict[str, Any] = {
    "schema": 1,
    "pages": [
        {
            "id": "home",
            "title": "Home",
            "icon": ["go-home"],
            "kind": "native",
            "description": "Overview: mode, memory, quick toggles",
            "keywords": ["start", "overview", "ram", "memory", "quick", "dark", "toggle", "taskbar"],
        },
        {
            "id": "system",
            "title": "System",
            "icon": ["computer"],
            "kind": "delegate",
            "description": "Display, sound, notifications, power, storage",
            "keywords": ["monitor", "screen", "resolution", "volume", "audio", "battery", "disk"],
            "subitems": [
                {
                    "id": "display",
                    "label": "Display",
                    "description": "Monitors, resolution, scaling, multiple displays",
                    "icon": ["video-display"],
                    "exec": ["xfce4-display-settings"],
                    "package": "xfce4-settings",
                    "keywords": ["monitor", "screen", "resolution", "hidpi", "scale"],
                },
                {
                    "id": "sound",
                    "label": "Sound",
                    "description": "Output and input devices, volume, per-app mixer",
                    "icon": ["audio-volume-high"],
                    "exec": ["pavucontrol"],
                    "package": "pavucontrol",
                    "keywords": ["audio", "volume", "microphone", "speaker", "pulseaudio", "pipewire"],
                },
                {
                    "id": "notifications",
                    "label": "Notifications",
                    "description": "Do not disturb, notification position and log",
                    "icon": ["preferences-system-notifications"],
                    "exec": ["xfce4-notifyd-config"],
                    "package": "xfce4-notifyd",
                    "keywords": ["do not disturb", "alerts", "toast"],
                },
                {
                    "id": "power",
                    "label": "Power & battery",
                    "description": "Sleep, screen blanking, lid action, battery",
                    "icon": ["battery"],
                    "exec": ["xfce4-power-manager-settings"],
                    "package": "xfce4-power-manager",
                    "keywords": ["sleep", "suspend", "lid", "battery", "brightness"],
                },
                {
                    "id": "storage",
                    "label": "Storage",
                    "description": "Disk usage analyser and drives",
                    "icon": ["drive-harddisk"],
                    "exec": ["baobab"],
                    "alternatives": [["gnome-disks"]],
                    "package": "baobab",
                    "keywords": ["disk", "usage", "partition", "drive", "space"],
                },
                {
                    "id": "default-apps",
                    "label": "Default apps",
                    "description": "Web browser, mail, file manager, file type associations",
                    "icon": ["preferences-desktop-default-applications"],
                    "exec": ["xfce4-mime-settings"],
                    "package": "xfce4-settings",
                    "keywords": ["browser", "mime", "associations", "open with"],
                },
                {
                    "id": "bluetooth",
                    "label": "Bluetooth & devices",
                    "description": "Pair headphones, controllers, mice and keyboards",
                    "icon": ["bluetooth"],
                    "exec": ["blueman-manager"],
                    "package": "blueman",
                    "keywords": ["pair", "headphones", "wireless"],
                },
                {
                    "id": "printers",
                    "label": "Printers & scanners",
                    "description": "Add and manage printers",
                    "icon": ["printer"],
                    "exec": ["system-config-printer"],
                    "package": "system-config-printer",
                    "keywords": ["print", "cups", "scanner"],
                },
            ],
        },
        {
            "id": "personalization",
            "title": "Personalization",
            "icon": ["preferences-desktop-wallpaper"],
            "kind": "native",
            "description": "Theme, accent colour, wallpaper, taskbar, fonts, cursor",
            "keywords": ["theme", "dark", "light", "accent", "colour", "color", "wallpaper", "background", "taskbar", "font", "cursor", "lock screen"],
        },
        {
            "id": "apps",
            "title": "Apps",
            "icon": ["applications-other"],
            "kind": "delegate",
            "description": "Store, installed apps, startup, default apps, web browsers",
            "keywords": ["software", "install", "uninstall", "store", "startup", "autostart", "browser", "edge", "chrome", "firefox"],
            "subitems": [
                {
                    "id": "store",
                    "label": "Store",
                    "description": "Browse and install apps (system packages and Flatpak)",
                    "icon": ["system-software-install"],
                    "exec": ["mintinstall"],
                    "package": "mintinstall",
                    "keywords": ["software manager", "flatpak", "install"],
                },
                {
                    "id": "installed",
                    "label": "Installed apps",
                    "description": "Review and remove installed applications",
                    "icon": ["package-x-generic"],
                    "exec": ["mintinstall"],
                    "alternatives": [["synaptic-pkexec"], ["synaptic"]],
                    "package": "mintinstall",
                    "keywords": ["uninstall", "remove", "packages", "synaptic"],
                },
                {
                    "id": "startup",
                    "label": "Startup",
                    "description": "Applications that start when you sign in",
                    "icon": ["system-run"],
                    "exec": ["xfce4-session-settings"],
                    "package": "xfce4-session",
                    "keywords": ["autostart", "login", "session"],
                },
                {
                    "id": "default-apps",
                    "label": "Default apps",
                    "description": "Web browser, mail, file type associations",
                    "icon": ["preferences-desktop-default-applications"],
                    "exec": ["xfce4-mime-settings"],
                    "package": "xfce4-settings",
                    "keywords": ["browser", "mime", "associations"],
                },
            ],
        },
        {
            "id": "windows-apps",
            "title": "Windows apps",
            "icon": ["wine", "application-x-executable"],
            "kind": "native",
            "description": "Run .exe/.msi programs through Wine / Proton, prefixes, recipes",
            "keywords": ["exe", "msi", "wine", "proton", "prefix", "recipe", "adobe", "office", "doctor", "install a windows program", "winget", "msix", "appx", "reg", "powershell", "dos", "transfer", "migrate", "easy transfer"],
        },
        {
            "id": "gaming",
            "title": "Gaming",
            "icon": ["applications-games"],
            "kind": "native",
            "description": "Game Mode, MangoHud, Proton-GE, launchers, controllers, refresh rate",
            "keywords": ["steam", "proton", "lutris", "heroic", "roblox", "minecraft", "controller", "gamepad", "fps", "mangohud", "gamemode", "anti-cheat", "refresh", "dual boot", "restart into windows", "cloud gaming", "geforce now", "xbox cloud"],
        },
        {
            "id": "hardware",
            "title": "Hardware",
            "icon": ["preferences-desktop-peripherals", "preferences-system"],
            "kind": "native",
            "description": "CPU governor, GPU drivers, fans, power profile, RGB, TRIM",
            "keywords": ["cpu", "gpu", "nvidia", "amd", "intel", "driver", "governor", "fan", "sensors", "temperature", "power profile", "openrgb", "trim", "ssd"],
        },
        {
            "id": "network",
            "title": "Network",
            "icon": ["network-workgroup", "network-wireless"],
            "kind": "delegate",
            "description": "Wi-Fi, Ethernet, VPN, firewall",
            "keywords": ["wifi", "ethernet", "vpn", "firewall", "proxy", "internet"],
            "subitems": [
                {
                    "id": "connections",
                    "label": "Network connections",
                    "description": "Wi-Fi, Ethernet, mobile broadband (NetworkManager)",
                    "icon": ["network-wireless"],
                    "exec": ["nm-connection-editor"],
                    "package": "network-manager-gnome",
                    "keywords": ["wifi", "ethernet", "dns", "ip"],
                },
                {
                    "id": "vpn",
                    "label": "VPN",
                    "description": "Add a VPN connection (OpenVPN, WireGuard, …)",
                    "icon": ["network-vpn"],
                    "exec": ["nm-connection-editor", "--create", "--type=vpn"],
                    "package": "network-manager-gnome",
                    "keywords": ["openvpn", "wireguard", "tunnel"],
                },
                {
                    "id": "firewall",
                    "label": "Firewall",
                    "description": "Uncomplicated Firewall (gufw)",
                    "icon": ["security-high"],
                    "exec": ["gufw"],
                    "package": "gufw",
                    "keywords": ["ufw", "ports", "security"],
                },
            ],
        },
        {
            "id": "accounts",
            "title": "Accounts",
            "icon": ["system-users", "avatar-default"],
            "kind": "delegate",
            "description": "Users, groups, your avatar",
            "keywords": ["user", "password", "avatar", "picture", "group", "login"],
            "subitems": [
                {
                    "id": "users",
                    "label": "Users and groups",
                    "description": "Add users, change passwords, administrator rights",
                    "icon": ["system-users"],
                    "exec": ["users-admin"],
                    "alternatives": [["mintusers"], ["cinnamon-settings", "users"]],
                    "package": "gnome-system-tools",
                    "keywords": ["password", "administrator", "sudo", "add user"],
                },
                {
                    "id": "avatar",
                    "label": "Your info",
                    "description": "Change your name and profile picture (mugshot)",
                    "icon": ["avatar-default"],
                    "exec": ["mugshot"],
                    "package": "mugshot",
                    "keywords": ["picture", "photo", "name", "profile"],
                },
            ],
        },
        {
            "id": "mode",
            "title": "Lindos Mode",
            "icon": ["lindos-start", "preferences-desktop"],
            "kind": "native",
            "description": "Everyday, Gaming, Work, Creator or Lite — switch any time",
            "keywords": ["everyday", "gaming", "work", "creator", "lite", "profile", "preset", "switch"],
        },
        {
            "id": "update",
            "title": "Update & Recovery",
            "icon": ["system-software-update"],
            "kind": "delegate",
            "description": "Updates, drivers, snapshots, kernels",
            "keywords": ["update", "upgrade", "driver", "timeshift", "snapshot", "backup", "kernel", "recovery"],
            "subitems": [
                {
                    "id": "updates",
                    "label": "Update Manager",
                    "description": "Install system and application updates",
                    "icon": ["system-software-update"],
                    "exec": ["mintupdate"],
                    "package": "mintupdate",
                    "keywords": ["upgrade", "apt", "patch"],
                },
                {
                    "id": "drivers",
                    "label": "Driver Manager",
                    "description": "Proprietary drivers (NVIDIA, Wi-Fi, firmware)",
                    "icon": ["jockey", "preferences-desktop-peripherals"],
                    "exec": ["mintdrivers"],
                    "package": "mintdrivers",
                    "keywords": ["nvidia", "broadcom", "firmware"],
                },
                {
                    "id": "timeshift",
                    "label": "System snapshots (Timeshift)",
                    "description": "Restore points — roll back if an update breaks something",
                    "icon": ["document-revert", "timeshift"],
                    "exec": ["timeshift-launcher"],
                    "alternatives": [["timeshift-gtk"]],
                    "package": "timeshift",
                    "keywords": ["backup", "restore", "snapshot", "rollback"],
                },
                {
                    "id": "recovery",
                    "label": "Recovery",
                    "description": "Boot repair guide (opens in your browser)",
                    "icon": ["system-help"],
                    "exec": ["xdg-open", "https://help.ubuntu.com/community/Boot-Repair"],
                    "package": "xdg-utils",
                    "keywords": ["boot", "grub", "repair", "rescue"],
                },
                {
                    "id": "kernels",
                    "label": "Kernels",
                    "description": "Manage Linux kernels (Update Manager → View → Linux Kernels)",
                    "icon": ["system-software-update"],
                    "exec": ["mintupdate"],
                    "package": "mintupdate",
                    "keywords": ["kernel", "linux", "hwe"],
                },
            ],
        },
        {
            "id": "updates",
            "title": "Updates",
            "icon": ["system-software-update", "lindos-start"],
            "kind": "native",
            "description": "Lindos component updates, kernel version, Secure Boot signing",
            "keywords": ["update", "upgrade", "apt", "package", "manager", "mintupdate", "lindos-update", "kernel", "secure boot", "sideload"],
        },
        {
            "id": "about",
            "title": "About",
            "icon": ["help-about", "dialog-information"],
            "kind": "native",
            "description": "Lindos version, device specifications",
            "keywords": ["version", "specs", "specifications", "kernel", "cpu", "gpu", "ram", "hostname", "device name"],
        },
    ],
}


def default_pages_path() -> str:
    """``/usr/share/lindos/settings/pages.json`` honouring ``LINDOS_ROOT`` (tests)."""
    root = os.environ.get("LINDOS_ROOT", "")
    if root:
        return root.rstrip("/\\") + PAGES_JSON
    return PAGES_JSON


def pages_from_data(data: Any) -> list[Page]:
    if not isinstance(data, dict) or not isinstance(data.get("pages"), list):
        raise ValueError("pages.json must be an object with a 'pages' list")
    pages = [page_from_dict(p) for p in data["pages"]]
    seen: set[str] = set()
    for p in pages:
        if p.id in seen:
            raise ValueError(f"duplicate page id {p.id!r}")
        seen.add(p.id)
    return pages


def builtin_pages() -> list[Page]:
    return pages_from_data(BUILTIN_PAGES)


def load_pages(path: Optional[str] = None) -> list[Page]:
    """Load the page registry.  Falls back to the embedded registry (with a warning) if the
    file is missing or invalid — the app must always start."""
    p = path or default_pages_path()
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        pages = pages_from_data(data)
        if not pages:
            raise ValueError("empty page list")
        return pages
    except FileNotFoundError:
        log.warning("pages registry %s not found; using built-in registry", p)
    except (OSError, ValueError, TypeError) as exc:
        log.warning("pages registry %s invalid (%s); using built-in registry", p, exc)
    return builtin_pages()


def pages_by_id(pages: Iterable[Page]) -> dict[str, Page]:
    return {p.id: p for p in pages}


def page_ids(pages: Iterable[Page]) -> list[str]:
    return [p.id for p in pages]


# ---------------------------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------------------------


def normalize_query(query: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (query or "").strip().lower())


def query_tokens(query: Optional[str]) -> list[str]:
    q = normalize_query(query)
    return q.split(" ") if q else []


def text_matches(text: str, query: Optional[str]) -> bool:
    """True when every whitespace-separated token of ``query`` occurs in ``text`` (case-insensitive).
    An empty query matches everything."""
    tokens = query_tokens(query)
    if not tokens:
        return True
    hay = (text or "").lower()
    return all(t in hay for t in tokens)


def page_matches(page: Page, query: Optional[str]) -> bool:
    return text_matches(page.search_text(), query)


def filter_pages(pages: Iterable[Page], query: Optional[str]) -> list[Page]:
    """Pages to keep visible in the sidebar for a query (order preserved)."""
    return [p for p in pages if page_matches(p, query)]


def filter_subitems(page: Page, query: Optional[str]) -> list[SubItem]:
    """Sub-items of a delegate page matching a query.  If the *page itself* matches by title
    (e.g. query 'system'), all sub-items are kept."""
    if not query_tokens(query):
        return list(page.subitems)
    if text_matches(" ".join([page.title, *page.keywords]), query):
        return list(page.subitems)
    return [s for s in page.subitems if text_matches(s.search_text(), query)]


@dataclass(frozen=True)
class SearchHit:
    page_id: str
    subitem_id: Optional[str]
    label: str
    description: str = ""


def search(pages: Iterable[Page], query: Optional[str]) -> list[SearchHit]:
    """Flat search results across pages and their sub-items (used by the header search)."""
    hits: list[SearchHit] = []
    if not query_tokens(query):
        return hits
    for p in pages:
        title_hit = text_matches(" ".join([p.title, p.description, *p.keywords]), query)
        if title_hit:
            hits.append(SearchHit(p.id, None, p.title, p.description))
        for s in p.subitems:
            if text_matches(s.search_text(), query):
                hits.append(SearchHit(p.id, s.id, s.label, s.description))
    return hits


# ---------------------------------------------------------------------------------------------
# which_or_install resolver
# ---------------------------------------------------------------------------------------------

WhichFn = Callable[[str], Optional[str]]


def which_or_install(argv: Sequence[str], package: str = "", which: WhichFn = shutil.which) -> tuple[str, Any]:
    """Return ``("run", argv_list)`` when ``argv[0]`` is on PATH, else ``("install", package)``.

    ``which`` is injectable for tests.  If no package is given the program name is used."""
    argv_list = [str(a) for a in argv]
    if argv_list and which(argv_list[0]):
        return ("run", argv_list)
    return ("install", package or (argv_list[0] if argv_list else ""))


def resolve_subitem(item: SubItem, which: WhichFn = shutil.which) -> tuple[str, Any]:
    """Try ``exec`` then each alternative; first program found wins.  Otherwise
    ``("install", package)``."""
    for argv in item.all_argvs():
        kind, value = which_or_install(argv, item.package, which)
        if kind == "run":
            return kind, value
    fallback = item.package or (item.exec[0] if item.exec else item.label)
    return ("install", fallback)


# ---------------------------------------------------------------------------------------------
# Quick toggles
# ---------------------------------------------------------------------------------------------

QUICK_TOGGLE_IDS: tuple[str, ...] = ("dark", "gamemode", "mangohud", "compositor")


@dataclass
class QuickToggle:
    """A boolean setting shown as a switch.  ``getter``/``setter`` are injected so the model
    can be tested with a fake backend."""

    id: str
    title: str
    subtitle: str
    icon: str
    getter: Callable[[], bool]
    setter: Callable[[bool], Any]
    keywords: tuple[str, ...] = ()

    def get(self) -> bool:
        try:
            return bool(self.getter())
        except Exception as exc:  # backend failures must never crash the UI
            log.warning("quick toggle %s: get failed: %s", self.id, exc)
            return False

    def set(self, value: bool) -> bool:
        try:
            self.setter(bool(value))
            return True
        except Exception as exc:
            log.warning("quick toggle %s: set failed: %s", self.id, exc)
            return False

    def search_text(self) -> str:
        return " ".join([self.title, self.subtitle, *self.keywords]).lower()


class ToggleBackend:
    """Duck-typed backend contract for :func:`build_quick_toggles`.

    Required methods::

        is_dark() -> bool                 set_dark(bool) -> None
        config_get(key, default) -> Any   config_set(key, value) -> None
        compositor_running() -> bool      set_compositor(bool) -> None
    """


def build_quick_toggles(backend: Any) -> list[QuickToggle]:
    return [
        QuickToggle(
            id="dark",
            title="Dark mode",
            subtitle="Lindos-Dark theme, icons and cursors",
            icon="weather-clear-night",
            getter=backend.is_dark,
            setter=backend.set_dark,
            keywords=("theme", "light", "night"),
        ),
        QuickToggle(
            id="gamemode",
            title="Game Mode",
            subtitle="Automatically use Feral GameMode when a game starts",
            icon="applications-games",
            getter=lambda: bool(backend.config_get("gamemode_auto", True)),
            setter=lambda v: backend.config_set("gamemode_auto", bool(v)),
            keywords=("gamemode", "performance", "fps"),
        ),
        QuickToggle(
            id="mangohud",
            title="MangoHud overlay",
            subtitle="FPS / frametime / temperature overlay in games (Shift_R+F12 toggles)",
            icon="utilities-system-monitor",
            getter=lambda: bool(backend.config_get("mangohud", False)),
            # set_mangohud also runs `lindos-mangohud sync` (writes ~/.config/MangoHud/MangoHud.conf)
            setter=lambda v: (backend.set_mangohud(bool(v)) if hasattr(backend, "set_mangohud")
                              else backend.config_set("mangohud", bool(v))),
            keywords=("fps", "overlay", "hud"),
        ),
        QuickToggle(
            id="compositor",
            title="Compositor",
            subtitle="Window shadows, transparency and smooth animations (picom)",
            icon="preferences-desktop-effects",
            getter=backend.compositor_running,
            setter=backend.set_compositor,
            keywords=("picom", "effects", "shadows", "transparency", "tearing"),
        ),
    ]


class InMemoryToggleBackend:
    """A tiny in-memory backend (used by tests and by the app when lindos-core is missing)."""

    def __init__(self, dark: bool = True, compositor: bool = True, config: Optional[dict[str, Any]] = None):
        self.dark = dark
        self.compositor = compositor
        self.config: dict[str, Any] = dict(config or {})

    def is_dark(self) -> bool:
        return self.dark

    def set_dark(self, value: bool) -> None:
        self.dark = bool(value)

    def config_get(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def config_set(self, key: str, value: Any) -> None:
        self.config[key] = value

    def compositor_running(self) -> bool:
        return self.compositor

    def set_compositor(self, value: bool) -> None:
        self.compositor = bool(value)


# ---------------------------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------------------------


def format_mb(mb: Any) -> str:
    """512 → '512 MB'; 2048 → '2.0 GB'; None → '—'."""
    try:
        value = float(mb)
    except (TypeError, ValueError):
        return "—"
    if value < 0:
        value = 0.0
    if value >= 1024:
        return f"{value / 1024:.1f} GB"
    return f"{int(round(value))} MB"


def format_bytes(n: Any) -> str:
    try:
        value = float(n)
    except (TypeError, ValueError):
        return "—"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    i = 0
    while value >= 1024 and i < len(units) - 1:
        value /= 1024.0
        i += 1
    if i == 0:
        return f"{int(value)} B"
    return f"{value:.1f} {units[i]}"


def format_uptime(seconds: Any) -> str:
    try:
        secs = int(float(seconds))
    except (TypeError, ValueError):
        return "—"
    if secs < 60:
        return "< 1 min"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts: list[str] = []
    if days:
        parts.append(f"{days} day" + ("s" if days != 1 else ""))
    if hours:
        parts.append(f"{hours} h")
    if minutes or not parts:
        parts.append(f"{minutes} min")
    return " ".join(parts)


def ram_fraction(used_mb: Any, total_mb: Any) -> float:
    try:
        used = float(used_mb)
        total = float(total_mb)
    except (TypeError, ValueError):
        return 0.0
    if total <= 0:
        return 0.0
    return max(0.0, min(1.0, used / total))


def ram_summary(used_mb: Any, total_mb: Any) -> str:
    """'812 MB used of 15.5 GB — target 350–500 MB idle'"""
    return f"{format_mb(used_mb)} used of {format_mb(total_mb)} — target {RAM_TARGET_MIN_MB}–{RAM_TARGET_MAX_MB} MB idle"


def ram_verdict(used_mb: Any) -> str:
    try:
        used = float(used_mb)
    except (TypeError, ValueError):
        return "unknown"
    if used < RAM_TARGET_MIN_MB:
        return "below target"
    if used <= RAM_TARGET_MAX_MB:
        return "within target"
    return "above target"


def initials(name: Optional[str]) -> str:
    """'Jagat Goel' → 'JG'; 'nitish' → 'N'; '' → '?'"""
    words = [w for w in re.split(r"[\s._-]+", (name or "").strip()) if w]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:1].upper()
    return (words[0][:1] + words[-1][:1]).upper()


def display_name(username: str, gecos: Optional[str] = None) -> str:
    """Prefer the GECOS full name (first comma field), else the username."""
    full = (gecos or "").split(",")[0].strip()
    return full or username or "User"


def mode_display_name(mode_id: Optional[str], modes: Optional[dict[str, Any]] = None) -> str:
    """Display name for a mode id, using loaded Mode objects when available."""
    if not mode_id:
        return "Everyday"
    if modes and mode_id in modes:
        m = modes[mode_id]
        name = getattr(m, "name", None) or (m.get("name") if isinstance(m, dict) else None)
        if name:
            return str(name)
    return MODE_NAMES.get(mode_id, mode_id.replace("-", " ").title())


#: Ubuntu codename -> release the About page shows (a base-system fact, not branding)
UBUNTU_RELEASES = {"noble": "24.04 LTS", "jammy": "22.04 LTS", "focal": "20.04 LTS"}


def base_description(osr: dict[str, str]) -> str:
    """What the system is built on, for About's "Based on" row: ``Ubuntu 24.04 LTS (noble) · Linux Mint 22.2``.

    Built from the identity fields Lindos keeps on purpose (ID, VERSION_ID, UBUNTU_CODENAME) — never from
    PRETTY_NAME, which says Lindos on a Lindos system.  This row is provenance (like a licence notice),
    not branding."""
    parts: list[str] = []
    ubuntu = str(osr.get("UBUNTU_CODENAME") or "").strip()
    if ubuntu:
        release = UBUNTU_RELEASES.get(ubuntu)
        parts.append("Ubuntu " + (f"{release} ({ubuntu})" if release else ubuntu))
    if str(osr.get("ID") or "") == "linuxmint" and osr.get("VERSION_ID"):
        parts.append("Linux Mint " + str(osr["VERSION_ID"]))
    return " · ".join(parts) or "Ubuntu / Debian packages"


# ---------------------------------------------------------------------------------------------
# Static data: accents, launchers, power menu
# ---------------------------------------------------------------------------------------------

DEFAULT_ACCENTS: tuple[tuple[str, str], ...] = (
    ("Lindos Blue", "#60CDFF"),
    ("Windows Blue", "#0078D4"),
    ("Iris", "#8764B8"),
    ("Orchid", "#B146C2"),
    ("Coral", "#E74856"),
    ("Orange", "#F7630C"),
    ("Meadow", "#10893E"),
    ("Teal", "#00B7C3"),
)

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def normalize_hex(value: Any) -> Optional[str]:
    m = _HEX_RE.match(str(value or "").strip())
    return ("#" + m.group(1).upper()) if m else None


def parse_accents(data: Any, limit: int = 8) -> list[tuple[str, str]]:
    """Accept ``["#hex", …]``, ``[{"name","hex"|"color"|"value"}, …]`` or ``{"name": "#hex"}``
    (also wrapped in ``{"accents": …}``).  Returns up to ``limit`` (name, #HEX) pairs; falls
    back to DEFAULT_ACCENTS if nothing usable."""
    if isinstance(data, dict) and "accents" in data:
        data = data["accents"]
    out: list[tuple[str, str]] = []
    if isinstance(data, dict):
        for name, val in data.items():
            hx = normalize_hex(val)
            if hx:
                out.append((str(name), hx))
    elif isinstance(data, list):
        for entry in data:
            if isinstance(entry, str):
                hx = normalize_hex(entry)
                if hx:
                    out.append((hx, hx))
            elif isinstance(entry, dict):
                hx = None
                for key in ("hex", "color", "colour", "value", "accent"):
                    hx = normalize_hex(entry.get(key))
                    if hx:
                        break
                if hx:
                    out.append((str(entry.get("name") or hx), hx))
    out = out[:limit]
    return out or list(DEFAULT_ACCENTS[:limit])


@dataclass(frozen=True)
class Launcher:
    id: str
    name: str
    description: str
    icon: tuple[str, ...]
    binaries: tuple[str, ...] = ()
    flatpaks: tuple[str, ...] = ()
    run: tuple[str, ...] = ()
    note: str = ""


LAUNCHERS: tuple[Launcher, ...] = (
    Launcher("steam", "Steam", "Valve's store; Proton runs Windows games", ("steam",), ("steam",), ("com.valvesoftware.Steam",), ("steam",)),
    Launcher("lutris", "Lutris", "Battle.net, EA, Ubisoft, GOG and emulators", ("lutris",), ("lutris",), ("net.lutris.Lutris",), ("lutris",)),
    Launcher("heroic", "Heroic", "Epic Games, GOG and Amazon Prime games", ("heroic",), ("heroic",), ("com.heroicgameslauncher.hgl",), ("heroic",)),
    Launcher("prism", "Prism Launcher", "Minecraft Java (native)", ("prismlauncher", "org.prismlauncher.PrismLauncher"), ("prismlauncher", "PrismLauncher"), ("org.prismlauncher.PrismLauncher",), ("prismlauncher",)),
    Launcher("sober", "Roblox (Sober)", "Roblox via Sober — not the Windows client", ("org.vinegarhq.Sober", "roblox"), (), ("org.vinegarhq.Sober",), ("flatpak", "run", "org.vinegarhq.Sober"), "Sober is a community runtime, not the official Windows player."),
    Launcher("vinegar", "Roblox Studio (Vinegar)", "Roblox Studio through Wine (Vinegar)", ("org.vinegarhq.Vinegar", "roblox-studio"), (), ("org.vinegarhq.Vinegar",), ("flatpak", "run", "org.vinegarhq.Vinegar")),
    Launcher("mcpelauncher", "Minecraft Bedrock", "mcpelauncher (unofficial; needs the Android version you own)", ("io.mrarm.mcpelauncher", "minecraft"), ("mcpelauncher-ui-qt",), ("io.mrarm.mcpelauncher",), ("flatpak", "run", "io.mrarm.mcpelauncher"), "Unofficial launcher — sign in with an account that owns Minecraft on Google Play."),
    Launcher("bottles", "Bottles", "Manage Wine prefixes with a friendly UI", ("com.usebottles.bottles", "bottles"), ("bottles",), ("com.usebottles.bottles",), ("flatpak", "run", "com.usebottles.bottles")),
)

LAUNCHER_INSTALL_ITEMS: tuple[str, ...] = tuple(launcher.id for launcher in LAUNCHERS)


def launcher_installed(launcher: Launcher, which: WhichFn = shutil.which, flatpaks: Iterable[str] = ()) -> bool:
    fl = set(flatpaks)
    if any(f in fl for f in launcher.flatpaks):
        return True
    return any(which(b) for b in launcher.binaries)


def launcher_run_argv(launcher: Launcher, which: WhichFn = shutil.which, flatpaks: Iterable[str] = ()) -> Optional[list[str]]:
    """argv to launch an installed launcher: native binary preferred, then flatpak run."""
    for b in launcher.binaries:
        if which(b):
            return [b]
    fl = set(flatpaks)
    for f in launcher.flatpaks:
        if f in fl and which("flatpak"):
            return ["flatpak", "run", f]
    if launcher.run and which(launcher.run[0]):
        return list(launcher.run)
    return None


@dataclass(frozen=True)
class PowerItem:
    id: str
    label: str
    icon: str
    argv: tuple[str, ...]
    separator_after: bool = False


POWER_MENU_ITEMS: tuple[PowerItem, ...] = (
    PowerItem("sleep", "Sleep", "system-suspend", ("xfce4-session-logout", "--suspend")),
    PowerItem("restart", "Restart", "system-reboot", ("xfce4-session-logout", "--reboot")),
    PowerItem("shutdown", "Shut down", "system-shutdown", ("xfce4-session-logout", "--halt")),
    PowerItem("signout", "Sign out", "system-log-out", ("xfce4-session-logout", "--logout")),
    PowerItem("lock", "Lock", "system-lock-screen", ("xflock4",), separator_after=True),
    PowerItem("settings", "Settings", "preferences-system", ("lindos-settings",)),
    PowerItem("files", "File Explorer", "system-file-manager", ("thunar",)),
    PowerItem("terminal", "Terminal", "utilities-terminal", ("xfce4-terminal",)),
    PowerItem("taskmanager", "Task Manager", "utilities-system-monitor", ("xfce4-taskmanager",)),
)


def power_menu_items() -> list[PowerItem]:
    return list(POWER_MENU_ITEMS)


ANTICHEAT_TEXT = (
    "Windows programs and games run through Wine / Proton — a translation layer, not Windows and "
    "not a virtual machine. Most single-player games work. Games with kernel-level anti-cheat do "
    "not: Valorant (Vanguard), Fortnite (Epic disabled EAC for Linux), League of Legends, Apex "
    "Legends, Rainbow Six Siege, Destiny 2 and PUBG will NOT run on Lindos or any Linux. Roblox "
    "runs through Sober (a community runtime, not the Windows client). Minecraft Java is native; "
    "Bedrock works via the unofficial mcpelauncher. Steam games depend on the developer enabling "
    "anti-cheat for Proton — check the links below before buying."
)
ANTICHEAT_LINKS: tuple[tuple[str, str], ...] = (
    ("Are We Anti-Cheat Yet?", "https://areweanticheatyet.com"),
    ("ProtonDB", "https://www.protondb.com"),
)

WINE_HONESTY_TEXT = (
    "Windows programs run through Wine / Proton, a compatibility layer (no virtual machine, "
    "near-native speed). It is not Windows: some installers, anti-cheat and DRM will not work."
)

# ---------------------------------------------------------------------------------------------
# Normalisers for data from lindos-core / CLIs (defensive: shapes may vary)
# ---------------------------------------------------------------------------------------------


def _first(d: Any, keys: Sequence[str], default: Any = None) -> Any:
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def normalize_cpu(info: Any) -> dict[str, Any]:
    """→ {model, cores, threads, governor, arch}"""
    if not isinstance(info, dict):
        info = {}
    model = _first(info, ("model", "model_name", "name", "brand", "cpu"), "Unknown CPU")
    cores = _first(info, ("cores", "physical_cores", "core_count"), None)
    threads = _first(info, ("threads", "logical", "logical_cores", "count", "nproc", "cpus"), None)
    if cores is None and threads is not None:
        cores = threads
    if threads is None and cores is not None:
        threads = cores

    def _int(v: Any) -> Optional[int]:
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    return {
        "model": str(model),
        "cores": _int(cores),
        "threads": _int(threads),
        "governor": _first(info, ("governor", "scaling_governor"), None),
        "arch": _first(info, ("arch", "architecture", "machine"), None),
    }


def normalize_gpus(info: Any) -> list[dict[str, Any]]:
    """→ [{vendor, model, driver}] ; vendor in nvidia|amd|intel|other"""
    items: list[Any]
    if isinstance(info, dict) and isinstance(info.get("gpus"), list):
        items = info["gpus"]
    elif isinstance(info, dict) and isinstance(info.get("devices"), list):
        items = info["devices"]
    elif isinstance(info, dict):
        items = [info]
    elif isinstance(info, list):
        items = info
    else:
        items = []
    out: list[dict[str, Any]] = []
    for g in items:
        if isinstance(g, str):
            out.append({"vendor": guess_gpu_vendor(g), "model": g, "driver": ""})
            continue
        if not isinstance(g, dict):
            continue
        model = str(_first(g, ("model", "name", "device", "description"), "Unknown GPU"))
        vendor = str(_first(g, ("vendor",), "") or guess_gpu_vendor(model)).lower()
        if vendor not in ("nvidia", "amd", "intel", "other"):
            vendor = guess_gpu_vendor(vendor + " " + model)
        out.append({"vendor": vendor, "model": model, "driver": str(_first(g, ("driver", "kernel_driver", "module"), "") or "")})
    return out


def guess_gpu_vendor(text: str) -> str:
    t = (text or "").lower()
    if "nvidia" in t or "geforce" in t or "quadro" in t:
        return "nvidia"
    if "amd" in t or "radeon" in t or "ati " in t or t.startswith("ati") or "advanced micro" in t:
        return "amd"
    if "intel" in t:
        return "intel"
    return "other"


def normalize_ram(info: Any) -> dict[str, Any]:
    """→ {total, used, available} in MB (ints), missing → 0"""
    if not isinstance(info, dict):
        return {"total": 0, "used": 0, "available": 0}

    def _num(v: Any) -> int:
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0

    total = _num(_first(info, ("total", "total_mb", "MemTotal"), 0))
    used = _num(_first(info, ("used", "used_mb"), 0))
    avail = _num(_first(info, ("available", "available_mb", "MemAvailable", "free"), 0))
    if not used and total and avail:
        used = max(0, total - avail)
    if not avail and total and used:
        avail = max(0, total - used)
    return {"total": total, "used": used, "available": avail}


def normalize_snapshot(snap: Any) -> dict[str, Any]:
    """lindos.ram.snapshot() → {total, used, available, top: [(name, rss_mb), …]}"""
    base = normalize_ram(snap)
    top: list[tuple[str, float]] = []
    raw_top = snap.get("top") if isinstance(snap, dict) else None
    if isinstance(raw_top, list):
        for entry in raw_top:
            try:
                if isinstance(entry, dict):
                    name = str(_first(entry, ("name", "comm", "process"), "?"))
                    rss = float(_first(entry, ("rss_mb", "rss", "mb"), 0) or 0)
                else:
                    name, rss = str(entry[0]), float(entry[1])
                top.append((name, rss))
            except (TypeError, ValueError, IndexError):
                continue
    base["top"] = top
    return base


def normalize_fans(data: Any) -> list[dict[str, Any]]:
    """Fan/temperature readings → [{name, value, unit, kind}] where kind ∈ fan|temp|other.

    Accepts a flat dict ``{name: value}``, a list of dicts, or the nested ``sensors -j``
    layout (chip → feature → {input keys})."""
    out: list[dict[str, Any]] = []

    def _add(name: str, value: Any, unit: str = "", kind: str = "other") -> None:
        try:
            val = float(value)
        except (TypeError, ValueError):
            return
        if not unit and not kind:
            kind = "other"
        out.append({"name": name, "value": val, "unit": unit, "kind": kind})

    def _classify(label: str, key: str = "") -> tuple[str, str]:
        lk = (label + " " + key).lower()
        if "fan" in lk or "rpm" in lk:
            return "RPM", "fan"
        if "temp" in lk or "°" in lk or "tctl" in lk or "tdie" in lk or "core" in lk or "edge" in lk or "junction" in lk:
            return "°C", "temp"
        if "power" in lk or "ppt" in lk:
            return "W", "other"
        if "in" in lk.split() or "vcore" in lk or "voltage" in lk:
            return "V", "other"
        return "", "other"

    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict):
                name = str(_first(entry, ("name", "label", "sensor"), "sensor"))
                chip = str(entry.get("chip") or "")
                if chip and chip.lower() not in name.lower():
                    name = f"{chip}: {name}"
                value = _first(entry, ("value", "rpm", "celsius", "temp", "input"), None)
                unit = str(entry.get("unit") or "")
                kind = str(entry.get("kind") or entry.get("type") or "")
                # lindos.hardware shapes: {"chip","name","rpm"} / {"chip","name","celsius"}
                if "rpm" in entry and not unit:
                    unit, kind = "RPM", kind or "fan"
                elif "celsius" in entry and not unit:
                    unit, kind = "°C", kind or "temp"
                if not unit or not kind:
                    u2, k2 = _classify(name, str(kind))
                    unit = unit or u2
                    kind = kind or k2
                _add(name, value, unit, kind)
            elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                unit, kind = _classify(str(entry[0]))
                _add(str(entry[0]), entry[1], unit, kind)
        return out
    if isinstance(data, dict):
        for chip, feats in data.items():
            if isinstance(feats, (int, float, str)):
                unit, kind = _classify(str(chip))
                _add(str(chip), feats, unit, kind)
                continue
            if not isinstance(feats, dict):
                continue
            for feat, vals in feats.items():
                if feat == "Adapter":
                    continue
                if isinstance(vals, (int, float)):
                    unit, kind = _classify(str(feat))
                    _add(f"{chip} {feat}".strip(), vals, unit, kind)
                    continue
                if not isinstance(vals, dict):
                    continue
                # sensors -j: {"temp1_input": 45.0, "temp1_max": 90.0, ...} — keep *_input
                for key, v in vals.items():
                    if key.endswith("_input"):
                        unit, kind = _classify(str(feat), key)
                        _add(f"{chip}: {feat}", v, unit, kind)
                        break
    return out


def _fmt_rate(value: Any) -> Optional[str]:
    try:
        return f"{float(str(value).strip().rstrip('*+')):.2f}"
    except (TypeError, ValueError):
        return None


def normalize_refresh_rates(data: Any) -> list[dict[str, Any]]:
    """→ [{output, current, rates:[str], resolution}] from various shapes.

    Understands the ``lindos.hardware.refresh_rates()`` layout
    ``{output: {"connected", "current_mode", "current_rate", "modes": {res: [rates]}}}``
    (rates of the *current* resolution are offered; disconnected outputs are dropped), a
    ``{output: {"current", "rates": [...]}}`` dict, an ``{"outputs": …}`` wrapper and a list of
    ``{"output", "current", "rates"}`` dicts."""
    out: list[dict[str, Any]] = []

    def _resolution(v: Any) -> str:
        return str(_first(v, ("resolution", "current_mode", "mode"), "") or "") if isinstance(v, dict) else ""

    def _rates(v: Any) -> list[str]:
        raw: Any = v
        if isinstance(v, dict):
            modes = v.get("modes")
            if isinstance(modes, dict) and modes:
                res = _resolution(v)
                raw = modes.get(res) if res in modes else None
                if raw is None:
                    # no current mode known: offer the union of every rate
                    raw = [r for rs in modes.values() if isinstance(rs, (list, tuple)) for r in rs]
            else:
                raw = v.get("rates") or v.get("available") or (modes if isinstance(modes, (list, tuple)) else []) or []
        if isinstance(raw, (int, float, str)):
            raw = [raw]
        rates: list[str] = []
        for r in raw if isinstance(raw, (list, tuple)) else []:
            s = _fmt_rate(r)
            if s and s not in rates:
                rates.append(s)
        return rates

    def _cur(v: Any) -> Optional[str]:
        if not isinstance(v, dict):
            return None
        c = _first(v, ("current", "rate", "active", "current_rate"), None)
        if c is None:
            return None
        return _fmt_rate(c) or str(c)

    def _connected(v: Any) -> bool:
        if isinstance(v, dict) and "connected" in v:
            return bool(v.get("connected"))
        return True

    if isinstance(data, dict):
        if isinstance(data.get("outputs"), (list, dict)):
            data = data["outputs"]
    if isinstance(data, dict):
        for name, v in data.items():
            if not _connected(v):
                continue
            out.append({"output": str(name), "current": _cur(v), "rates": _rates(v), "resolution": _resolution(v)})
    elif isinstance(data, list):
        for v in data:
            if not isinstance(v, dict) or not _connected(v):
                continue
            name = str(_first(v, ("output", "name", "connector", "monitor"), "?"))
            out.append({"output": name, "current": _cur(v), "rates": _rates(v), "resolution": _resolution(v)})
    for entry in out:
        if entry["current"] and entry["current"] not in entry["rates"]:
            entry["rates"].insert(0, entry["current"])
        entry["rates"] = sorted(set(entry["rates"]), key=lambda s: -float(s))
    return out


_XRANDR_MODE_RE = re.compile(r"^\s+(\d+x\d+[a-z]?)\s+(.*)$")
_XRANDR_RATE_RE = re.compile(r"(\d+(?:\.\d+)?)([*+]*)")


def parse_xrandr(text: str) -> list[dict[str, Any]]:
    """Parse ``xrandr`` (no args) output → same shape as :func:`normalize_refresh_rates`."""
    outputs: list[dict[str, Any]] = []
    cur: Optional[dict[str, Any]] = None
    for line in (text or "").splitlines():
        if not line.startswith(" ") and " connected" in line:
            name = line.split()[0]
            cur = {"output": name, "current": None, "rates": [], "resolution": ""}
            outputs.append(cur)
            continue
        if not line.startswith(" ") and " disconnected" in line:
            cur = None
            continue
        if cur is None:
            continue
        m = _XRANDR_MODE_RE.match(line)
        if not m:
            continue
        res, rest = m.group(1), m.group(2)
        for rate, flags in _XRANDR_RATE_RE.findall(rest):
            try:
                rs = f"{float(rate):.2f}"
            except ValueError:
                continue
            if "*" in flags:
                cur["current"] = rs
                cur["resolution"] = res
                if rs not in cur["rates"]:
                    cur["rates"].insert(0, rs)
            if not cur["resolution"] or res == cur["resolution"]:
                if rs not in cur["rates"]:
                    cur["rates"].append(rs)
    for o in outputs:
        o["rates"] = sorted(set(o["rates"]), key=lambda s: -float(s))
    return outputs


COMPAT_STATUSES: tuple[str, ...] = ("works", "partial", "broken", "native", "not-possible")


def normalize_compat_status(value: Any) -> str:
    s = str(value or "").strip().lower().replace("_", "-").replace(" ", "-")
    if s in ("works", "working", "gold", "platinum", "ok", "yes"):
        return "works"
    if s in ("native", "linux-native"):
        return "native"
    if s in ("partial", "silver", "bronze", "mixed", "unstable"):
        return "partial"
    if s in ("not-possible", "impossible", "no", "unsupported", "never", "blocked"):
        return "not-possible"
    if s in ("broken", "borked", "fails", "fail"):
        return "broken"
    return s or "unknown"


def parse_compat_matrix(data: Any) -> list[dict[str, str]]:
    """``/usr/share/lindos/compat-matrix.json`` → [{name, status, reason, link, how}] sorted by name."""
    items: Any = data
    if isinstance(data, dict):
        for key in ("games", "entries", "items", "matrix"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
        else:
            items = [dict(v, name=k) if isinstance(v, dict) else {"name": k, "status": v} for k, v in data.items() if k not in ("schema", "version", "updated")]
    if not isinstance(items, list):
        return []
    out: list[dict[str, str]] = []
    for e in items:
        if not isinstance(e, dict):
            continue
        name = str(_first(e, ("name", "title", "game"), "") or "")
        if not name:
            continue
        out.append(
            {
                "name": name,
                "status": normalize_compat_status(_first(e, ("status", "state"), "")),
                "reason": str(_first(e, ("reason", "notes", "note", "why"), "") or ""),
                "link": str(_first(e, ("link", "url", "href"), "") or ""),
                "how": str(_first(e, ("how", "via", "launcher", "method"), "") or ""),
            }
        )
    return sorted(out, key=lambda d: d["name"].lower())


def parse_recipe(data: Any, fallback_id: str = "") -> Optional[dict[str, Any]]:
    """One recipe JSON → {id, name, vendor, category, status, notes, runner}."""
    if not isinstance(data, dict):
        return None
    rid = str(data.get("id") or fallback_id or "").strip()
    if not rid:
        return None
    return {
        "id": rid,
        "name": str(data.get("name") or rid),
        "vendor": str(data.get("vendor") or ""),
        "category": str(data.get("category") or ""),
        "status": normalize_compat_status(data.get("status") or "unknown"),
        "notes": str(data.get("notes") or ""),
        "runner": str(data.get("runner") or ""),
        "arch": str(data.get("arch") or ""),
    }


def normalize_apps_db(db: Any) -> list[dict[str, Any]]:
    """APPS_DB dict {slug: {...}} (or list) → sorted list of {slug, name, exe, prefix, runner,
    installed_at, last_used, kind}."""
    entries: list[dict[str, Any]] = []
    if isinstance(db, dict):
        # tolerate {"apps": {...}} wrapper
        if "apps" in db and isinstance(db["apps"], (dict, list)) and not any(isinstance(v, dict) and "exe" in v for v in db.values()):
            db = db["apps"]
    if isinstance(db, dict):
        it: Iterable[tuple[str, Any]] = db.items()
    elif isinstance(db, list):
        it = ((str(_first(e, ("slug", "id"), i)), e) for i, e in enumerate(db))
    else:
        it = ()
    for slug, e in it:
        if not isinstance(e, dict):
            continue
        entries.append(
            {
                "slug": str(e.get("slug") or slug),
                "name": str(e.get("name") or slug),
                "exe": str(e.get("exe") or e.get("path") or ""),
                "prefix": str(e.get("prefix") or slug),
                "runner": str(e.get("runner") or "wine"),
                "installed_at": str(e.get("installed_at") or ""),
                "last_used": str(e.get("last_used") or e.get("last_run") or ""),
                "kind": str(e.get("kind") or "app"),
            }
        )
    return sorted(entries, key=lambda d: d["name"].lower())


def prefix_path(prefix: str, prefixes_dir: str) -> str:
    """Absolute WINEPREFIX for an APPS_DB entry: keep absolute paths, else <prefixes_dir>/<slug>."""
    if not prefix:
        return prefixes_dir
    if os.path.isabs(prefix) or prefix.startswith("~"):
        return os.path.expanduser(prefix)
    return os.path.join(prefixes_dir, prefix)


def mode_diff(current: dict[str, Any], target: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Rows (label, from, to) describing what a mode switch changes.  ``current``/``target``
    are dicts with keys governor, compositor, pins (list), zram_percent, packages (list),
    flatpaks, services_enable, services_disable, sysctl (dict)."""

    def _s(v: Any) -> str:
        if v is None or v == "":
            return "—"
        if isinstance(v, (list, tuple, dict, set)):
            return str(len(v))
        return str(v)

    rows: list[tuple[str, str, str]] = []
    rows.append(("CPU governor", _s(current.get("governor")), _s(target.get("governor"))))
    rows.append(("Compositor", _s(current.get("compositor")), _s(target.get("compositor"))))
    rows.append(("Taskbar pins", _s(current.get("pins")), _s(target.get("pins")) + " apps"))
    zc, zt = current.get("zram_percent"), target.get("zram_percent")
    rows.append(("zram size", (f"{zc} %" if zc not in (None, "") else "—"), (f"{zt} %" if zt not in (None, "") else "—")))
    pk = target.get("packages") or []
    fp = target.get("flatpaks") or []
    rows.append(("Packages", "", f"{len(pk)} apt + {len(fp)} flatpak (installed only if missing & online)"))
    en = target.get("services_enable") or []
    di = target.get("services_disable") or []
    rows.append(("Services", "", f"enable {len(en)}, disable {len(di)}"))
    sc = target.get("sysctl") or {}
    rows.append(("Kernel sysctl", "", f"{len(sc)} setting" + ("s" if len(sc) != 1 else "")))
    return rows


def parse_governor_list(text: str) -> list[str]:
    """'schedutil performance powersave' → list"""
    return [g for g in re.split(r"[\s,]+", (text or "").strip()) if g]


def parse_lines(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip() and not ln.strip().startswith("#")]


DRIVER_IDS: tuple[str, ...] = ("nvidia-open", "nvidia-proprietary", "amd", "intel")


def driver_install_payload(vendor: str, variant: str = "") -> dict[str, Any]:
    """Payload for helper action ``install-drivers`` (SPEC §4.6 / lindos.helper):
    ``{"driver": "nvidia-open"|"nvidia-proprietary"|"amd"|"intel"}`` or ``{"args": []}`` for
    auto-detection when the vendor is unknown."""
    v = (vendor or "").strip().lower()
    if v == "nvidia":
        flavour = "open" if (variant or "").strip().lower() in ("open", "nvidia-open") else "proprietary"
        return {"driver": f"nvidia-{flavour}"}
    if v in ("amd", "intel"):
        return {"driver": v}
    return {"args": []}


def summarize_driver_status(data: Any) -> str:
    """One-line human summary of ``lindos-drivers status --json`` (or ``detect --json``)."""
    if not isinstance(data, dict):
        return ""
    parts: list[str] = []
    gpus = data.get("gpus")
    if isinstance(gpus, list) and gpus:
        names = []
        for g in gpus:
            if not isinstance(g, dict):
                continue
            nm = str(_first(g, ("model", "name", "description"), "GPU"))
            drv = str(_first(g, ("driver", "kernel_driver"), "") or "")
            names.append(nm + (f" [{drv}]" if drv else " [no kernel driver]"))
        if names:
            parts.append("; ".join(names))
    pk = data.get("nvidia_packages")
    if isinstance(pk, list) and pk:
        parts.append("NVIDIA: " + ", ".join(f"{p.get('package')} {p.get('version', '')}".strip() if isinstance(p, dict) else str(p) for p in pk))
    elif isinstance(data.get("vendors"), list) and "nvidia" in data["vendors"]:
        parts.append("NVIDIA driver: not installed (nouveau) — use Install drivers")
    if data.get("opengl_renderer"):
        parts.append(f"OpenGL: {data['opengl_renderer']}")
    mv = data.get("mesa_vulkan")
    if isinstance(mv, dict):
        parts.append("Vulkan 32-bit: " + ("yes" if mv.get("i386") else "missing (needed by many Steam/Wine games)"))
    if data.get("secure_boot") == "enabled":
        parts.append("Secure Boot on (NVIDIA modules need MOK signing)")
    if data.get("reboot_required"):
        parts.append("reboot required")
    ud = data.get("ubuntu_drivers")
    if isinstance(ud, list):
        rec = [str(d.get("package")) for d in ud if isinstance(d, dict) and d.get("recommended")]
        if rec:
            parts.append("recommended: " + ", ".join(rec))
    if not parts:
        for k in ("status", "message", "error"):
            if data.get(k):
                parts.append(f"{k}: {data[k]}")
    return " · ".join(parts)


# ---------------------------------------------------------------------------------------------
# Windows apps: file formats, terminal .exe, winget (SPEC-WINDOWS §28, §32)
# ---------------------------------------------------------------------------------------------

FORMAT_STATUSES: tuple[str, ...] = ("works", "partial", "unsupported")


def normalize_format_row(d: Any) -> dict[str, Any]:
    """One row of ``lindos-compat formats --json`` -> {id,label,suffixes,mime,handler,status,note}
    (SPEC-WINDOWS §28.2 ``FormatSpec``); defensive against missing/odd fields."""
    if not isinstance(d, dict):
        d = {}
    suffixes = d.get("suffixes")
    if isinstance(suffixes, str):
        suffixes = [suffixes]
    elif not isinstance(suffixes, (list, tuple)):
        suffixes = []
    status = str(d.get("status") or "").strip().lower()
    if status not in FORMAT_STATUSES:
        status = "unsupported" if status else "unknown"
    return {
        "id": str(d.get("id") or ""),
        "label": str(d.get("label") or d.get("id") or ""),
        "suffixes": [str(s) for s in suffixes if str(s)],
        "mime": str(d.get("mime") or ""),
        "handler": str(d.get("handler") or ""),
        "status": status,
        "note": str(d.get("note") or ""),
    }


def parse_formats_table(data: Any) -> list[dict[str, Any]]:
    """``lindos-compat formats --json`` (a bare list, or ``{"formats": [...]}``\\ ) -> normalised
    rows, sorted works -> partial -> unsupported, then by label. Never raises."""
    rows: Any = data
    if isinstance(data, dict):
        rows = data.get("formats") if isinstance(data.get("formats"), list) else []
    if not isinstance(rows, list):
        rows = []
    out = [normalize_format_row(r) for r in rows if isinstance(r, dict)]
    order = {s: i for i, s in enumerate(FORMAT_STATUSES)}
    return sorted(out, key=lambda r: (order.get(r["status"], len(order)), r["label"].lower()))


def normalize_binfmt_status(data: Any) -> dict[str, Any]:
    """``lindos-compat binfmt status --json`` (SPEC-WINDOWS §28.7) ->
    ``{registered,enabled,masked,conflicts,interpreter,note}``, honest about conflicts."""
    if not isinstance(data, dict):
        data = {}
    conflicts_raw = data.get("conflicts")
    conflicts: list[dict[str, str]] = []
    if isinstance(conflicts_raw, list):
        for c in conflicts_raw:
            if isinstance(c, dict):
                conflicts.append({"name": str(c.get("name") or ""), "interpreter": str(c.get("interpreter") or "")})
    return {
        "registered": bool(data.get("registered")),
        "enabled": bool(data.get("enabled")),
        "masked": bool(data.get("masked")),
        "conflicts": conflicts,
        "interpreter": str(data.get("interpreter") or ""),
        "note": str(data.get("note") or ""),
    }


def binfmt_summary(st: dict[str, Any]) -> str:
    """One-line honest summary for the 'Run .exe from the terminal' switch's subtitle."""
    if st.get("registered") and st.get("enabled"):
        text = "On — double-click behaviour also works when you type ./program.exe in a terminal"
    elif st.get("masked"):
        text = "Off"
    elif st.get("registered"):
        text = "Off (registered but disabled)"
    else:
        text = "Off"
    if st.get("conflicts"):
        names = ", ".join(c["name"] for c in st["conflicts"] if c.get("name"))
        text += f" — also registered: {names} (Lindos does not change it; whichever loaded last wins)"
    if st.get("note"):
        text += f" — {st['note']}"
    return text


def parse_winget_results(data: Any) -> list[dict[str, str]]:
    """``lindos-compat winget search <q> --json`` -> ``[{id,name,version,moniker,match}]``."""
    if not isinstance(data, list):
        return []
    out: list[dict[str, str]] = []
    for r in data:
        if not isinstance(r, dict):
            continue
        rid = str(r.get("id") or "")
        if not rid:
            continue
        out.append({
            "id": rid,
            "name": str(r.get("name") or rid),
            "version": str(r.get("version") or ""),
            "moniker": str(r.get("moniker") or ""),
            "match": str(r.get("match") or ""),
        })
    return out


# ---------------------------------------------------------------------------------------------
# Gaming: play-anywhere routes, dual boot (SPEC-WINDOWS §30, §32)
# ---------------------------------------------------------------------------------------------

GAME_ROUTE_TYPES: tuple[str, ...] = ("native", "proton", "cloud", "windows", "vm")


def normalize_game_route_entry(d: Any) -> dict[str, Any]:
    if not isinstance(d, dict):
        d = {}
    action = d.get("action")
    requires = d.get("requires")
    provider = d.get("provider")
    return {
        "type": str(d.get("type") or ""),
        "provider": str(provider) if provider else None,
        "label": str(d.get("label") or ""),
        "available": bool(d.get("available")),
        "why": str(d.get("why") or ""),
        "requires": [str(x) for x in requires] if isinstance(requires, (list, tuple)) else [],
        "action": dict(action) if isinstance(action, dict) else None,
    }


def normalize_game_route(data: Any) -> dict[str, Any]:
    """``lindos-game route <title> --json`` (SPEC-WINDOWS §30.2) -> a fully-defensive dict;
    an odd/partial shape degrades to empty routes rather than raising."""
    if not isinstance(data, dict):
        data = {}
    routes_raw = data.get("routes")
    routes = [normalize_game_route_entry(r) for r in routes_raw if isinstance(r, dict)] if isinstance(routes_raw, list) else []
    try:
        recommended = int(data.get("recommended", 0))
    except (TypeError, ValueError):
        recommended = 0
    if not routes or not (0 <= recommended < len(routes)):
        recommended = 0
    notes_raw = data.get("notes")
    notes = [str(n) for n in notes_raw if n] if isinstance(notes_raw, list) else []
    return {
        "title": str(data.get("title") or ""),
        "id": str(data.get("id") or ""),
        "status": str(data.get("status") or ""),
        "anticheat": str(data.get("anticheat") or ""),
        "region": str(data.get("region") or "unknown"),
        "routes": routes,
        "recommended": recommended,
        "notes": notes,
    }


#: First entry ("auto") means "detect from the system locale/timezone" (empty ``region`` config
#: key) -- never an empty combo-box id, which some GtkComboBoxText versions treat as "no match".
REGION_CHOICES: tuple[tuple[str, str], ...] = (
    ("auto", "Detect automatically"),
    ("US", "United States"),
    ("GB", "United Kingdom"),
    ("IN", "India"),
    ("CA", "Canada"),
    ("AU", "Australia"),
    ("DE", "Germany"),
    ("FR", "France"),
    ("BR", "Brazil"),
    ("JP", "Japan"),
)


def region_combo_id(value: str) -> str:
    """Stored ``region`` config value -> combo-box id (``"" -> "auto"``)."""
    return str(value) if value else "auto"


def region_config_value(region_id: str) -> str:
    """Combo-box id -> the ``region`` config value to store (``"auto" -> ""``)."""
    return "" if region_id in ("", "auto") else region_id


def normalize_boot_entry(d: Any) -> dict[str, str]:
    if not isinstance(d, dict):
        d = {}
    return {
        "num": str(d.get("num") or ""),
        "label": str(d.get("label") or ""),
        "partuuid": str(d.get("partuuid") or ""),
        "disk": str(d.get("disk") or ""),
    }


def normalize_dualboot_status(data: Any) -> dict[str, Any]:
    """``lindos-dualboot status --json`` (SPEC-WINDOWS §30.3) -> an honest dict; never raises,
    never invents a `can_reboot_to_windows: True` when the shape is unexpected."""
    if not isinstance(data, dict):
        data = {}
    entries_raw = data.get("windows_entries")
    entries = [normalize_boot_entry(e) for e in entries_raw if isinstance(e, dict)] if isinstance(entries_raw, list) else []
    return {
        "firmware": str(data.get("firmware") or "unknown"),
        "secure_boot": str(data.get("secure_boot") or "unknown"),
        "tpm": data.get("tpm"),
        "windows_entries": entries,
        "can_reboot_to_windows": bool(data.get("can_reboot_to_windows")),
        "method": data.get("method") if data.get("method") else None,
        "why": str(data.get("why") or ""),
        "lindos_kernel_signed": data.get("lindos_kernel_signed"),
        "bitlocker_hint": bool(data.get("bitlocker_hint")),
    }


def dualboot_summary(st: dict[str, Any]) -> str:
    """One-line honest summary shown under 'Restart into Windows'."""
    parts: list[str] = []
    if st.get("can_reboot_to_windows"):
        parts.append("Ready")
    else:
        parts.append("Not available")
    sb = st.get("secure_boot")
    if sb and sb != "unknown":
        parts.append(f"Secure Boot {sb}")
    tpm = st.get("tpm")
    if tpm:
        parts.append(f"TPM {tpm}")
    if not st.get("can_reboot_to_windows") and st.get("why"):
        parts.append(st["why"])
    return " · ".join(parts)


# ---------------------------------------------------------------------------------------------
# Lindos updates: lindos-update check/kernel-status/repo-status, lindos-kernel secureboot status
# (SPEC-UPDATE §36/§37)
# ---------------------------------------------------------------------------------------------

#: ``PackageUpdate.channel`` (SPEC-UPDATE §36.2).
UPDATE_CHANNELS: tuple[str, ...] = ("lindos", "system", "kernel")


def normalize_package_update(d: Any) -> dict[str, str]:
    """One ``PackageUpdate`` (SPEC-UPDATE §36.2) -> ``{name, installed, candidate, channel}``;
    an odd/missing shape degrades to empty strings rather than raising."""
    if not isinstance(d, dict):
        d = {}
    channel = str(d.get("channel") or "").strip().lower()
    if channel not in UPDATE_CHANNELS:
        channel = channel or "lindos"
    return {
        "name": str(d.get("name") or ""),
        "installed": str(d.get("installed") or ""),
        "candidate": str(d.get("candidate") or ""),
        "channel": channel,
    }


def _package_update_list(data: Any, key: str) -> list[dict[str, str]]:
    raw = data.get(key) if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    return [normalize_package_update(e) for e in raw if isinstance(e, dict) and e.get("name")]


def _missing_or_error_message(data: Any, tool: str) -> str:
    if not isinstance(data, dict):
        return ""
    if data.get("_error"):
        return str(data["_error"])
    if data.get("_missing"):
        return f"{tool} is not installed"
    return ""


def normalize_update_status(data: Any) -> dict[str, Any]:
    """``lindos-update check --json`` (SPEC-UPDATE §36.2 ``UpdateStatus``) -> an honest dict;
    never raises. The backend passes a missing binary / non-zero exit / bad JSON in as
    ``{"_missing": True}`` / ``{"_error": "..."}`` — this degrades to an empty "nothing known
    yet" status plus that message in ``error``, so the page never needs its own try/except."""
    if not isinstance(data, dict):
        data = {}
    error = _missing_or_error_message(data, "lindos-update")
    kernel_avail = data.get("kernel_available")
    repo_reachable = data.get("repo_reachable")
    return {
        "refreshed_at": str(data.get("refreshed_at") or "") or None,
        "lindos_updates": _package_update_list(data, "lindos_updates"),
        "system_updates": _package_update_list(data, "system_updates"),
        "kernel_available": normalize_package_update(kernel_avail) if isinstance(kernel_avail, dict) else None,
        "booted_kernel": str(data.get("booted_kernel") or ""),
        "booted_is_lindos_kernel": bool(data.get("booted_is_lindos_kernel")),
        "reboot_required": bool(data.get("reboot_required")),
        "repo_configured": bool(data.get("repo_configured")),
        "repo_reachable": repo_reachable if isinstance(repo_reachable, bool) else None,
        "error": error,
    }


def normalize_kernel_status(data: Any) -> dict[str, Any]:
    """``lindos-update kernel-status --json`` -> the kernel-relevant subset of ``UpdateStatus``
    (booted vs. installed vs. available kernel version, whether a restart is pending)."""
    if not isinstance(data, dict):
        data = {}
    error = _missing_or_error_message(data, "lindos-update")
    kernel_avail = data.get("kernel_available")
    return {
        "booted_kernel": str(data.get("booted_kernel") or ""),
        "booted_is_lindos_kernel": bool(data.get("booted_is_lindos_kernel")),
        "kernel_available": normalize_package_update(kernel_avail) if isinstance(kernel_avail, dict) else None,
        "reboot_required": bool(data.get("reboot_required")),
        "error": error,
    }


def normalize_repo_status(data: Any) -> dict[str, Any]:
    """``lindos-update repo status --json`` -> ``{configured, reachable, message, url}``.
    Accepts either ``configured``/``reachable`` or the ``UpdateStatus`` field spellings
    ``repo_configured``/``repo_reachable`` — both are plausible for this CLI subcommand and
    guessing wrong must never crash the page. ``reachable`` stays ``None`` (unknown) unless the
    data explicitly says so, matching ``repo_status()``'s own honesty rule (never invents
    "trusted")."""
    if not isinstance(data, dict):
        data = {}
    error = _missing_or_error_message(data, "lindos-update")
    configured = data.get("configured", data.get("repo_configured"))
    reachable = data.get("reachable", data.get("repo_reachable"))
    return {
        "configured": bool(configured),
        "reachable": reachable if isinstance(reachable, bool) else None,
        "message": str(data.get("message") or error or ""),
        "url": str(data.get("url") or ""),
    }


def normalize_secureboot_status(data: Any) -> dict[str, Any]:
    """``lindos-kernel secureboot status --json`` (SPEC-WINDOWS §31.3, already shipped by
    ``lindos_kernel.secureboot.status()``) -> a defensive copy; an unusual/missing field
    degrades to ``None``/empty rather than raising."""
    if not isinstance(data, dict):
        data = {}
    mok = data.get("mok") if isinstance(data.get("mok"), dict) else {}
    kernels_raw = data.get("kernels")
    kernels: list[dict[str, Any]] = []
    if isinstance(kernels_raw, list):
        for k in kernels_raw:
            if isinstance(k, dict):
                signed = k.get("signed")
                kernels.append({
                    "version": str(k.get("version") or ""),
                    "path": str(k.get("path") or ""),
                    "signed": signed if isinstance(signed, bool) else None,
                })
    any_signed = data.get("any_lindos_kernel_signed")
    secure_boot = data.get("secure_boot")
    return {
        "firmware": str(data.get("firmware") or "unknown"),
        "secure_boot": secure_boot if isinstance(secure_boot, bool) else None,
        "mok_present": bool(mok.get("present")),
        "mok_enrolled": mok.get("enrolled") if isinstance(mok.get("enrolled"), bool) else None,
        "kernels": kernels,
        "any_lindos_kernel_signed": any_signed if isinstance(any_signed, bool) else None,
        "error": _missing_or_error_message(data, "lindos-kernel"),
    }


def lindos_update_payload(status: dict[str, Any]) -> list[str]:
    """``"name=candidate"`` tokens for every Lindos-channel package with a real update
    (the helper ``system-upgrade`` payload, SPEC-UPDATE §36.4) — never the kernel, which has its
    own separately-confirmed "Apply now" flow, and never a bare name with no ``=version``."""
    return [f"{u['name']}={u['candidate']}" for u in (status.get("lindos_updates") or []) if u.get("name") and u.get("candidate")]


def kernel_update_payload(status_or_kernel: dict[str, Any]) -> list[str]:
    """``["name=candidate"]`` for the available kernel update, or ``[]`` when there is none.
    Works against either ``normalize_update_status``'s or ``normalize_kernel_status``'s output
    (both carry a ``kernel_available`` ``PackageUpdate``)."""
    ka = status_or_kernel.get("kernel_available") if isinstance(status_or_kernel, dict) else None
    if not isinstance(ka, dict) or not ka.get("name") or not ka.get("candidate"):
        return []
    return [f"{ka['name']}={ka['candidate']}"]


def update_status_summary(status: dict[str, Any]) -> str:
    """One-line subtitle for the "Lindos components" card."""
    if status.get("error"):
        return status["error"]
    updates = status.get("lindos_updates") or []
    if not updates:
        refreshed = status.get("refreshed_at")
        when = f" (checked {refreshed[:19].replace('T', ' ')})" if refreshed else " (never checked yet — press Check now)"
        return "Lindos components are up to date" + when
    names = ", ".join(u["name"] for u in updates[:6])
    more = " …" if len(updates) > 6 else ""
    n = len(updates)
    return f"{n} update{'s' if n != 1 else ''} available: {names}{more}"


def sideload_note_text(status: dict[str, Any]) -> str:
    """Plain-language note shown above the "Load updates from a folder…" picker (SPEC-UPDATE
    §37) — only rendered by the page when :func:`needs_sideload_note` is true."""
    if status.get("repo_reachable") is False:
        return ("Lindos's own apt repository is configured but could not be reached right now. "
                "You can still update by loading updates from a folder of .deb files below.")
    return ("No Lindos update channel is configured yet. You can still update by loading updates "
            "from a folder of .deb files below (for example, ones you built or downloaded).")


def needs_sideload_note(status: dict[str, Any]) -> bool:
    """True when the honest "no repo configured" note + folder picker should show."""
    return not bool(status.get("repo_configured"))


def kernel_status_summary(kernel: dict[str, Any]) -> str:
    """Booted vs. installed vs. available kernel line for the Kernel card."""
    if kernel.get("error"):
        return kernel["error"]
    parts = [f"Booted: {kernel.get('booted_kernel') or 'unknown'}"]
    if kernel.get("booted_kernel") and not kernel.get("booted_is_lindos_kernel"):
        parts[-1] += " (not the Lindos kernel)"
    ka = kernel.get("kernel_available")
    if ka and ka.get("candidate"):
        parts.append(f"Installed: {ka.get('installed') or '—'} → Available: {ka['candidate']}")
    else:
        parts.append("No newer Lindos kernel available")
    if kernel.get("reboot_required"):
        parts.append("A restart is needed to finish a previous update")
    return "  ·  ".join(parts)


def secureboot_summary(sb: dict[str, Any]) -> str:
    """One-line honest summary of ``lindos-kernel secureboot status`` for the Kernel card."""
    if sb.get("error"):
        return sb["error"]
    if sb.get("firmware") != "uefi":
        return "Secure Boot: not applicable (BIOS/legacy firmware)"
    if sb.get("secure_boot") is None:
        return "Secure Boot: unknown"
    if sb.get("secure_boot") is False:
        return "Secure Boot is off"
    signed = sb.get("any_lindos_kernel_signed")
    if signed is True:
        return "Secure Boot on — the Lindos kernel is signed"
    if signed is False:
        return "Secure Boot on — the Lindos kernel is NOT signed yet (see Kernels in Update Manager)"
    return "Secure Boot on — kernel signing state unknown"


def compositor_state_from_output(code: int, out: str) -> bool:
    """Interpret ``lindos-compositor status``: exit 0 + text without 'stopped/off/inactive'
    → running.  Non-zero exit → not running."""
    text = (out or "").strip().lower()
    negative = any(w in text for w in ("stopped", "not running", "inactive", "disabled", "off", "false", "none"))
    positive = any(w in text for w in ("running", "active", "enabled", "started", "on", "true", "picom", "xfwm"))
    if code != 0:
        return False
    if negative and not positive:
        return False
    if negative and positive:
        # e.g. "compositor: xfwm (picom stopped)" – treat first token as authoritative
        first = text.split(":", 1)[-1].strip().split()[0] if ":" in text else text.split()[0]
        return first not in ("stopped", "off", "inactive", "disabled", "none", "false")
    return True


__all__ = [
    "PAGE_ORDER",
    "NATIVE_PAGES",
    "DELEGATE_PAGES",
    "PAGES_JSON",
    "ACCENTS_JSON",
    "COMPAT_MATRIX_JSON",
    "RECIPES_DIR",
    "RAM_TARGET_MIN_MB",
    "RAM_TARGET_MAX_MB",
    "MODE_IDS",
    "MODE_NAMES",
    "MODE_ICONS",
    "MODE_DESCRIPTIONS",
    "SubItem",
    "Page",
    "BUILTIN_PAGES",
    "builtin_pages",
    "load_pages",
    "pages_from_data",
    "pages_by_id",
    "page_ids",
    "default_pages_path",
    "normalize_query",
    "text_matches",
    "page_matches",
    "filter_pages",
    "filter_subitems",
    "SearchHit",
    "search",
    "which_or_install",
    "resolve_subitem",
    "QuickToggle",
    "QUICK_TOGGLE_IDS",
    "ToggleBackend",
    "InMemoryToggleBackend",
    "build_quick_toggles",
    "format_mb",
    "format_bytes",
    "format_uptime",
    "ram_fraction",
    "ram_summary",
    "ram_verdict",
    "initials",
    "display_name",
    "mode_display_name",
    "base_description",
    "DEFAULT_ACCENTS",
    "normalize_hex",
    "parse_accents",
    "Launcher",
    "LAUNCHERS",
    "LAUNCHER_INSTALL_ITEMS",
    "launcher_installed",
    "launcher_run_argv",
    "PowerItem",
    "POWER_MENU_ITEMS",
    "power_menu_items",
    "ANTICHEAT_TEXT",
    "ANTICHEAT_LINKS",
    "WINE_HONESTY_TEXT",
    "normalize_cpu",
    "normalize_gpus",
    "guess_gpu_vendor",
    "normalize_ram",
    "normalize_snapshot",
    "normalize_fans",
    "normalize_refresh_rates",
    "parse_xrandr",
    "COMPAT_STATUSES",
    "normalize_compat_status",
    "parse_compat_matrix",
    "parse_recipe",
    "normalize_apps_db",
    "prefix_path",
    "mode_diff",
    "parse_governor_list",
    "parse_lines",
    "DRIVER_IDS",
    "driver_install_payload",
    "summarize_driver_status",
    "compositor_state_from_output",
    "FORMAT_STATUSES",
    "normalize_format_row",
    "parse_formats_table",
    "normalize_binfmt_status",
    "binfmt_summary",
    "parse_winget_results",
    "GAME_ROUTE_TYPES",
    "normalize_game_route_entry",
    "normalize_game_route",
    "REGION_CHOICES",
    "region_combo_id",
    "region_config_value",
    "normalize_boot_entry",
    "normalize_dualboot_status",
    "dualboot_summary",
    "UPDATE_CHANNELS",
    "normalize_package_update",
    "normalize_update_status",
    "normalize_kernel_status",
    "normalize_repo_status",
    "normalize_secureboot_status",
    "lindos_update_payload",
    "kernel_update_payload",
    "update_status_summary",
    "sideload_note_text",
    "needs_sideload_note",
    "kernel_status_summary",
    "secureboot_summary",
]
