#!/usr/bin/env python3
"""Can the installed system get oem-config from the medium?  (build-time assertion, build-iso.sh)

Ubiquity's OEM mode installs ``oem-config-gtk`` into the new system from the medium's package pool (an
apt ``cdrom:`` source) and SILENTLY skips a package it cannot find: the install then "succeeds" without the
first-boot wizard.  oem-config-gtk needs exactly the same version of ubiquity-frontend-gtk/ubiquity that the
squashfs carries.  This module checks the finished image tree so that a base ISO that changed under us fails
the build instead of a real installation:

* the ubiquity version in the squashfs (``var/lib/dpkg/status``);
* ``oem-config_<v>_all.deb`` and ``oem-config-gtk_<v>_all.deb`` in ``pool/`` with exactly that version;
* ``.disk/info`` and ``.disk/cd_type`` (apt-setup's cdrom generator needs both) and a ``dists/`` tree; when a
  Packages index is found it must list oem-config-gtk;
* what oem-config-gtk depends on beyond the ubiquity family (aptdaemon, python3-aptdaemon.gtk3widgets) is installed
  in the squashfs or in the pool (the base ISO has it installed; a debloat step that removed it would break OEM mode);
* or, failing that, a fallback directory (``OEM_DEBS_DIR``) with matching debs, copied to
  ``lindos/oem-debs/`` on the medium, which finalize.sh installs with dpkg.

Exit codes: 0 fine (pool or fallback), 1 not fine and ``--require`` given, 2 usage.  Prints one summary line.
Standard library only; the versions come from file names and the dpkg status file, so no dpkg tools needed.
"""

from __future__ import annotations

import argparse
import gzip
import re
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional

NEEDED = ("oem-config", "oem-config-gtk")
#: oem-config-gtk's dependencies that are not part of the ubiquity family (any version will do)
NEEDED_DEPENDS = ("aptdaemon", "python3-aptdaemon.gtk3widgets")


def installed_version(status_file: Path, package: str = "ubiquity") -> Optional[str]:
    """Version of an installed package from a dpkg status file (None when absent / not installed)."""
    if not status_file.is_file():
        return None
    for stanza in status_file.read_text(encoding="utf-8", errors="replace").split("\n\n"):
        fields: Dict[str, str] = {}
        for line in stanza.splitlines():
            if ":" in line and not line.startswith((" ", "\t")):
                key, _, value = line.partition(":")
                fields[key.strip()] = value.strip()
        if fields.get("Package") == package and "installed" in fields.get("Status", "").split():
            return fields.get("Version")
    return None


def strip_epoch(version: str) -> str:
    return re.sub(r"^\d+(:|%3a)", "", version, flags=re.I)


def debs_in(root: Path, package: str) -> List[str]:
    """Versions of ``<package>_<version>_<arch>.deb`` files below *root* (epoch stripped)."""
    versions: List[str] = []
    if not root.is_dir():
        return versions
    pattern = re.compile(r"^%s_(?P<v>.+)_(?:all|amd64)\.deb$" % re.escape(package))
    for path in root.rglob("*.deb"):
        m = pattern.match(path.name)
        if m:
            versions.append(strip_epoch(m.group("v")))
    return versions


def index_mentions(dists: Path, package: str) -> Optional[bool]:
    """True/False when a Packages index below *dists* does/doesn't list *package*; None when there is none."""
    found_index = False
    for path in list(dists.rglob("Packages.gz")) + list(dists.rglob("Packages")):
        if not path.is_file():
            continue
        found_index = True
        try:
            opener = gzip.open if path.suffix == ".gz" else open
            with opener(path, "rt", encoding="utf-8", errors="replace") as fh:  # type: ignore[operator]
                if re.search(r"^Package: %s$" % re.escape(package), fh.read(), re.M):
                    return True
        except OSError:
            continue
    return False if found_index else None


