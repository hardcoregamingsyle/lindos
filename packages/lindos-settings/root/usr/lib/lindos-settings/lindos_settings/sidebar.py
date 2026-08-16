"""Left navigation column: avatar + user + mode caption, then one row per page (Win11 order)."""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from . import model
from .widgets import HAVE_GTK, add_class, box, icon_image, label

if HAVE_GTK:  # pragma: no cover
    from gi.repository import GdkPixbuf, Gtk  # type: ignore
else:  # pragma: no cover
    GdkPixbuf = Gtk = None  # type: ignore

log = logging.getLogger("lindos.settings.sidebar")

SIDEBAR_WIDTH = 260


class Sidebar:
    def __init__(self, pages: list[model.Page], backend: Any, on_select: Callable[[str], None]):
        self.pages = pages
        self.backend = backend
        self.on_select = on_select
        self._rows: dict[str, Any] = {}
        self._silent = False

        self.widget = box("v", 0, ("lindos-sidebar",))
        self.widget.set_size_request(SIDEBAR_WIDTH, -1)
        self.widget.set_hexpand(False)

        # -- profile block
        profile = box("h", 12, ("sidebar-profile",))
        profile.set_margin_top(16)
        profile.set_margin_bottom(8)
        profile.set_margin_start(16)
        profile.set_margin_end(12)
        self.avatar = self._build_avatar()
        profile.pack_start(self.avatar, False, False, 0)
        names = box("v", 2)
        names.set_valign(Gtk.Align.CENTER)
        self.name_label = label(backend.user_display_name(), ("sidebar-name",), ellipsize=True)
        self.user_label = label(backend.username(), ("dim-label", "sidebar-user"), ellipsize=True)
        self.mode_label = label("", ("sidebar-mode", "dim-label"), ellipsize=True)
        names.pack_start(self.name_label, False, False, 0)
        names.pack_start(self.user_label, False, False, 0)
        names.pack_start(self.mode_label, False, False, 0)
        profile.pack_start(names, True, True, 0)
        self.widget.pack_start(profile, False, False, 0)
        self.update_mode()

        # -- page list
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.set_vexpand(True)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        add_class(self.listbox, "sidebar-list")
        self.listbox.set_margin_start(8)
        self.listbox.set_margin_end(8)
        self.listbox.set_margin_top(4)
        for p in pages:
            row = self._build_row(p)
            self._rows[p.id] = row
            self.listbox.add(row)
        self.listbox.connect("row-selected", self._row_selected)
        sw.add(self.listbox)
        self.widget.pack_start(sw, True, True, 0)

        # -- footer (version)
        foot = label("Lindos Settings 1.0 · Aurora", ("dim-label", "sidebar-footer"))
        foot.set_margin_start(16)
        foot.set_margin_bottom(10)
        foot.set_margin_top(6)
        self.widget.pack_end(foot, False, False, 0)
        self.widget.show_all()

    # ------------------------------------------------------------------ building blocks
    def _build_avatar(self) -> Any:
        path = self.backend.user_avatar_path()
        if path:
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 48, 48, True)
                img = Gtk.Image.new_from_pixbuf(pb)
                add_class(img, "avatar-image")
                return img
            except Exception as exc:  # GLib.Error for broken images
                log.debug("avatar %s unusable: %s", path, exc)
        lbl = Gtk.Label(label=model.initials(self.backend.user_display_name()))
        lbl.set_size_request(48, 48)
        lbl.set_valign(Gtk.Align.CENTER)
        add_class(lbl, "avatar")
        return lbl

    def _build_row(self, page: model.Page) -> Any:
        row = Gtk.ListBoxRow()
        add_class(row, "sidebar-row")
        row.page_id = page.id  # type: ignore[attr-defined]
        hb = box("h", 12)
        hb.set_margin_top(7)
        hb.set_margin_bottom(7)
        hb.set_margin_start(10)
        hb.set_margin_end(10)
        hb.pack_start(icon_image(page.icon, 18, "preferences-system"), False, False, 0)
        hb.pack_start(label(page.title, ("sidebar-label",), ellipsize=True), True, True, 0)
        row.add(hb)
        row.set_tooltip_text(page.description or page.title)
        return row

    # ------------------------------------------------------------------ behaviour
    def _row_selected(self, _lb: Any, row: Any) -> None:
        if row is None or self._silent:
            return
        pid = getattr(row, "page_id", None)
        if pid:
            self.on_select(pid)

    def select(self, page_id: str) -> None:
        row = self._rows.get(page_id)
        if row is None:
            return
        self._silent = True
        try:
            self.listbox.select_row(row)
        finally:
            self._silent = False

    def filter(self, query: str) -> list[str]:
        """Hide rows not matching ``query``; returns visible page ids in order."""
        visible: list[str] = []
        keep = {p.id for p in model.filter_pages(self.pages, query)}
        for p in self.pages:
            row = self._rows[p.id]
            on = p.id in keep
            row.set_no_show_all(not on)
            row.set_visible(on)
            if on:
                visible.append(p.id)
        return visible

    def update_mode(self, modes: Optional[dict[str, Any]] = None) -> None:
        mode_id = self.backend.effective_mode()
        self.mode_label.set_text("Lindos Mode: " + model.mode_display_name(mode_id, modes))

    def refresh_user(self) -> None:
        self.name_label.set_text(self.backend.user_display_name())


__all__ = ["Sidebar", "SIDEBAR_WIDTH"]
