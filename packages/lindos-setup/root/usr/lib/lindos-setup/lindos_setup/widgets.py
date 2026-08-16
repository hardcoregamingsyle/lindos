"""GTK 3 widgets for the Lindos OOBE (cards, swatches, thumbnails, dots ...).

Styling lives in ``ui/oobe.css``; widgets only add CSS classes:

* ``.card`` / ``.card.selected``  -- selectable option card (:class:`Card`)
* ``.swatch`` / ``.swatch.selected`` -- accent colour circle (:class:`Swatch`)
* ``.thumb`` / ``.thumb.selected`` -- wallpaper thumbnail (:class:`WallpaperThumb`)
* ``.dot`` / ``.dot.active``        -- step indicator (:class:`StepDots`)
* ``.check-row``, ``.switch-row``  -- list rows
"""
from __future__ import annotations

import logging
import os
from typing import Callable, Dict, List, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, GObject, Gtk, Pango  # noqa: E402

log = logging.getLogger("lindos-setup.widgets")

THUMB_W, THUMB_H = 192, 108
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


def label(text: str, *classes: str, xalign: float = 0.0, wrap: bool = False,
          max_chars: int = 0, justify: Optional[Gtk.Justification] = None,
          selectable: bool = False) -> Gtk.Label:
    lbl = Gtk.Label(label=text)
    lbl.set_xalign(xalign)
    lbl.set_line_wrap(wrap)
    if wrap:
        lbl.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        # bound the natural width so the 900 px card never grows past its size
        lbl.set_max_width_chars(max_chars or 60)
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
.oobe .card.selected {{ border-color: {hex}; background-color: alpha({hex}, 0.10); }}
.oobe .card-check, .oobe .card-badge {{ background-color: {hex}; }}
.oobe .thumb.selected {{ border-color: {hex}; }}
.oobe .dot.active {{ background-color: {hex}; }}
.oobe .accent-text, .oobe .card-hint, .oobe .btn-link {{ color: {hex}; }}
.oobe progressbar progress {{ background-color: {hex}; }}
.oobe switch:checked {{ background-color: {hex}; border-color: {hex}; }}
.oobe checkbutton check:checked, .oobe radiobutton radio:checked {{ background-color: {hex}; border-color: {hex}; }}
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
    """

    __gsignals__ = {"chosen": (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    def __init__(self, key: str, title: str, description: str = "", *,
                 icon_name: Optional[str] = None, pixbuf: Optional[GdkPixbuf.Pixbuf] = None,
                 hint: str = "", badge: str = "", icon_size: int = 40,
                 horizontal: bool = False, width: int = -1, height: int = -1) -> None:
        super().__init__()
        self.key = key
        self.selected = False
        self.set_relief(Gtk.ReliefStyle.NONE)
        self.set_can_focus(True)
        self.set_focus_on_click(True)
        add_class(self, "card")
        if width > 0 or height > 0:
            self.set_size_request(width, height)

        outer = hbox(12) if horizontal else vbox(6)
        outer.set_border_width(2)

        if pixbuf is not None:
            img: Gtk.Widget = Gtk.Image.new_from_pixbuf(pixbuf)
        else:
            img = icon_image(icon_name, icon_size)
        img.set_halign(Gtk.Align.START)
        img.set_valign(Gtk.Align.START)
        add_class(img, "card-icon")
        outer.pack_start(img, False, False, 0)

        text = vbox(2)
        head = hbox(6)
        chars = 40 if horizontal else 18
        self.title_label = label(title, "card-title", wrap=True, max_chars=chars)
        head.pack_start(self.title_label, True, True, 0)
        # a small check mark (top-right) that shows in the selected state
        self.check = Gtk.Label(label="✓")
        add_class(self.check, "card-check")
        self.check.set_valign(Gtk.Align.START)
        self.check.set_halign(Gtk.Align.END)
        self.check.set_no_show_all(True)
        head.pack_end(self.check, False, False, 0)
        if badge:
            self.badge_label = label(badge, "card-badge")
            self.badge_label.set_valign(Gtk.Align.START)
            head.pack_end(self.badge_label, False, False, 0)
        text.pack_start(head, False, False, 0)
        if description:
            self.desc_label = label(description, "card-desc", wrap=True, max_chars=chars + 6)
            text.pack_start(self.desc_label, True, True, 0)
        if hint:
            self.hint_label = label(hint, "card-hint", wrap=True, max_chars=chars + 4)
            text.pack_end(self.hint_label, False, False, 0)
        outer.pack_start(text, True, True, 0)

        self.add(outer)
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
# accent swatch
# ---------------------------------------------------------------------------
class Swatch(Gtk.Button):
    """A round colour swatch button (``.swatch``)."""

    __gsignals__ = {"chosen": (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    def __init__(self, key: str, hex_colour: str, name: str = "", size: int = 30) -> None:
        super().__init__()
        self.key = key
        self.hex = hex_colour
        self.selected = False
        self.set_relief(Gtk.ReliefStyle.NONE)
        self.set_size_request(size, size)
        self.set_tooltip_text("%s %s" % (name, hex_colour) if name else hex_colour)
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
# step dots
# ---------------------------------------------------------------------------
class StepDots(Gtk.Box):
    """Row of small dots, one per wizard page; the active one is wider/accent."""

    def __init__(self, count: int) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self.dots: List[Gtk.Widget] = []
        for _ in range(count):
            d = Gtk.Box()
            add_class(d, "dot")
            d.set_size_request(6, 6)
            self.pack_start(d, False, False, 0)
            self.dots.append(d)
        self.active = -1

    def set_active(self, index: int) -> None:
        self.active = index
        for i, d in enumerate(self.dots):
            ctx = d.get_style_context()
            if i == index:
                ctx.add_class("active")
                d.set_size_request(16, 6)
            else:
                ctx.remove_class("active")
                d.set_size_request(6, 6)


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


class SwitchRow(Gtk.Box):
    """``[title / description]  ..........  [Gtk.Switch]``."""

    def __init__(self, title: str, description: str = "", active: bool = False,
                 on_toggle: Optional[Callable[[bool], None]] = None,
                 on_label: str = "", off_label: str = "") -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        add_class(self, "switch-row")
        text = vbox(2)
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
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
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


def scrolled(child: Gtk.Widget, height: int = -1, hpolicy: Gtk.PolicyType = Gtk.PolicyType.NEVER) -> Gtk.ScrolledWindow:
    sw = Gtk.ScrolledWindow()
    sw.set_policy(hpolicy, Gtk.PolicyType.AUTOMATIC)
    sw.set_shadow_type(Gtk.ShadowType.NONE)
    if height > 0:
        sw.set_min_content_height(height)
    sw.add(child)
    return sw


__all__ = [
    "THUMB_W", "THUMB_H", "style_priority", "add_class", "label", "hbox", "vbox", "section_title", "load_css_file",
    "load_svg_thumbnail", "icon_image", "AccentCss", "Card", "CardGroup", "Swatch",
    "WallpaperThumb", "StepDots", "CheckRow", "SwitchRow", "InfoBanner", "scrolled",
]
