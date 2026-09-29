"""GTK 3 widgets for the Lindos OOBE (cards, swatches, thumbnails, step indicator ...).

Styling lives in ``ui/oobe.css``; widgets only add CSS classes:

* ``.card`` / ``.card.selected``  -- selectable option card (:class:`Card`)
* ``.swatch`` / ``.swatch.selected`` -- accent colour circle (:class:`Swatch`)
* ``.thumb`` / ``.thumb.selected`` -- wallpaper thumbnail (:class:`WallpaperThumb`)
* ``.step-indicator`` / ``.step-bar`` / ``.step-text`` -- slim progress line (:class:`StepIndicator`)
* ``.check-row``, ``.switch-row``  -- list rows
* ``.learn-more``                  -- collapsed "Learn more" expander (:class:`LearnMore`)
* ``.theme-preview`` / ``.taskbar-preview`` -- tiny CSS-drawn previews used on option cards
"""
from __future__ import annotations

import logging
import os
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, GObject, Gtk, Pango  # noqa: E402

from .i18n import _  # noqa: E402

log = logging.getLogger("lindos-setup.widgets")

THUMB_W, THUMB_H = 192, 108
COLUMN_MAX_W = 760       # the centred content column of every page (Windows-OOBE-style whitespace)
COLUMN_MIN_W = 480
COLUMN_MARGIN = 48       # keep at least this much air on each side on small screens
_STYLE_PRIORITY_APPLICATION = 600      # GTK_STYLE_PROVIDER_PRIORITY_APPLICATION


def style_priority(offset: int = 0) -> int:
    """``GTK_STYLE_PROVIDER_PRIORITY_APPLICATION + offset`` (resolved at call
    time so the module also imports under the permissive test ``gi`` stub)."""
    try:
        base = int(Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    except (TypeError, ValueError):
        base = _STYLE_PRIORITY_APPLICATION
    return base + offset


# ---------------------------------------------------------------------------
# layout arithmetic (pure)
# ---------------------------------------------------------------------------
def column_width(screen_w: Optional[int] = None) -> int:
    """Width of the centred content column: 760 px, narrower only on very small screens."""
    try:
        screen = int(screen_w) if screen_w else 0
    except (TypeError, ValueError):
        screen = 0
    if screen <= 0:
        return COLUMN_MAX_W
    return max(COLUMN_MIN_W, min(COLUMN_MAX_W, screen - 2 * COLUMN_MARGIN))


def screen_width() -> Optional[int]:
    """Width of the default screen in pixels, or None when there is no display to ask."""
    try:
        screen = Gdk.Screen.get_default()
        if screen is None:
            return None
        width = int(screen.get_width())
    except Exception as exc:  # noqa: BLE001 - layout must never break start-up
        log.debug("screen width unavailable: %s", exc)
        return None
    return width if width > 0 else None


def step_position(counted: Sequence[bool], index: int) -> Optional[Tuple[int, int]]:
    """``(n, total)`` -- the 1-based position of page ``index`` among the pages that count as a
    step -- or None when that page is not one of them (welcome, apply and done are not)."""
    if index < 0 or index >= len(counted) or not counted[index]:
        return None
    total = sum(1 for c in counted if c)
    return sum(1 for c in counted[:index + 1] if c), total


def step_fraction(position: Optional[Tuple[int, int]]) -> float:
    if position is None:
        return 0.0
    n, total = position
    if total <= 0:
        return 0.0
    return min(1.0, max(0.0, n / float(total)))


def step_text(position: Optional[Tuple[int, int]]) -> str:
    if position is None:
        return ""
    return _("Step %d of %d") % position


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def add_class(widget: Gtk.Widget, *classes: str) -> Gtk.Widget:
    ctx = widget.get_style_context()
    for c in classes:
        ctx.add_class(c)
    return widget


def set_selected_class(widget: Gtk.Widget, selected: bool) -> None:
    ctx = widget.get_style_context()
    if selected:
        ctx.add_class("selected")
    else:
        ctx.remove_class("selected")


def set_a11y(widget: Gtk.Widget, name: str, description: str = "") -> Gtk.Widget:
    """Give a widget an accessible name/description (screen readers); never raises."""
    try:
        acc = widget.get_accessible()
        if acc is not None:
            if name:
                acc.set_name(name)
            acc.set_description(description or "")
    except Exception as exc:  # noqa: BLE001 - a11y is best effort
        log.debug("cannot set accessible name %r: %s", name, exc)
    return widget


def label(text: str, *classes: str, xalign: float = 0.0, wrap: bool = False,
          max_chars: int = 0, justify: Optional[Gtk.Justification] = None,
          selectable: bool = False) -> Gtk.Label:
    lbl = Gtk.Label(label=text)
    lbl.set_xalign(xalign)
    lbl.set_line_wrap(wrap)
    if wrap:
        lbl.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        # bound the natural width so a long paragraph never stretches the content column
        lbl.set_max_width_chars(max_chars or 70)
    elif max_chars:
        lbl.set_max_width_chars(max_chars)
    if justify is not None:
        lbl.set_justify(justify)
    lbl.set_selectable(selectable)
    add_class(lbl, *classes)
    return lbl


def hbox(spacing: int = 8) -> Gtk.Box:
    return Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=spacing)


