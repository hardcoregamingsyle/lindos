#!/usr/bin/env python3
"""lindos-settings -- Lindos Settings entry point (SPEC §7).

Usage::

    lindos-settings [PAGE] [--power-menu] [--debug] [--list-pages] [--version]

* ``PAGE``          open this page (home, system, personalization, apps, windows-apps, gaming,
                    hardware, network, accounts, mode, update, about).  Unknown ids fall back
                    to ``home`` with a warning.
* ``--power-menu``  show the Win+X style popup (Sleep / Restart / Shut down / Sign out / Lock /
                    Settings / File Explorer / Terminal / Task Manager) instead of the window.
* ``--list-pages``  print the page registry (id, kind, title) and exit — no GTK needed.
* ``--debug``       verbose logging on stderr as well as the log file.

Log: ``~/.local/state/lindos/settings.log``.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from lindos_settings import __version__, model  # noqa: E402

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

APP_ID = "org.lindos.Settings"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lindos-settings", description="Lindos Settings — Windows-11-style settings centre.")
    p.add_argument("page", nargs="?", default=None, metavar="PAGE", help="page to open (%s)" % ", ".join(model.PAGE_ORDER))
    p.add_argument("--power-menu", action="store_true", help="show the Win+X power menu popup instead of the window")
    p.add_argument("--list-pages", action="store_true", help="print the page registry and exit")
    p.add_argument("--debug", action="store_true", help="verbose logging (also to stderr)")
    p.add_argument("--version", action="version", version="lindos-settings %s" % __version__)
    return p


def log_dir() -> str:
    home = os.environ.get("LINDOS_HOME") or os.path.expanduser("~")
    return os.path.join(home, ".local", "state", "lindos")


def setup_logging(debug: bool = False) -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_ok = False
    try:
        os.makedirs(log_dir(), exist_ok=True)
        fh = logging.FileHandler(os.path.join(log_dir(), "settings.log"), encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
        file_ok = True
    except OSError as exc:
        sys.stderr.write("lindos-settings: cannot open log file: %s\n" % exc)
    if debug or not file_ok:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        sh.setLevel(logging.DEBUG if debug else logging.WARNING)
        root.addHandler(sh)
    else:
        # keep warnings visible on a terminal without duplicating everything
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(logging.Formatter("lindos-settings: %(levelname)s: %(message)s"))
        sh.setLevel(logging.ERROR)
        root.addHandler(sh)
    return logging.getLogger("lindos.settings")


def list_pages() -> int:
    for page in model.load_pages():
        print(f"{page.id:<16} {page.kind:<9} {page.title}")
        for sub in page.subitems:
            print(f"    {sub.id:<20} {' '.join(sub.exec) or '-'}")
    return EXIT_OK


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    log = setup_logging(args.debug)
    if args.list_pages:
        return list_pages()

    page = args.page
    if page is not None:
        page = page.strip().lower().replace("_", "-")
        aliases = {"windowsapps": "windows-apps", "wine": "windows-apps", "games": "gaming", "modes": "mode", "updates": "update", "personalisation": "personalization"}
        page = aliases.get(page, page)
        if page not in model.PAGE_ORDER:
            log.warning("unknown page %r; opening home", args.page)
            page = "home"

    try:
        import gi  # noqa: F401

        gi.require_version("Gtk", "3.0")
        from gi.repository import GLib, Gtk  # noqa: F401
    except (ImportError, ValueError) as exc:
        sys.stderr.write("lindos-settings: GTK 3 for Python is required (python3-gi, gir1.2-gtk-3.0): %s\n" % exc)
        return EXIT_ERROR

    if args.power_menu:
        from lindos_settings.power_menu import run_power_menu

        return run_power_menu()

    from lindos_settings.app import SettingsApplication

    app = SettingsApplication(initial_page=page, application_id=APP_ID)
    try:
        # Register first so that a second `lindos-settings <page>` forwards the page to the
        # running window instead of opening another one.
        app.register(None)
        if app.get_is_remote():
            log.info("forwarding page %r to the running instance", page or "home")
            app.activate_action("open", GLib.Variant("s", page or "home"))
            return EXIT_OK
    except Exception as exc:  # no session bus (e.g. TTY): run standalone
        log.warning("application registration failed (%s); running standalone", exc)
    try:
        return int(app.run([sys.argv[0]]))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
