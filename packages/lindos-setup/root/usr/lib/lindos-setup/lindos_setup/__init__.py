"""Lindos first-boot setup (OOBE) package.

Modules:

* :mod:`lindos_setup.plan`    -- pure planning logic (no GTK, no lindos imports)
* :mod:`lindos_setup.core`    -- guarded bridge to ``lindos-core`` (config, modes, ...)
* :mod:`lindos_setup.i18n`    -- gettext helper (``_``)
* :mod:`lindos_setup.widgets` -- GTK 3 widgets (cards, swatches, dots, ...)
* :mod:`lindos_setup.pages`   -- the wizard pages
* :mod:`lindos_setup.app`     -- the window / application entry point

Only ``widgets``, ``pages`` and ``app`` import ``gi``; everything else is
importable on any OS.
"""
from __future__ import annotations

__version__ = "1.0.0"
__codename__ = "Aurora"
APP_ID = "org.lindos.Setup"
GETTEXT_DOMAIN = "lindos-setup"

__all__ = ["__version__", "__codename__", "APP_ID", "GETTEXT_DOMAIN"]
