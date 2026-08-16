"""Lindos Mode page: five mode cards, a 'what will change' diff and Apply (runs
``lindos.modes.apply_mode`` in a thread behind a progress dialog)."""

from __future__ import annotations

import logging
from typing import Any, Optional

from .. import model
from ..widgets import HAVE_GTK, PageBase, ProgressDialog, add_class, box, button, confirm, icon_image, idle, label, run_async

if HAVE_GTK:  # pragma: no cover
    from gi.repository import Gtk  # type: ignore
else:  # pragma: no cover
    Gtk = None  # type: ignore

log = logging.getLogger("lindos.settings.mode")


class ModePage(PageBase):
    PAGE_ID = "mode"

    def build(self) -> None:
        self.modes: dict[str, Any] = self.app.modes or self.backend.load_modes()
        self.selected: str = self.backend.effective_mode()
        self._buttons: dict[str, Any] = {}

        self.add_section("Choose a mode")
        self.grid = Gtk.FlowBox()
        self.grid.set_selection_mode(Gtk.SelectionMode.NONE)
        self.grid.set_max_children_per_line(5)
        self.grid.set_min_children_per_line(1)
        self.grid.set_row_spacing(8)
        self.grid.set_column_spacing(8)
        self.grid.set_homogeneous(True)
        add_class(self.grid, "mode-grid")
        for mid in model.MODE_IDS:
            m = self.modes.get(mid)
            md = self.backend.mode_as_dict(m) if m is not None else {"id": mid, "name": model.MODE_NAMES[mid], "description": model.MODE_DESCRIPTIONS[mid], "icon": model.MODE_ICONS[mid]}
            self.grid.add(self._mode_tile(mid, md))
        self.add_widget(self.grid, 4)
        self.add_filterable(self._filter_tiles)

        # -- diff panel
        self.add_section("What will change")
        self.diff_frame = Gtk.Frame()
        add_class(self.diff_frame, "settings-card", "diff-card")
        self.diff_grid = Gtk.Grid()
        self.diff_grid.set_column_spacing(24)
        self.diff_grid.set_row_spacing(6)
        self.diff_grid.set_margin_top(12)
        self.diff_grid.set_margin_bottom(12)
        self.diff_grid.set_margin_start(16)
        self.diff_grid.set_margin_end(16)
        self.diff_frame.add(self.diff_grid)
        self.add_widget(self.diff_frame, 4)

        # -- apply row
        row = box("h", 10)
        row.set_margin_top(10)
        self.status_label = label("", ("dim-label",), wrap=True)
        row.pack_start(self.status_label, True, True, 0)
        self.apply_btn = button("Apply", classes=("suggested-action",), on_click=self._apply)
        row.pack_end(self.apply_btn, False, False, 0)
        self.add_widget(row, 4)
        note = label(
            "Applying a mode changes the taskbar pins, compositor, CPU governor, zram size and services; packages "
            "listed by the mode are installed only when missing and online (administrator password required for the system part).",
            ("dim-label",),
            wrap=True,
        )
        self.add_widget(note, 6)
        self._select(self.selected)

    # ------------------------------------------------------------------ tiles
    def _mode_tile(self, mid: str, md: dict[str, Any]) -> Any:
        btn = Gtk.Button()
        add_class(btn, "mode-card")
        vb = box("v", 6)
        vb.set_margin_top(14)
        vb.set_margin_bottom(14)
        vb.set_margin_start(12)
        vb.set_margin_end(12)
        icon = icon_image((str(md.get("icon") or ""), model.MODE_ICONS.get(mid, "preferences-desktop")), 36)
        vb.pack_start(icon, False, False, 0)
        vb.pack_start(label(str(md.get("name") or model.MODE_NAMES.get(mid, mid)), ("mode-name",), xalign=0.5), False, False, 0)
        desc = label(str(md.get("description") or model.MODE_DESCRIPTIONS.get(mid, "")), ("dim-label", "mode-desc"), xalign=0.5, wrap=True)
        desc.set_justify(Gtk.Justification.CENTER)
        desc.set_max_width_chars(24)
        vb.pack_start(desc, True, True, 0)
        badge_lbl = label("Current", ("badge", "badge-works"), xalign=0.5)
        badge_lbl.set_no_show_all(True)
        vb.pack_start(badge_lbl, False, False, 0)
        btn.add(vb)
        btn.set_tooltip_text(mid)
        btn.connect("clicked", lambda _b, m=mid: self._select(m))
        self._buttons[mid] = (btn, badge_lbl, str(md.get("name") or mid) + " " + str(md.get("description") or ""))
        return btn

    def _filter_tiles(self, query: str) -> int:
        n = 0
        for mid, (btn, _badge, text) in self._buttons.items():
            ok = model.text_matches(text + " " + mid, query)
            parent = btn.get_parent()  # FlowBoxChild
            if parent is not None:
                parent.set_no_show_all(not ok)
                parent.set_visible(ok)
            n += 1 if ok else 0
        return n

    def _select(self, mid: str) -> None:
        self.selected = mid
        current = self.backend.effective_mode()
        for m, (btn, badge_lbl, _t) in self._buttons.items():
            if m == mid:
                add_class(btn, "selected")
            else:
                btn.get_style_context().remove_class("selected")
            badge_lbl.set_visible(m == current)
        self._update_diff()

    # ------------------------------------------------------------------ diff
    def _update_diff(self) -> None:
        for child in self.diff_grid.get_children():
            self.diff_grid.remove(child)
        current_id = self.backend.effective_mode()
        target = self.backend.mode_as_dict(self.modes.get(self.selected)) if self.modes.get(self.selected) is not None else {}
        cur_mode = self.backend.mode_as_dict(self.modes.get(current_id)) if self.modes.get(current_id) is not None else {}
        current = dict(cur_mode)
        current["governor"] = self.backend.current_governor() or cur_mode.get("governor")
        current["compositor"] = ("on" if self.backend.compositor_running() else "off") if self.backend.which("lindos-compositor") or self.backend.which("xfconf-query") else cur_mode.get("compositor")
        rows = model.mode_diff(current, target)
        headers = ("Setting", "Now", f"{model.mode_display_name(self.selected, self.modes)} mode")
        for col, text in enumerate(headers):
            self.diff_grid.attach(label(text, ("diff-header",)), col, 0, 1, 1)
        for i, (name, frm, to) in enumerate(rows, start=1):
            self.diff_grid.attach(label(name, ("kv-key", "dim-label")), 0, i, 1, 1)
            self.diff_grid.attach(label(frm or "—", ("kv-value",)), 1, i, 1, 1)
            self.diff_grid.attach(label(to or "—", ("kv-value", "diff-to")), 2, i, 1, 1)
        self.diff_grid.show_all()
        same = self.selected == current_id
        self.apply_btn.set_label("Re-apply" if same else "Apply")
        self.status_label.set_text(
            f"{model.mode_display_name(self.selected, self.modes)} is already your mode — re-apply to repair pins/services."
            if same
            else f"Switch from {model.mode_display_name(current_id, self.modes)} to {model.mode_display_name(self.selected, self.modes)}."
        )
        if not target:
            self.status_label.set_text("Mode definitions (/usr/share/lindos/modes) not found — lindos-core missing? Apply will try `lindos-mode set`.")

    # ------------------------------------------------------------------ apply
    def _apply(self) -> None:
        mid = self.selected
        name = model.mode_display_name(mid, self.modes)
        if not confirm(self.app.window, f"Apply {name} mode?", "The taskbar, compositor and system tuning will be reconfigured. You may be asked for your password.", "Apply"):
            return
        dlg = ProgressDialog(self.app.window, f"Applying {name} mode", "Please wait — do not log out while this runs.")
        dlg.start_pulse()
        self.apply_btn.set_sensitive(False)

        def _log(line: str) -> None:
            idle(dlg.log, str(line))

        def _work() -> Any:
            return self.backend.apply_mode(mid, _log)

        def _done(res: Any, exc: Optional[BaseException]) -> None:
            self.apply_btn.set_sensitive(True)
            if exc or res is None:
                dlg.finish(False, [("apply_mode", False, str(exc or "no result"))], "Failed")
                self.toast("Mode change failed")
                return
            steps = list(getattr(res, "steps", []) or [])
            ok = bool(getattr(res, "ok", False))
            failed = [s for s in steps if not s[1]]
            summary = "Done — %d step(s)" % len(steps) if ok else "%d of %d step(s) failed" % (len(failed), len(steps))
            dlg.finish(ok, steps, summary)
            self.backend.reload_config()
            self.app.on_mode_changed()
            self._select(mid)
            self.toast(f"{name} mode applied" if ok else f"{name} mode applied with errors — see log")

        run_async(_work, _done, name="apply-mode")

    def on_show(self) -> None:
        self._select(self.selected)


__all__ = ["ModePage"]