def vbox(spacing: int = 8) -> Gtk.Box:
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)


def section_title(text: str) -> Gtk.Label:
    return label(text, "section-title")


def load_css_file(path: str, priority: Optional[int] = None) -> Optional[Gtk.CssProvider]:
    """Load a CSS file for the default screen; returns the provider or None."""
    if priority is None:
        priority = style_priority(0)
    provider = Gtk.CssProvider()
    if not os.path.isfile(path):
        log.error("CSS file %s not found", path)
        return None
    try:
        provider.load_from_path(path)
    except GLib.Error as exc:
        # GTK keeps every rule it could parse; only the offending one is dropped
        log.warning("CSS %s has a parse problem (rules before/after it still apply): %s", path, exc)
    screen = Gdk.Screen.get_default()
    if screen is None:
        log.error("no default screen; CSS not applied")
        return None
    Gtk.StyleContext.add_provider_for_screen(screen, provider, priority)
    return provider


def load_svg_thumbnail(path: str, width: int = THUMB_W, height: int = THUMB_H) -> Optional[GdkPixbuf.Pixbuf]:
    """Render an SVG/PNG/JPG file to a pixbuf of exactly ``width``x``height``.

    Wide art is scaled to cover the box (centre-cropped) so thumbnails look
    like the real desktop.  Returns None when the file cannot be loaded.
    """
    if not path or not os.path.isfile(path):
        return None
    try:
        # scale so that the shorter side matches, then crop the centre
        base = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, width, -1, True)
        if base is None:
            return None
        if base.get_height() < height:
            base = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, -1, height, True)
        if base is None:
            return None
        bw, bh = base.get_width(), base.get_height()
        x = max(0, (bw - width) // 2)
        y = max(0, (bh - height) // 2)
        cw = min(width, bw)
        ch = min(height, bh)
        if cw == width and ch == height and (bw != width or bh != height):
            return base.new_subpixbuf(x, y, cw, ch).copy()
        if bw == width and bh == height:
            return base
        return base.scale_simple(width, height, GdkPixbuf.InterpType.BILINEAR)
    except (GLib.Error, Exception) as exc:  # noqa: BLE001 - GdkPixbuf raises GLib.Error
        log.warning("thumbnail: cannot render %s: %s", path, exc)
        return None


def icon_image(icon_name: Optional[str], size: int = 48, fallback: str = "image-missing") -> Gtk.Image:
    """Themed icon at pixel ``size``; falls back to a generic icon name."""
    name = icon_name or fallback
    theme = Gtk.IconTheme.get_default()
    if theme is not None and not theme.has_icon(name):
        # try a symbolic variant, then the fallback
        if theme.has_icon(name + "-symbolic"):
            name = name + "-symbolic"
        elif fallback and theme.has_icon(fallback):
            name = fallback
    img = Gtk.Image.new_from_icon_name(name, Gtk.IconSize.DIALOG)
    img.set_pixel_size(size)
    return img


# ---------------------------------------------------------------------------
# accent CSS override (live)
# ---------------------------------------------------------------------------
_ACCENT_TEMPLATE = """
@define-color lindos_accent {hex};
.oobe .btn-next {{ background-color: {hex}; color: #000000; }}
.oobe .btn-next:hover {{ background-color: shade({hex}, 1.08); }}
.oobe .btn-next:active {{ background-color: shade({hex}, 0.92); }}
.oobe .btn-next:disabled {{ background-color: #4A4A4A; color: #8A8A8A; }}
.oobe.light .btn-next:disabled {{ background-color: #D6D6D6; color: #8A8A8A; }}
.oobe .card.selected {{ border-color: {hex}; background-color: alpha({hex}, 0.14); }}
.oobe .card-check, .oobe .card-badge {{ background-color: {hex}; }}
.oobe .thumb.selected {{ border-color: {hex}; }}
.oobe .accent-text, .oobe .card-hint, .oobe .btn-link, .oobe .learn-more title label {{ color: {hex}; }}
.oobe progressbar progress {{ background-color: {hex}; }}
.oobe spinner {{ color: {hex}; }}
.oobe .theme-preview-accent {{ background-color: {hex}; }}
.oobe .done-check {{ color: {hex}; background-color: alpha({hex}, 0.16); }}
.oobe switch:checked {{ background-color: {hex}; border-color: {hex}; }}
.oobe checkbutton check:checked {{ background-color: {hex}; border-color: {hex}; }}
"""


class AccentCss:
    """(Re)loads a tiny CSS provider with the chosen accent colour."""

    def __init__(self, priority: Optional[int] = None) -> None:
        self.priority = style_priority(1) if priority is None else priority
        self.provider: Optional[Gtk.CssProvider] = None
        self.hex = ""

    def apply(self, hex_colour: str) -> None:
        screen = Gdk.Screen.get_default()
        if screen is None:
            return
        if self.provider is not None:
            Gtk.StyleContext.remove_provider_for_screen(screen, self.provider)
            self.provider = None
        provider = Gtk.CssProvider()
        try:
            provider.load_from_data(_ACCENT_TEMPLATE.format(hex=hex_colour).encode("utf-8"))
        except GLib.Error as exc:
            log.warning("accent css parse problem for %s (partial rules kept): %s", hex_colour, exc)
        Gtk.StyleContext.add_provider_for_screen(screen, provider, self.priority)
        self.provider = provider
        self.hex = hex_colour


# ---------------------------------------------------------------------------
# selectable card
# ---------------------------------------------------------------------------
class Card(Gtk.Button):
    """A Windows-11-style selectable option card.

    Emits ``chosen`` (key) when clicked; selection visuals via ``.selected``.
    ``preview`` replaces the icon with any widget (see :func:`theme_preview`).
    ``horizontal=True`` lays the card out as a wide list row (icon left, check right).
    """

    __gsignals__ = {"chosen": (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    def __init__(self, key: str, title: str, description: str = "", *,
                 icon_name: Optional[str] = None, pixbuf: Optional[GdkPixbuf.Pixbuf] = None,
                 preview: Optional[Gtk.Widget] = None,
                 hint: str = "", badge: str = "", icon_size: int = 40,
                 horizontal: bool = False, width: int = -1, height: int = -1) -> None:
        super().__init__()
        self.key = key
        self.title = title
        self.selected = False
        self.set_relief(Gtk.ReliefStyle.NONE)
        self.set_can_focus(True)
        self.set_focus_on_click(True)
        add_class(self, "card")
        if horizontal:
            add_class(self, "card-row")
        if width > 0 or height > 0:
            self.set_size_request(width, height)

        outer = hbox(16) if horizontal else vbox(10)
        outer.set_border_width(4)

        if preview is not None:
            img: Gtk.Widget = preview
        elif pixbuf is not None:
            img = Gtk.Image.new_from_pixbuf(pixbuf)
        else:
            img = icon_image(icon_name, icon_size)
        img.set_halign(Gtk.Align.START)
        img.set_valign(Gtk.Align.CENTER if horizontal else Gtk.Align.START)
        add_class(img, "card-icon")
        outer.pack_start(img, False, False, 0)

        text = vbox(3)
        text.set_valign(Gtk.Align.CENTER)
        head = hbox(8)
        chars = 64 if horizontal else 24
        self.title_label = label(title, "card-title", wrap=True, max_chars=chars)
        head.pack_start(self.title_label, True, True, 0)
        # a round check mark that shows in the selected state (far right on list rows)
        self.check = Gtk.Label(label="✓")
        add_class(self.check, "card-check")
        self.check.set_no_show_all(True)
        if horizontal:
            self.check.set_valign(Gtk.Align.CENTER)
        else:
            self.check.set_valign(Gtk.Align.START)
            self.check.set_halign(Gtk.Align.END)
            head.pack_end(self.check, False, False, 0)
        if badge:
            self.badge_label = label(badge, "card-badge")
            self.badge_label.set_valign(Gtk.Align.CENTER)
            head.pack_end(self.badge_label, False, False, 0)
        text.pack_start(head, False, False, 0)
        if description:
            self.desc_label = label(description, "card-desc", wrap=True, max_chars=chars + 6)
            text.pack_start(self.desc_label, False, False, 0)
        if hint:
            self.hint_label = label(hint, "card-hint", wrap=True, max_chars=chars + 4)
            text.pack_start(self.hint_label, False, False, 0)
        outer.pack_start(text, True, True, 0)
        if horizontal:
            outer.pack_end(self.check, False, False, 0)

        self.add(outer)
        set_a11y(self, title, description)
        self.connect("clicked", self._on_clicked)

    def _on_clicked(self, _btn: Gtk.Button) -> None:
        self.emit("chosen", self.key)

    def set_selected(self, selected: bool) -> None:
        self.selected = bool(selected)
        set_selected_class(self, self.selected)
        self.check.set_visible(self.selected)

    def set_disabled(self, disabled: bool, reason: str = "") -> None:
        self.set_sensitive(not disabled)
        if reason:
            self.set_tooltip_text(reason)
        elif not disabled:
            self.set_tooltip_text(None)


class CardGroup:
    """Exclusive selection among :class:`Card` (or any widget with ``key`` and
    ``set_selected``) instances."""

    def __init__(self, on_change: Optional[Callable[[str], None]] = None) -> None:
        self.cards: Dict[str, Gtk.Widget] = {}
        self.order: List[str] = []
        self.selected: Optional[str] = None
        self.on_change = on_change

    def add(self, card: Gtk.Widget) -> Gtk.Widget:
        key = getattr(card, "key")
        self.cards[key] = card
        self.order.append(key)
        card.connect("chosen", self._on_chosen)
        return card

    def _on_chosen(self, _card: Gtk.Widget, key: str) -> None:
        self.select(key, notify=True)

    def select(self, key: Optional[str], notify: bool = False) -> None:
        if key is not None and key not in self.cards:
            log.debug("CardGroup.select: unknown key %r", key)
            return
        for k, c in self.cards.items():
            c.set_selected(k == key)
        changed = self.selected != key
        self.selected = key
        if notify and changed and self.on_change is not None and key is not None:
            self.on_change(key)

    def set_disabled(self, key: str, disabled: bool, reason: str = "") -> None:
        card = self.cards.get(key)
        if card is not None and hasattr(card, "set_disabled"):
            card.set_disabled(disabled, reason)


# ---------------------------------------------------------------------------
# CSS-drawn previews for option cards
# ---------------------------------------------------------------------------
PREVIEW_W, PREVIEW_H = 148, 84


def theme_preview(kind: str) -> Gtk.Widget:
    """A tiny desktop -- wallpaper, one window with an accent bar, a taskbar strip.

    ``kind`` is ``"dark"`` or ``"light"``; everything is drawn by CSS classes so it follows
    the live accent colour.
    """
    frame = vbox(0)
    add_class(frame, "theme-preview", "theme-preview-" + ("light" if kind == "light" else "dark"))
    frame.set_size_request(PREVIEW_W, PREVIEW_H)
    win = vbox(0)
    add_class(win, "theme-preview-window")
    win.set_size_request(76, 34)
    win.set_halign(Gtk.Align.START)
    win.set_margin_start(16)
    win.set_margin_top(14)
    bar = Gtk.Box()
    add_class(bar, "theme-preview-accent")
    bar.set_size_request(30, 6)
    bar.set_halign(Gtk.Align.START)
    bar.set_margin_start(8)
    bar.set_margin_top(8)
    win.pack_start(bar, False, False, 0)
    frame.pack_start(win, False, False, 0)
    strip = Gtk.Box()
    add_class(strip, "theme-preview-taskbar")
    strip.set_size_request(-1, 12)
    frame.pack_end(strip, False, False, 0)
    return frame


def taskbar_preview(alignment: str) -> Gtk.Widget:
    """A tiny taskbar with four app icons, centred (Windows 11) or left-aligned (classic)."""
    frame = hbox(0)
    add_class(frame, "taskbar-preview")
    frame.set_size_request(PREVIEW_W, 34)
    icons = hbox(6)
    icons.set_valign(Gtk.Align.CENTER)
    for _i in range(4):
        dot = Gtk.Box()
        add_class(dot, "taskbar-preview-icon")
        dot.set_size_request(14, 14)
        icons.pack_start(dot, False, False, 0)
    if alignment == "left":
        icons.set_halign(Gtk.Align.START)
        icons.set_margin_start(10)
    else:
        icons.set_halign(Gtk.Align.CENTER)
    frame.pack_start(icons, True, True, 0)
    return frame


# ---------------------------------------------------------------------------
# accent swatch
# ---------------------------------------------------------------------------
class Swatch(Gtk.Button):
    """A round colour swatch button (``.swatch``)."""

    __gsignals__ = {"chosen": (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    def __init__(self, key: str, hex_colour: str, name: str = "", size: int = 36) -> None:
        super().__init__()
        self.key = key
        self.hex = hex_colour
        self.selected = False
        self.set_relief(Gtk.ReliefStyle.NONE)
        self.set_size_request(size, size)
        self.set_tooltip_text("%s %s" % (name, hex_colour) if name else hex_colour)
        set_a11y(self, name or hex_colour, hex_colour)
        add_class(self, "swatch")
        self._provider = Gtk.CssProvider()
        css = ".swatch.swatch-%s { background-color: %s; }" % (key.replace("#", ""), hex_colour)
        try:
            self._provider.load_from_data(css.encode("utf-8"))
            self.get_style_context().add_provider(self._provider, style_priority(2))
            add_class(self, "swatch-%s" % key.replace("#", ""))
        except GLib.Error as exc:
            log.warning("swatch css failed: %s", exc)
        self.connect("clicked", lambda _b: self.emit("chosen", self.key))

    def set_selected(self, selected: bool) -> None:
        self.selected = bool(selected)
        set_selected_class(self, self.selected)


# ---------------------------------------------------------------------------
# wallpaper thumbnail
# ---------------------------------------------------------------------------
class WallpaperThumb(Gtk.Button):
    """192x108 thumbnail rendered from the wallpaper (SVG via GdkPixbuf)."""

    __gsignals__ = {"chosen": (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    def __init__(self, path: str, display_name: str, width: int = THUMB_W, height: int = THUMB_H) -> None:
        super().__init__()
        self.key = path
        self.path = path
        self.selected = False
        self.set_relief(Gtk.ReliefStyle.NONE)
        add_class(self, "thumb")
        self.set_tooltip_text(display_name)
        set_a11y(self, display_name)
        box = vbox(4)
        pixbuf = load_svg_thumbnail(path, width, height)
        if pixbuf is not None:
            image: Gtk.Widget = Gtk.Image.new_from_pixbuf(pixbuf)
        else:
            # honest placeholder when the art is missing (e.g. running from the repo)
            image = Gtk.Label(label=display_name)
            add_class(image, "thumb-missing")
            image.set_size_request(width, height)
        add_class(image, "thumb-image")
        box.pack_start(image, False, False, 0)
        name = label(display_name, "thumb-name", xalign=0.5)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_max_width_chars(22)
        box.pack_start(name, False, False, 0)
        self.add(box)
        self.connect("clicked", lambda _b: self.emit("chosen", self.key))

    def set_selected(self, selected: bool) -> None:
        self.selected = bool(selected)
        set_selected_class(self, self.selected)


# ---------------------------------------------------------------------------
# slim step indicator
# ---------------------------------------------------------------------------
class StepIndicator(Gtk.Box):
    """A thin progress line with a small "Step 3 of 7" caption.

    Invisible (but still taking its space, so nothing jumps) on pages that are not a
    numbered step -- see :func:`step_position`.
    """

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        add_class(self, "step-indicator")
        self.bar = Gtk.ProgressBar()
        add_class(self.bar, "step-bar")
        self.bar.set_valign(Gtk.Align.CENTER)
        self.bar.set_fraction(0.0)
        self.pack_start(self.bar, True, True, 0)
        self.text = label("", "step-text", xalign=1.0)
        self.text.set_valign(Gtk.Align.CENTER)
        self.pack_end(self.text, False, False, 0)
        self.position: Optional[Tuple[int, int]] = None
        self.fraction = 0.0
        self.set_opacity(0.0)
        set_a11y(self, _("Setup progress"))

    def set_step(self, position: Optional[Tuple[int, int]]) -> None:
        self.position = position
        self.fraction = step_fraction(position)
        self.bar.set_fraction(self.fraction)
        self.text.set_text(step_text(position))
        self.set_opacity(0.0 if position is None else 1.0)
        set_a11y(self, _("Setup progress"), step_text(position))


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------
class CheckRow(Gtk.CheckButton):
    """Check button with a bold title and a wrapped description."""

    def __init__(self, key: str, title: str, description: str = "", active: bool = False) -> None:
        super().__init__()
        self.key = key
        add_class(self, "check-row")
        box = vbox(2)
        box.pack_start(label(title, "row-title", wrap=True), False, False, 0)
        if description:
            box.pack_start(label(description, "row-desc", wrap=True), False, False, 0)
        self.add(box)
        self.set_active(active)
        set_a11y(self, title, description)


class SwitchRow(Gtk.Box):
    """``[title / description]  ..........  [Gtk.Switch]``."""

    def __init__(self, title: str, description: str = "", active: bool = False,
                 on_toggle: Optional[Callable[[bool], None]] = None,
                 on_label: str = "", off_label: str = "") -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        add_class(self, "switch-row")
        text = vbox(3)
        text.set_valign(Gtk.Align.CENTER)
        text.pack_start(label(title, "row-title", wrap=True), False, False, 0)
        if description:
            text.pack_start(label(description, "row-desc", wrap=True), False, False, 0)
        self.pack_start(text, True, True, 0)
        self.state_label = label(on_label if active else off_label, "row-state")
        self.state_label.set_valign(Gtk.Align.CENTER)
        self._on_label, self._off_label = on_label, off_label
        if on_label or off_label:
            self.pack_start(self.state_label, False, False, 0)
        self.switch = Gtk.Switch()
        self.switch.set_valign(Gtk.Align.CENTER)
        self.switch.set_active(active)
        set_a11y(self.switch, title, description)
        self.pack_end(self.switch, False, False, 0)
        self._on_toggle = on_toggle
        self.switch.connect("notify::active", self._changed)

    def _changed(self, sw: Gtk.Switch, _pspec: object) -> None:
        active = sw.get_active()
        self.state_label.set_text(self._on_label if active else self._off_label)
        if self._on_toggle is not None:
            self._on_toggle(active)

    def get_active(self) -> bool:
        return self.switch.get_active()

    def set_active(self, active: bool) -> None:
        self.switch.set_active(active)


class InfoBanner(Gtk.Box):
    """Rounded note box with an icon (``.banner``, ``.banner.warn``)."""

    def __init__(self, text: str, icon_name: str = "dialog-information-symbolic", warn: bool = False) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        add_class(self, "banner")
        if warn:
            add_class(self, "warn")
        img = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU)
        img.set_valign(Gtk.Align.START)
        self.pack_start(img, False, False, 0)
        self.text_label = label(text, "banner-text", wrap=True)
        self.pack_start(self.text_label, True, True, 0)

    def set_text(self, text: str) -> None:
        self.text_label.set_text(text)


class LearnMore(Gtk.Expander):
    """A collapsed "Learn more" disclosure holding a few wrapped paragraphs.

    Used to keep the long honesty text (Wine is not Windows, anti-cheat) one click away
    instead of a wall of text on the page.
    """

    def __init__(self, summary: str, paragraphs: Sequence[str]) -> None:
        super().__init__(label=summary)
        add_class(self, "learn-more")
        self.paragraphs = list(paragraphs)
        body = vbox(8)
        add_class(body, "learn-more-body")
        for para in self.paragraphs:
            body.pack_start(label(para, "learn-more-text", wrap=True), False, False, 0)
        self.add(body)
        set_a11y(self, summary)


def scrolled(child: Gtk.Widget, height: int = -1, hpolicy: Gtk.PolicyType = Gtk.PolicyType.NEVER) -> Gtk.ScrolledWindow:
    sw = Gtk.ScrolledWindow()
    sw.set_policy(hpolicy, Gtk.PolicyType.AUTOMATIC)
    sw.set_shadow_type(Gtk.ShadowType.NONE)
    if height > 0:
        sw.set_min_content_height(height)
    sw.add(child)
    return sw


__all__ = [
    "THUMB_W", "THUMB_H", "COLUMN_MAX_W", "COLUMN_MIN_W", "style_priority", "column_width", "screen_width",
    "step_position", "step_fraction", "step_text", "add_class", "set_a11y", "label", "hbox", "vbox",
    "section_title", "load_css_file", "load_svg_thumbnail", "icon_image", "AccentCss", "Card", "CardGroup",
    "theme_preview", "taskbar_preview", "Swatch", "WallpaperThumb", "StepIndicator", "CheckRow",
    "SwitchRow", "InfoBanner", "LearnMore", "scrolled",
]
