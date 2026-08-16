"""System page (delegate): Display, Sound, Notifications, Power, Storage, Default apps,
Bluetooth, Printers — each launches the existing XFCE / Mint tool."""

from __future__ import annotations

from typing import Any

from .. import model
from ..widgets import Card, label
from . import DelegatePage


class SystemPage(DelegatePage):
    PAGE_ID = "system"

    def build_extra_top(self) -> None:
        # Small live status strip: hostname · uptime · disk free (cheap, no daemons)
        cards = self.add_section("")
        self.status_card = Card("This PC", "", ("computer",), ("hostname", "uptime", "disk"))
        cards.add(self.status_card)
        self._update_status()

    def _update_status(self) -> None:
        b = self.backend
        parts = [b.hostname(), "up " + model.format_uptime(b.uptime_seconds())]
        du = b.disk_usage("/")
        if du:
            parts.append(f"{model.format_bytes(du[2])} free of {model.format_bytes(du[0])} on /")
        self.status_card.set_subtitle(" · ".join(parts))

    def on_show(self) -> None:
        super().on_show()
        self._update_status()

    def build_extra_bottom(self) -> None:
        note = label("Display, sound and power settings open the standard XFCE dialogs so Lindos stays light on memory.", ("dim-label",), wrap=True)
        self.add_widget(note, 12)


__all__: list[Any] = ["SystemPage"]
