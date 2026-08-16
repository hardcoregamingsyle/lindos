"""The Lindos OOBE window (GTK 3) and ``run_app`` entry point.

One fullscreen, undecorated ``Gtk.Window`` (falls back to maximised when the
window manager refuses fullscreen), dark ``#202020`` background, a centred
900×620 card with the page stack, Back/Next buttons and step dots.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Any, Dict, List, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from . import core  # noqa: E402
from .i18n import _  # noqa: E402
from .pages import PAGE_ORDER, Page, PageContext, make_pages  # noqa: E402
from .plan import Plan, Selections, load_accents, load_catalog, make_printing_executors  # noqa: E402
from .widgets import (  # noqa: E402
    Card, StepDots, Swatch, WallpaperThumb, add_class, hbox, load_css_file, vbox,
)

log = logging.getLogger("lindos-setup.app")

CARD_W, CARD_H = 900, 620
UI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")
CSS_PATH = os.path.join(UI_DIR, "oobe.css")


class SetupWindow(Gtk.Window):
    """Fullscreen wizard window hosting the page stack."""

    def __init__(self, ctx: PageContext, pages: List[Page], *, start_page: str = "welcome",
                 allow_quit: bool = False) -> None:
        super().__init__(title=_("Lindos Setup"))
        self.ctx = ctx
        self.pages = pages
        self.index = 0
        self.allow_quit = allow_quit
        self.finished = False
        self.exit_code = 0
        self._fullscreen_checked = False
        ctx.window = self

        self.set_wmclass("lindos-setup", "Lindos Setup")
        self.set_role("lindos-setup")
        self.set_decorated(False)
        self.set_keep_above(False)
        self.set_skip_taskbar_hint(False)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_default_size(1024, 720)
        self.set_icon_name("lindos-start")
        add_class(self, "oobe")

        # ---- layout ---------------------------------------------------------
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.set_halign(Gtk.Align.CENTER)
        outer.set_valign(Gtk.Align.CENTER)
        add_class(outer, "oobe-outer")

        card = vbox(0)
        card.set_size_request(CARD_W, CARD_H)
        add_class(card, "oobe-card")

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.stack.set_transition_duration(260)
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)
        self.stack.set_homogeneous(True)
        for page in self.pages:
            widget = page.build(ctx)
            self.stack.add_named(widget, page.id)
        card.pack_start(self.stack, True, True, 0)

        footer = hbox(12)
        add_class(footer, "oobe-footer")
        self.back_btn = Gtk.Button(label=_("Back"))
        add_class(self.back_btn, "btn-back")
        self.back_btn.set_size_request(96, 34)
        self.back_btn.connect("clicked", lambda _b: self.go_back())
        footer.pack_start(self.back_btn, False, False, 0)

        self.dots = StepDots(len(self.pages))
        footer.set_center_widget(self.dots)

        self.next_btn = Gtk.Button(label=_("Next"))
        add_class(self.next_btn, "btn-next")
        self.next_btn.set_size_request(120, 34)
        self.next_btn.set_can_default(True)
        self.next_btn.connect("clicked", lambda _b: self.go_next())
        footer.pack_end(self.next_btn, False, False, 0)
        card.pack_end(footer, False, False, 0)

        outer.pack_start(card, False, False, 0)
        self.add(outer)
        self.set_default(self.next_btn)

        # ---- signals --------------------------------------------------------
        self.connect("key-press-event", self._on_key)
        self.connect("delete-event", self._on_delete)
        self.connect("destroy", self._on_destroy)
        self.connect("map-event", self._on_map)

        if not ctx.selections.dark:
            self.set_light(True)
        ctx.accent_css.apply(ctx.selections.accent)

        start = start_page if start_page in PAGE_ORDER else "welcome"
        self._start_index = PAGE_ORDER.index(start)

    # ------------------------------------------------------------------ show
    def present_wizard(self) -> None:
        self.fullscreen()
        self.show_all()
        self.show_page(self._start_index, animate=False)
        self.present()

    def _on_map(self, *_args: Any) -> bool:
        if not self._fullscreen_checked:
            self._fullscreen_checked = True
            GLib.timeout_add(900, self._check_fullscreen)
        return False

    def _check_fullscreen(self) -> bool:
        gdk_win = self.get_window()
        state = gdk_win.get_state() if gdk_win is not None else 0
        if not (state & Gdk.WindowState.FULLSCREEN):
            log.warning("fullscreen not granted by the window manager; maximizing instead")
            self.unfullscreen()
            self.maximize()
        return False

    # ------------------------------------------------------------ navigation
    @property
    def page(self) -> Page:
        return self.pages[self.index]

    def show_page(self, index: int, animate: bool = True) -> None:
        index = max(0, min(index, len(self.pages) - 1))
        forward = index >= self.index
        self.index = index
        page = self.pages[index]
        if animate:
            self.stack.set_transition_type(
                Gtk.StackTransitionType.SLIDE_LEFT if forward else Gtk.StackTransitionType.SLIDE_RIGHT)
        else:
            self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.stack.set_visible_child_name(page.id)
        self.dots.set_active(index)
        self.set_back_visible(page.can_go_back(self.ctx) and index > 0)
        self.set_next_label(_(page.next_label))
        self.next_btn.set_visible(page.next_visible)
        self.set_next_sensitive(True)
        try:
            page.on_enter(self.ctx)
        except Exception as exc:  # a broken page must not kill the wizard
            log.exception("page %s on_enter failed: %s", page.id, exc)
        self.next_btn.grab_focus()
        log.info("page: %s", page.id)

    def go_next(self) -> None:
        if not self.next_btn.get_sensitive() or not self.next_btn.get_visible():
            return
        page = self.page
        try:
            if not page.on_leave(self.ctx, True):
                return
        except Exception as exc:
            log.exception("page %s on_leave failed: %s", page.id, exc)
        if self.index >= len(self.pages) - 1:
            self.finish()
            return
        self.show_page(self.index + 1)

    def go_back(self) -> None:
        if self.index <= 0 or not self.back_btn.get_sensitive():
            return
        page = self.page
        try:
            page.on_leave(self.ctx, False)
        except Exception as exc:
            log.exception("page %s on_leave failed: %s", page.id, exc)
        self.show_page(self.index - 1)

    # ------------------------------------------------- API used by the pages
    def set_next_sensitive(self, sensitive: bool) -> None:
        self.next_btn.set_sensitive(bool(sensitive))

    def set_next_label(self, text: str) -> None:
        self.next_btn.set_label(text)

    def set_back_visible(self, visible: bool) -> None:
        # keep the footer balanced: a hidden Back still reserves its space
        self.back_btn.set_opacity(1.0 if visible else 0.0)
        self.back_btn.set_sensitive(bool(visible))
        self.back_btn.set_can_focus(bool(visible))

    def set_light(self, light: bool) -> None:
        ctx = self.get_style_context()
        if light:
            ctx.add_class("light")
        else:
            ctx.remove_class("light")

    def finish(self, open_settings: bool = False) -> None:
        """Finish button: mark setup done, optionally open Lindos Settings, quit."""
        if self.finished:
            return
        self.finished = True
        if not core.mark_setup_done(dry_run=self.ctx.dry_run):
            log.error("could not fully record setup completion; the wizard may run again")
        if open_settings:
            if self.ctx.dry_run:
                log.info("dry-run: would launch lindos-settings")
            elif not core.launch_settings():
                log.warning("lindos-settings could not be started")
        self.exit_code = 0
        self.destroy()

    def request_quit(self) -> None:
        """Escape / close: only honoured in --reconfigure (or after finishing)."""
        if self.allow_quit or self.finished:
            log.info("wizard closed by user")
            self.exit_code = 0 if self.finished else 1
            self.destroy()

    # --------------------------------------------------------------- events
    def _on_key(self, _widget: Gtk.Widget, event: Gdk.EventKey) -> bool:
        keyval = event.keyval
        if keyval == Gdk.KEY_Escape:
            self.request_quit()
            return True          # swallow on first-run: nothing happens
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if self._focus_handles_enter():
                return False
            self.go_next()
            return True
        if keyval == Gdk.KEY_BackSpace and event.state & Gdk.ModifierType.MOD1_MASK:
            self.go_back()
            return True
        return False

    def _focus_handles_enter(self) -> bool:
        """True when the focused widget should consume Enter itself.

        Windows-OOBE rule: Enter means *Next* everywhere -- selectable cards,
        swatches, thumbnails, check boxes and switches are changed with the
        mouse or Space, so Enter on them still advances.  Only text widgets
        and explicit action buttons (Back, "Open Lindos Settings", "Check
        connection again") keep the key.
        """
        focus = self.get_focus()
        if focus is None or focus is self.next_btn:
            return False
        if isinstance(focus, (Gtk.TextView, Gtk.Entry)):
            return True
        if isinstance(focus, (Card, Swatch, WallpaperThumb, Gtk.CheckButton, Gtk.Switch)):
            return False
        if isinstance(focus, Gtk.Button):
            style = focus.get_style_context()
            return any(style.has_class(c) for c in ("btn-back", "btn-secondary", "btn-link"))
        return False

    def _on_delete(self, *_args: Any) -> bool:
        if self.allow_quit or self.finished:
            self.exit_code = 0 if self.finished else 1
            return False
        return True              # block closing during first-run

    def _on_destroy(self, *_args: Any) -> None:
        Gtk.main_quit()


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def _gtk_init_ok() -> bool:
    """``Gtk.init_check`` in both PyGObject calling conventions."""
    try:
        res = Gtk.init_check()
    except TypeError:
        res = Gtk.init_check([sys.argv[0]])
    except Exception as exc:  # pragma: no cover - defensive
        log.error("Gtk.init_check failed: %s", exc)
        return False
    if isinstance(res, (tuple, list)):
        return bool(res[0])
    return bool(res)


def build_context(*, dry_run: bool, first_run: bool, logger: logging.Logger,
                  online: Optional[bool] = None) -> PageContext:
    """Collect everything the pages need (guarded core calls).

    ``online=None`` (the default) leaves the connectivity result *unknown*;
    :func:`start_online_probe` fills it in from a background thread so the
    window appears immediately even when the probe has to time out offline.
    """
    catalog = load_catalog()
    accents = load_accents()
    modes = core.load_modes()
    browsers = core.browsers_table()
    wallpapers = core.list_wallpapers()
    ram_total = core.ram_total_mb()
    selections = Selections()
    # reconfigure: start from the current user config where possible
    config = core.core_module("config")
    if config is not None:
        try:
            cfg = config.Config.load()
            selections = Selections.from_dict({
                "mode": cfg.get("mode", selections.mode),
                "browser": cfg.get("browser", selections.browser),
                "theme": cfg.get("theme", selections.theme),
                "accent": cfg.get("accent", selections.accent),
                "wallpaper": cfg.get("wallpaper", selections.wallpaper),
                "taskbar_alignment": cfg.get("taskbar_alignment", selections.taskbar_alignment),
                "crash_reports": cfg.get("telemetry", False),
                "location": cfg.get("location_services", False),
            })
            try:
                selections.validate()
            except ValueError as exc:
                logger.warning("user config has odd values (%s); using defaults", exc)
                selections = Selections()
        except Exception as exc:
            logger.debug("Config.load failed: %s", exc)
    if online is not None and not online and selections.browser in ("edge", "chrome"):
        selections.browser = "firefox"
    selections.apps = catalog.default_ids(selections.mode)
    live = core.LiveApplier(dry_run=dry_run, threaded=True)

    def executors_factory(plan: Plan) -> Dict[str, Any]:
        if dry_run:
            return make_printing_executors(plan)   # lines go through the runner log
        return core.make_real_executors()

    logger.info("context: online=%s ram=%s modes=%s browsers=%s wallpapers=%d apps=%d dry_run=%s first_run=%s",
                "unknown" if online is None else online, ram_total, list(modes), list(browsers),
                len(wallpapers), len(catalog), dry_run, first_run)
    return PageContext(selections=selections, catalog=catalog, accents=accents, modes=modes,
                       browsers=browsers, online=bool(online), online_known=online is not None,
                       dry_run=dry_run, first_run=first_run, wallpapers=wallpapers,
                       ram_total_mb=ram_total, live=live, executors_factory=executors_factory,
                       logger=logger)


def start_online_probe(ctx: PageContext) -> None:
    """Run ``core.is_online()`` off the UI thread; result lands via ``ctx.set_online``."""
    if ctx.online_known:
        return

    def worker() -> None:
        try:
            online = core.is_online()
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("connectivity probe failed: %s", exc)
            online = False
        GLib.idle_add(ctx.set_online, online)

    threading.Thread(target=worker, name="lindos-setup-online-probe", daemon=True).start()


def run_app(*, dry_run: bool = False, reconfigure: bool = False, page: Optional[str] = None,
            logger: Optional[logging.Logger] = None) -> int:
    """Create the wizard window and run the GTK main loop. Returns exit code."""
    logger = logger or logging.getLogger("lindos-setup")
    if page is not None and page not in PAGE_ORDER:
        sys.stderr.write("lindos-setup: unknown page %r (choose from %s)\n" % (page, ", ".join(PAGE_ORDER)))
        return 2
    if not _gtk_init_ok():
        if dry_run:
            logger.warning("no display available; running headless dry-run")
            return core.headless_dry_run(logger)
        sys.stderr.write("lindos-setup: cannot open display (is DISPLAY/WAYLAND_DISPLAY set?)\n")
        return 1

    if not load_css_file(CSS_PATH):
        logger.warning("oobe.css missing or invalid at %s; using theme defaults", CSS_PATH)

    ctx = build_context(dry_run=dry_run, first_run=not reconfigure, logger=logger)
    win = SetupWindow(ctx, make_pages(), start_page=page or "welcome", allow_quit=reconfigure)
    win.present_wizard()
    start_online_probe(ctx)
    try:
        Gtk.main()
    finally:
        ctx.live.close()
    return win.exit_code


__all__ = ["SetupWindow", "build_context", "start_online_probe", "run_app", "CSS_PATH", "UI_DIR"]
