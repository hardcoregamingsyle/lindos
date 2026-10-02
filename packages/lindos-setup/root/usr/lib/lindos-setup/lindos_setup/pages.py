"""Wizard pages for the Lindos OOBE (SPEC §6).

Page ids, in order: ``welcome`` → ``mode`` → ``browser`` → ``personalize`` →
``privacy`` → ``transfer`` → ``summary`` → ``apply`` → ``done``.

The wizard is install-free: the installer already installed the browser, drivers, apps and updates
(its record, ``install-state.json``, only feeds read-only hints on the Browser, Mode and Done
pages), and the account was created by oem-config before this wizard starts.

Every page derives from :class:`Page`; :class:`PageContext` carries the shared
state (selections, install-state hints, ...) and a reference to the window's navigation
API (``ctx.window``: ``set_next_sensitive``, ``set_next_label``,
``set_back_visible``, ``go_next``, ``finish``, ``set_light``).

Look (Windows 11 out-of-box style): each page is a centred column (about 760 px) with a big
heading, one short subtitle and one focused question; ``hero`` pages (welcome, apply, done)
centre everything and lead with a big image (logo, spinner, check mark).
"""
from __future__ import annotations

import logging
import os
import re
import sys
import threading
from typing import Any, Callable, Dict, List, NamedTuple, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

from . import core  # noqa: E402
from .i18n import N_, _  # noqa: E402
from .plan import (  # noqa: E402
    BROWSER_INSTALLED, BROWSER_PENDING, BROWSER_UNAVAILABLE, INSTALL_STEP_NAMES, Plan, RunResult,
    Runner, Selections, Step, StepResult, build_plan, install_recap, mode_extras_pending,
    pending_steps, summarize,
)
from .widgets import (  # noqa: E402
    AccentCss, Card, CardGroup, InfoBanner, LearnMore, Swatch, SwitchRow, WallpaperThumb,
    add_class, column_width, hbox, icon_image, label, load_svg_thumbnail, screen_width, section_title,
    set_a11y, taskbar_preview, theme_preview, vbox,
)

log = logging.getLogger("lindos-setup.pages")

PAGE_ORDER: List[str] = [
    "welcome", "mode", "browser", "personalize", "privacy", "transfer", "summary", "apply", "done",
]

MODE_ICON_FALLBACK: Dict[str, str] = {
    "everyday": "user-home", "gaming": "applications-games", "work": "x-office-document",
    "creator": "applications-graphics", "lite": "battery-good",
}
BROWSER_ICONS: Dict[str, str] = {
    "edge": "microsoft-edge", "chrome": "google-chrome", "firefox": "firefox",
}
BROWSER_BLURBS: Dict[str, str] = {
    "edge": "Microsoft's browser.",
    "chrome": "Google's browser.",
    "firefox": "Open source, from Mozilla. Included with Lindos.",
}
BROWSER_PENDING_HINT = N_("Will be added when you're online")
LOW_RAM_MB = 4096

# SPEC §0.1 honesty text, shown on the Done page (a short always-visible line plus a "Learn more"
# disclosure) instead of on the welcome page.
WINE_HONESTY = N_(
    "Windows apps run through Wine and Proton — a translation layer with near-native speed, not a "
    "copy of Windows. Most software and Steam games work. Games with kernel anti-cheat such as "
    "Valorant and Fortnite do not run on any Linux today, so they are not supported on Lindos yet: "
    "that is up to their publishers, Lindos will list them once they enable Linux and it has tested "
    "them, and there is no date.")
LEARN_MORE_TITLE = N_("Learn more about Windows apps")
LEARN_MORE_LINES = (
    N_("Double-click an .exe or .msi file and Lindos opens it with Wine or Proton. You can manage "
       "installed Windows programs later in Lindos Settings › Windows apps."),
    N_("Steam games depend on the developer enabling anti-cheat for Proton. Check protondb.com and "
       "areweanticheatyet.com before you rely on a game."),
    N_("Games that are not supported yet can still be played through official cloud streaming "
       "(where the publisher offers it) or by restarting into your PC's own Windows. Open "
       "Lindos Settings › Gaming, or run lindos-game route with the game's name."),
    N_("Roblox runs through Sober, a community runtime for the Android client, because the Windows "
       "client does not run on Linux. Minecraft Java runs natively."),
    N_("Adobe: Creative Cloud 2019–2021 era Photoshop and Illustrator work through Wine recipes; "
       "newer releases are unreliable."),
)

# "Just a moment…" rotating lines (Windows-style). Tick 0 is the greeting; afterwards the rest repeat.
# Saving choices is local and takes seconds, so nothing here talks about downloads or waiting.
APPLY_LINES = (
    N_("Hi"),
    N_("We're getting things ready for you"),
    N_("Saving your choices"),
    N_("Setting up your desktop"),
)
APPLY_LINE_SECONDS = 6