def check(iso_dir: Path, squashfs_root: Path, fallback_dir: Optional[Path] = None,
          copy_fallback_to: Optional[Path] = None) -> Dict[str, object]:
    """Inspect the tree; returns {"ok": bool, "via": "pool"|"fallback"|"", "problems": [...], "version": str}."""
    problems: List[str] = []
    version = installed_version(squashfs_root / "var" / "lib" / "dpkg" / "status")
    if not version:
        problems.append("ubiquity is not installed in the squashfs (no var/lib/dpkg/status entry)")
        return {"ok": False, "via": "", "problems": problems, "version": ""}
    want = strip_epoch(version)
    pool_problems: List[str] = []
    status_file = squashfs_root / "var" / "lib" / "dpkg" / "status"
    missing_deps = [dep for dep in NEEDED_DEPENDS
                    if installed_version(status_file, dep) is None and not debs_in(iso_dir / "pool", dep)]
    for dep in missing_deps:
        pool_problems.append("%s is neither installed in the squashfs nor in pool/ (oem-config-gtk depends on it)" % dep)
    for name in NEEDED:
        have = debs_in(iso_dir / "pool", name)
        if want not in have:
            pool_problems.append("pool/ has no %s_%s (found: %s)" % (name, want, ", ".join(sorted(have)) or "none"))
    for meta in ("info", "cd_type"):
        f = iso_dir / ".disk" / meta
        if not f.is_file() or not f.read_text(encoding="utf-8", errors="replace").strip():
            pool_problems.append(".disk/%s is missing or empty (apt-setup's cdrom source needs it)" % meta)
    dists = iso_dir / "dists"
    if not dists.is_dir():
        pool_problems.append("dists/ is missing (apt-cdrom needs the medium's package indexes)")
    elif index_mentions(dists, "oem-config-gtk") is False:
        pool_problems.append("no Packages index under dists/ lists oem-config-gtk")
    if not pool_problems:
        return {"ok": True, "via": "pool", "problems": [], "version": want}

    problems.extend(pool_problems)
    if fallback_dir and fallback_dir.is_dir():
        missing = [n for n in NEEDED if want not in debs_in(fallback_dir, n)]
        missing += [dep for dep in missing_deps if not debs_in(fallback_dir, dep)]
        if not missing:
            if copy_fallback_to is not None:
                copy_fallback_to.mkdir(parents=True, exist_ok=True)
                for deb in sorted(fallback_dir.glob("*.deb")):
                    shutil.copy2(deb, copy_fallback_to / deb.name)
            return {"ok": True, "via": "fallback", "problems": problems, "version": want}
        problems.append("fallback %s lacks matching %s" % (fallback_dir, ", ".join(missing)))
    return {"ok": False, "via": "", "problems": problems, "version": want}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--iso-dir", required=True, type=Path)
    ap.add_argument("--squashfs-root", required=True, type=Path)
    ap.add_argument("--fallback-dir", type=Path, default=None, help="directory with matching oem-config debs (+ closure)")
    ap.add_argument("--copy-fallback-to", type=Path, default=None, help="where the fallback debs go on the medium")
    ap.add_argument("--require", action="store_true", help="exit 1 unless the pool or the fallback is fine")
    ns = ap.parse_args(argv)
    fallback = ns.fallback_dir if ns.fallback_dir and str(ns.fallback_dir) != "." else None
    result = check(ns.iso_dir, ns.squashfs_root, fallback, ns.copy_fallback_to)
    if result["ok"]:
        print("oem-config check: ok via %s (ubiquity %s)%s" % (
            result["via"], result["version"], "; pool problems: " + "; ".join(result["problems"]) if result["problems"] else ""))
        return 0
    print("oem-config check: NOT ok - %s" % "; ".join(result["problems"]))  # type: ignore[arg-type]
    return 1 if ns.require else 0


if __name__ == "__main__":
    sys.exit(main())
