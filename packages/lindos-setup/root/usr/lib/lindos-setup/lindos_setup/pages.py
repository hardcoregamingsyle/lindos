"""Wizard pages for the Lindos OOBE (SPEC §6).

Page ids, in order: ``welcome`` → ``mode`` → ``browser`` → ``personalize`` →
``apps`` → ``privacy`` → ``summary`` → ``apply`` → ``done``.

Every page derives from :class:`Page`; :class:`PageContext` carries the shared
state (selections, catalog, ...) and a reference to the window's navigation
API (``ctx.window``: ``set_next_sensitive``, ``set_next_label``,
``set_back_visible``, ``go_next``, ``finish``, ``set_light``).
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Any, Callable, Dict, List, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

from . import core  # noqa: E402
from .i18n import _  # noqa: E402
from .plan import (  # noqa: E402
    Catalog, Plan, RunResult, Runner, Selections, Step, StepResult, build_plan, summarize,
)
from .widgets import (  # noqa: E402
    AccentCss, Card, CardGroup, CheckRow, InfoBanner, Swatch, SwitchRow, WallpaperThumb,
    add_class, hbox, icon_image, label, load_svg_thumbnail, scrolled, section_title, vbox,
)

log = logging.getLogger("lindos-setup.pages")

PAGE_ORDER: List[str] = [
    "welcome", "mode", "browser", "personalize", "apps", "privacy", "transfer", "summary", "apply", "done",
]

MODE_ICON_FALLBACK: Dict[str, str] = {
    "everyday": "user-home", "gaming": "applications-games", "work": "x-office-document",
    "creator": "applications-graphics", "lite": "battery-good",
}
BROWSER_ICONS: Dict[str, str] = {
    "edge": "microsoft-edge", "chrome": "google-chrome", "firefox": "firefox",
}
BROWSER_BLURBS: Dict[str, str] = {
    "edge": "Microsoft's browser. Downloaded from packages.microsoft.com during setup.",
    "chrome": "Google's browser. Downloaded from dl.google.com during setup.",
    "firefox": "Already installed on Lindos. Open source, works offline right now.",
}
LOW_RAM_MB = 4096


class PageContext:
    """Shared wizard state handed to every page."""

    def __init__(self, *, selections: Selections, catalog: Catalog, accents: List[Dict[str, str]],
                 modes: Dict[str, Any], browsers: Dict[str, Dict[str, Any]], online: bool,
                 dry_run: bool, first_run: bool, wallpapers: List[str],
                 ram_total_mb: Optional[int], live: core.LiveApplier,
                 executors_factory: Callable[[Plan], Dict[str, Any]],
                 logger: Optional[logging.Logger] = None, online_known: bool = True) -> None:
        self.selections = selections
        self.catalog = catalog
        self.accents = accents
        self.modes = modes
        self.browsers = browsers
        self.online = online
        self.online_known = online_known     # False while the start-up probe is still running
        self.online_listeners: List[Callable[[], None]] = []
        self.dry_run = dry_run
        self.first_run = first_run
        self.wallpapers = wallpapers
        self.ram_total_mb = ram_total_mb
        self.live = live
        self.executors_factory = executors_factory
        self.log = logger or logging.getLogger("lindos-setup")
        self.accent_css = AccentCss()
        self.window: Any = None          # set by SetupWindow
        self.plan: Optional[Plan] = None
        self.run_result: Optional[RunResult] = None
        self.applied = False
        self.transfer_launched = False   # DonePage spawns lindos-transfer-gui at most once

    # connectivity ------------------------------------------------------------
    def set_online(self, online: bool) -> bool:
        """Record the connectivity result (UI thread) and notify listeners.

        Returns False so it can be used directly as a ``GLib.idle_add`` callback.
        """
        self.online = bool(online)
        self.online_known = True
        self.log.info("connectivity: %s", "online" if self.online else "offline")
        for cb in list(self.online_listeners):
            try:
                cb()
            except Exception as exc:  # a listener must never break the wizard
                log.warning("online listener failed: %s", exc)
        return False

    def ensure_online_known(self) -> bool:
        """Block on the connectivity probe if it has not finished yet (rare)."""
        if not self.online_known:
            self.set_online(core.is_online())
        return self.online

    # convenience -------------------------------------------------------------
    def mode_name(self, mid: str) -> str:
        m = self.modes.get(mid)
        return getattr(m, "name", mid.capitalize()) if m is not None else mid.capitalize()

    def browser_name(self, bid: str) -> str:
        b = self.browsers.get(bid)
        return str(b.get("name", bid)) if isinstance(b, dict) else bid.capitalize()

    def accent_name(self, hex_colour: str) -> str:
        for a in self.accents:
            if a["hex"].upper() == hex_colour.upper():
                return a["name"]
        return hex_colour.upper()

    def mode_names(self) -> Dict[str, str]:
        return {mid: self.mode_name(mid) for mid in self.modes}

    def browser_names(self) -> Dict[str, str]:
        return {bid: self.browser_name(bid) for bid in self.browsers}

    def accent_names(self) -> Dict[str, str]:
        return {a["hex"].upper(): a["name"] for a in self.accents}


# ---------------------------------------------------------------------------
# base page
# ---------------------------------------------------------------------------
class Page:
    id: str = ""
    title: str = ""
    subtitle: str = ""
    next_label: str = "Next"
    back_visible: bool = True
    next_visible: bool = True

    def __init__(self) -> None:
        self.ctx: Optional[PageContext] = None
        self.root: Optional[Gtk.Widget] = None
        self.title_label: Optional[Gtk.Label] = None
        self.subtitle_label: Optional[Gtk.Label] = None

    # -- framework -----------------------------------------------------------
    def build(self, ctx: PageContext) -> Gtk.Widget:
        self.ctx = ctx
        content = self.build_content(ctx)
        self.root = self._frame(content)
        return self.root

    def _frame(self, content: Gtk.Widget) -> Gtk.Widget:
        box = vbox(6)
        add_class(box, "page", "page-" + self.id)
        self.title_label = label(_(self.title), "oobe-title", wrap=True)
        box.pack_start(self.title_label, False, False, 0)
        self.subtitle_label = label(_(self.subtitle), "oobe-subtitle", wrap=True)
        self.subtitle_label.set_no_show_all(not self.subtitle)
        box.pack_start(self.subtitle_label, False, False, 0)
        spacer = Gtk.Box()
        spacer.set_size_request(-1, 10)
        box.pack_start(spacer, False, False, 0)
        content.set_vexpand(True)
        box.pack_start(content, True, True, 0)
        return box

    def set_titles(self, title: str, subtitle: str = "") -> None:
        if self.title_label is not None:
            self.title_label.set_text(title)
        if self.subtitle_label is not None:
            self.subtitle_label.set_text(subtitle)
            self.subtitle_label.set_visible(bool(subtitle))

    # -- to override ---------------------------------------------------------
    def build_content(self, ctx: PageContext) -> Gtk.Widget:  # pragma: no cover - abstract
        raise NotImplementedError

    def on_enter(self, ctx: PageContext) -> None:
        """Called every time the page becomes visible."""

    def on_leave(self, ctx: PageContext, forward: bool) -> bool:
        """Return False to veto navigation."""
        return True

    def can_go_back(self, ctx: PageContext) -> bool:
        return self.back_visible

    def can_go_next(self, ctx: PageContext) -> bool:
        return True


# ---------------------------------------------------------------------------
# welcome
# ---------------------------------------------------------------------------
class WelcomePage(Page):
    id = "welcome"
    title = "Welcome to Lindos"
    subtitle = "Let's set up your PC in a few quick steps. Everything can be changed later in Lindos Settings."
    next_label = "Get started"
    back_visible = False

    def build_content(self, ctx: PageContext) -> Gtk.Widget:
        box = vbox(18)
        head = hbox(20)
        logo = self._logo()
        logo.set_valign(Gtk.Align.START)
        head.pack_start(logo, False, False, 0)
        intro = vbox(6)
        intro.pack_start(label(_("Here is what we'll do:"), "body-strong"), False, False, 0)
        steps = [
            _("Pick a Mode — Everyday, Gaming, Work, Creator or Lite."),
            _("Choose your web browser."),
            _("Personalize the look: theme, accent colour, wallpaper, taskbar."),
            _("Add apps such as Windows app support, Steam or Office."),
            _("Review privacy settings (Lindos sends nothing anywhere)."),
        ]
        for s in steps:
            intro.pack_start(label("•  " + s, "body", wrap=True), False, False, 0)
        head.pack_start(intro, True, True, 0)
        box.pack_start(head, False, False, 0)

        pc_line = self._pc_line(ctx)
        if pc_line:
            box.pack_start(InfoBanner(pc_line, "computer-symbolic"), False, False, 0)

        honesty = InfoBanner(_(
            "Lindos runs Windows programs through Wine and Proton — a translation layer with "
            "near-native speed, not a copy of Windows. Most software and Steam games work; "
            "games with kernel anti-cheat such as Valorant and Fortnite do not run on any Linux."),
            "dialog-information-symbolic")
        box.pack_end(honesty, False, False, 0)
        return box

    def _logo(self) -> Gtk.Widget:
        for cand in (os.path.join(os.environ.get("LINDOS_ROOT", "") or "/", "usr/share/pixmaps/lindos-logo.svg"),
                     "/usr/share/pixmaps/lindos-logo.svg"):
            pix = load_svg_thumbnail(cand, 96, 96)
            if pix is not None:
                img = Gtk.Image.new_from_pixbuf(pix)
                add_class(img, "logo")
                return img
        img = icon_image("lindos-start", 96, fallback="preferences-desktop")
        add_class(img, "logo")
        return img

    def _pc_line(self, ctx: PageContext) -> str:
        if not ctx.ram_total_mb:
            return ""
        gb = ctx.ram_total_mb / 1024.0
        text = _("This PC has about %.0f GB of RAM.") % gb
        if ctx.ram_total_mb <= LOW_RAM_MB:
            text += " " + _("Lite mode is recommended for 4 GB or less.")
        else:
            text += " " + _("Lindos aims to idle at 350–500 MB so the rest is yours.")
        return text


# ---------------------------------------------------------------------------
# mode
# ---------------------------------------------------------------------------
class ModePage(Page):
    id = "mode"
    title = "Choose your Mode"
    subtitle = "A Mode tunes performance, the taskbar pins and suggested apps. Change it any time in Lindos Settings › Lindos Mode."

    def __init__(self) -> None:
        super().__init__()
        self.group = CardGroup(on_change=self._changed)
        self.detail: Optional[Gtk.Label] = None

    def build_content(self, ctx: PageContext) -> Gtk.Widget:
        box = vbox(14)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.set_homogeneous(True)
        low_ram = bool(ctx.ram_total_mb and ctx.ram_total_mb <= LOW_RAM_MB)
        for mid, mode in ctx.modes.items():
            icon = getattr(mode, "icon", "") or MODE_ICON_FALLBACK.get(mid, "preferences-desktop")
            badge = ""
            if mid == "everyday":
                badge = _("Default")
            if mid == "lite" and low_ram:
                badge = _("Recommended")
            card = Card(mid, getattr(mode, "name", mid.capitalize()),
                        getattr(mode, "description", "") or "",
                        icon_name=icon, hint=_(core.RAM_HINTS.get(mid, "")), badge=badge,
                        icon_size=40, height=210)
            self.group.add(card)
            row.pack_start(card, True, True, 0)
        box.pack_start(row, False, False, 0)
        self.detail = label("", "note", wrap=True)
        box.pack_start(self.detail, False, False, 0)
        return box

    def on_enter(self, ctx: PageContext) -> None:
        self.group.select(ctx.selections.mode)
        self._update_detail(ctx.selections.mode)

    def _changed(self, key: str) -> None:
        assert self.ctx is not None
        self.ctx.selections.mode = key
        self._update_detail(key)

    def _update_detail(self, mid: str) -> None:
        if self.detail is None or self.ctx is None:
            return
        mode = self.ctx.modes.get(mid)
        parts: List[str] = []
        governor = getattr(mode, "governor", "") if mode is not None else ""
        compositor = getattr(mode, "compositor", "") if mode is not None else ""
        zram = getattr(mode, "zram_percent", None) if mode is not None else None
        pins = list(getattr(mode, "pins", []) or []) if mode is not None else []
        if governor:
            parts.append(_("CPU governor: %s") % governor)
        if compositor:
            parts.append(_("Compositor: %s") % compositor)
        if zram:
            parts.append(_("zram: %s %%") % zram)
        if pins:
            names = [p[:-8] if p.endswith(".desktop") else p for p in pins]
            parts.append(_("Taskbar pins: %s") % ", ".join(names[:6]) + (" …" if len(names) > 6 else ""))
        if not parts:
            parts.append(_("Mode details are applied by lindos-mode when you finish setup."))
        self.detail.set_text("  ·  ".join(parts))


# ---------------------------------------------------------------------------
# browser
# ---------------------------------------------------------------------------
class BrowserPage(Page):
    id = "browser"
    title = "Choose a web browser"
    subtitle = ("Firefox is on the Lindos disc. Microsoft Edge and Google Chrome are downloaded from "
                "the vendors' official repositories during setup — their licences do not allow "
                "shipping them on the ISO.")

    def __init__(self) -> None:
        super().__init__()
        self.group = CardGroup(on_change=self._changed)
        self.banner: Optional[InfoBanner] = None
        self.recheck: Optional[Gtk.Button] = None
        self._checking = False

    def build_content(self, ctx: PageContext) -> Gtk.Widget:
        box = vbox(14)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.set_homogeneous(True)
        for bid, info in ctx.browsers.items():
            desc = _(BROWSER_BLURBS.get(bid, ""))
            hint = ""
            if bid in ("edge", "chrome"):
                hint = _("Downloaded from vendor · needs internet")
            elif core.browser_installed(bid):
                hint = _("Installed")
            card = Card(bid, str(info.get("name", bid)), desc,
                        icon_name=BROWSER_ICONS.get(bid, "web-browser"), hint=hint,
                        icon_size=40, height=170)
            self.group.add(card)
            row.pack_start(card, True, True, 0)
        box.pack_start(row, False, False, 0)
        self.banner = InfoBanner("", "network-wireless-symbolic")
        box.pack_start(self.banner, False, False, 0)
        self.recheck = Gtk.Button(label=_("Check connection again"))
        add_class(self.recheck, "btn-link")
        self.recheck.set_halign(Gtk.Align.START)
        self.recheck.connect("clicked", self._on_recheck)
        box.pack_start(self.recheck, False, False, 0)
        # the start-up connectivity probe runs in the background; refresh when it lands
        ctx.online_listeners.append(self._apply_online_state)
        return box

    def on_enter(self, ctx: PageContext) -> None:
        self._apply_online_state()

    def on_leave(self, ctx: PageContext, forward: bool) -> bool:
        if forward and not ctx.online_known:
            # probe still running (user was very fast): settle it now so the choice is honest
            wanted = ctx.selections.browser
            ctx.ensure_online_known()
            if not ctx.online and wanted in ("edge", "chrome"):
                self._apply_online_state()      # switches to Firefox and shows the notice
                return False                    # let the user see it before moving on
        return True

    def _apply_online_state(self) -> None:
        ctx = self.ctx
        if ctx is None or self.banner is None or self.recheck is None:
            return
        if not ctx.online_known:
            self.banner.get_style_context().remove_class("warn")
            self.banner.set_text(_(
                "Checking your internet connection… Edge and Chrome need it because they are "
                "downloaded from the vendor's apt repository during the Apply step."))
            self.recheck.hide()
            for bid in ("edge", "chrome"):
                self.group.set_disabled(bid, False)
        elif ctx.online:
            self.banner.get_style_context().remove_class("warn")
            self.banner.set_text(_(
                "You're online. Edge or Chrome will be added from the vendor's apt repository "
                "during the Apply step and become the default browser."))
            self.recheck.hide()
            for bid in ("edge", "chrome"):
                self.group.set_disabled(bid, False)
        else:
            self.banner.get_style_context().add_class("warn")
            self.banner.set_text(_(
                "You're offline, so Edge and Chrome can't be downloaded right now. Firefox is "
                "selected. You can install Edge or Chrome later from Lindos Settings › Apps › "
                "Web browsers (lindos-settings apps) or with 'lindos-browser install edge' / "
                "'lindos-browser install chrome' in a Terminal."))
            self.recheck.show()
            for bid in ("edge", "chrome"):
                self.group.set_disabled(bid, True, _("Needs an internet connection"))
            if ctx.selections.browser in ("edge", "chrome"):
                ctx.selections.browser = "firefox"
        self.group.select(ctx.selections.browser)

    def _changed(self, key: str) -> None:
        assert self.ctx is not None
        self.ctx.selections.browser = key

    def _on_recheck(self, _btn: Gtk.Button) -> None:
        if self._checking:
            return
        self._checking = True
        assert self.recheck is not None
        self.recheck.set_sensitive(False)

        def worker() -> None:
            online = core.is_online()
            GLib.idle_add(self._recheck_done, online)

        threading.Thread(target=worker, name="lindos-setup-online", daemon=True).start()

    def _recheck_done(self, online: bool) -> bool:
        self._checking = False
        assert self.ctx is not None and self.recheck is not None
        self.recheck.set_sensitive(True)
        self.ctx.set_online(online)          # notifies listeners -> _apply_online_state
        return False


# ---------------------------------------------------------------------------
# personalize
# ---------------------------------------------------------------------------
class PersonalizePage(Page):
    id = "personalize"
    title = "Personalize your desktop"
    subtitle = "Choices apply immediately so you can see them. Everything is in Lindos Settings › Personalization too."

    def __init__(self) -> None:
        super().__init__()
        self.dark_row: Optional[SwitchRow] = None
        self.swatches = CardGroup(on_change=self._accent_changed)
        self.thumbs = CardGroup(on_change=self._wallpaper_changed)
        self.accent_label: Optional[Gtk.Label] = None
        self.radio_center: Optional[Gtk.RadioButton] = None
        self.radio_left: Optional[Gtk.RadioButton] = None
        self._syncing = False

    def build_content(self, ctx: PageContext) -> Gtk.Widget:
        cols = hbox(24)

        left = vbox(14)
        # 826 px of card content: 372 (left) + 24 + 2 thumbnails x ~200 + spacing fits
        left.set_size_request(372, -1)
        self.dark_row = SwitchRow(_("Dark mode"),
                                  _("Dark is the Lindos default. Light uses the Lindos-Light theme."),
                                  active=ctx.selections.dark, on_toggle=self._dark_toggled,
                                  on_label=_("Dark"), off_label=_("Light"))
        left.pack_start(self.dark_row, False, False, 0)

        left.pack_start(section_title(_("Accent colour")), False, False, 0)
        sw_row = hbox(8)
        for acc in ctx.accents:
            sw = Swatch(acc["hex"].upper(), acc["hex"], acc["name"], size=30)
            self.swatches.add(sw)
            sw_row.pack_start(sw, False, False, 0)
        left.pack_start(sw_row, False, False, 0)
        self.accent_label = label("", "note")
        left.pack_start(self.accent_label, False, False, 0)

        left.pack_start(section_title(_("Taskbar alignment")), False, False, 0)
        radios = hbox(18)
        self.radio_center = Gtk.RadioButton.new_with_label(None, _("Center (Windows 11)"))
        self.radio_left = Gtk.RadioButton.new_with_label_from_widget(self.radio_center, _("Left (classic)"))
        add_class(self.radio_center, "radio-row")
        add_class(self.radio_left, "radio-row")
        self.radio_center.connect("toggled", self._align_toggled, "center")
        self.radio_left.connect("toggled", self._align_toggled, "left")
        radios.pack_start(self.radio_center, False, False, 0)
        radios.pack_start(self.radio_left, False, False, 0)
        left.pack_start(radios, False, False, 0)
        cols.pack_start(left, False, False, 0)

        right = vbox(8)
        right.pack_start(section_title(_("Wallpaper")), False, False, 0)
        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_max_children_per_line(2)
        flow.set_min_children_per_line(2)
        flow.set_column_spacing(6)
        flow.set_row_spacing(6)
        flow.set_homogeneous(True)
        for path in ctx.wallpapers:
            thumb = WallpaperThumb(path, core.wallpaper_display_name(path))
            self.thumbs.add(thumb)
            flow.add(thumb)
        # keep the flowbox children from grabbing focus rings
        for child in flow.get_children():
            child.set_can_focus(False)
        right.pack_start(scrolled(flow, height=300), True, True, 0)
        cols.pack_start(right, True, True, 0)
        return cols

    def on_enter(self, ctx: PageContext) -> None:
        self._syncing = True
        try:
            if self.dark_row is not None:
                self.dark_row.set_active(ctx.selections.dark)
            self.swatches.select(ctx.selections.accent.upper())
            if self.accent_label is not None:
                self.accent_label.set_text(ctx.accent_name(ctx.selections.accent))
            ctx.accent_css.apply(ctx.selections.accent)
            if ctx.selections.wallpaper in self.thumbs.cards:
                self.thumbs.select(ctx.selections.wallpaper)
            else:
                self.thumbs.select(None)
            if self.radio_center is not None and self.radio_left is not None:
                if ctx.selections.taskbar_alignment == "left":
                    self.radio_left.set_active(True)
                else:
                    self.radio_center.set_active(True)
        finally:
            self._syncing = False

    # -- handlers -------------------------------------------------------------
    def _dark_toggled(self, active: bool) -> None:
        if self._syncing or self.ctx is None:
            return
        ctx = self.ctx
        ctx.selections.set_theme("dark" if active else "light")
        ctx.live.set_dark(active)
        if ctx.window is not None:
            ctx.window.set_light(not active)
        # follow the aurora wallpaper variant if it exists in the thumbnails
        if ctx.selections.wallpaper in self.thumbs.cards:
            self.thumbs.select(ctx.selections.wallpaper)
            ctx.live.set_wallpaper(ctx.selections.wallpaper)

    def _accent_changed(self, key: str) -> None:
        if self.ctx is None:
            return
        self.ctx.selections.accent = key.upper()
        if self.accent_label is not None:
            self.accent_label.set_text(self.ctx.accent_name(key))
        self.ctx.accent_css.apply(key)
        if not self._syncing:
            self.ctx.live.set_accent(key)

    def _wallpaper_changed(self, key: str) -> None:
        if self.ctx is None:
            return
        self.ctx.selections.wallpaper = key
        if not self._syncing:
            self.ctx.live.set_wallpaper(key)

    def _align_toggled(self, radio: Gtk.RadioButton, alignment: str) -> None:
        if not radio.get_active() or self.ctx is None:
            return
        self.ctx.selections.taskbar_alignment = alignment
        if not self._syncing:
            self.ctx.live.set_taskbar_alignment(alignment)


# ---------------------------------------------------------------------------
# apps
# ---------------------------------------------------------------------------
class AppsPage(Page):
    id = "apps"
    title = "Add apps"
    subtitle = "Pick what to install now. Anything you skip can be added later from Lindos Settings › Apps."

    def __init__(self) -> None:
        super().__init__()
        self.rows: Dict[str, CheckRow] = {}
        self._last_mode: Optional[str] = None
        self.banner: Optional[InfoBanner] = None
        self._syncing = False

    def build_content(self, ctx: PageContext) -> Gtk.Widget:
        box = vbox(10)
        if len(ctx.catalog) == 0:
            box.pack_start(InfoBanner(_(
                "No optional apps catalog was found (apps.json). Nothing extra will be installed; "
                "use Lindos Settings › Apps later."), warn=True), False, False, 0)
            return box
        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_min_children_per_line(2)
        flow.set_max_children_per_line(2)
        flow.set_column_spacing(16)
        flow.set_row_spacing(4)
        flow.set_homogeneous(True)
        for entry in ctx.catalog:
            row = CheckRow(entry.id, entry.name, entry.description)
            row.connect("toggled", self._toggled, entry.id)
            self.rows[entry.id] = row
            flow.add(row)
        for child in flow.get_children():
            child.set_can_focus(False)
        box.pack_start(scrolled(flow, height=330), True, True, 0)
        self.banner = InfoBanner("", "network-wireless-symbolic")
        box.pack_end(self.banner, False, False, 0)
        return box

    def on_enter(self, ctx: PageContext) -> None:
        if len(ctx.catalog) == 0:
            return
        if self._last_mode != ctx.selections.mode:
            ctx.selections.apps = ctx.catalog.default_ids(ctx.selections.mode)
            self._last_mode = ctx.selections.mode
        self._syncing = True
        try:
            for app_id, row in self.rows.items():
                row.set_active(app_id in ctx.selections.apps)
        finally:
            self._syncing = False
        if self.banner is not None:
            if ctx.online or not ctx.online_known:
                self.banner.get_style_context().remove_class("warn")
                self.banner.set_text(_(
                    "Downloads run during the Apply step. Windows app support means Wine + Proton "
                    "(a translation layer). Anti-cheat games such as Valorant or Fortnite do not "
                    "run on any Linux — check protondb.com and areweanticheatyet.com."))
            else:
                self.banner.get_style_context().add_class("warn")
                self.banner.set_text(_(
                    "You're offline: downloads will be skipped and listed for later. Finish them "
                    "from Lindos Settings › Apps (lindos-settings apps) once connected."))

    def _toggled(self, row: Gtk.CheckButton, app_id: str) -> None:
        if self._syncing or self.ctx is None:
            return
        apps = self.ctx.selections.apps
        if row.get_active():
            if app_id not in apps:
                apps.append(app_id)
        else:
            if app_id in apps:
                apps.remove(app_id)
        # keep catalog order for a stable summary
        order = self.ctx.catalog.ids()
        apps.sort(key=lambda a: order.index(a) if a in order else len(order))


# ---------------------------------------------------------------------------
# privacy
# ---------------------------------------------------------------------------
class PrivacyPage(Page):
    id = "privacy"
    title = "Privacy"
    subtitle = "Lindos collects nothing. No telemetry, no ads, no account needed."

    def __init__(self) -> None:
        super().__init__()
        self.location_row: Optional[SwitchRow] = None
        self.crash_row: Optional[SwitchRow] = None

    def build_content(self, ctx: PageContext) -> Gtk.Widget:
        box = vbox(14)
        box.pack_start(InfoBanner(_(
            "Lindos never sends usage data anywhere and shows no advertising. Updates come from "
            "the Linux Mint / Ubuntu repositories; Edge and Chrome (if chosen) come from Microsoft "
            "and Google, whose own privacy policies apply inside those browsers."),
            "security-high-symbolic"), False, False, 0)
        self.location_row = SwitchRow(
            _("Location services"),
            _("Let apps that ask (weather, maps) use your approximate location through GeoClue. "
              "Off by default; nothing is shared until an app requests it."),
            active=ctx.selections.location, on_toggle=self._location, on_label=_("On"), off_label=_("Off"))
        box.pack_start(self.location_row, False, False, 0)
        self.crash_row = SwitchRow(
            _("Crash reports"),
            _("Keep crash reports on this PC so you can attach them to a bug report yourself. "
              "Nothing is uploaded automatically — there is no server to send them to."),
            active=ctx.selections.crash_reports, on_toggle=self._crash, on_label=_("On"), off_label=_("Off"))
        box.pack_start(self.crash_row, False, False, 0)
        box.pack_start(label(_("Both switches only record your preference in ~/.config/lindos/config.json."),
                             "note", wrap=True), False, False, 0)
        return box

    def on_enter(self, ctx: PageContext) -> None:
        if self.location_row is not None:
            self.location_row.set_active(ctx.selections.location)
        if self.crash_row is not None:
            self.crash_row.set_active(ctx.selections.crash_reports)

    def _location(self, active: bool) -> None:
        if self.ctx is not None:
            self.ctx.selections.location = bool(active)

    def _crash(self, active: bool) -> None:
        if self.ctx is not None:
            self.ctx.selections.crash_reports = bool(active)


# ---------------------------------------------------------------------------
# transfer (optional; SPEC-WINDOWS §29 / §32)
# ---------------------------------------------------------------------------
class TransferPage(Page):
    id = "transfer"
    title = "Bring your stuff from Windows"
    subtitle = ("Optional. Copy documents, browser bookmarks, wallpaper and more from a Windows "
                "drive or a transfer folder made with the Windows kit. Nothing is copied now -- "
                "the Transfer tool opens after setup finishes.")

    SKIP_KEY = "skip"

    def __init__(self) -> None:
        super().__init__()
        self.group = CardGroup(on_change=self._changed)
        self.banner: Optional[InfoBanner] = None
        self.recheck: Optional[Gtk.Button] = None
        self.list_box: Optional[Gtk.Box] = None
        self._sources: Dict[str, Dict[str, str]] = {}
        self._loading = False
        self._loaded = False

    def build_content(self, ctx: PageContext) -> Gtk.Widget:
        box = vbox(10)
        self.banner = InfoBanner(_("Looking for a Windows drive or a transfer folder…"),
                                 "drive-harddisk-symbolic")
        box.pack_start(self.banner, False, False, 0)
        self.list_box = vbox(8)
        box.pack_start(scrolled(self.list_box, height=300), True, True, 0)
        self.recheck = Gtk.Button(label=_("Check again"))
        add_class(self.recheck, "btn-link")
        self.recheck.set_halign(Gtk.Align.START)
        self.recheck.connect("clicked", self._on_recheck)
        self.recheck.set_no_show_all(True)
        self.recheck.hide()
        box.pack_start(self.recheck, False, False, 0)
        return box

    def on_enter(self, ctx: PageContext) -> None:
        if not self._loaded and not self._loading:
            self._start_load(ctx)

    # -- loading (worker thread; never blocks the UI, handles the CLI being absent) -----------
    def _start_load(self, ctx: PageContext) -> None:
        self._loading = True
        assert self.banner is not None and self.recheck is not None
        self.banner.get_style_context().remove_class("warn")
        self.banner.set_text(_("Looking for a Windows drive or a transfer folder…"))
        self.recheck.hide()

        def worker() -> None:
            data = core.transfer_sources()
            GLib.idle_add(self._loaded_cb, data)

        threading.Thread(target=worker, name="lindos-setup-transfer-sources", daemon=True).start()

    def _loaded_cb(self, data: Dict[str, Any]) -> bool:
        self._loading = False
        self._loaded = True
        assert self.list_box is not None and self.banner is not None and self.recheck is not None
        for child in list(self.list_box.get_children()):
            self.list_box.remove(child)
        self.group = CardGroup(on_change=self._changed)
        self._sources = {self.SKIP_KEY: {"type": "", "source": ""}}
        skip = Card(self.SKIP_KEY, _("Skip for now"),
                   _("Bring your stuff later from Lindos Settings › Windows apps › "
                     "Transfer from Windows…"),
                   icon_name="edit-clear-all-symbolic", icon_size=32, height=88)
        self.group.add(skip)
        self.list_box.pack_start(skip, False, False, 0)

        if not data.get("available", True):
            self.banner.get_style_context().add_class("warn")
            self.banner.set_text(str(data.get("note")) if data.get("note") else _(
                "The Transfer tool isn't installed. Add it later from Lindos Settings."))
            self.recheck.hide()
        else:
            found = self._add_cards(data)
            if found:
                self.banner.get_style_context().remove_class("warn")
                self.banner.set_text(_("Choose what to bring in, or skip and do it later -- "
                                       "everything can be picked again in the Transfer tool."))
                self.recheck.hide()
            else:
                self.banner.get_style_context().remove_class("warn")
                self.banner.set_text(_(
                    "No Windows drive or transfer folder found yet. Plug in a USB stick made "
                    "with the Windows kit, or open the Windows drive once in File Explorer, "
                    "then check again."))
                self.recheck.show()
        self.list_box.show_all()
        self._select_current(ctx=self.ctx)
        return False

    def _add_cards(self, data: Dict[str, Any]) -> int:
        assert self.list_box is not None
        found = 0
        for part in data.get("partitions") or []:
            if not isinstance(part, dict) or not part.get("windows"):
                continue
            found += 1
            device = str(part.get("device") or found)
            key = "part:%s" % device
            tags = [t for t, ok in (("BitLocker", part.get("bitlocker")),
                                    ("hibernated", part.get("hibernated"))) if ok]
            title = str(part.get("label") or device or _("Windows drive"))
            card = Card(key, title, str(part.get("note") or ""), icon_name="drive-harddisk",
                       hint=", ".join(tags), icon_size=32, height=104)
            if not part.get("mountpoint"):
                card.set_disabled(True, _("Not opened yet -- open it once in File Explorer, "
                                          "then press Check again"))
            self.group.add(card)
            self.list_box.pack_start(card, False, False, 0)
            self._sources[key] = {"type": "partition", "source": str(part.get("mountpoint") or device)}
        for bundle in data.get("bundles") or []:
            if not isinstance(bundle, dict):
                continue
            found += 1
            path = str(bundle.get("path") or found)
            key = "bundle:%s" % path
            title = _("Transfer folder from %s") % (bundle.get("computer") or "?")
            desc = _("User %s · made %s") % (bundle.get("user") or "?", bundle.get("created") or "?")
            card = Card(key, title, desc, icon_name="folder-download", icon_size=32, height=104)
            self.group.add(card)
            self.list_box.pack_start(card, False, False, 0)
            self._sources[key] = {"type": "bundle", "source": path}
        return found

    def _select_current(self, ctx: Optional[PageContext]) -> None:
        if ctx is None:
            return
        cur = ctx.selections.transfer or {}
        want = self.SKIP_KEY
        if cur.get("enabled") and cur.get("source"):
            for key, info in self._sources.items():
                if info.get("type") == cur.get("source_type") and info.get("source") == cur.get("source"):
                    want = key
                    break
        self.group.select(want)

    def _changed(self, key: str) -> None:
        if self.ctx is None:
            return
        info = self._sources.get(key, {"type": "", "source": ""})
        if key == self.SKIP_KEY or not info.get("type"):
            self.ctx.selections.transfer = {"enabled": False, "source_type": "", "source": ""}
        else:
            self.ctx.selections.transfer = {
                "enabled": True, "source_type": info["type"], "source": info["source"],
            }

    def _on_recheck(self, _btn: Gtk.Button) -> None:
        if self._loading or self.ctx is None:
            return
        self._loaded = False
        self._start_load(self.ctx)


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------
class SummaryPage(Page):
    id = "summary"
    title = "Review your choices"
    subtitle = "Go Back to change anything. Apply starts the setup — you may be asked for your password once."
    next_label = "Apply"

    def __init__(self) -> None:
        super().__init__()
        self.grid: Optional[Gtk.Grid] = None
        self.notes: Optional[Gtk.Box] = None
        self.steps_label: Optional[Gtk.Label] = None

    def build_content(self, ctx: PageContext) -> Gtk.Widget:
        box = vbox(12)
        self.grid = Gtk.Grid()
        self.grid.set_column_spacing(24)
        self.grid.set_row_spacing(8)
        add_class(self.grid, "summary-grid")
        box.pack_start(self.grid, False, False, 0)
        self.notes = vbox(6)
        box.pack_start(self.notes, False, False, 0)
        self.steps_label = label("", "note", wrap=True)
        box.pack_end(self.steps_label, False, False, 0)
        return box

    def on_enter(self, ctx: PageContext) -> None:
        assert self.grid is not None and self.notes is not None and self.steps_label is not None
        ctx.ensure_online_known()            # the plan depends on it (browser fallback, notes)
        if not ctx.online and ctx.selections.browser in ("edge", "chrome"):
            ctx.selections.browser = "firefox"
        for child in self.grid.get_children():
            self.grid.remove(child)
        for child in self.notes.get_children():
            self.notes.remove(child)
        rows = summarize(ctx.selections, ctx.catalog, ctx.mode_names(), ctx.browser_names(),
                         ctx.accent_names())
        for i, (key, value) in enumerate(rows):
            k = label(_(key), "summary-key")
            k.set_valign(Gtk.Align.START)
            v = label(value, "summary-value", wrap=True)
            v.set_max_width_chars(70)
            self.grid.attach(k, 0, i, 1, 1)
            self.grid.attach(v, 1, i, 1, 1)
        plan = build_plan(ctx.selections, ctx.catalog, online=ctx.online)
        ctx.plan = plan
        for note in plan.notes:
            self.notes.pack_start(InfoBanner(note, warn=True), False, False, 0)
        n_sys = len(plan.system_steps())
        text = _("%d steps in total; %d need administrator rights.") % (len(plan), n_sys)
        if ctx.dry_run:
            text += "  " + _("Dry run: nothing will be changed; the plan is printed to the terminal.")
        self.steps_label.set_text(text)
        self.grid.show_all()
        self.notes.show_all()


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------
class ApplyPage(Page):
    id = "apply"
    title = "Setting up Lindos"
    subtitle = "This can take a few minutes; downloads depend on your connection. Please keep the PC on."
    back_visible = False

    def __init__(self) -> None:
        super().__init__()
        self.progress: Optional[Gtk.ProgressBar] = None
        self.step_label: Optional[Gtk.Label] = None
        self.textview: Optional[Gtk.TextView] = None
        self.buffer: Optional[Gtk.TextBuffer] = None
        self.banner: Optional[InfoBanner] = None
        self._started = False
        self._runner: Optional[Runner] = None

    def build_content(self, ctx: PageContext) -> Gtk.Widget:
        box = vbox(10)
        self.step_label = label(_("Preparing…"), "body-strong")
        box.pack_start(self.step_label, False, False, 0)
        self.progress = Gtk.ProgressBar()
        self.progress.set_show_text(True)
        self.progress.set_fraction(0.0)
        self.progress.set_text("0 %")
        add_class(self.progress, "oobe-progress")
        box.pack_start(self.progress, False, False, 0)
        self.textview = Gtk.TextView()
        self.textview.set_editable(False)
        self.textview.set_cursor_visible(False)
        self.textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.textview.set_left_margin(8)
        self.textview.set_right_margin(8)
        self.textview.set_top_margin(6)
        self.textview.set_bottom_margin(6)
        add_class(self.textview, "log-view")
        self.buffer = self.textview.get_buffer()
        box.pack_start(scrolled(self.textview, height=280), True, True, 0)
        self.banner = InfoBanner("", "emblem-ok-symbolic")
        self.banner.set_no_show_all(True)
        box.pack_end(self.banner, False, False, 0)
        return box

    def on_enter(self, ctx: PageContext) -> None:
        if ctx.window is not None:
            ctx.window.set_next_sensitive(False)
            ctx.window.set_back_visible(False)
        if self._started:
            return
        self._started = True
        GLib.idle_add(self._start)

    # -- run ------------------------------------------------------------------
    def _start(self) -> bool:
        ctx = self.ctx
        assert ctx is not None
        try:
            ctx.ensure_online_known()
            plan = build_plan(ctx.selections, ctx.catalog, online=ctx.online)
            ctx.plan = plan
            ctx.log.info("plan: %s", plan.to_json(indent=None))
            if ctx.dry_run:
                try:
                    sys.stdout.write(plan.to_json() + "\n")
                    sys.stdout.flush()
                except (OSError, ValueError):
                    pass
                self._append(_("Dry run — nothing is changed. Plan JSON printed to the terminal."))
            for note in plan.notes:
                self._append("! " + note)
            executors = ctx.executors_factory(plan)
            self._runner = Runner(plan, executors, log=self._log_from_thread,
                                  on_step_start=self._on_step_start, on_step_done=self._on_step_done)
        except Exception as exc:
            log.exception("cannot start apply: %s", exc)
            self._append(_("Setup could not start: %s") % exc)
            self._finished(RunResult([StepResult("plan", ok=False, message=str(exc))]))
            return False
        thread = threading.Thread(target=self._run_thread, name="lindos-setup-apply", daemon=True)
        thread.start()
        return False

    def _run_thread(self) -> None:
        assert self._runner is not None and self.ctx is not None
        try:
            # let any queued live-preview theme call finish before the plan writes config
            self.ctx.live.drain(15.0)
            result = self._runner.run()
        except Exception as exc:  # the runner isolates steps; this is belt and braces
            log.exception("runner crashed: %s", exc)
            result = self._runner.result
        GLib.idle_add(self._finished, result)

    # -- callbacks from the worker thread (marshalled to the UI thread) -------
    def _log_from_thread(self, msg: str) -> None:
        assert self.ctx is not None
        self.ctx.log.info("%s", msg)
        if self.ctx.dry_run:
            try:
                sys.stdout.write(msg + "\n")
                sys.stdout.flush()
            except (OSError, ValueError):
                pass
        GLib.idle_add(self._append, msg)

    def _on_step_start(self, index: int, total: int, step: Step) -> None:
        GLib.idle_add(self._show_step, index, total, step.title)

    def _on_step_done(self, index: int, total: int, step: Step, res: StepResult) -> None:
        GLib.idle_add(self._show_progress, index + 1, total)

    def _show_step(self, index: int, total: int, title: str) -> bool:
        if self.step_label is not None:
            self.step_label.set_text(_("Step %d of %d — %s") % (index + 1, total, title))
        self._show_progress(index, total)
        return False

    def _show_progress(self, done: int, total: int) -> bool:
        if self.progress is not None and total > 0:
            frac = min(1.0, max(0.0, done / float(total)))
            self.progress.set_fraction(frac)
            self.progress.set_text("%d %%" % int(frac * 100))
        return False

    def _append(self, msg: str) -> bool:
        if self.buffer is None or self.textview is None:
            return False
        end = self.buffer.get_end_iter()
        self.buffer.insert(end, msg + "\n")
        mark = self.buffer.create_mark(None, self.buffer.get_end_iter(), False)
        self.textview.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)
        self.buffer.delete_mark(mark)
        return False

    def _finished(self, result: RunResult) -> bool:
        ctx = self.ctx
        assert ctx is not None and self.banner is not None
        ctx.run_result = result
        ctx.applied = True
        self._show_progress(1, 1)
        if self.step_label is not None:
            self.step_label.set_text(_("Finished — %s") % result.summary())
        problems = result.failed_ids + result.skipped_ids
        if problems:
            self.banner.get_style_context().add_class("warn")
            self.banner.set_text(_(
                "Some steps were skipped or failed: %s. Your desktop is usable; finish the rest "
                "later from Lindos Settings › Apps (lindos-settings apps). Log: %s")
                % (", ".join(problems), core.log_file()))
        else:
            self.banner.get_style_context().remove_class("warn")
            self.banner.set_text(_("All done. Press Next to finish."))
        self.banner.set_no_show_all(False)
        self.banner.show_all()
        if ctx.window is not None:
            ctx.window.set_next_sensitive(True)
        return False


# ---------------------------------------------------------------------------
# done
# ---------------------------------------------------------------------------
class DonePage(Page):
    id = "done"
    title = "Welcome to Lindos"
    subtitle = "Your PC is ready."
    next_label = "Finish"
    back_visible = False

    def __init__(self) -> None:
        super().__init__()
        self.recap: Optional[Gtk.Label] = None
        self.settings_btn: Optional[Gtk.Button] = None

    def build_content(self, ctx: PageContext) -> Gtk.Widget:
        box = vbox(16)
        self.recap = label("", "body", wrap=True)
        box.pack_start(self.recap, False, False, 0)
        tips = vbox(6)
        tips.pack_start(label(_("A few Windows-style shortcuts:"), "body-strong"), False, False, 0)
        for t in (
            _("Super — Start menu      ·   Super+I — Settings      ·   Super+E — File Explorer"),
            _("Super+X — power menu   ·   Super+Shift+S — screenshot   ·   Ctrl+Shift+Esc — Task Manager"),
        ):
            tips.pack_start(label(t, "body"), False, False, 0)
        box.pack_start(tips, False, False, 0)
        box.pack_start(InfoBanner(_(
            "Double-click an .exe or .msi to run it through Wine/Proton. Games with anti-cheat "
            "that block Linux (Valorant, Fortnite, League of Legends) will not work — Roblox runs "
            "via Sober, Minecraft Java natively. See Lindos Settings › Windows apps."),
            "dialog-information-symbolic"), False, False, 0)
        self.settings_btn = Gtk.Button(label=_("Open Lindos Settings"))
        add_class(self.settings_btn, "btn-secondary")
        self.settings_btn.set_halign(Gtk.Align.START)
        self.settings_btn.connect("clicked", self._open_settings)
        box.pack_end(self.settings_btn, False, False, 0)
        return box

    def on_enter(self, ctx: PageContext) -> None:
        if self.recap is not None:
            parts = [
                _("Mode: %s") % ctx.mode_name(ctx.selections.mode),
                _("Browser: %s") % ctx.browser_name(ctx.selections.browser),
                _("Theme: %s") % (_("Dark") if ctx.selections.dark else _("Light")),
            ]
            if ctx.run_result is not None and (ctx.run_result.failed_ids or ctx.run_result.skipped_ids):
                parts.append(_("Some installs are pending — see Lindos Settings › Apps."))
            if (ctx.selections.transfer or {}).get("enabled"):
                parts.append(_("Opening the Transfer tool for your Windows files…"))
            self.recap.set_text("   ·   ".join(parts))
        if self.settings_btn is not None:
            self.settings_btn.set_visible(core.which("lindos-settings") is not None or ctx.dry_run)
        if ctx.window is not None:
            ctx.window.set_next_sensitive(True)
        self._maybe_launch_transfer(ctx)

    def _maybe_launch_transfer(self, ctx: PageContext) -> None:
        """Spawn ``lindos-transfer-gui`` once when the transfer page's choice was 'enabled'
        (SPEC-WINDOWS §32: "the Done page launches lindos-transfer-gui when chosen"). Detached
        and non-blocking; never copies anything itself."""
        if ctx.transfer_launched:
            return
        transfer = ctx.selections.transfer or {}
        if not transfer.get("enabled"):
            return
        ctx.transfer_launched = True
        source = str(transfer.get("source") or "")
        if ctx.dry_run:
            ctx.log.info("dry-run: would launch lindos-transfer-gui --from %r", source)
            return
        if not core.launch_transfer_gui(source):
            log.warning("lindos-transfer-gui could not be started")

    def _open_settings(self, _btn: Gtk.Button) -> None:
        if self.ctx is not None and self.ctx.window is not None:
            self.ctx.window.finish(open_settings=True)


def make_pages() -> List[Page]:
    """Instantiate all pages in SPEC order."""
    pages: List[Page] = [WelcomePage(), ModePage(), BrowserPage(), PersonalizePage(), AppsPage(),
                         PrivacyPage(), TransferPage(), SummaryPage(), ApplyPage(), DonePage()]
    assert [p.id for p in pages] == PAGE_ORDER
    return pages


__all__ = ["PAGE_ORDER", "PageContext", "Page", "make_pages", "WelcomePage", "ModePage",
           "BrowserPage", "PersonalizePage", "AppsPage", "PrivacyPage", "TransferPage",
           "SummaryPage", "ApplyPage", "DonePage"]