SHORTCUTS = (
    ("Super", N_("Start menu")),
    ("Super+I", N_("Settings")),
    ("Super+E", N_("File Explorer")),
    ("Super+X", N_("Power menu")),
    ("Super+Shift+S", N_("Screenshot")),
    ("Ctrl+Shift+Esc", N_("Task Manager")),
)


def apply_line(tick: int) -> str:
    """The friendly line for rotation step ``tick`` (0 is the greeting, then the others cycle)."""
    if tick <= 0:
        return _(APPLY_LINES[0])
    rest = APPLY_LINES[1:]
    return _(rest[(tick - 1) % len(rest)])


def set_shown(widget: Any, shown: bool) -> None:
    """Show or hide a widget that was built with ``set_no_show_all(True)``, children included.

    ``set_visible(True)`` alone would reveal only the outer box: the window's ``show_all()`` skipped
    the whole subtree, so a banner's icon and text (or a box's heading) would stay hidden."""
    if shown:
        widget.set_no_show_all(False)
        widget.show_all()
        widget.set_no_show_all(True)
    else:
        widget.hide()


class BatchLine(NamedTuple):
    index: int
    total: int
    step_id: str
    state: str          # "start" | "done" | "failed"


_BATCH_LINE = re.compile(r"^\[batch (\d+)/(\d+)\] (\S+): (.+)$")


def parse_batch_line(msg: str) -> Optional[BatchLine]:
    """Parse a helper progress line such as ``[batch 2/4] install-packages: done``."""
    match = _BATCH_LINE.match((msg or "").strip())
    if match is None:
        return None
    rest = match.group(4)
    if rest == "done":
        state = "done"
    elif rest.startswith("FAILED"):
        state = "failed"
    else:
        state = "start"
    return BatchLine(int(match.group(1)), int(match.group(2)), match.group(3), state)


class ApplyProgress:
    """Monotonic progress estimate for the apply page.

    The Runner reports one step at a time, but every privileged step is sent to the helper as ONE
    batch that runs inside the first system step, so Runner progress alone would sit still for the
    whole batch.  Batch lines from the helper credit those steps as they finish.
    """

    def __init__(self) -> None:
        self.total = 0
        self.done = 0
        self.batch_base: Optional[int] = None
        self.batch_done = 0
        self.value = 0.0

    def step_started(self, index: int, total: int) -> float:
        self.total = total
        self.done = max(self.done, index)
        return self._update()

    def step_done(self, done: int, total: int) -> float:
        self.total = total
        self.done = max(self.done, done)
        return self._update()

    def batch_line(self, state: str) -> float:
        if self.batch_base is None:
            self.batch_base = self.done
        if state in ("done", "failed"):
            self.batch_done += 1
        return self._update()

    def finish(self) -> float:
        self.value = 1.0
        return self.value

    def _update(self) -> float:
        total = max(self.total, 1)
        estimate = self.done
        if self.batch_base is not None:
            estimate = max(estimate, self.batch_base + self.batch_done)
        estimate = min(estimate, max(total - 1, self.done))   # never claim 100 % before the end
        self.value = max(self.value, min(1.0, estimate / float(total)))
        return self.value


