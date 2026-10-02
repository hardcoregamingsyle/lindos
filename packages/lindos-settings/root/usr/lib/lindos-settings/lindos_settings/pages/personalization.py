"""Personalization page: theme (dark/light), accent swatches, wallpaper grid, taskbar
alignment/position, font size, cursor theme, Fonts / Lock screen shortcuts."""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

from .. import model
from ..widgets import HAVE_GTK, ButtonCard, Card, ComboCard, PageBase, add_class, box, button, choose_file, confirm, icon_image, label, run_async, set_widget_background

if HAVE_GTK:  # pragma: no cover
    from gi.repository import GdkPixbuf, Gtk  # type: ignore
else:  # pragma: no cover
    GdkPixbuf = Gtk = None  # type: ignore

log = logging.getLogger("lindos.settings.personalization")

THUMB_W, THUMB_H = 168, 96
_FONT_RE = re.compile(r"^(?P<family>.*?)\s*(?P<size>\d+(?:\.\d+)?)?$")


def split_font(name: str) -> tuple[str, float]:
    m = _FONT_RE.match((name or "").strip())
    if not m or not m.group("family"):
        return ("Selawik", 10.0)
    fam = m.group("family").strip() or "Selawik"
    try:
        size = float(m.group("size")) if m.group("size") else 10.0
    except ValueError:
        size = 10.0
    return fam, size


