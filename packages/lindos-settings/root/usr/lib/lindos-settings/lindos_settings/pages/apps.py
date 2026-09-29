"""Apps page (delegate): Store, Installed apps, Startup, Default apps — plus the web-browser
cards (Edge / Chrome / Firefox: install through the helper action ``install-browser`` and set the
default) so an offline first boot can be finished here (SPEC §6: "finish later in
`lindos-settings apps`"), and shortcuts to Windows apps / Gaming launchers."""

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
