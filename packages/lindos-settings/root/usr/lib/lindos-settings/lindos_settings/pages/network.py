"""Network page (delegate): NetworkManager connection editor, VPN, Firewall (gufw) — plus a
read-only card with the active connections (``nmcli``)."""

from __future__ import annotations

from typing import Any, Optional

from ..widgets import Card, button, run_async
from . import DelegatePage


class NetworkPage(DelegatePage):
    PAGE_ID = "network"

    def build_extra_top(self) -> None:
        cards = self.add_section("")
        self.status_card = Card("Active connections", "Checking…", ("network-transmit-receive",), ("wifi", "ethernet", "connected", "ip"))
        self.status_card.set_control(button("Refresh", on_click=self.refresh))
        cards.add(self.status_card)

    def on_show(self) -> None:
        super().on_show()
        self.refresh()

    def refresh(self) -> None:
        b = self.backend
        if not b.which("nmcli"):
            self.status_card.set_subtitle("NetworkManager (nmcli) not available")
            return

        def _work() -> str:
            r = b.run(["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"], timeout=8)
            if not r.ok:
                return "Could not query NetworkManager"
            rows = []
            for line in r.out.splitlines():
                parts = line.split(":")
                if len(parts) >= 3 and parts[0]:
                    ctype = parts[1].replace("802-11-wireless", "Wi-Fi").replace("802-3-ethernet", "Ethernet")
                    rows.append(f"{parts[0]} ({ctype}, {parts[2]})")
            return "; ".join(rows) if rows else "No active connection"

        def _done(text: Any, exc: Optional[BaseException]) -> None:
            self.status_card.set_subtitle(str(text) if not exc else f"Error: {exc}")

        run_async(_work, _done, name="nmcli")


__all__: list[Any] = ["NetworkPage"]