class PageContext:
    """Shared wizard state handed to every page."""

    def __init__(self, *, selections: Selections, accents: List[Dict[str, str]],
                 modes: Dict[str, Any], browsers: Dict[str, Dict[str, Any]],
                 dry_run: bool, first_run: bool, wallpapers: List[str],
                 ram_total_mb: Optional[int], live: core.LiveApplier,
                 executors_factory: Callable[[Plan], Dict[str, Any]],
                 logger: Optional[logging.Logger] = None,
                 install_steps: Optional[Dict[str, str]] = None,
                 browser_states: Optional[Dict[str, str]] = None) -> None:
        self.selections = selections
        self.accents = accents
        self.modes = modes
        self.browsers = browsers
        # what the installer recorded (step -> done|pending|skipped|failed); {} = no record
        self.install_steps: Dict[str, str] = dict(install_steps or {})
        # browser id -> installed|pending|unavailable; Firefox is always installed
        self.browser_states: Dict[str, str] = (
            dict(browser_states) if browser_states is not None
            else {bid: (BROWSER_INSTALLED if bid == "firefox" else BROWSER_UNAVAILABLE) for bid in browsers})
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
    hero: bool = False           # centred layout with a big image on top (welcome, apply, done)
    step_counted: bool = True    # counts in the slim "Step n of m" indicator

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
        """Heading, subtitle and content in a centred column inside a vertical scroller."""
        hero = self.hero
        column = vbox(0)
        column.set_size_request(column_width(screen_width()), -1)
        column.set_halign(Gtk.Align.CENTER)
        column.set_valign(Gtk.Align.CENTER if hero else Gtk.Align.START)
        add_class(column, "page", "page-" + self.id)
        if hero:
            add_class(column, "page-hero")
        image = self.hero_image(self.ctx) if self.ctx is not None else None
        if image is not None:
            image.set_halign(Gtk.Align.CENTER)
            column.pack_start(image, False, False, 0)
        align = 0.5 if hero else 0.0
        justify = Gtk.Justification.CENTER if hero else Gtk.Justification.LEFT
        self.title_label = label(_(self.title), "oobe-title", xalign=align, wrap=True, max_chars=48,
                                 justify=justify)
        column.pack_start(self.title_label, False, False, 0)
        self.subtitle_label = label(_(self.subtitle), "oobe-subtitle", xalign=align, wrap=True, max_chars=80,
                                    justify=justify)
        self.subtitle_label.set_no_show_all(not self.subtitle)
        column.pack_start(self.subtitle_label, False, False, 0)
        content.set_vexpand(False)
        column.pack_start(content, False, False, 0)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_shadow_type(Gtk.ShadowType.NONE)
        add_class(scroller, "page-scroll")
        scroller.add(column)
        return scroller

    def set_titles(self, title: str, subtitle: str = "") -> None:
        if self.title_label is not None:
            self.title_label.set_text(title)
        if self.subtitle_label is not None:
            self.subtitle_label.set_text(subtitle)
            self.subtitle_label.set_visible(bool(subtitle))

    # -- to override ---------------------------------------------------------
    def build_content(self, ctx: PageContext) -> Gtk.Widget:  # pragma: no cover - abstract
        raise NotImplementedError

    def hero_image(self, ctx: PageContext) -> Optional[Gtk.Widget]:
        """A big image shown above the heading (hero pages only)."""
        return None

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
    title = N_("Let's get you set up")
    subtitle = N_("This only takes a few minutes. You can change everything later in Lindos Settings.")
    next_label = N_("Get started")
    back_visible = False
    hero = True
    step_counted = False

    def hero_image(self, ctx: PageContext) -> Optional[Gtk.Widget]:
        return self._logo()

    def build_content(self, ctx: PageContext) -> Gtk.Widget:
        box = vbox(10)
        pc_line = self._pc_line(ctx)
        if pc_line:
            box.pack_start(label(pc_line, "note", xalign=0.5, wrap=True, justify=Gtk.Justification.CENTER),
                           False, False, 0)
        return box

    def _logo(self) -> Gtk.Widget:
        for cand in (os.path.join(os.environ.get("LINDOS_ROOT", "") or "/", "usr/share/pixmaps/lindos-logo.svg"),
                     "/usr/share/pixmaps/lindos-logo.svg"):
            pix = load_svg_thumbnail(cand, 112, 112)
            if pix is not None:
                img = Gtk.Image.new_from_pixbuf(pix)
                add_class(img, "logo")
                return img
        img = icon_image("lindos-start", 112, fallback="preferences-desktop")
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
    title = N_("How will you use this PC?")
    subtitle = N_("Pick the closest fit. A Mode tunes performance and the taskbar pins — nothing is "
                  "downloaded. Change it any time in Lindos Settings › Lindos Mode.")

    def __init__(self) -> None:
        super().__init__()
        self.group = CardGroup(on_change=self._changed)
        self.detail: Optional[Gtk.Label] = None
        self.extras_note: Optional[Gtk.Label] = None

    def build_content(self, ctx: PageContext) -> Gtk.Widget:
        box = vbox(14)
        cards = vbox(10)
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
                        icon_size=48, horizontal=True)
            self.group.add(card)
            cards.pack_start(card, False, False, 0)
        box.pack_start(cards, False, False, 0)
        self.detail = label("", "note", wrap=True)
        box.pack_start(self.detail, False, False, 0)
        # switching a Mode here is configuration only: say plainly when its extras are still missing
        self.extras_note = label("", "note", wrap=True)
        self.extras_note.set_no_show_all(True)
        box.pack_start(self.extras_note, False, False, 0)
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
        self._update_extras_note(mid, mode)

    def _update_extras_note(self, mid: str, mode: Any) -> None:
        if self.extras_note is None or self.ctx is None:
            return
        waiting = mode is not None and mode_extras_pending(mode, mid, self.ctx.install_steps)
        if waiting:
            self.extras_note.set_text(_(
                "Some apps for this Mode aren't installed yet. Add them from Lindos Settings › Apps "
                "once you're online (use Install now)."))
        self.extras_note.set_visible(waiting)


