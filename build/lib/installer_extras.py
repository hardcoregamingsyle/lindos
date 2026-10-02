#!/usr/bin/env python3
"""Derive packages/lindos-installer's extras.json - what the installer adds for every Mode.

The Mode (Everyday / Gaming / Work / Creator / Lite) is chosen only after the installation, so the
installer adds the UNION of what every Mode needs, once, while it installs (packages/lindos-installer,
docs: "the installer does everything heavy").  The union is derived, never hand-edited:

* apt        every ``packages`` entry of packages/lindos-core/.../modes/*/mode.json, plus the ``apt``
             entries of the OOBE catalogue (apps.json) that are preselected for some Mode;
* flatpaks   every ``flatpaks`` entry of the mode.json files (the launchers that only exist as Flatpaks:
             Prism, Sober, Heroic, Bottles);
* compat     the install-compat.sh items of the ``script`` entries of apps.json that run install-compat
             (Wine + umu) plus the items the mode.json files name in ``compat_items`` that the script
             knows - ``bottles`` is a Flatpak and stays with the flatpaks step;
* gaming     the install-gaming.sh items of apps.json that the script installs from apt/deb (steam,
             lutris) - the ones that are Flatpaks in a mode.json (prism, sober, heroic) come from the
             flatpaks step instead, so nothing is installed twice;
* drivers    a fixed list: firmware the installer makes sure of (already on the image, upgraded later);
* evidence   what proves an item of ``compat`` / ``gaming`` is really installed (any of some packages, files or
             Flatpak apps): the installer looks again at the very end, and the CI install test's disk checks read
             the same table, so a step that says "done" while a later step removed its result is caught.

An apt package is never added when it conflicts with something another step installs (``CONFLICTING_APT``):
``apt-get install steam-devices`` removes Valve's ``steam-launcher``.

Not installed by the installer, listed as ``optional_flatpaks``: what mode.json calls "suggested"
(OnlyOffice: several hundred MB) - Settings > Apps installs it on demand.

Usage::

    python3 build/lib/installer_extras.py --write     # regenerate the committed file
    python3 build/lib/installer_extras.py --check     # exit 1 when the committed file is stale
    python3 build/lib/installer_extras.py             # print the derived JSON

Standard library only; importable on any OS.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MODES_DIR = REPO_ROOT / "packages" / "lindos-core" / "root" / "usr" / "share" / "lindos" / "modes"
APPS_JSON = REPO_ROOT / "packages" / "lindos-setup" / "root" / "usr" / "share" / "lindos" / "setup" / "apps.json"
EXTRAS_JSON = (REPO_ROOT / "packages" / "lindos-installer" / "root" / "usr" / "share" / "lindos"
               / "installer" / "extras.json")
COMPAT_SH = REPO_ROOT / "packages" / "lindos-compat" / "root" / "usr" / "libexec" / "lindos" / "install-compat.sh"
GAMING_SH = REPO_ROOT / "packages" / "lindos-gaming" / "root" / "usr" / "libexec" / "lindos" / "install-gaming.sh"

#: firmware the installer makes sure of (all are on the image already; the updates step upgrades them)
DRIVER_FIRMWARE = ["linux-firmware", "firmware-sof-signed", "intel-microcode", "amd64-microcode"]

#: apt packages that must not be installed next to a gaming item because apt would REMOVE what that item installed:
#: {package: gaming item}.  steam-launcher (Valve's package, the Steam item) ships the udev rules itself; installing
#: the separate steam-devices package removed steam-launcher in the first real install.
CONFLICTING_APT = {"steam-devices": "steam"}

#: what proves an item is installed - ANY of the packages, files (relative to /) or Flatpak apps.  Keep in step with
#: install-compat.sh (WineHQ staging, else wine-staging, else Ubuntu's wine; umu is the pinned zipapp in /usr/local/bin
#: or the deb) and install-gaming.sh (Valve's steam-launcher, else Ubuntu's steam-installer; lutris from apt, else the
#: Flatpak); tests/build tests read the scripts.
ITEM_EVIDENCE: Dict[str, Dict[str, Dict[str, List[str]]]] = {
    "compat": {
        "wine": {"pkgs": ["winehq-staging", "wine-staging", "wine"]},
        "winetricks": {"pkgs": ["winetricks"]},
        "umu": {"pkgs": ["umu-launcher", "python3-umu-launcher"], "files": ["usr/local/bin/umu-run", "usr/bin/umu-run"]},
    },
    "gaming": {
        "steam": {"pkgs": ["steam-launcher", "steam-installer"]},
        "lutris": {"pkgs": ["lutris"], "flatpaks": ["net.lutris.Lutris"]},
    },
}

#: mode.json flatpaks that are launchers install-gaming.sh knows by another name
FLATPAK_TO_GAMING = {
    "org.prismlauncher.PrismLauncher": "prism",
    "org.vinegarhq.Sober": "sober",
    "com.heroicgameslauncher.hgl": "heroic",
    "com.usebottles.bottles": "bottles",
}


def _load(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _bash_array(text: str, name: str) -> List[str]:
    """Words of a top-level ``NAME=(a b c)`` array in a shell script."""
    match = re.search(r"^%s=\(([^)]*)\)" % re.escape(name), text, re.M)
    return match.group(1).split() if match else []


def _uniq(items: List[str]) -> List[str]:
    seen: Dict[str, None] = {}
    for item in items:
        if item and item not in seen:
            seen[item] = None
    return list(seen)


def derive(modes_dir: Path = MODES_DIR, apps_json: Path = APPS_JSON,
           compat_sh: Path = COMPAT_SH, gaming_sh: Path = GAMING_SH) -> Dict[str, Any]:
    """The extras document for the current repository state (deterministic: sorted, no timestamps)."""
    modes = {p.parent.name: _load(p) for p in sorted(modes_dir.glob("*/mode.json"))}
    apps = _load(apps_json).get("apps", [])
    compat_known = set(_bash_array(compat_sh.read_text(encoding="utf-8"), "KNOWN_ITEMS"))
    gaming_text = gaming_sh.read_text(encoding="utf-8")
    gaming_apt = set(_bash_array(gaming_text, "APT_ITEMS"))

    apt_sources: Dict[str, List[str]] = {}
    flatpak_sources: Dict[str, List[str]] = {}
    for mode_id, data in modes.items():
        for pkg in data.get("packages", []) or []:
            apt_sources.setdefault(pkg, []).append(mode_id)
        for app in data.get("flatpaks", []) or []:
            flatpak_sources.setdefault(app, []).append(mode_id)

    compat: List[str] = []
    gaming: List[str] = []
    for app in apps:
        modes_on = app.get("default_on_modes") or []
        if not modes_on:
            continue
        kind = app.get("kind")
        if kind == "apt":
            for pkg in app.get("packages", []) or []:
                apt_sources.setdefault(pkg, []).extend(m for m in modes_on if m not in apt_sources.get(pkg, []))
        elif kind == "script" and app.get("action") == "install-compat":
            compat.extend(i for i in app.get("items", []) if i in compat_known and i != "bottles")
        elif kind == "script" and app.get("action") == "install-gaming":
            for item in app.get("items", []):
                # Flatpak launchers come from the flatpaks step (mode.json lists them); apt/deb ones here
                flatpak_ids = [fid for fid, name in FLATPAK_TO_GAMING.items() if name == item and fid in flatpak_sources]
                if item in gaming_apt and not flatpak_ids:
                    gaming.append(item)
    for data in modes.values():
        compat.extend(i for i in data.get("compat_items", []) or [] if i in compat_known and i != "bottles")

    # the script's own ordering: wine first, umu last (compat_known order is irrelevant)
    order = ["wine", "winetricks", "dependencies", "fonts", "umu"]
    compat = sorted(_uniq(compat), key=lambda i: order.index(i) if i in order else len(order))
    optional_flatpaks = sorted({app for data in modes.values() for app in data.get("suggested_flatpaks", []) or []})
    for app in apps:
        if app.get("kind") == "flatpak":
            for fid in app.get("flatpaks", []) or []:
                if fid not in flatpak_sources and fid not in optional_flatpaks:
                    optional_flatpaks.append(fid)

    gaming = _uniq(gaming)
    for pkg, item in CONFLICTING_APT.items():
        if item in gaming:
            apt_sources.pop(pkg, None)
    evidence = {step: {item: ITEM_EVIDENCE[step][item] for item in items if item in ITEM_EVIDENCE[step]}
                for step, items in (("compat", compat), ("gaming", gaming))}

    return {
        "schema": 1,
        "comment": ("Generated by build/lib/installer_extras.py from the mode.json files and the OOBE apps.json "
                    "(tests keep it in sync): the union of every Mode's extras, installed once by the installer."),
        "apt": sorted(apt_sources),
        "apt_sources": {pkg: sorted(apt_sources[pkg]) for pkg in sorted(apt_sources)},
        "compat": compat,
        "gaming": gaming,
        "evidence": evidence,
        "flatpaks": sorted(flatpak_sources),
        "flatpak_sources": {app: sorted(flatpak_sources[app]) for app in sorted(flatpak_sources)},
        "optional_flatpaks": sorted(optional_flatpaks),
        "drivers": {"firmware": list(DRIVER_FIRMWARE)},
    }


def render(doc: Dict[str, Any]) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="rewrite extras.json")
    mode.add_argument("--check", action="store_true", help="exit 1 when extras.json is stale")
    ns = ap.parse_args(argv)
    text = render(derive())
    if ns.write:
        EXTRAS_JSON.parent.mkdir(parents=True, exist_ok=True)
        with open(EXTRAS_JSON, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print("wrote %s" % EXTRAS_JSON)
        return 0
    if ns.check:
        current = EXTRAS_JSON.read_text(encoding="utf-8") if EXTRAS_JSON.is_file() else ""
        if current != text:
            print("stale: %s (run: python3 build/lib/installer_extras.py --write)" % EXTRAS_JSON, file=sys.stderr)
            return 1
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
