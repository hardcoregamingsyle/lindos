"""Apps page (delegate): Store, Installed apps, Startup, Default apps — plus a "Left to finish from
setup" list (what the installer could not do while offline, from install-state.json, each with an
"Install now" button that uses the ordinary helper actions), the web-browser cards (Edge / Chrome /
Firefox: install through the helper action ``install-browser`` and set the default) and shortcuts
to Windows apps / Gaming launchers."""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..widgets import ButtonCard, OutputDialog, confirm, run_async
from . import DelegatePage

log = logging.getLogger("lindos.settings.apps")

BROWSER_ICONS: dict[str, tuple[str, ...]] = {
    "edge": ("microsoft-edge", "web-browser"),
    "chrome": ("google-chrome", "web-browser"),
    "firefox": ("firefox", "web-browser"),
}


class AppsPage(DelegatePage):
    PAGE_ID = "apps"

    # ------------------------------------------------------------------ left to finish from setup
    def build_extra_top(self) -> None:
        self._pending_cards = self.add_section("Left to finish from setup")
        self._pending_title = self._sections[-1][0]
        self._pending_items: list[dict[str, Any]] = []
        self._installed_now: set[str] = set()      # finished from this page in this session
        self._pending_busy = False
        self._set_pending_visible(False)

    def _set_pending_visible(self, visible: bool) -> None:
        if self._pending_title is not None:
            self._pending_title.set_no_show_all(not visible)
            self._pending_title.set_visible(visible)
        self._pending_cards.widget.set_no_show_all(not visible)
        self._pending_cards.widget.set_visible(visible)

    def _refresh_pending(self) -> None:
        def _done(rows: Any, exc: Optional[BaseException]) -> None:
            items = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) and not exc else []
            self._render_pending(items)

        run_async(self.backend.setup_pending_items, _done, name="setup-pending")

    def _render_pending(self, items: list[dict[str, Any]]) -> None:
        items = [it for it in items if it.get("id") not in self._installed_now]
        self._pending_items = items
        self._pending_cards.clear()
        for item in items:
            card = ButtonCard(
                str(item.get("title") or ""),
                str(item.get("subtitle") or ""),
                ("emblem-downloads", "system-software-install"),
                ("setup", "install", "pending", str(item.get("id") or "")),
                button_label=str(item.get("button") or "Install now"),
                on_click=lambda it=item: self._install_pending(it),
                activatable=False,
            )
            self._pending_cards.add(card)
        # reveal the list first: show_all() is a no-op on a widget that still has no_show_all set
        self._set_pending_visible(bool(items))
        self._pending_cards.show_all()

    def _install_pending(self, item: dict[str, Any]) -> None:
        if self._pending_busy:
            self.toast("Another install is still running")
            return
        title = str(item.get("title") or "this item")
        if not confirm(self.app.window, f"Install {title}?",
                       "It is downloaded from its official source, so this PC needs an internet connection. "
                       "The administrator password is asked once.", "Install now"):
            return
        self._pending_busy = True
        self.toast(f"Installing {title}…")

        def _done(res: Any, exc: Optional[BaseException]) -> None:
            self._pending_busy = False
            ok = res is not None and getattr(res, "ok", False) and not exc
            if ok:
                self._installed_now.add(str(item.get("id") or ""))
                self.toast(f"{title} installed")
            else:
                err = (getattr(res, "err", "") or getattr(res, "out", "") or str(exc or "")).strip()
                self.toast(f"Install failed: {err[:140] or 'see /var/log/lindos/helper.log'}")
            self._refresh_pending()
            self._refresh_browsers()

        run_async(lambda: self.backend.install_setup_item(item), _done, name="install-setup-" + str(item.get("id")))

    # ------------------------------------------------------------------ web browsers and shortcuts
    def build_extra_bottom(self) -> None:
        # -- web browsers (helper install-browser; Edge/Chrome are never on the ISO)
        self._browser_cards: dict[str, ButtonCard] = {}
        cards = self.add_section("Web browsers")
        for bid, name in (("edge", "Microsoft Edge"), ("chrome", "Google Chrome"), ("firefox", "Mozilla Firefox")):
            card = ButtonCard(
                name,
                "Checking…",
                BROWSER_ICONS.get(bid, ("web-browser",)),
                ("browser", "web", "internet", bid, name.lower()),
                button_label="Install",
                on_click=lambda b=bid: self._browser_action(b),
                activatable=False,
            )
            cards.add(card)
            self._browser_cards[bid] = card
        self._browser_state: dict[str, dict[str, Any]] = {}

        # -- shortcuts
        cards = self.add_section("Windows programs")
        cards.add(
            ButtonCard(
                "Windows apps",
                "Install and manage .exe/.msi programs (Wine / Proton — a translation layer, not Windows)",
                ("wine", "application-x-executable"),
                ("exe", "msi", "wine", "proton"),
                button_label="Open",
                on_click=lambda: self.app.show_page("windows-apps"),
            )
        )
        cards.add(
            ButtonCard(
                "Gaming launchers",
                "Steam, Lutris, Heroic, Prism Launcher, Roblox (Sober), Bottles",
                ("applications-games",),
                ("steam", "lutris", "heroic", "roblox", "minecraft"),
                button_label="Open",
                on_click=lambda: self.app.show_page("gaming"),
            )
        )

    def on_show(self) -> None:
        super().on_show()
        self._refresh_pending()
        self._refresh_browsers()

    # ------------------------------------------------------------------ browsers
    def _refresh_browsers(self) -> None:
        def _done(rows: Any, exc: Optional[BaseException]) -> None:
            if exc or not isinstance(rows, list):
                for card in self._browser_cards.values():
                    card.set_subtitle("Status unknown — try `lindos-browser list`")
                return
            for row in rows:
                bid = str(row.get("id", ""))
                card = self._browser_cards.get(bid)
                if card is None:
                    continue
                self._browser_state[bid] = row
                installed = bool(row.get("installed"))
                is_default = bool(row.get("default"))
                note = str(row.get("note") or "")
                if installed:
                    sub = "Installed" + (" · default browser" if is_default else "")
                    card.set_button_label("Default" if is_default else "Make default")
                    card.button.set_sensitive(not is_default)
                    card.button.set_tooltip_text("" if is_default else "xdg-settings set default-web-browser")
                else:
                    sub = "Not installed — " + (note or "installed on demand")
                    card.set_button_label("Install")
                    card.button.set_sensitive(True)
                    card.button.set_tooltip_text("Adds the vendor's apt repository and installs the package through the Lindos helper (administrator password)"
                                                 if bid != "firefox" else "Installs the firefox package from the system repositories through the Lindos helper")
                card.set_subtitle(sub)

        run_async(self.backend.browsers, _done, name="browsers")

    def _browser_action(self, bid: str) -> None:
        state = self._browser_state.get(bid, {})
        card = self._browser_cards[bid]
        if state.get("installed"):
            ok = self.backend.set_default_browser(bid)
            self.toast(f"{card.title} is now the default browser" if ok else f"Could not set {card.title} as default (xdg-settings)")
            self._refresh_browsers()
            return
        vendor = "Mozilla" if bid == "firefox" else ("Microsoft" if bid == "edge" else "Google")
        if not confirm(self.app.window, f"Install {card.title}?",
                       f"{card.title} is downloaded from {vendor}'s official apt repository (it is not on the Lindos ISO). "
                       "Needs an internet connection and the administrator password. It becomes the default browser.", "Install"):
            return
        card.button.set_sensitive(False)
        card.set_subtitle("Installing… (adding the repository and downloading the package)")

        def _done(res: Any, exc: Optional[BaseException]) -> None:
            card.button.set_sensitive(True)
            ok = res is not None and getattr(res, "ok", False) and not exc
            if ok:
                self.toast(f"{card.title} installed and set as default")
            else:
                err = (getattr(res, "err", "") or getattr(res, "out", "") or str(exc or "")).strip()
                self.toast(f"Install failed: {err[:140] or 'see /var/log/lindos/helper.log'}")
                if err:
                    dlg = OutputDialog(self.app.window, f"{card.title} — install output", self.backend)
                    dlg.set_text((getattr(res, "out", "") or "") + "\n" + (getattr(res, "err", "") or ""))
                    dlg.set_status("Failed", False)
            self._refresh_browsers()

        run_async(lambda: self.backend.install_browser(bid, set_default=True), _done, name=f"install-browser-{bid}")


__all__: list[Any] = ["AppsPage"]
