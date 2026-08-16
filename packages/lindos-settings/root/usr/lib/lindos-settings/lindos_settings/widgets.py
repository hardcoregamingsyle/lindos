"""Reusable GTK 3 widgets for Lindos Settings (Win11-style cards, dialogs, async helpers).

GTK is imported guarded so this module can be *imported* on a machine without gi (tests);
instantiating anything requires GTK 3.  No Gtk classes are subclassed — every widget here is a
plain Python object exposing a ``.widget`` (or ``.row``) attribute.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Iterable, Optional, Sequence

try:  # pragma: no cover - exercised only where GTK exists
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango  # type: ignore

    HAVE_GTK = True
except (ImportError, ValueError, AttributeError):  # pragma: no cover
    HAVE_GTK = False
    Gdk = GdkPixbuf = GLib = Gtk = Pango = None  # type: ignore

log = logging.getLogger("lindos.settings.widgets")

FALLBACK_ICON = "application-x-executable"


def require_gtk() -> None:
    if not HAVE_GTK:
        raise RuntimeError("GTK 3 (python3-gi, gir1.2-gtk-3.0) is required for the Lindos Settings UI")


# ---------------------------------------------------------------------------------------------
# main-loop helpers
# ---------------------------------------------------------------------------------------------


def idle(fn: Callable[..., Any], *args: Any) -> None:
    """Run ``fn(*args)`` once on the GTK main loop."""

    def _wrap() -> bool:
        try:
            fn(*args)
        except Exception:  # never let a callback kill the main loop
            log.exception("idle callback failed")
        return False

    GLib.idle_add(_wrap)


def run_async(fn: Callable[[], Any], on_done: Optional[Callable[[Any, Optional[BaseException]], None]] = None, name: str = "worker") -> threading.Thread:
    """Run ``fn`` in a daemon thread; ``on_done(result, exception)`` runs on the main loop."""

    def worker() -> None:
        result: Any = None
        error: Optional[BaseException] = None
        try:
            result = fn()
        except BaseException as exc:  # noqa: BLE001 — surfaced to the UI
            error = exc
            log.warning("async %s failed: %s", name, exc)
        if on_done is not None:
            idle(on_done, result, error)

    t = threading.Thread(target=worker, name=name, daemon=True)
    t.start()
    return t


def add_timeout_seconds(seconds: int, fn: Callable[[], bool]) -> int:
    return GLib.timeout_add_seconds(seconds, fn)


def remove_source(source_id: Optional[int]) -> None:
    if source_id:
        try:
            GLib.source_remove(source_id)
        except Exception:  # already removed
            pass


# ---------------------------------------------------------------------------------------------
# CSS / icons / labels
# ---------------------------------------------------------------------------------------------


def load_css(path: str) -> bool:
    provider = Gtk.CssProvider()
    try:
        provider.load_from_path(path)
    except Exception as exc:  # GLib.Error
        log.error("cannot load CSS %s: %s", path, exc)
        return False
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    return True


def add_css_to_widget(widget: Any, css: str) -> None:
    provider = Gtk.CssProvider()
    try:
        provider.load_from_data(css.encode("utf-8"))
    except Exception as exc:
        log.warning("bad inline css: %s", exc)
        return
    # Same priority as the global sheet: a provider on the widget's own context takes precedence
    # over screen-wide providers of equal priority (GTK 3 docs, gtk_style_context_add_provider).
    widget.get_style_context().add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def add_class(widget: Any, *classes: str) -> Any:
    ctx = widget.get_style_context()
    for c in classes:
        if c:
            ctx.add_class(c)
    return widget


def remove_class(widget: Any, *classes: str) -> Any:
    ctx = widget.get_style_context()
    for c in classes:
        ctx.remove_class(c)
    return widget


def pick_icon(names: Iterable[str], fallback: str = FALLBACK_ICON) -> str:
    theme = Gtk.IconTheme.get_default()
    for n in names:
        if n and theme.has_icon(n):
            return n
    return fallback


def icon_image(names: Any, pixel_size: int = 20, fallback: str = FALLBACK_ICON) -> Any:
    if isinstance(names, str):
        names = (names,)
    img = Gtk.Image.new_from_icon_name(pick_icon(names or (), fallback), Gtk.IconSize.LARGE_TOOLBAR)
    img.set_pixel_size(pixel_size)
    return img


def label(text: str = "", classes: Sequence[str] = (), xalign: float = 0.0, wrap: bool = False, markup: bool = False, selectable: bool = False, ellipsize: bool = False) -> Any:
    lbl = Gtk.Label()
    if markup:
        lbl.set_markup(text)
    else:
        lbl.set_text(text)
    lbl.set_xalign(xalign)
    lbl.set_halign(Gtk.Align.START if xalign == 0.0 else Gtk.Align.FILL)
    if wrap:
        lbl.set_line_wrap(True)
        lbl.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        lbl.set_max_width_chars(80)
    if ellipsize:
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
    if selectable:
        lbl.set_selectable(True)
    add_class(lbl, *classes)
    return lbl


def box(orientation: str = "v", spacing: int = 6, classes: Sequence[str] = ()) -> Any:
    b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL if orientation == "v" else Gtk.Orientation.HORIZONTAL, spacing=spacing)
    add_class(b, *classes)
    return b


def button(text: str = "", icon: Optional[str] = None, classes: Sequence[str] = (), on_click: Optional[Callable[[], Any]] = None, tooltip: str = "") -> Any:
    if icon and text:
        btn = Gtk.Button()
        hb = box("h", 6)
        hb.pack_start(icon_image(icon, 16), False, False, 0)
        hb.pack_start(Gtk.Label(label=text), False, False, 0)
        btn.add(hb)
    elif icon:
        btn = Gtk.Button()
        btn.add(icon_image(icon, 16))
    else:
        btn = Gtk.Button(label=text)
    btn.set_valign(Gtk.Align.CENTER)
    add_class(btn, *classes)
    if tooltip:
        btn.set_tooltip_text(tooltip)
    if on_click is not None:
        btn.connect("clicked", lambda *_: on_click())
    return btn


def badge(status: str) -> Any:
    text = {
        "works": "Works",
        "native": "Native",
        "partial": "Partial",
        "broken": "Broken",
        "not-possible": "Not possible",
        "unknown": "Unknown",
    }.get(status, status.title() if status else "Unknown")
    lbl = Gtk.Label(label=text)
    lbl.set_valign(Gtk.Align.CENTER)
    add_class(lbl, "badge", f"badge-{status or 'unknown'}")
    return lbl


def set_widget_background(widget: Any, hex_color: str, radius: int = 6) -> None:
    add_css_to_widget(widget, f"* {{ background-color: {hex_color}; background-image: none; border-radius: {radius}px; }}")


def clipboard_set(text: str) -> None:
    cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    cb.set_text(text, -1)
    cb.store()


# ---------------------------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------------------------


class Card:
    """A Win11 settings card: [icon] title / subtitle … [control]  (a Gtk.ListBoxRow)."""

    def __init__(self, title: str, subtitle: str = "", icon: Any = None, keywords: Sequence[str] = (), activatable: bool = False, icon_size: int = 22):
        self.title = title
        self.subtitle = subtitle
        self.keywords = tuple(keywords)
        self._on_activate: Optional[Callable[[], Any]] = None
        self.row = Gtk.ListBoxRow()
        self.row.set_selectable(False)
        self.row.set_activatable(activatable)
        add_class(self.row, "settings-card")
        self.outer = box("v", 8)
        self.outer.set_margin_top(12)
        self.outer.set_margin_bottom(12)
        self.outer.set_margin_start(16)
        self.outer.set_margin_end(16)
        self.row.add(self.outer)
        self.header = box("h", 14)
        self.outer.pack_start(self.header, False, False, 0)
        self.icon_widget = None
        if icon:
            self.icon_widget = icon_image(icon, icon_size)
            self.icon_widget.set_valign(Gtk.Align.CENTER)
            add_class(self.icon_widget, "card-icon")
            self.header.pack_start(self.icon_widget, False, False, 0)
        self.text_box = box("v", 2)
        self.text_box.set_valign(Gtk.Align.CENTER)
        self.title_label = label(title, ("card-title",), ellipsize=False, wrap=True)
        self.text_box.pack_start(self.title_label, False, False, 0)
        self.subtitle_label = label(subtitle, ("card-subtitle", "dim-label"), wrap=True)
        self.subtitle_label.set_no_show_all(not subtitle)
        self.subtitle_label.set_visible(bool(subtitle))
        self.text_box.pack_start(self.subtitle_label, False, False, 0)
        self.header.pack_start(self.text_box, True, True, 0)
        self.control_box = box("h", 8)
        self.control_box.set_valign(Gtk.Align.CENTER)
        self.header.pack_end(self.control_box, False, False, 0)
        self.body_box = box("v", 8)
        self.body_box.set_no_show_all(True)
        self.outer.pack_start(self.body_box, False, False, 0)
        self._body_used = False

    # -- content
    def set_subtitle(self, text: str) -> None:
        self.subtitle = text
        self.subtitle_label.set_text(text)
        self.subtitle_label.set_no_show_all(not text)
        self.subtitle_label.set_visible(bool(text))

    def set_title(self, text: str) -> None:
        self.title = text
        self.title_label.set_text(text)

    def set_control(self, widget: Any) -> Any:
        widget.set_valign(Gtk.Align.CENTER)
        self.control_box.pack_end(widget, False, False, 0)
        return widget

    def add_control(self, widget: Any) -> Any:
        widget.set_valign(Gtk.Align.CENTER)
        self.control_box.pack_start(widget, False, False, 0)
        return widget

    def add_body(self, widget: Any) -> Any:
        self._body_used = True
        self.body_box.set_no_show_all(False)
        self.body_box.pack_start(widget, False, False, 0)
        self.body_box.show_all()
        return widget

    def set_activatable(self, on: bool) -> None:
        self.row.set_activatable(on)

    def on_activate(self, cb: Callable[[], Any]) -> None:
        self._on_activate = cb
        self.row.set_activatable(True)

    def activate(self) -> None:
        if self._on_activate:
            self._on_activate()

    # -- visibility / search
    def search_text(self) -> str:
        return " ".join([self.title, self.subtitle, *self.keywords]).lower()

    def set_visible(self, on: bool) -> None:
        self.row.set_no_show_all(not on)
        self.row.set_visible(on)

    def matches(self, query: str) -> bool:
        from . import model  # local import keeps module import cheap

        return model.text_matches(self.search_text(), query)


class SwitchCard(Card):
    def __init__(self, title: str, subtitle: str = "", icon: Any = None, keywords: Sequence[str] = (), on_toggle: Optional[Callable[[bool], Any]] = None, active: bool = False):
        super().__init__(title, subtitle, icon, keywords)
        self.switch = Gtk.Switch()
        self.switch.set_active(bool(active))
        self._silent = False
        self._cb = on_toggle
        self.switch.connect("notify::active", self._changed)
        self.set_control(self.switch)

    def _changed(self, *_: Any) -> None:
        if self._silent or self._cb is None:
            return
        self._cb(self.switch.get_active())

    def set_active_silent(self, value: bool) -> None:
        self._silent = True
        try:
            self.switch.set_active(bool(value))
        finally:
            self._silent = False

    def get_active(self) -> bool:
        return bool(self.switch.get_active())


class ComboCard(Card):
    def __init__(self, title: str, subtitle: str = "", icon: Any = None, keywords: Sequence[str] = (), options: Sequence[tuple[str, str]] = (), on_change: Optional[Callable[[str], Any]] = None, active_id: Optional[str] = None, with_entry: bool = False):
        super().__init__(title, subtitle, icon, keywords)
        self.combo = Gtk.ComboBoxText.new_with_entry() if with_entry else Gtk.ComboBoxText()
        self._silent = False
        self._cb = on_change
        self.set_options(options, active_id)
        self.combo.connect("changed", self._changed)
        self.set_control(self.combo)

    def set_options(self, options: Sequence[tuple[str, str]], active_id: Optional[str] = None) -> None:
        self._silent = True
        try:
            self.combo.remove_all()
            for oid, text in options:
                self.combo.append(str(oid), str(text))
            if active_id is not None:
                self.combo.set_active_id(str(active_id))
            elif options:
                self.combo.set_active(0)
        finally:
            self._silent = False

    def set_active_id_silent(self, oid: Optional[str]) -> None:
        self._silent = True
        try:
            if oid is None or not self.combo.set_active_id(str(oid)):
                entry = self.combo.get_child() if self.combo.get_has_entry() else None
                if entry is not None:
                    entry.set_text(str(oid or ""))
        finally:
            self._silent = False

    def get_active_id(self) -> Optional[str]:
        aid = self.combo.get_active_id()
        if aid is None and self.combo.get_has_entry():
            return self.combo.get_child().get_text().strip() or None
        return aid

    def _changed(self, *_: Any) -> None:
        if self._silent or self._cb is None:
            return
        aid = self.get_active_id()
        if aid:
            self._cb(aid)


class ButtonCard(Card):
    def __init__(self, title: str, subtitle: str = "", icon: Any = None, keywords: Sequence[str] = (), button_label: str = "Open", on_click: Optional[Callable[[], Any]] = None, activatable: bool = True):
        super().__init__(title, subtitle, icon, keywords, activatable=activatable)
        self.button = button(button_label, on_click=on_click)
        self.set_control(self.button)
        if on_click is not None and activatable:
            self.on_activate(on_click)

    def set_button_label(self, text: str) -> None:
        self.button.set_label(text)


class InfoCard(Card):
    def __init__(self, title: str, body: str, icon: Any = None, keywords: Sequence[str] = ()):
        super().__init__(title, "", icon, keywords)
        self.body_label = label(body, ("card-body",), wrap=True, selectable=False)
        self.add_body(self.body_label)


class CardList:
    """Gtk.ListBox holding cards; supports search filtering."""

    def __init__(self) -> None:
        self.widget = Gtk.ListBox()
        self.widget.set_selection_mode(Gtk.SelectionMode.NONE)
        add_class(self.widget, "settings-cards")
        self.cards: list[Card] = []
        self.widget.connect("row-activated", self._activated)

    def add(self, card: Card) -> Card:
        self.cards.append(card)
        self.widget.add(card.row)
        return card

    def clear(self) -> None:
        for child in self.widget.get_children():
            self.widget.remove(child)
        self.cards.clear()

    def _activated(self, _lb: Any, row: Any) -> None:
        for c in self.cards:
            if c.row is row:
                c.activate()
                return

    def filter(self, query: str) -> int:
        visible = 0
        for c in self.cards:
            ok = c.matches(query)
            c.set_visible(ok)
            visible += 1 if ok else 0
        return visible

    def show_all(self) -> None:
        self.widget.show_all()


# ---------------------------------------------------------------------------------------------
# Page base
# ---------------------------------------------------------------------------------------------


class PageBase:
    """Scrollable page with a title, sections and card lists.  Subclasses override
    :meth:`build` and optionally :meth:`on_show` / :meth:`on_hide` / :meth:`refresh`."""

    PAGE_ID = ""

    def __init__(self, app: Any, page: Any):
        self.app = app
        self.backend = app.backend
        self.page = page
        self.id = page.id
        self.widget = Gtk.ScrolledWindow()
        self.widget.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.widget.set_hexpand(True)
        self.widget.set_vexpand(True)
        add_class(self.widget, "settings-page")
        self.body = box("v", 8)
        self.body.set_margin_top(20)
        self.body.set_margin_bottom(32)
        self.body.set_margin_start(28)
        self.body.set_margin_end(28)
        self.widget.add(self.body)
        self.title_label = label(page.title, ("page-title",))
        self.body.pack_start(self.title_label, False, False, 0)
        if page.description:
            self.body.pack_start(label(page.description, ("page-subtitle", "dim-label"), wrap=True), False, False, 4)
        self._sections: list[tuple[Any, CardList]] = []
        self._extra_filterables: list[Callable[[str], int]] = []
        self._visible = False
        self.widget.connect("map", lambda *_: self._mapped())
        self.widget.connect("unmap", lambda *_: self._unmapped())
        self.build()
        self.widget.show_all()

    # hooks
    def build(self) -> None:  # pragma: no cover - overridden
        pass

    def on_show(self) -> None:
        pass

    def on_hide(self) -> None:
        pass

    def refresh(self) -> None:
        pass

    def _mapped(self) -> None:
        self._visible = True
        try:
            self.on_show()
        except Exception:
            log.exception("page %s on_show failed", self.id)

    def _unmapped(self) -> None:
        self._visible = False
        try:
            self.on_hide()
        except Exception:
            log.exception("page %s on_hide failed", self.id)

    @property
    def is_visible(self) -> bool:
        return self._visible

    # layout helpers
    def add_section(self, title: str = "") -> CardList:
        lbl = None
        if title:
            lbl = label(title, ("section-title",))
            lbl.set_margin_top(14)
            self.body.pack_start(lbl, False, False, 0)
        cards = CardList()
        self.body.pack_start(cards.widget, False, False, 0)
        self._sections.append((lbl, cards))
        return cards

    def add_widget(self, widget: Any, margin_top: int = 8) -> Any:
        widget.set_margin_top(margin_top)
        self.body.pack_start(widget, False, False, 0)
        return widget

    def add_filterable(self, fn: Callable[[str], int]) -> None:
        self._extra_filterables.append(fn)

    def apply_filter(self, query: str) -> int:
        total = 0
        for lbl, cards in self._sections:
            n = cards.filter(query)
            total += n
            if lbl is not None:
                lbl.set_no_show_all(n == 0)
                lbl.set_visible(n > 0)
        for fn in self._extra_filterables:
            total += fn(query)
        return total

    def toast(self, text: str) -> None:
        self.app.toast(text)


# ---------------------------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------------------------


def confirm(parent: Any, title: str, text: str, ok_label: str = "OK", destructive: bool = False) -> bool:
    dlg = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.NONE, text=title)
    dlg.format_secondary_text(text)
    dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
    ok = dlg.add_button(ok_label, Gtk.ResponseType.OK)
    if destructive:
        add_class(ok, "destructive-action")
    else:
        add_class(ok, "suggested-action")
    resp = dlg.run()
    dlg.destroy()
    return resp == Gtk.ResponseType.OK


def message(parent: Any, title: str, text: str, error: bool = False) -> None:
    dlg = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=Gtk.MessageType.ERROR if error else Gtk.MessageType.INFO, buttons=Gtk.ButtonsType.CLOSE, text=title)
    dlg.format_secondary_text(text)
    dlg.run()
    dlg.destroy()


def choose_file(parent: Any, title: str, filters: Sequence[tuple[str, Sequence[str]]] = (), folder: Optional[str] = None) -> Optional[str]:
    """Native file chooser (portal/GTK).  Returns the selected path or None."""
    chooser = Gtk.FileChooserNative.new(title, parent, Gtk.FileChooserAction.OPEN, "Open", "Cancel")
    for name, patterns in filters:
        ff = Gtk.FileFilter()
        ff.set_name(name)
        for p in patterns:
            ff.add_pattern(p)
        chooser.add_filter(ff)
    if folder:
        chooser.set_current_folder(folder)
    resp = chooser.run()
    path = chooser.get_filename() if resp == Gtk.ResponseType.ACCEPT else None
    chooser.destroy()
    return path


class OutputDialog:
    """Dialog with a monospace log view.  ``run_argv`` streams a command into it."""

    def __init__(self, parent: Any, title: str, backend: Any = None, width: int = 760, height: int = 480):
        self.backend = backend
        self.dialog = Gtk.Dialog(title=title, transient_for=parent, modal=False)
        self.dialog.set_default_size(width, height)
        add_class(self.dialog, "output-dialog")
        area = self.dialog.get_content_area()
        area.set_spacing(6)
        top = box("h", 8)
        top.set_margin_start(12)
        top.set_margin_end(12)
        top.set_margin_top(8)
        self.spinner = Gtk.Spinner()
        self.status = label("", ("dim-label",))
        top.pack_start(self.spinner, False, False, 0)
        top.pack_start(self.status, True, True, 0)
        area.pack_start(top, False, False, 0)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.set_margin_start(12)
        sw.set_margin_end(12)
        sw.set_margin_bottom(8)
        self.view = Gtk.TextView()
        self.view.set_editable(False)
        self.view.set_cursor_visible(False)
        self.view.set_monospace(True)
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        add_class(self.view, "log-view")
        self.buffer = self.view.get_buffer()
        sw.add(self.view)
        area.pack_start(sw, True, True, 0)
        self.copy_btn = self.dialog.add_button("Copy", 1)
        self.close_btn = self.dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        self.dialog.connect("response", self._response)
        self._thread: Optional[threading.Thread] = None
        self.exit_code: Optional[int] = None
        self.on_finished: Optional[Callable[[int], None]] = None
        self.dialog.show_all()

    def _response(self, dlg: Any, resp: int) -> None:
        if resp == 1:
            start, end = self.buffer.get_bounds()
            clipboard_set(self.buffer.get_text(start, end, True))
            return
        dlg.destroy()

    def append(self, line: str) -> None:
        end = self.buffer.get_end_iter()
        self.buffer.insert(end, line + "\n")
        mark = self.buffer.create_mark(None, self.buffer.get_end_iter(), False)
        self.view.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)

    def set_text(self, text: str) -> None:
        self.buffer.set_text(text)

    def set_status(self, text: str, busy: bool) -> None:
        self.status.set_text(text)
        if busy:
            self.spinner.start()
        else:
            self.spinner.stop()

    def run_argv(self, argv: Sequence[str], env: Optional[dict[str, str]] = None, on_finished: Optional[Callable[[int], None]] = None) -> None:
        if self.backend is None:
            self.append("internal error: no backend")
            return
        self.on_finished = on_finished
        self.set_status("Running: " + " ".join(argv), True)
        self.append("$ " + " ".join(argv))

        def _line(text: str) -> None:
            idle(self.append, text)

        def _done(code: int) -> None:
            idle(self._finished, code)

        self._thread = self.backend.stream(argv, _line, _done, env=env)

    def _finished(self, code: int) -> None:
        self.exit_code = code
        self.set_status("Finished (exit code %d)" % code if code else "Done", False)
        self.append("" if code == 0 else f"[exit code {code}]")
        if self.on_finished:
            try:
                self.on_finished(code)
            except Exception:
                log.exception("on_finished failed")

    def run_fn(self, fn: Callable[[Callable[[str], None]], Any], on_finished: Optional[Callable[[Any, Optional[BaseException]], None]] = None, status: str = "Working…") -> None:
        """Run ``fn(log_line)`` in a thread; log lines stream into the view."""
        self.set_status(status, True)

        def _log(text: str) -> None:
            idle(self.append, str(text))

        def _done(result: Any, exc: Optional[BaseException]) -> None:
            self.set_status("Failed: %s" % exc if exc else "Done", False)
            if exc:
                self.append(f"error: {exc}")
            if on_finished:
                on_finished(result, exc)

        run_async(lambda: fn(_log), _done, name="output-dialog")


class ProgressDialog:
    """Modal progress dialog with a step list + log (used for Apply mode)."""

    def __init__(self, parent: Any, title: str, subtitle: str = ""):
        self.dialog = Gtk.Dialog(title=title, transient_for=parent, modal=True)
        self.dialog.set_default_size(700, 520)
        self.dialog.set_deletable(False)
        add_class(self.dialog, "progress-dialog")
        area = self.dialog.get_content_area()
        area.set_spacing(8)
        area.set_margin_top(12)
        area.set_margin_bottom(4)
        area.set_margin_start(12)
        area.set_margin_end(12)
        self.heading = label(title, ("dialog-heading",))
        area.pack_start(self.heading, False, False, 0)
        if subtitle:
            area.pack_start(label(subtitle, ("dim-label",), wrap=True), False, False, 0)
        self.bar = Gtk.ProgressBar()
        self.bar.set_show_text(True)
        self.bar.set_text("Working…")
        area.pack_start(self.bar, False, False, 4)
        self.steps_box = box("v", 2)
        sw1 = Gtk.ScrolledWindow()
        sw1.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw1.set_min_content_height(120)
        sw1.add(self.steps_box)
        area.pack_start(sw1, False, False, 0)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.view = Gtk.TextView()
        self.view.set_editable(False)
        self.view.set_monospace(True)
        self.view.set_cursor_visible(False)
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        add_class(self.view, "log-view")
        self.buffer = self.view.get_buffer()
        sw.add(self.view)
        area.pack_start(sw, True, True, 0)
        self.close_btn = self.dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        self.close_btn.set_sensitive(False)
        self.dialog.connect("response", lambda d, *_: d.destroy())
        self._pulse_id: Optional[int] = None
        self._steps: dict[str, Any] = {}
        self.dialog.show_all()

    def start_pulse(self) -> None:
        def _pulse() -> bool:
            self.bar.pulse()
            return True

        self._pulse_id = GLib.timeout_add(120, _pulse)

    def stop_pulse(self) -> None:
        remove_source(self._pulse_id)
        self._pulse_id = None

    def log(self, text: str) -> None:
        end = self.buffer.get_end_iter()
        self.buffer.insert(end, str(text) + "\n")
        mark = self.buffer.create_mark(None, self.buffer.get_end_iter(), False)
        self.view.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)

    def add_step(self, name: str, ok: Optional[bool], detail: str = "") -> None:
        row = box("h", 8)
        icon = "emblem-ok-symbolic" if ok else ("dialog-error-symbolic" if ok is False else "content-loading-symbolic")
        row.pack_start(icon_image(icon, 16), False, False, 0)
        row.pack_start(label(name, ("step-name",)), False, False, 0)
        if detail:
            row.pack_start(label(detail, ("dim-label",), ellipsize=True), True, True, 0)
        row.show_all()
        self.steps_box.pack_start(row, False, False, 0)
        self._steps[name] = row

    def finish(self, ok: bool, steps: Sequence[tuple[str, bool, str]], summary: str = "") -> None:
        self.stop_pulse()
        for child in self.steps_box.get_children():
            self.steps_box.remove(child)
        for name, sok, detail in steps:
            self.add_step(name, sok, detail)
        self.bar.set_fraction(1.0)
        self.bar.set_text(summary or ("Done" if ok else "Finished with errors"))
        add_class(self.bar, "ok" if ok else "error")
        self.close_btn.set_sensitive(True)
        self.dialog.set_deletable(True)


class Toast:
    """Transient message at the bottom of the window (Gtk.Revealer in an overlay)."""

    def __init__(self, overlay: Any):
        self.revealer = Gtk.Revealer()
        self.revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.revealer.set_halign(Gtk.Align.CENTER)
        self.revealer.set_valign(Gtk.Align.END)
        self.revealer.set_margin_bottom(20)
        self.label = label("", ("toast-label",))
        self.label.set_margin_top(8)
        self.label.set_margin_bottom(8)
        self.label.set_margin_start(16)
        self.label.set_margin_end(16)
        frame = Gtk.Frame()
        add_class(frame, "toast")
        frame.add(self.label)
        self.revealer.add(frame)
        overlay.add_overlay(self.revealer)
        self.revealer.show_all()
        self.revealer.set_reveal_child(False)
        self._timer: Optional[int] = None

    def show(self, text: str, seconds: int = 3) -> None:
        self.label.set_text(text)
        self.revealer.set_reveal_child(True)
        remove_source(self._timer)
        self._timer = GLib.timeout_add_seconds(seconds, self._hide)

    def _hide(self) -> bool:
        self.revealer.set_reveal_child(False)
        self._timer = None
        return False


class KeyValueGrid:
    """Two-column grid (Win11 'Device specifications' look)."""

    def __init__(self, rows: Sequence[tuple[str, str]] = ()):
        self.widget = Gtk.Grid()
        self.widget.set_column_spacing(24)
        self.widget.set_row_spacing(6)
        add_class(self.widget, "kv-grid")
        self._n = 0
        self._values: dict[str, Any] = {}
        for k, v in rows:
            self.add(k, v)

    def add(self, key: str, value: str) -> None:
        k = label(key, ("kv-key", "dim-label"))
        v = label(value, ("kv-value",), selectable=True, wrap=True)
        self.widget.attach(k, 0, self._n, 1, 1)
        self.widget.attach(v, 1, self._n, 1, 1)
        self._values[key] = v
        self._n += 1

    def set(self, key: str, value: str) -> None:
        if key in self._values:
            self._values[key].set_text(value)
        else:
            self.add(key, value)

    def as_text(self) -> str:
        return "\n".join(f"{k}: {v.get_text()}" for k, v in self._values.items())


__all__ = [
    "HAVE_GTK",
    "require_gtk",
    "idle",
    "run_async",
    "add_timeout_seconds",
    "remove_source",
    "load_css",
    "add_css_to_widget",
    "add_class",
    "remove_class",
    "pick_icon",
    "icon_image",
    "label",
    "box",
    "button",
    "badge",
    "set_widget_background",
    "clipboard_set",
    "Card",
    "SwitchCard",
    "ComboCard",
    "ButtonCard",
    "InfoCard",
    "CardList",
    "PageBase",
    "confirm",
    "message",
    "choose_file",
    "OutputDialog",
    "ProgressDialog",
    "Toast",
    "KeyValueGrid",
]
