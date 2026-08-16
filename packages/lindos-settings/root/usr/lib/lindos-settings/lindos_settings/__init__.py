"""Lindos Settings — Windows-11-style settings centre for Lindos (Linux Mint XFCE remaster).

Package layout:
    model.py        pure logic (page registry, quick toggles, search, format helpers) — no GTK
    backend.py      adapter over lindos-core (``lindos.*``) and external CLIs, guarded imports
    widgets.py      reusable GTK widgets (cards, dialogs, async helpers)
    sidebar.py      left navigation (avatar, search, page list)
    app.py          Gtk.Application, main window, page stack
    pages/          one module per page id (see model.PAGE_ORDER)
    power_menu.py   Win+X style popup (``lindos-settings --power-menu``)
"""

__version__ = "1.0.0"
__codename__ = "Aurora"
__all__ = ["__version__", "__codename__"]