# ---------------------------------------------------------------------------
# browser
# ---------------------------------------------------------------------------
class BrowserPage(Page):
    id = "browser"
    title = N_("Choose your web browser")
    subtitle = N_("Pick the browser you'd like to use. You can add or switch browsers later in "
                  "Lindos Settings › Apps.")

    def __init__(self) -> None:
        super().__init__()
        self.group = CardGroup(on_change=self._changed)
        self.banner: Optional[InfoBanner] = None

    def build_content(self, ctx: PageContext) -> Gtk.Widget:
        box = vbox(16)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        row.set_homogeneous(True)
        # only browsers that are on this PC (or that Lindos is about to add) are offered; the
        # installer already downloaded Chrome, and Edge is only listed if it was installed by hand
        for bid, info in ctx.browsers.items():
            state = ctx.browser_states.get(bid, BROWSER_UNAVAILABLE)
            if state == BROWSER_UNAVAILABLE:
                continue
            hint = _("Installed") if state == BROWSER_INSTALLED else _(BROWSER_PENDING_HINT)
            card = Card(bid, str(info.get("name", bid)), _(BROWSER_BLURBS.get(bid, "")),
                        icon_name=BROWSER_ICONS.get(bid, "web-browser"), hint=hint,
                        icon_size=48, height=200)
            self.group.add(card)
            row.pack_start(card, True, True, 0)
        box.pack_start(row, False, False, 0)
        self.banner = InfoBanner("", "dialog-information-symbolic")
        self.banner.set_no_show_all(True)
        box.pack_start(self.banner, False, False, 0)
        return box

    def on_enter(self, ctx: PageContext) -> None:
        self._sync()

    def _sync(self) -> None:
        ctx = self.ctx
        if ctx is None:
            return
        if ctx.selections.browser not in self.group.cards:
            ctx.selections.browser = "firefox"    # the choice must be a card that is shown
        self.group.select(ctx.selections.browser)
        self._update_banner()

    def _update_banner(self) -> None:
        ctx = self.ctx
        if ctx is None or self.banner is None:
            return
        waiting = [bid for bid in self.group.order if ctx.browser_states.get(bid) == BROWSER_PENDING]
        if not waiting:
            set_shown(self.banner, False)
            return
        names = ", ".join(ctx.browser_name(bid) for bid in waiting)
        text = _("%s isn't on this PC yet. Lindos adds it in the background when you're online, or you can "
                 "press Install now in Lindos Settings › Apps. Until then Firefox is your browser.") % names
        if ctx.selections.browser in waiting:
            text += " " + _("%s becomes your default as soon as it's added.") % ctx.browser_name(ctx.selections.browser)
        self.banner.set_text(text)
        set_shown(self.banner, True)

    def _changed(self, key: str) -> None:
        assert self.ctx is not None
        self.ctx.selections.browser = key
        self._update_banner()


# ---------------------------------------------------------------------------
# personalize
# ---------------------------------------------------------------------------
class PersonalizePage(Page):
    id = "personalize"
    title = N_("Make it yours")
    subtitle = N_("Changes show up right away so you can see them. Everything is also in "
                  "Lindos Settings › Personalization.")

    def __init__(self) -> None:
        super().__init__()
        self.theme_group = CardGroup(on_change=self._theme_changed)
        self.swatches = CardGroup(on_change=self._accent_changed)
        self.thumbs = CardGroup(on_change=self._wallpaper_changed)
        self.align_group = CardGroup(on_change=self._align_changed)
        self.accent_label: Optional[Gtk.Label] = None
        self._syncing = False

    def build_content(self, ctx: PageContext) -> Gtk.Widget:
        box = vbox(12)

        box.pack_start(section_title(_("Theme")), False, False, 0)
        themes = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        themes.set_homogeneous(True)
        for key, title, desc in (
                ("dark", _("Dark"), _("Easy on the eyes. This is the Lindos default.")),
                ("light", _("Light"), _("Bright and clean, using the Lindos-Light theme."))):
            card = Card(key, title, desc, preview=theme_preview(key), horizontal=True)
            self.theme_group.add(card)
            themes.pack_start(card, True, True, 0)
        box.pack_start(themes, False, False, 0)

        box.pack_start(section_title(_("Accent colour")), False, False, 0)
        sw_row = hbox(12)
        for acc in ctx.accents:
            sw = Swatch(acc["hex"].upper(), acc["hex"], acc["name"], size=36)
            self.swatches.add(sw)
            sw_row.pack_start(sw, False, False, 0)
        box.pack_start(sw_row, False, False, 0)
        self.accent_label = label("", "note")
        box.pack_start(self.accent_label, False, False, 0)

        box.pack_start(section_title(_("Wallpaper")), False, False, 0)
        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_max_children_per_line(3)
        flow.set_min_children_per_line(2)
        flow.set_column_spacing(10)
        flow.set_row_spacing(10)
        flow.set_homogeneous(True)
        flow.set_halign(Gtk.Align.START)
        for path in ctx.wallpapers:
            thumb = WallpaperThumb(path, core.wallpaper_display_name(path))
            self.thumbs.add(thumb)
            flow.add(thumb)
        # keep the flowbox children from grabbing focus rings
        for child in flow.get_children():
            child.set_can_focus(False)
        box.pack_start(flow, False, False, 0)

        box.pack_start(section_title(_("Taskbar")), False, False, 0)
        bars = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        bars.set_homogeneous(True)
        for key, title, desc in (
                ("center", _("Center"), _("Icons in the middle, like Windows 11.")),
                ("left", _("Left"), _("Icons on the left, the classic layout."))):
            card = Card(key, title, desc, preview=taskbar_preview(key), horizontal=True)
            self.align_group.add(card)
            bars.pack_start(card, True, True, 0)
        box.pack_start(bars, False, False, 0)
        return box

    def on_enter(self, ctx: PageContext) -> None:
        self._syncing = True
        try:
            self.theme_group.select("dark" if ctx.selections.dark else "light")
            self.swatches.select(ctx.selections.accent.upper())
            if self.accent_label is not None:
                self.accent_label.set_text(ctx.accent_name(ctx.selections.accent))
            ctx.accent_css.apply(ctx.selections.accent)
            if ctx.selections.wallpaper in self.thumbs.cards:
                self.thumbs.select(ctx.selections.wallpaper)
            else:
                self.thumbs.select(None)
            self.align_group.select("left" if ctx.selections.taskbar_alignment == "left" else "center")
        finally:
            self._syncing = False

    # -- handlers -------------------------------------------------------------
    def _theme_changed(self, key: str) -> None:
        if self._syncing or self.ctx is None:
            return
        ctx = self.ctx
        dark = key != "light"
        ctx.selections.set_theme("dark" if dark else "light")
        ctx.live.set_dark(dark)
        if ctx.window is not None:
            ctx.window.set_light(not dark)
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

    def _align_changed(self, key: str) -> None:
        if self.ctx is None:
            return
        self.ctx.selections.taskbar_alignment = key
        if not self._syncing:
            self.ctx.live.set_taskbar_alignment(key)


