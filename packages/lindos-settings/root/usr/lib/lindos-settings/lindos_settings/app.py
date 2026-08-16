"""Gtk.Application + main window for Lindos Settings.

Layout (SPEC §7): 1100×720 window (min 800×560), a Gtk.HeaderBar *inside* the window (CSD off —
xfwm4 draws the frame) with the title and the search box, a 260 px sidebar on the left
(avatar, user, "Lindos Mode: …", page rows in Win11 order) and a Gtk.Stack of pages on the
right.  Pages are built lazily on first visit to keep start-up fast and RAM low.

A second ``lindos-settings <page>`` invocation while the window is open is forwarded to the
running instance through the ``open`` GAction (see :func:`main.main`).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from . import model
from .backend import get_backend
from .sidebar import Sidebar
from .widgets import HAVE_GTK, OutputDialog, Toast, add_class, box, load_css, pick_icon

if HAVE_GTK:  # pragma: no cover - needs GTK
    from gi.repository import Gdk, Gio, GLib, Gtk  # type: ignore
else:  # pragma: no cover
    Gdk = Gio = GLib = Gtk = None  # type: ignore

log = logging.getLogger("lindos.settings.app")

APP_ID = "org.lindos.Settings"
WINDOW_TITLE = "Settings"
DEFAULT_SIZE = (1100, 720)
MIN_SIZE = (800, 560)

LIB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS_PATH = os.path.join(LIB_DIR, "ui", "settings.css")


def _base_application() -> Any:
    """Gtk.Application when GTK is present; a plain object otherwise (import safety)."""
    return Gtk.Application if HAVE_GTK else object


class SettingsApplication(_base_application()):  # type: ignore[misc]
    """The application.  ``initial_page`` is shown at start-up (default: home)."""

    def __init__(self, initial_page: Optional[str] = None, application_id: str = APP_ID) -> None:
        if not HAVE_GTK:
            raise RuntimeError("GTK 3 (python3-gi, gir1.2-gtk-3.0) is required for lindos-settings")
        super().__init__(application_id=application_id, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.initial_page = initial_page or "home"
        self.backend = get_backend()
        self.window: Any = None
        self.sidebar: Any = None
        self.stack: Any = None
        self.search: Any = None
        self.header: Any = None
        self._toast: Any = None
        self.pages: list[model.Page] = []
        self.pages_by_id: dict[str, model.Page] = {}
        self.page_widgets: dict[str, Any] = {}
        self.modes: dict[str, Any] = {}
        self._current: Optional[str] = None
        self._query: str = ""

    # ------------------------------------------------------------------ GApplication hooks
    def do_startup(self) -> None:  # noqa: D401 - GObject vfunc
        Gtk.Application.do_startup(self)
        open_action = Gio.SimpleAction.new("open", GLib.VariantType.new("s"))
        open_action.connect("activate", self._on_open_action)
        self.add_action(open_action)
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Primary>q", "<Primary>w"])
        if os.path.isfile(CSS_PATH):
            load_css(CSS_PATH)
        else:
            log.warning("stylesheet %s missing — using plain GTK theme", CSS_PATH)
        Gtk.Window.set_default_icon_name(pick_icon(("lindos-settings", "preferences-system", "preferences-desktop"), "preferences-system"))

    def do_activate(self) -> None:  # noqa: D401 - GObject vfunc
        if self.window is None:
            self._build_window()
            self.show_page(self.initial_page)
        self.window.present()

    def _on_open_action(self, _action: Any, param: Any) -> None:
        page = str(param.get_string()) if param is not None else "home"
        self.activate()
        self.show_page(page if page in self.pages_by_id else "home")

    # ------------------------------------------------------------------ window
    def _build_window(self) -> None:
        self.pages = model.load_pages()
        self.pages_by_id = model.pages_by_id(self.pages)
        try:
            self.modes = self.backend.load_modes()
        except Exception as exc:  # never block start-up
            log.warning("load_modes failed: %s", exc)
            self.modes = {}

        win = Gtk.ApplicationWindow(application=self)
        win.set_title(WINDOW_TITLE)
        if hasattr(win, "set_wmclass"):
            try:
                win.set_wmclass("lindos-settings", "Lindos Settings")
            except Exception:  # deprecated in some bindings; harmless
                pass
        win.set_default_size(*DEFAULT_SIZE)
        win.set_size_request(*MIN_SIZE)
        win.set_position(Gtk.WindowPosition.CENTER)
        add_class(win, "lindos-settings")
        win.connect("key-press-event", self._on_key)
        win.connect("destroy", lambda *_: self.quit())
        self.window = win

        outer = box("v", 0)
        win.add(outer)

        # -- header bar (inside the window: server-side decorations stay with xfwm4)
        header = Gtk.HeaderBar()
        header.set_show_close_button(False)
        header.set_has_subtitle(False)
        add_class(header, "settings-header")
        title_box = box("h", 10)
        title_box.pack_start(Gtk.Image.new_from_icon_name(pick_icon(("lindos-settings", "preferences-system"), "preferences-system"), Gtk.IconSize.LARGE_TOOLBAR), False, False, 0)
        title_lbl = Gtk.Label(label=WINDOW_TITLE)
        add_class(title_lbl, "header-title")
        title_box.pack_start(title_lbl, False, False, 0)
        header.pack_start(title_box)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Find a setting")
        self.search.set_width_chars(34)
        self.search.set_tooltip_text("Search pages and settings (Ctrl+F)")
        add_class(self.search, "settings-search")
        self.search.connect("search-changed", self._on_search)
        self.search.connect("stop-search", lambda *_: self._clear_search())
        header.pack_end(self.search)
        self.header = header
        outer.pack_start(header, False, False, 0)

        # -- body: overlay(toast) > hbox(sidebar | separator | stack)
        overlay = Gtk.Overlay()
        outer.pack_start(overlay, True, True, 0)
        body = box("h", 0)
        overlay.add(body)
        self.sidebar = Sidebar(self.pages, self.backend, self.show_page)
        body.pack_start(self.sidebar.widget, False, False, 0)
        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        add_class(sep, "sidebar-separator")
        body.pack_start(sep, False, False, 0)
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(120)
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)
        add_class(self.stack, "settings-stack")
        body.pack_start(self.stack, True, True, 0)
        self._toast = Toast(overlay)
        win.show_all()

    # ------------------------------------------------------------------ pages
    def _ensure_page(self, page_id: str) -> Any:
        widget = self.page_widgets.get(page_id)
        if widget is not None:
            return widget
        page = self.pages_by_id.get(page_id)
        if page is None:
            return None
        from .pages import build_page  # lazy: keeps start-up light

        try:
            pw = build_page(self, page)
        except Exception:
            log.exception("building page %s failed", page_id)
            return None
        self.page_widgets[page_id] = pw
        self.stack.add_named(pw.widget, page_id)
        if self._query:
            try:
                pw.apply_filter(self._query)
            except Exception:
                log.exception("filter on page %s failed", page_id)
        return pw

    def show_page(self, page_id: str) -> None:
        if page_id not in self.pages_by_id:
            log.warning("unknown page %r", page_id)
            page_id = "home"
        pw = self._ensure_page(page_id)
        if pw is None:
            self.toast(f"Page '{page_id}' could not be opened")
            return
        self._current = page_id
        self.stack.set_visible_child_name(page_id)
        self.sidebar.select(page_id)
        if self.window is not None:
            title = self.pages_by_id[page_id].title
            self.window.set_title(f"{title} — {WINDOW_TITLE}" if page_id != "home" else WINDOW_TITLE)

    @property
    def current_page(self) -> Optional[Any]:
        return self.page_widgets.get(self._current or "")

    # ------------------------------------------------------------------ search
    def _on_search(self, entry: Any) -> None:
        self._query = model.normalize_query(entry.get_text())
        visible = self.sidebar.filter(self._query)
        for pid, pw in self.page_widgets.items():
            try:
                pw.apply_filter(self._query)
            except Exception:
                log.exception("filter on page %s failed", pid)
        if self._query and visible and self._current not in visible:
            # the current page has nothing matching: jump to the first hit (Win11 behaviour)
            self.show_page(visible[0])
        elif self._query and not visible:
            self.toast("No settings match “%s”" % entry.get_text().strip())

    def _clear_search(self) -> None:
        if self.search is not None and self.search.get_text():
            self.search.set_text("")
        self._query = ""
        self.sidebar.filter("")
        for pw in self.page_widgets.values():
            pw.apply_filter("")

    def _on_key(self, _win: Any, event: Any) -> bool:
        keyval = event.keyval
        state = event.state & Gtk.accelerator_get_default_mod_mask()
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if ctrl and keyval in (Gdk.KEY_f, Gdk.KEY_F):
            self.search.grab_focus()
            return True
        if keyval == Gdk.KEY_Escape:
            if self.search.has_focus() or self.search.get_text():
                self._clear_search()
                if self.current_page is not None:
                    self.current_page.widget.grab_focus()
                return True
        if ctrl and keyval in (Gdk.KEY_l, Gdk.KEY_L):
            self.search.grab_focus()
            return True
        return False

    # ------------------------------------------------------------------ services for pages
    def toast(self, text: str) -> None:
        if self._toast is not None:
            self._toast.show(text)
        log.info("toast: %s", text)

    def show_output(self, title: str, argv: list[str], env: Optional[dict[str, str]] = None) -> Any:
        dlg = OutputDialog(self.window, title, self.backend)
        dlg.run_argv(argv, env=env)
        return dlg

    def on_theme_changed(self) -> None:
        """Called after Dark/Light switched: the CSS follows @theme_* colours automatically,
        so we only re-sync widgets that display the state."""
        home = self.page_widgets.get("home")
        if home is not None:
            try:
                home.refresh()
            except Exception:
                log.exception("home refresh failed")
        pers = self.page_widgets.get("personalization")
        if pers is not None:
            try:
                pers.on_show()
            except Exception:
                log.exception("personalization refresh failed")

    def on_mode_changed(self) -> None:
        try:
            self.modes = self.backend.load_modes()
        except Exception as exc:
            log.warning("load_modes failed: %s", exc)
        self.sidebar.update_mode(self.modes)
        home = self.page_widgets.get("home")
        if home is not None:
            try:
                home.refresh()
            except Exception:
                log.exception("home refresh failed")


__all__ = ["SettingsApplication", "APP_ID", "CSS_PATH", "WINDOW_TITLE", "DEFAULT_SIZE", "MIN_SIZE"]
