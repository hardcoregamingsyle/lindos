"""Page implementations.  :func:`build_page` maps a registry entry to its GTK page class.

Delegating pages (System, Apps, Network, Accounts, Update & Recovery) share
:class:`DelegatePage`: one activatable card per sub-item that launches the existing tool, or
offers *Install* (helper ``install-packages``) when the tool is missing.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Optional

from .. import model
from ..widgets import ButtonCard, HAVE_GTK, PageBase, confirm, run_async

log = logging.getLogger("lindos.settings.pages")

# page id → (module, class)
PAGE_CLASSES: dict[str, tuple[str, str]] = {
    "home": ("home", "HomePage"),
    "system": ("system", "SystemPage"),
    "personalization": ("personalization", "PersonalizationPage"),
    "apps": ("apps", "AppsPage"),
    "windows-apps": ("windows_apps", "WindowsAppsPage"),
    "gaming": ("gaming", "GamingPage"),
    "hardware": ("hardware", "HardwarePage"),
    "network": ("network", "NetworkPage"),
    "accounts": ("accounts", "AccountsPage"),
    "mode": ("mode", "ModePage"),
    "update": ("update", "UpdatePage"),
    "about": ("about", "AboutPage"),
}


class DelegatePage(PageBase):
    """Cards that launch existing tools (keeps RAM low: nothing embedded)."""

    def build(self) -> None:
        self.build_extra_top()
        cards = self.add_section("")
        self._item_cards: list[tuple[model.SubItem, ButtonCard]] = []
        for item in self.page.subitems:
            card = ButtonCard(item.label, item.description, item.icon or self.page.icon, item.keywords, button_label="Open")
            card.on_activate(lambda it=item, c=card: self.activate_item(it, c))
            card.button.connect("clicked", lambda _b, it=item, c=card: self.activate_item(it, c))
            cards.add(card)
            self._item_cards.append((item, card))
        self.build_extra_bottom()
        self.refresh_labels()

    def build_extra_top(self) -> None:  # optional hook
        pass

    def build_extra_bottom(self) -> None:  # optional hook
        pass

    def on_show(self) -> None:
        self.refresh_labels()

    def refresh_labels(self) -> None:
        for item, card in self._item_cards:
            kind, _ = model.resolve_subitem(item, self.backend.which)
            card.set_button_label("Open" if kind == "run" else "Install")
            if kind != "run":
                card.button.set_tooltip_text(f"Not installed — installs package '{_ or item.package}'")
            else:
                card.button.set_tooltip_text("")

    def activate_item(self, item: model.SubItem, card: ButtonCard) -> None:
        kind, value = model.resolve_subitem(item, self.backend.which)
        if kind == "run":
            if not self.backend.spawn(value):
                self.toast(f"Could not start {value[0]}")
            else:
                self.toast(f"Opening {item.label}…")
            return
        pkg = str(value)
        if not confirm(self.app.window, f"Install {pkg}?", f"'{item.label}' needs the package '{pkg}', which is not installed. Install it now? (administrator password required)", "Install"):
            return
        card.button.set_sensitive(False)
        self.toast(f"Installing {pkg}…")

        def _done(res: Any, exc: Optional[BaseException]) -> None:
            card.button.set_sensitive(True)
            if exc or res is None or not res.ok:
                err = (getattr(res, "err", "") or getattr(res, "out", "") or str(exc or "")).strip()
                self.toast(f"Install failed: {err[:160] or 'see log'}")
            else:
                self.toast(f"{pkg} installed")
            self.refresh_labels()

        run_async(lambda: self.backend.install_packages([pkg]), _done, name="install-" + pkg)


def build_page(app: Any, page: model.Page) -> PageBase:
    """Instantiate the page class for a registry entry (delegate pages fall back to
    :class:`DelegatePage`, unknown native ids get a generic delegate page)."""
    entry = PAGE_CLASSES.get(page.id)
    if entry is not None:
        modname, clsname = entry
        try:
            mod = importlib.import_module(f"{__name__}.{modname}")
            cls = getattr(mod, clsname)
            return cls(app, page)
        except Exception:
            log.exception("page %s failed to build; using delegate fallback", page.id)
    return DelegatePage(app, page)


__all__ = ["DelegatePage", "build_page", "PAGE_CLASSES", "HAVE_GTK"]
