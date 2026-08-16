"""Update & Recovery page (delegate): Update Manager, Driver Manager, Timeshift snapshots,
Recovery (boot-repair guide), Kernels."""

from __future__ import annotations

from typing import Any, Optional

from ..widgets import Card, button, run_async
from . import DelegatePage


class UpdatePage(DelegatePage):
    PAGE_ID = "update"

    def build_extra_top(self) -> None:
        cards = self.add_section("")
        self.status_card = Card("Pending updates", "Checking…", ("software-update-available",), ("pending", "upgradable", "apt"))
        self.status_card.set_control(button("Check", on_click=self.refresh))
        cards.add(self.status_card)

    def on_show(self) -> None:
        super().on_show()
        self.refresh()

    def refresh(self) -> None:
        b = self.backend
        if not b.which("apt"):
            self.status_card.set_subtitle("apt not available")
            return
        self.status_card.set_subtitle("Checking…")

        def _work() -> str:
            # `apt list --upgradable` reads the local cache only (no root, no network)
            r = b.run(["apt", "list", "--upgradable"], timeout=30, env={"LC_ALL": "C.UTF-8"})
            if not r.ok:
                return "Could not read the package cache"
            names = [ln.split("/")[0] for ln in r.out.splitlines() if "/" in ln and "upgradable" in ln]
            if not names:
                return "System is up to date (per local package cache — Update Manager refreshes it)"
            head = ", ".join(names[:6]) + (" …" if len(names) > 6 else "")
            return f"{len(names)} package(s) can be upgraded: {head}"

        def _done(text: Any, exc: Optional[BaseException]) -> None:
            self.status_card.set_subtitle(str(text) if not exc else f"Error: {exc}")

        run_async(_work, _done, name="apt-upgradable")


__all__: list[Any] = ["UpdatePage"]
