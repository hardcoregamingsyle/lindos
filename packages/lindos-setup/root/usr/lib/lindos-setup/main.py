#!/usr/bin/env python3
"""lindos-setup -- Lindos first-boot setup (OOBE) entry point (SPEC §6).

Usage::

    lindos-setup [--first-run | --reconfigure] [--dry-run] [--page ID] [--debug]

* ``--first-run``   exit 0 silently when SETUP_DONE exists or the session is
                    not XFCE (used by the autostart entry).
* ``--reconfigure`` run the wizard again (Escape / close allowed).
* ``--dry-run``     no helper calls, nothing changed; the plan JSON is printed
                    to stdout and every step is only described.
* ``--page ID``     start on a given page (welcome, mode, browser, personalize,
                    apps, privacy, transfer, summary, apply, done).

Log: ``~/.local/state/lindos/setup.log``.
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

from lindos_setup import __version__  # noqa: E402
from lindos_setup import core, i18n  # noqa: E402

PAGE_IDS = ["welcome", "mode", "browser", "personalize", "apps", "privacy", "transfer", "summary",
           "apply", "done"]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lindos-setup",
        description="Lindos first-boot setup (Out-Of-Box Experience).")
    grp = p.add_mutually_exclusive_group()
    grp.add_argument("--first-run", action="store_true",
                     help="autostart mode: exit 0 silently if setup was already done or the session is not XFCE")
    grp.add_argument("--reconfigure", action="store_true",
                     help="run the wizard again even if setup was completed")
    p.add_argument("--dry-run", action="store_true",
                   help="do not change anything; print the plan JSON to stdout")
    p.add_argument("--page", metavar="ID", choices=PAGE_IDS, default=None,
                   help="start on this page (%s)" % ", ".join(PAGE_IDS))
    p.add_argument("--debug", action="store_true", help="verbose logging (also to stderr)")
    p.add_argument("--version", action="version", version="lindos-setup %s" % __version__)
    return p


def setup_logging(debug: bool = False) -> logging.Logger:
    logger = logging.getLogger("lindos-setup")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    for old in list(logger.handlers):          # idempotent (main() may be called twice in tests)
        logger.removeHandler(old)
        try:
            old.close()
        except Exception:  # noqa: BLE001 - closing a handler must never fail start-up
            pass
    logger.addHandler(logging.NullHandler())   # keep the "lastResort" stderr handler quiet
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_ok = False
    try:
        os.makedirs(core.log_dir(), exist_ok=True)
        fh = logging.FileHandler(core.log_file(), encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        file_ok = True
    except OSError as exc:
        sys.stderr.write("lindos-setup: cannot open log file: %s\n" % exc)
    if debug or not file_ok:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        sh.setLevel(logging.DEBUG if debug else logging.WARNING)
        logger.addHandler(sh)
    return logger


def first_run_gate(logger: logging.Logger) -> Optional[int]:
    """Return an exit code when the wizard must not run, else None."""
    if core.setup_done_exists():
        logger.info("first-run: setup already done (%s); exiting", core.setup_done_path())
        return 0
    if not core.in_xfce():
        logger.info("first-run: not an XFCE session (XDG_CURRENT_DESKTOP=%r); exiting",
                    os.environ.get("XDG_CURRENT_DESKTOP", ""))
        return 0
    return None


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logger = setup_logging(args.debug)
    i18n.init()
    logger.info("lindos-setup %s starting (argv=%s)", __version__, argv if argv is not None else sys.argv[1:])

    if args.first_run:
        code = first_run_gate(logger)
        if code is not None:
            return code

    try:
        from lindos_setup.app import run_app
    except (ImportError, ValueError) as exc:  # no PyGObject / GTK (ValueError: gi.require_version)
        logger.error("GTK is not available: %s", exc)
        if args.dry_run:
            logger.warning("running headless dry-run without GTK")
            return core.headless_dry_run(logger)
        sys.stderr.write("lindos-setup: python3-gi with GTK 3 is required (%s)\n" % exc)
        return 1

    try:
        return run_app(dry_run=args.dry_run, reconfigure=args.reconfigure, page=args.page, logger=logger)
    except KeyboardInterrupt:
        logger.info("interrupted")
        return 1
    except Exception as exc:  # last-resort: never leave the user with a traceback-only screen
        logger.exception("unhandled error: %s", exc)
        sys.stderr.write("lindos-setup: unexpected error: %s (see %s)\n" % (exc, core.log_file()))
        return 1


if __name__ == "__main__":
    sys.exit(main())
