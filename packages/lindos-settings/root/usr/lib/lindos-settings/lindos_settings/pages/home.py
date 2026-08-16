"""Home page: mode card, RAM meter, quick toggles (Dark mode, Game Mode, MangoHud, Compositor),
taskbar alignment, shortcuts."""

from __future__ import annotations

from typing import Any, Optional

from .. import model
from ..widgets import HAVE_GTK, ButtonCard, Card, ComboCard, PageBase, SwitchCard, add_class, add_timeout_seconds, box, label, remove_source, run_async

if HAVE_GTK:  # pragma: no cover
    from gi.repository import Gtk  # type: ignore
else:  # pragma: no cover
    Gtk = None  # type: ignore

RAM_REFRESH_SECONDS = 5


class HomePage(PageBase):
    PAGE_ID = "home"

    def build(self) -> None:
        b = self.backend
        self._timer: Optional[int] = None

        # -- mode card
        top = self.add_section("")
        self.mode_card = Card("Lindos Mode", "", (model.MODE_ICONS["everyday"],), ("mode", "everyday", "gaming", "work", "creator", "lite"), icon_size=28)
        self.mode_card.set_control(self._button("Change", lambda: self.app.show_page("mode")))
        top.add(self.mode_card)

        # -- RAM meter
        self.ram_card = Card("Memory", "", ("utilities-system-monitor",), ("ram", "memory", "used", "free", "target"))
        self.level = Gtk.LevelBar()
        self.level.set_min_value(0.0)
        self.level.set_max_value(1.0)
        self.level.set_hexpand(True)
        add_class(self.level, "ram-level")
        self.level.add_offset_value("lindos-good", 0.35)
        self.level.add_offset_value("lindos-warn", 0.7)
        self.level.add_offset_value("lindos-high", 1.0)
        self.ram_text = label("", ("card-subtitle",))
        self.ram_top = label("", ("dim-label",), wrap=True)
        vb = box("v", 4)
        vb.pack_start(self.level, False, False, 0)
        vb.pack_start(self.ram_text, False, False, 0)
        vb.pack_start(self.ram_top, False, False, 0)
        self.ram_card.add_body(vb)
        self.ram_card.set_control(self._button("Details", self._ram_details))
        top.add(self.ram_card)

        # -- quick toggles
        toggles = self.add_section("Quick settings")
        self.toggle_cards: dict[str, SwitchCard] = {}
        for qt in model.build_quick_toggles(b):
            card = SwitchCard(qt.title, qt.subtitle, (qt.icon,), qt.keywords, on_toggle=lambda v, q=qt: self._toggle(q, v))
            self.toggle_cards[qt.id] = card
            toggles.add(card)
        self.taskbar_card = ComboCard(
            "Taskbar alignment",
            "Centre the Start button and pinned apps (Windows 11) or align left (Windows 10)",
            ("view-list-details",),
            ("taskbar", "panel", "center", "left", "start"),
            options=[("center", "Center"), ("left", "Left")],
            on_change=self._taskbar_alignment,
            active_id=b.taskbar_alignment(),
        )
        toggles.add(self.taskbar_card)

        # -- shortcuts
        short = self.add_section("Recommended")
        short.add(ButtonCard("Windows apps", "Install a Windows program (.exe/.msi) or manage installed ones", ("wine", "application-x-executable"), ("exe", "msi"), "Open", lambda: self.app.show_page("windows-apps")))
        short.add(ButtonCard("Gaming", "Proton-GE, launchers, controllers, refresh rate", ("applications-games",), ("steam", "proton"), "Open", lambda: self.app.show_page("gaming")))
        short.add(ButtonCard("Update & Recovery", "Updates, drivers and Timeshift snapshots", ("system-software-update",), ("update", "driver", "timeshift"), "Open", lambda: self.app.show_page("update")))
        short.add(ButtonCard("Personalization", "Theme, accent colour, wallpaper, taskbar", ("preferences-desktop-wallpaper",), ("theme", "wallpaper"), "Open", lambda: self.app.show_page("personalization")))
        self.refresh()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _button(text: str, cb: Any) -> Any:
        from ..widgets import button

        return button(text, on_click=cb)

    def _toggle(self, qt: model.QuickToggle, value: bool) -> None:
        card = self.toggle_cards[qt.id]

        def _work() -> tuple[bool, bool]:
            # set + re-read in the worker: both may call xfconf-query / lindos-compositor
            ok = qt.set(value)
            return ok, qt.get()

        def _done(res: Any, exc: Optional[BaseException]) -> None:
            ok, actual = (res if isinstance(res, tuple) else (False, qt.get()))
            card.set_active_silent(actual)
            if exc or not ok:
                self.toast(f"Could not change {qt.title}")
            else:
                self.toast(f"{qt.title} {'on' if actual else 'off'}")
            if qt.id == "dark":
                self.app.on_theme_changed()

        run_async(_work, _done, name="toggle-" + qt.id)

    def _taskbar_alignment(self, value: str) -> None:
        def _done(ok: Any, exc: Optional[BaseException]) -> None:
            self.toast("Taskbar alignment updated" if ok and not exc else "Taskbar alignment could not be changed (lindos-core missing?)")

        run_async(lambda: self.backend.set_taskbar_alignment(value), _done, name="taskbar-align")

    def _ram_details(self) -> None:
        if self.backend.which("lindos-tune"):
            self.app.show_output("Memory report (lindos-tune status)", ["lindos-tune", "status"])
        elif self.backend.which("lindos-ram"):
            self.app.show_output("Memory report (lindos-ram)", ["lindos-ram"])
        else:
            self.app.show_output("Memory report (free -m)", ["free", "-m"])

    # ------------------------------------------------------------------ refresh
    def refresh(self) -> None:
        b = self.backend
        modes = self.app.modes
        mid = b.effective_mode()
        m = modes.get(mid) if modes else None
        md = b.mode_as_dict(m) if m is not None else {}
        self.mode_card.set_title(f"{model.mode_display_name(mid, modes)} mode")
        self.mode_card.set_subtitle(str(md.get("description") or model.MODE_DESCRIPTIONS.get(mid, "")))
        self.taskbar_card.set_active_id_silent(b.taskbar_alignment())
        toggles = model.build_quick_toggles(b)

        def _work() -> dict[str, bool]:
            # is_dark / compositor status shell out (xfconf-query, lindos-compositor): keep off the UI thread
            return {qt.id: qt.get() for qt in toggles}

        def _done(states: Any, exc: Optional[BaseException]) -> None:
            if not isinstance(states, dict):
                return
            for tid, value in states.items():
                card = self.toggle_cards.get(tid)
                if card is not None:
                    card.set_active_silent(bool(value))

        run_async(_work, _done, name="toggle-states")
        self._update_ram()

    def _update_ram(self) -> bool:
        def _work() -> dict[str, Any]:
            return self.backend.ram_snapshot()

        def _done(snap: Any, exc: Optional[BaseException]) -> None:
            if exc or not isinstance(snap, dict):
                self.ram_text.set_text("Memory information unavailable")
                return
            used, total = snap.get("used", 0), snap.get("total", 0)
            self.level.set_value(model.ram_fraction(used, total))
            self.ram_text.set_text(model.ram_summary(used, total) + f" ({model.ram_verdict(used)})")
            top = snap.get("top") or []
            if top:
                self.ram_top.set_text("Top: " + ", ".join(f"{n} {int(r)} MB" for n, r in top[:5]))
            else:
                self.ram_top.set_text("Idle target is measured right after login with no apps open (lindos-tune status).")

        run_async(_work, _done, name="ram-snapshot")
        return True

    def on_show(self) -> None:
        self.refresh()
        remove_source(self._timer)
        self._timer = add_timeout_seconds(RAM_REFRESH_SECONDS, self._update_ram)

    def on_hide(self) -> None:
        remove_source(self._timer)
        self._timer = None


__all__ = ["HomePage"]