class PersonalizationPage(PageBase):
    PAGE_ID = "personalization"

    def build(self) -> None:
        b = self.backend
        # -- theme
        sec = self.add_section("Colours")
        self.theme_card = ComboCard(
            "Choose your mode",
            "Dark (default) uses Lindos-Dark for windows, icons and the taskbar",
            ("weather-clear-night",),
            ("theme", "dark", "light"),
            options=[("dark", "Dark"), ("light", "Light")],
            on_change=self._set_theme,
            active_id="dark" if b.is_dark() else "light",
        )
        sec.add(self.theme_card)

        # -- accent swatches
        self.accent_card = Card("Accent colour", "Used for highlights, switches and the Start button glow", ("preferences-color",), ("accent", "colour", "color", "highlight"))
        self.swatch_box = box("h", 8)
        self.swatch_box.set_halign(Gtk.Align.START)
        self.accent_card.add_body(self.swatch_box)
        self.accent_buttons: dict[str, Any] = {}
        self._build_swatches()
        sec.add(self.accent_card)

        # -- wallpaper
        wsec = self.add_section("Background")
        self.wall_card = Card("Wallpaper", "Pick a Lindos wallpaper or browse for your own picture", ("preferences-desktop-wallpaper",), ("wallpaper", "background", "picture", "image"))
        self.wall_card.set_control(button("Browse…", on_click=self._browse_wallpaper))
        self.flow = Gtk.FlowBox()
        self.flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.flow.set_max_children_per_line(6)
        self.flow.set_min_children_per_line(2)
        self.flow.set_row_spacing(8)
        self.flow.set_column_spacing(8)
        self.flow.set_homogeneous(True)
        self.flow.set_halign(Gtk.Align.START)
        add_class(self.flow, "wallpaper-grid")
        self.wall_card.add_body(self.flow)
        self.wall_buttons: dict[str, Any] = {}
        wsec.add(self.wall_card)
        self._load_wallpapers()

        # -- taskbar
        tsec = self.add_section("Taskbar")
        self.align_card = ComboCard("Taskbar alignment", model.taskbar_alignment_hint(b.taskbar_can_centre()), ("view-list-details",), ("taskbar", "panel", "center", "left"), options=[("center", "Center"), ("left", "Left")], on_change=self._set_alignment, active_id=b.taskbar_alignment())
        tsec.add(self.align_card)
        self.pos_card = ComboCard("Taskbar position", "Bottom (default) or top of the screen", ("view-list-details",), ("taskbar", "panel", "top", "bottom", "position"), options=[("bottom", "Bottom"), ("top", "Top")], on_change=self._set_position, active_id=b.taskbar_position())
        tsec.add(self.pos_card)

        # -- fonts / cursor
        fsec = self.add_section("Text and cursor")
        family, size = split_font(b.current_font())
        self._font_family = family
        self.font_card = Card("Text size", f"System font: {family}. Larger sizes make everything easier to read.", ("preferences-desktop-font",), ("font", "text", "size", "scaling", "dpi"))
        self.font_spin = Gtk.SpinButton.new_with_range(8, 20, 1)
        self.font_spin.set_value(int(round(size)))
        self.font_spin.set_valign(Gtk.Align.CENTER)
        self.font_spin.connect("value-changed", self._font_changed)
        self.font_card.set_control(self.font_spin)
        fsec.add(self.font_card)

        cursors = b.cursor_themes()
        cur = b.cursor_theme()
        opts = [(c, c) for c in cursors] or [(cur or "default", cur or "default")]
        self.cursor_card = ComboCard("Mouse pointer", "Cursor theme (Lindos-Cursors-Dark / Lindos-Cursors ship with Lindos)", ("input-mouse",), ("cursor", "pointer", "mouse"), options=opts, on_change=self._set_cursor, active_id=cur if cur in dict(opts) else None)
        fsec.add(self.cursor_card)

        fsec.add(ButtonCard("Fonts", "Font family, hinting and anti-aliasing (XFCE Appearance)", ("preferences-desktop-font",), ("hinting", "antialias", "font family"), "Open", self._open_fonts))
        self.lock_card = ButtonCard("Lock screen", "Lock timeout and behaviour (light-locker)", ("system-lock-screen",), ("lock", "screensaver", "light-locker"), "Open", self._open_lock)
        fsec.add(self.lock_card)

    # ------------------------------------------------------------------ swatches
    def _build_swatches(self) -> None:
        for child in self.swatch_box.get_children():
            self.swatch_box.remove(child)
        self.accent_buttons.clear()
        current = model.normalize_hex(self.backend.config_get("accent", "#60CDFF")) or "#60CDFF"
        for name, hx in self.backend.accents():
            btn = Gtk.Button()
            btn.set_size_request(40, 40)
            btn.set_tooltip_text(f"{name} ({hx})")
            add_class(btn, "accent-swatch")
            set_widget_background(btn, hx, 8)
            check = icon_image("object-select-symbolic", 18)
            check.set_no_show_all(True)
            btn.add(check)
            btn.connect("clicked", lambda _b, h=hx, n=name: self._set_accent(h, n))
            self.swatch_box.pack_start(btn, False, False, 0)
            self.accent_buttons[hx] = (btn, check)
        self._mark_accent(current)
        self.swatch_box.show_all()

    def _mark_accent(self, hx: str) -> None:
        for h, (btn, check) in self.accent_buttons.items():
            on = h.upper() == hx.upper()
            check.set_visible(on)
            if on:
                add_class(btn, "selected")
            else:
                btn.get_style_context().remove_class("selected")

    def _set_accent(self, hx: str, name: str) -> None:
        self._mark_accent(hx)

        def _done(ok: Any, exc: Optional[BaseException]) -> None:
            self.toast(f"Accent set to {name}" if ok and not exc else "Accent saved; apply needs lindos-core (theme.set_accent)")

        run_async(lambda: self.backend.set_accent(hx), _done, name="accent")

    # ------------------------------------------------------------------ wallpapers
    def _load_wallpapers(self) -> None:
        for child in self.flow.get_children():
            self.flow.remove(child)
        self.wall_buttons.clear()
        current = str(self.backend.config_get("wallpaper", "") or "")
        paths = self.backend.list_wallpapers()
        if not paths:
            self.flow.add(label("No wallpapers found in /usr/share/backgrounds/lindos", ("dim-label",)))
        for path in paths:
            self._add_wall_tile(path, path == current)
        self.flow.show_all()

    def _add_wall_tile(self, path: str, selected: bool) -> None:
        btn = Gtk.Button()
        add_class(btn, "wallpaper-tile")
        btn.set_tooltip_text(os.path.basename(path))
        vb = box("v", 4)
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, THUMB_W, THUMB_H, False)
            img = Gtk.Image.new_from_pixbuf(pb)
        except Exception as exc:  # broken / unsupported image
            log.debug("thumbnail failed for %s: %s", path, exc)
            img = icon_image("image-x-generic", 48)
            img.set_size_request(THUMB_W, THUMB_H)
        vb.pack_start(img, False, False, 0)
        name = os.path.splitext(os.path.basename(path))[0].replace("-", " ").replace("_", " ").title()
        vb.pack_start(label(name, ("dim-label",), xalign=0.5, ellipsize=True), False, False, 0)
        btn.add(vb)
        btn.connect("clicked", lambda _b, p=path: self._set_wallpaper(p))
        if selected:
            add_class(btn, "selected")
        self.flow.add(btn)
        self.wall_buttons[path] = btn

    def _set_wallpaper(self, path: str) -> None:
        for p, btn in self.wall_buttons.items():
            if p == path:
                add_class(btn, "selected")
            else:
                btn.get_style_context().remove_class("selected")

        def _done(ok: Any, exc: Optional[BaseException]) -> None:
            self.toast("Wallpaper changed" if ok and not exc else "Could not set wallpaper")

        run_async(lambda: self.backend.set_wallpaper(path), _done, name="wallpaper")

    def _browse_wallpaper(self) -> None:
        path = choose_file(self.app.window, "Choose a wallpaper", [("Images", ["*.png", "*.jpg", "*.jpeg", "*.svg", "*.webp", "*.bmp"])], os.path.expanduser("~/Pictures") if os.path.isdir(os.path.expanduser("~/Pictures")) else None)
        if not path:
            return
        if path not in self.wall_buttons:
            self._add_wall_tile(path, True)
            self.flow.show_all()
        self._set_wallpaper(path)

    # ------------------------------------------------------------------ other setters
    def _set_theme(self, value: str) -> None:
        dark = value == "dark"

        def _done(ok: Any, exc: Optional[BaseException]) -> None:
            self.toast(("Dark" if dark else "Light") + " mode applied" if ok and not exc else "Theme change failed")
            self.app.on_theme_changed()

        run_async(lambda: self.backend.set_dark(dark), _done, name="theme")

    def _set_alignment(self, value: str) -> None:
        run_async(lambda: self.backend.set_taskbar_alignment(value), lambda ok, exc: self.toast("Taskbar aligned " + value if ok and not exc else "Taskbar alignment needs lindos-core"), name="align")

    def _set_position(self, value: str) -> None:
        run_async(lambda: self.backend.set_taskbar_position(value), lambda ok, exc: self.toast("Taskbar moved to " + value if ok and not exc else "Taskbar position needs lindos-core"), name="position")

    def _font_changed(self, spin: Any) -> None:
        size = int(spin.get_value())
        name = f"{self._font_family} {size}"
        run_async(lambda: self.backend.set_font(name), lambda ok, exc: self.toast(f"Font set to {name}" if ok and not exc else "Font change failed"), name="font")

    def _set_cursor(self, value: str) -> None:
        run_async(lambda: self.backend.set_cursor_theme(value), lambda ok, exc: self.toast(f"Cursor theme: {value} (takes effect for new windows)" if ok and not exc else "Cursor change failed"), name="cursor")

    def _open_fonts(self) -> None:
        kind, value = model.which_or_install(["xfce4-appearance-settings"], "xfce4-settings", self.backend.which)
        if kind == "run":
            self.backend.spawn(value)
        else:
            self.toast("xfce4-appearance-settings is not installed")

    def _open_lock(self) -> None:
        kind, value = model.which_or_install(["light-locker-settings"], "light-locker-settings", self.backend.which)
        if kind == "run":
            self.backend.spawn(value)
            return
        if confirm(self.app.window, "Install light-locker-settings?", "The lock-screen settings tool is not installed. Install it now?", "Install"):
            run_async(lambda: self.backend.install_packages([str(value)]), lambda res, exc: self.toast("Installed — open Lock screen again" if res is not None and getattr(res, "ok", False) and not exc else "Install failed"), name="install-light-locker")

    def on_show(self) -> None:
        b = self.backend
        self.theme_card.set_active_id_silent("dark" if b.is_dark() else "light")
        self.align_card.set_active_id_silent(b.taskbar_alignment())
        self.pos_card.set_active_id_silent(b.taskbar_position())


__all__ = ["PersonalizationPage", "split_font"]
