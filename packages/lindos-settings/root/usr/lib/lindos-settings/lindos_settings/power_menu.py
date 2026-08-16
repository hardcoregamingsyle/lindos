"""Win+X style power menu (``lindos-settings --power-menu``).

An undecorated popup window at the bottom-left of the work area (above the panel; top-left when
the taskbar is at the top) listing Sleep / Restart / Shut down / Sign out / Lock / Settings /
File Explorer / Terminal / Task Manager.  Escape, focus loss or a click outside closes it.
Items come from :data:`model.POWER_MENU_ITEMS`; every command is spawned detached.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from . import model
from .backend import get_backend
from .widgets import HAVE_GTK, add_class, box, icon_image, label, load_css, pick_icon

if HAVE_GTK:  # pragma: no cover
    from gi.repository import Gdk, GLib, Gtk  # type: ignore
else:  # pragma: no cover
    Gdk = GLib = Gtk = None  # type: ignore

log = logging.getLogger("lindos.settings.power_menu")

MENU_WIDTH = 300
MARGIN = 8
CSS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui", "settings.css")


class PowerMenu:
    def __init__(self, backend: Any = None):
        if not HAVE_GTK:
            raise RuntimeError("GTK 3 is required for the power menu")
        self.backend = backend or get_backend()
        self.window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        w = self.window
        w.set_title("Lindos power menu")
        w.set_decorated(False)
        w.set_resizable(False)
        w.set_skip_taskbar_hint(True)
        w.set_skip_pager_hint(True)
        w.set_keep_above(True)
        w.set_type_hint(Gdk.WindowTypeHint.POPUP_MENU)
        w.set_default_size(MENU_WIDTH, -1)
        w.set_size_request(MENU_WIDTH, -1)
        add_class(w, "power-menu")
        # RGBA visual so rounded corners look right under a compositor
        screen = w.get_screen()
        visual = screen.get_rgba_visual() if screen else None
        if visual is not None and screen.is_composited():
            w.set_visual(visual)
            w.set_app_paintable(True)

        frame = Gtk.Frame()
        add_class(frame, "power-menu-frame")
        w.add(frame)
        vb = box("v", 0, ("power-menu-list",))
        vb.set_margin_top(6)
        vb.set_margin_bottom(6)
        vb.set_margin_start(6)
        vb.set_margin_end(6)
        frame.add(vb)

        head = box("h", 10, ("power-menu-head",))
        head.set_margin_top(6)
        head.set_margin_bottom(6)
        head.set_margin_start(8)
        head.set_margin_end(8)
        head.pack_start(icon_image(("lindos-start", "preferences-system"), 20), False, False, 0)
        head.pack_start(label(self.backend.user_display_name(), ("power-menu-user",), ellipsize=True), True, True, 0)
        vb.pack_start(head, False, False, 0)
        vb.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 2)

        self.buttons: list[Any] = []
        for item in model.power_menu_items():
            btn = self._item_button(item)
            self.buttons.append(btn)
            vb.pack_start(btn, False, False, 0)
            if item.separator_after:
                vb.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)

        w.connect("key-press-event", self._on_key)
        w.connect("focus-out-event", self._on_focus_out)
        w.connect("button-press-event", self._on_button_press)
        w.connect("delete-event", lambda *_: self.close() or True)
        w.connect("destroy", lambda *_: Gtk.main_quit())
        w.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.FOCUS_CHANGE_MASK | Gdk.EventMask.KEY_PRESS_MASK)
        self._closed = False

    # ------------------------------------------------------------------ building
    def _item_button(self, item: model.PowerItem) -> Any:
        btn = Gtk.Button()
        btn.set_relief(Gtk.ReliefStyle.NONE)
        add_class(btn, "power-menu-item", "flat")
        hb = box("h", 12)
        hb.set_margin_top(4)
        hb.set_margin_bottom(4)
        hb.set_margin_start(6)
        hb.set_margin_end(6)
        hb.pack_start(icon_image((item.icon, item.icon + "-symbolic"), 18), False, False, 0)
        hb.pack_start(label(item.label, ("power-menu-label",)), True, True, 0)
        available = bool(self.backend.which(item.argv[0]))
        if not available:
            hint = label("not installed", ("dim-label",))
            hb.pack_end(hint, False, False, 0)
            btn.set_sensitive(False)
            btn.set_tooltip_text(f"{item.argv[0]} is not installed")
        btn.add(hb)
        btn.connect("clicked", lambda _b, it=item: self._activate(it))
        return btn

    # ------------------------------------------------------------------ behaviour
    def _activate(self, item: model.PowerItem) -> None:
        argv = list(item.argv)
        ok = self.backend.spawn(argv)
        if not ok:
            log.error("could not start %s", argv)
        self.close()

    def _on_key(self, _w: Any, event: Any) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self.close()
            return True
        # Windows-style accelerator letters
        letters = {
            Gdk.KEY_u: "sleep",  # "sleep" has no natural letter; U like Win+X → U (shut down or sign out)
            Gdk.KEY_r: "restart",
            Gdk.KEY_s: "shutdown",
            Gdk.KEY_o: "signout",
            Gdk.KEY_l: "lock",
            Gdk.KEY_n: "settings",
            Gdk.KEY_e: "files",
            Gdk.KEY_t: "terminal",
            Gdk.KEY_k: "taskmanager",
        }
        target = letters.get(event.keyval)
        if target:
            for item in model.power_menu_items():
                if item.id == target and self.backend.which(item.argv[0]):
                    self._activate(item)
                    return True
        return False

    def _on_focus_out(self, *_: Any) -> bool:
        # Give a click on one of our own buttons a chance to be delivered first.
        GLib.timeout_add(120, self._close_if_unfocused)
        return False

    def _close_if_unfocused(self) -> bool:
        if not self._closed and not self.window.is_active():
            self.close()
        return False

    def _on_button_press(self, _w: Any, event: Any) -> bool:
        alloc = self.window.get_allocation()
        if event.x < 0 or event.y < 0 or event.x > alloc.width or event.y > alloc.height:
            self.close()
            return True
        return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.window.destroy()
        except Exception:
            Gtk.main_quit()

    # ------------------------------------------------------------------ placement
    def _place(self) -> None:
        w = self.window
        w.show_all()
        _, nat = w.get_preferred_size()
        width, height = max(nat.width, MENU_WIDTH), nat.height
        x, y = MARGIN, MARGIN
        display = Gdk.Display.get_default()
        monitor = None
        if display is not None:
            monitor = display.get_primary_monitor() or (display.get_monitor(0) if display.get_n_monitors() > 0 else None)
        top = self.backend.taskbar_position() == "top"
        if monitor is not None:
            area = monitor.get_workarea()
            x = area.x + MARGIN
            y = (area.y + MARGIN) if top else (area.y + area.height - height - MARGIN)
        w.move(x, y)
        w.present()
        try:
            w.get_window().focus(Gdk.CURRENT_TIME)
        except Exception:
            pass

    def run(self) -> int:
        self._place()
        Gtk.main()
        return 0


def run_power_menu() -> int:
    if os.path.isfile(CSS_PATH):
        load_css(CSS_PATH)
    try:
        menu = PowerMenu()
    except Exception as exc:
        log.error("power menu failed: %s", exc)
        return 1
    Gtk.Window.set_default_icon_name(pick_icon(("lindos-start", "system-shutdown"), "system-shutdown"))
    return menu.run()


__all__ = ["PowerMenu", "run_power_menu"]