# ---------------------------------------------------------------------------
# privacy
# ---------------------------------------------------------------------------
class PrivacyPage(Page):
    id = "privacy"
    title = N_("Choose your privacy settings")
    subtitle = N_("Lindos collects nothing. No telemetry, no ads, no account needed.")
    next_label = N_("Accept")

    def __init__(self) -> None:
        super().__init__()
        self.location_row: Optional[SwitchRow] = None
        self.crash_row: Optional[SwitchRow] = None

    def build_content(self, ctx: PageContext) -> Gtk.Widget:
        box = vbox(12)
        box.pack_start(InfoBanner(_(
            "Lindos never sends usage data anywhere and shows no advertising. Browsers such as Chrome "
            "or Edge are made by Google and Microsoft, and their own privacy policies apply inside "
            "those browsers."),
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
    title = N_("Bring your stuff from Windows")
    subtitle = N_("Optional. Copy documents, browser bookmarks, wallpaper and more from a Windows "
                  "drive or a transfer folder made with the Windows kit. Nothing is copied now — "
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
        box = vbox(12)
        self.banner = InfoBanner(_("Looking for a Windows drive or a transfer folder…"),
                                 "drive-harddisk-symbolic")
        box.pack_start(self.banner, False, False, 0)
        self.list_box = vbox(10)
        box.pack_start(self.list_box, False, False, 0)
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
                    icon_name="edit-clear-all-symbolic", icon_size=40, horizontal=True)
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
                self.banner.set_text(_("Choose what to bring in, or skip and do it later — "
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
                        hint=", ".join(tags), icon_size=40, horizontal=True)
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
            card = Card(key, title, desc, icon_name="folder-download", icon_size=40, horizontal=True)
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
    title = N_("Ready to set up your PC?")
    subtitle = N_("Take a last look. Go Back to change anything — Apply saves these choices on this PC. "
                  "It only takes a moment and nothing is downloaded.")
    next_label = N_("Apply")

    def __init__(self) -> None:
        super().__init__()
        self.grid: Optional[Gtk.Grid] = None
        self.notes: Optional[Gtk.Box] = None
        self.steps_label: Optional[Gtk.Label] = None

    def build_content(self, ctx: PageContext) -> Gtk.Widget:
        box = vbox(14)
        self.grid = Gtk.Grid()
        self.grid.set_column_spacing(28)
        self.grid.set_row_spacing(10)
        add_class(self.grid, "summary-grid")
        card = vbox(0)
        add_class(card, "summary-card")
        card.pack_start(self.grid, False, False, 0)
        box.pack_start(card, False, False, 0)
        self.notes = vbox(8)
        box.pack_start(self.notes, False, False, 0)
        self.steps_label = label("", "note", wrap=True)
        box.pack_start(self.steps_label, False, False, 0)
        return box

    def on_enter(self, ctx: PageContext) -> None:
        assert self.grid is not None and self.notes is not None and self.steps_label is not None
        for child in self.grid.get_children():
            self.grid.remove(child)
        for child in self.notes.get_children():
            self.notes.remove(child)
        rows = summarize(ctx.selections, ctx.mode_names(), ctx.browser_names(), ctx.accent_names(),
                         ctx.browser_states)
        for i, (key, value) in enumerate(rows):
            k = label(_(key), "summary-key")
            k.set_valign(Gtk.Align.START)
            v = label(value, "summary-value", wrap=True)
            v.set_max_width_chars(70)
            self.grid.attach(k, 0, i, 1, 1)
            self.grid.attach(v, 1, i, 1, 1)
        plan = build_plan(ctx.selections, browser_states=ctx.browser_states)
        ctx.plan = plan
        for note in plan.notes:
            self.notes.pack_start(InfoBanner(note, warn=True), False, False, 0)
        text = ""
        if plan.system_steps():
            text = _("Saving the Mode for the whole PC may ask for your password once.")
        if ctx.dry_run:
            text = (text + "  " if text else "") + _(
                "Dry run: nothing will be changed; the plan is printed to the terminal.")
        self.steps_label.set_text(text)
        self.grid.show_all()
        self.notes.show_all()


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------
class ApplyPage(Page):
    id = "apply"
    title = N_("Just a moment…")
    subtitle = APPLY_LINES[0]           # rotates through APPLY_LINES while the plan runs
    back_visible = False
    hero = True
    step_counted = False

    def __init__(self) -> None:
        super().__init__()
        self.spinner: Optional[Gtk.Spinner] = None
        self.progress: Optional[Gtk.ProgressBar] = None
        self.percent_label: Optional[Gtk.Label] = None
        self.step_label: Optional[Gtk.Label] = None
        self.textview: Optional[Gtk.TextView] = None
        self.buffer: Optional[Gtk.TextBuffer] = None
        self.details_btn: Optional[Gtk.Button] = None
        self.details: Optional[Gtk.Revealer] = None
        self.banner: Optional[InfoBanner] = None
        self.progress_model = ApplyProgress()
        self._started = False
        self._details_shown = False
        self._tick = 0
        self._timer_id = 0
        self._runner: Optional[Runner] = None

    def hero_image(self, ctx: PageContext) -> Optional[Gtk.Widget]:
        self.spinner = Gtk.Spinner()
        self.spinner.set_size_request(72, 72)
        add_class(self.spinner, "apply-spinner")
        set_a11y(self.spinner, _("Setting up your PC"))
        return self.spinner

    def build_content(self, ctx: PageContext) -> Gtk.Widget:
        box = vbox(12)
        self.step_label = label(_("Preparing…"), "apply-step", xalign=0.5, wrap=True,
                                justify=Gtk.Justification.CENTER)
        box.pack_start(self.step_label, False, False, 0)
        self.progress = Gtk.ProgressBar()
        self.progress.set_fraction(0.0)
        self.progress.set_size_request(400, -1)
        self.progress.set_halign(Gtk.Align.CENTER)
        add_class(self.progress, "apply-bar")
        box.pack_start(self.progress, False, False, 0)
        self.percent_label = label("0 %", "note", xalign=0.5)
        box.pack_start(self.percent_label, False, False, 0)

        # the log stays out of sight (it used to make this page look like a terminal)
        self.details_btn = Gtk.Button(label=_("Show details"))
        add_class(self.details_btn, "btn-link")
        self.details_btn.set_halign(Gtk.Align.CENTER)
        self.details_btn.connect("clicked", self._toggle_details)
        box.pack_start(self.details_btn, False, False, 0)
        self.textview = Gtk.TextView()
        self.textview.set_editable(False)
        self.textview.set_cursor_visible(False)
        self.textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.textview.set_left_margin(10)
        self.textview.set_right_margin(10)
        self.textview.set_top_margin(8)
        self.textview.set_bottom_margin(8)
        add_class(self.textview, "log-view")
        self.buffer = self.textview.get_buffer()
        self.details = Gtk.Revealer()
        self.details.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_shadow_type(Gtk.ShadowType.NONE)
        scroller.set_min_content_height(220)
        scroller.add(self.textview)
        self.details.add(scroller)
        self.details.set_reveal_child(False)
        box.pack_start(self.details, False, False, 0)

        self.banner = InfoBanner("", "emblem-ok-symbolic")
        self.banner.set_no_show_all(True)
        box.pack_start(self.banner, False, False, 0)
        return box

    def on_enter(self, ctx: PageContext) -> None:
        if ctx.window is not None:
            ctx.window.set_next_sensitive(False)
            ctx.window.set_back_visible(False)
        if self._started:
            return
        self._started = True
        if self.spinner is not None:
            self.spinner.start()
        self._timer_id = GLib.timeout_add_seconds(APPLY_LINE_SECONDS, self._rotate)
        GLib.idle_add(self._start)

    # -- friendly rotating lines / details -------------------------------------
    def _rotate(self) -> bool:
        """GLib timer: swap in the next friendly line; stops once the run has finished."""
        if self.ctx is not None and self.ctx.applied:
            self._timer_id = 0
            return False
        self._tick += 1
        if self.subtitle_label is not None:
            self.subtitle_label.set_text(apply_line(self._tick))
        return True

    def _stop_rotation(self) -> None:
        if self._timer_id:
            try:
                GLib.source_remove(self._timer_id)
            except Exception:  # noqa: BLE001 - the timer may already be gone
                pass
            self._timer_id = 0

    def _toggle_details(self, _btn: Gtk.Button) -> None:
        self._details_shown = not self._details_shown
        if self.details is not None:
            self.details.set_reveal_child(self._details_shown)
        if self.details_btn is not None:
            self.details_btn.set_label(_("Hide details") if self._details_shown else _("Show details"))

    # -- run ------------------------------------------------------------------
    def _start(self) -> bool:
        ctx = self.ctx
        assert ctx is not None
        try:
            plan = build_plan(ctx.selections, browser_states=ctx.browser_states)
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
        line = parse_batch_line(msg)
        if line is not None:
            GLib.idle_add(self._on_batch_line, line)

    def _on_step_start(self, index: int, total: int, step: Step) -> None:
        GLib.idle_add(self._show_step, index, total, step.title)

    def _on_step_done(self, index: int, total: int, step: Step, res: StepResult) -> None:
        GLib.idle_add(self._show_progress, index + 1, total)

    def _on_batch_line(self, line: BatchLine) -> bool:
        """A privileged step started/finished inside the helper batch (UI thread)."""
        if line.state == "start" and self.ctx is not None and self.ctx.plan is not None:
            step = self.ctx.plan.get(line.step_id)
            if step is not None and self.step_label is not None:
                self.step_label.set_text(step.title)
        self._set_fraction(self.progress_model.batch_line(line.state))
        return False

    def _show_step(self, index: int, total: int, title: str) -> bool:
        if self.step_label is not None:
            self.step_label.set_text(title)
        self._set_fraction(self.progress_model.step_started(index, total))
        return False

    def _show_progress(self, done: int, total: int) -> bool:
        self._set_fraction(self.progress_model.step_done(done, total))
        return False

    def _set_fraction(self, frac: float) -> None:
        frac = min(1.0, max(0.0, frac))
        if self.progress is not None:
            self.progress.set_fraction(frac)
        if self.percent_label is not None:
            self.percent_label.set_text("%d %%" % int(frac * 100))

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
        self._stop_rotation()
        if self.spinner is not None:
            self.spinner.stop()
            self.spinner.hide()
        self._set_fraction(self.progress_model.finish())
        problems = result.failed_ids + result.skipped_ids
        if self.step_label is not None:
            self.step_label.set_text(_("Finished — %s") % result.summary())
        if problems:
            self.set_titles(_("Setup finished, with a few things left to do"), "")
            self.banner.get_style_context().add_class("warn")
            self.banner.set_text(_(
                "Some choices could not be saved: %s. Your desktop is usable; you can change them "
                "later in Lindos Settings. Log: %s")
                % (", ".join(problems), core.log_file()))
        else:
            self.set_titles(_("Everything is in place"), _("Select Next to finish."))
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
    title = N_("All set")
    subtitle = N_("Welcome to Lindos — your PC is ready.")
    next_label = N_("Start using Lindos")
    back_visible = False
    hero = True
    step_counted = False

    def __init__(self) -> None:
        super().__init__()
        self.recap: Optional[Gtk.Label] = None
        self.settings_btn: Optional[Gtk.Button] = None
        self.installed_box: Optional[Gtk.Box] = None
        self.installed_rows: Optional[Gtk.Box] = None
        self.waiting_banner: Optional[InfoBanner] = None

    def hero_image(self, ctx: PageContext) -> Optional[Gtk.Widget]:
        badge = Gtk.Label(label="✓")
        add_class(badge, "done-check")
        badge.set_size_request(88, 88)
        set_a11y(badge, _("Setup complete"))
        return badge

    def build_content(self, ctx: PageContext) -> Gtk.Widget:
        box = vbox(18)
        self.recap = label("", "body", xalign=0.5, wrap=True, justify=Gtk.Justification.CENTER)
        box.pack_start(self.recap, False, False, 0)

        box.pack_start(section_title(_("A few Windows-style shortcuts")), False, False, 0)
        grid = Gtk.Grid()
        grid.set_column_spacing(12)
        grid.set_row_spacing(10)
        grid.set_halign(Gtk.Align.CENTER)
        per_column = (len(SHORTCUTS) + 1) // 2
        for i, (keys, meaning) in enumerate(SHORTCUTS):
            col, row = divmod(i, per_column)
            chip = label(keys, "kbd", xalign=0.5)
            chip.set_halign(Gtk.Align.START)
            grid.attach(chip, col * 2, row, 1, 1)
            grid.attach(label(_(meaning), "body"), col * 2 + 1, row, 1, 1)
        box.pack_start(grid, False, False, 0)

        # what the installer set up (read-only; filled in on_enter from install-state.json)
        self.installed_box = vbox(6)
        self.installed_box.pack_start(section_title(_("Set up while Lindos was installing")), False, False, 0)
        self.installed_rows = vbox(4)
        self.installed_box.pack_start(self.installed_rows, False, False, 0)
        self.installed_box.set_no_show_all(True)
        box.pack_start(self.installed_box, False, False, 0)
        self.waiting_banner = InfoBanner("", "network-wireless-symbolic", warn=True)
        self.waiting_banner.set_no_show_all(True)
        box.pack_start(self.waiting_banner, False, False, 0)

        # SPEC §0.1: the Wine / anti-cheat reality check, plainly, with the rest one click away
        box.pack_start(InfoBanner(_(WINE_HONESTY), "dialog-information-symbolic"), False, False, 0)
        box.pack_start(LearnMore(_(LEARN_MORE_TITLE), [_(t) for t in LEARN_MORE_LINES]), False, False, 0)
        self.settings_btn = Gtk.Button(label=_("Open Lindos Settings"))
        add_class(self.settings_btn, "btn-secondary")
        self.settings_btn.set_halign(Gtk.Align.CENTER)
        self.settings_btn.connect("clicked", self._open_settings)
        box.pack_start(self.settings_btn, False, False, 0)
        return box

    def on_enter(self, ctx: PageContext) -> None:
        if self.recap is not None:
            parts = [
                _("Mode: %s") % ctx.mode_name(ctx.selections.mode),
                self._browser_recap(ctx),
                _("Theme: %s") % (_("Dark") if ctx.selections.dark else _("Light")),
            ]
            if ctx.run_result is not None and (ctx.run_result.failed_ids or ctx.run_result.skipped_ids):
                parts.append(_("Some choices could not be saved — see Lindos Settings."))
            if (ctx.selections.transfer or {}).get("enabled"):
                parts.append(_("Opening the Transfer tool for your Windows files…"))
            self.recap.set_text("   ·   ".join(parts))
        self._show_installed(ctx)
        if self.settings_btn is not None:
            self.settings_btn.set_visible(core.which("lindos-settings") is not None or ctx.dry_run)
        if ctx.window is not None:
            ctx.window.set_next_sensitive(True)
        self._maybe_launch_transfer(ctx)

    @staticmethod
    def _browser_recap(ctx: PageContext) -> str:
        name = ctx.browser_name(ctx.selections.browser)
        if ctx.browser_states.get(ctx.selections.browser) == BROWSER_PENDING:
            return _("Browser: %s (Firefox until it's added)") % name
        return _("Browser: %s") % name

    def _show_installed(self, ctx: PageContext) -> None:
        """Read-only recap of what the installer did, from install-state.json (nothing is installed)."""
        if self.installed_box is None or self.installed_rows is None or self.waiting_banner is None:
            return
        rows = install_recap(ctx.install_steps)
        for child in list(self.installed_rows.get_children()):
            self.installed_rows.remove(child)
        for step, name, text in rows:
            mark = "✓" if ctx.install_steps.get(step) == "done" else "•"
            self.installed_rows.pack_start(label("%s  %s — %s" % (mark, _(name), _(text)), "body", wrap=True),
                                           False, False, 0)
        set_shown(self.installed_box, bool(rows))
        waiting = pending_steps(ctx.install_steps)
        if waiting:
            text = _("Still waiting for an internet connection: %s.") % ", ".join(
                _(INSTALL_STEP_NAMES[step]) for step in waiting)
            if {"browser", "drivers"} & set(waiting):
                text += " " + _("Lindos retries Chrome and drivers in the background.")
            if {"compat", "gaming", "mode_extras", "flatpaks"} & set(waiting):
                text += " " + _("Add the apps with Install now in Lindos Settings › Apps.")
            if "updates" in waiting:
                text += " " + _("Install system updates from the Update Manager.")
            self.waiting_banner.set_text(text)
        set_shown(self.waiting_banner, bool(waiting))

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
    pages: List[Page] = [WelcomePage(), ModePage(), BrowserPage(), PersonalizePage(),
                         PrivacyPage(), TransferPage(), SummaryPage(), ApplyPage(), DonePage()]
    assert [p.id for p in pages] == PAGE_ORDER
    return pages


__all__ = ["PAGE_ORDER", "PageContext", "Page", "make_pages", "WelcomePage", "ModePage",
           "BrowserPage", "PersonalizePage", "PrivacyPage", "TransferPage",
           "SummaryPage", "ApplyPage", "DonePage", "ApplyProgress", "BatchLine", "parse_batch_line",
           "apply_line", "APPLY_LINES", "WINE_HONESTY", "LEARN_MORE_TITLE", "LEARN_MORE_LINES"]
