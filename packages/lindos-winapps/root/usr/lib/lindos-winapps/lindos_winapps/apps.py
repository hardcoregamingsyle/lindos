"""The Windows-app catalog and ``.desktop`` generation (SPEC-VM §22).

``apps.json`` ships a catalog of well-known Windows programs (Photoshop, Office, ...) with the
path each one is normally installed to inside Windows.  ``install <id>`` turns a catalog entry
into ``~/.local/share/applications/lindos-winapp-<id>.desktop`` so the Windows program appears
seamlessly in the Lindos menu; ``run <id>`` (see :mod:`rdp`) launches it over RDP RemoteApp.

Everything here is pure/stdlib and importable on any OS.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import applications_dir, catalog_path, desktop_path, get_logger

__all__ = [
    "App",
    "CatalogError",
    "load_catalog",
    "get_app",
    "installed_ids",
    "is_installed",
    "desktop_content",
    "install_app",
    "remove_app",
    "app_rows",
]

log = get_logger("lindos-winapps.apps")

#: Note attached to every catalog entry and to each generated launcher (honesty, SPEC-VM §20).
LICENSE_NOTE = (
    "Needs a user-supplied, licensed Windows with this app installed in the backend. "
    "Lindos does not download Windows or bypass licensing."
)


class CatalogError(RuntimeError):
    """Raised when the app catalog cannot be read or is malformed."""


@dataclass
class App:
    """One catalog entry (SPEC-VM §22: ``id,name,rdp_path,categories,icon``)."""

    id: str
    name: str
    rdp_path: str
    categories: List[str] = field(default_factory=list)
    icon: str = "lindos-winapp"
    note: str = LICENSE_NOTE

    def as_dict(self) -> Dict[str, object]:
        return asdict(self)


def load_catalog(path: Optional[Path] = None) -> Dict[str, App]:
    """Load and validate ``apps.json`` into ``{id: App}``.

    Raises :class:`CatalogError` on a missing/invalid file so the CLI can report it cleanly.
    """
    src = Path(path) if path is not None else catalog_path()
    try:
        raw = json.loads(src.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CatalogError(f"app catalog not found: {src}") from exc
    except (OSError, ValueError) as exc:
        raise CatalogError(f"app catalog is invalid ({src}): {exc}") from exc

    entries = raw.get("apps", raw) if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        raise CatalogError(f"app catalog {src}: expected a list of apps")

    catalog: Dict[str, App] = {}
    for item in entries:
        if not isinstance(item, dict):
            continue
        app_id = str(item.get("id", "")).strip()
        name = str(item.get("name", "")).strip()
        rdp_path = str(item.get("rdp_path", "")).strip()
        if not app_id or not name or not rdp_path:
            log.warning("skipping malformed catalog entry: %r", item)
            continue
        cats = item.get("categories") or []
        if isinstance(cats, str):
            cats = [c for c in cats.split(";") if c]
        catalog[app_id] = App(
            id=app_id,
            name=name,
            rdp_path=rdp_path,
            categories=[str(c) for c in cats],
            icon=str(item.get("icon") or "lindos-winapp"),
            note=str(item.get("note") or LICENSE_NOTE),
        )
    if not catalog:
        raise CatalogError(f"app catalog {src}: no usable entries")
    return catalog


def get_app(app_id: str, catalog: Optional[Dict[str, App]] = None) -> App:
    """Return the :class:`App` for ``app_id`` (loading the catalog if not supplied)."""
    cat = catalog if catalog is not None else load_catalog()
    try:
        return cat[app_id]
    except KeyError as exc:
        known = ", ".join(sorted(cat)) or "(none)"
        raise CatalogError(f"unknown app id '{app_id}'. Known ids: {known}") from exc


def installed_ids(apps_dir: Optional[Path] = None) -> List[str]:
    """Catalog ids that currently have a generated launcher, sorted."""
    directory = Path(apps_dir) if apps_dir is not None else applications_dir()
    if not directory.is_dir():
        return []
    ids: List[str] = []
    for entry in directory.glob("lindos-winapp-*.desktop"):
        stem = entry.name[len("lindos-winapp-"):-len(".desktop")]
        if stem:
            ids.append(stem)
    return sorted(ids)


def is_installed(app_id: str) -> bool:
    """True when a launcher exists for ``app_id``."""
    return desktop_path(app_id).is_file()


def desktop_content(app: App, *, icon: Optional[str] = None) -> str:
    """Return the ``.desktop`` text for a catalog entry (Exec=``lindos-winapps run <id>``)."""
    categories = ";".join([c for c in app.categories if c] + ["X-Lindos", "X-WinApps"])
    icon_name = icon or app.icon or "lindos-winapp"
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Version=1.0",
        f"Name={app.name}",
        "GenericName=Windows program",
        f"Comment={app.name} on Windows, over RDP - {app.note}",
        f"Exec=lindos-winapps run {app.id}",
        "TryExec=lindos-winapps",
        f"Icon={icon_name}",
        "Terminal=false",
        "StartupNotify=true",
        f"StartupWMClass={app.id}",
        f"Categories={categories};",
        "Keywords=Windows;RDP;RemoteApp;WinApps;",
        "X-Lindos-Component=winapps",
        f"X-Lindos-WinApp-Id={app.id}",
        "",
    ]
    return "\n".join(lines)


def install_app(app: App, *, icon: Optional[str] = None) -> Path:
    """Write the launcher for ``app`` and return its path.  Idempotent (overwrites)."""
    target = desktop_path(app.id)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".desktop.tmp")
    tmp.write_text(desktop_content(app, icon=icon), encoding="utf-8")
    tmp.replace(target)
    try:
        target.chmod(0o755)
    except OSError:  # pragma: no cover - non-POSIX filesystems
        pass
    log.info("installed launcher: %s", target)
    return target


def remove_app(app_id: str) -> bool:
    """Delete the launcher for ``app_id``.  Returns True if a file was removed."""
    target = desktop_path(app_id)
    if target.is_file():
        target.unlink()
        log.info("removed launcher: %s", target)
        return True
    return False


def app_rows(catalog: Dict[str, App]) -> List[Dict[str, object]]:
    """A JSON-able list describing every catalog entry and whether it is installed."""
    rows: List[Dict[str, object]] = []
    for app_id in sorted(catalog):
        app = catalog[app_id]
        rows.append({
            "id": app.id,
            "name": app.name,
            "rdp_path": app.rdp_path,
            "categories": app.categories,
            "icon": app.icon,
            "installed": is_installed(app_id),
            "note": app.note,
        })
    return rows
