"""gettext helper for lindos-setup.

``_()`` returns the untranslated English string when no catalogue for the
``lindos-setup`` domain is installed (identity fallback), so the wizard always
works.  Call :func:`init` once at start-up (safe to call again).
"""
from __future__ import annotations

import gettext
import locale
import logging
import os
from typing import Optional

DOMAIN = "lindos-setup"
_LOCALEDIR_CANDIDATES = ("/usr/share/locale", "/usr/local/share/locale")

log = logging.getLogger("lindos-setup.i18n")
_translation: gettext.NullTranslations = gettext.NullTranslations()
_initialised = False


def init(localedir: Optional[str] = None) -> gettext.NullTranslations:
    """Bind the ``lindos-setup`` domain; falls back to identity translation."""
    global _translation, _initialised
    try:
        locale.setlocale(locale.LC_ALL, "")
    except (locale.Error, ValueError):
        # unsupported locale env; gettext still works with LANG/LANGUAGE
        pass
    dirs = [localedir] if localedir else []
    dirs += [d for d in _LOCALEDIR_CANDIDATES if os.path.isdir(d)]
    trans: gettext.NullTranslations = gettext.NullTranslations()
    for d in dirs:
        try:
            trans = gettext.translation(DOMAIN, localedir=d, fallback=True)
        except (OSError, ValueError) as exc:  # pragma: no cover - defensive
            log.debug("gettext translation lookup failed in %s: %s", d, exc)
            continue
        if type(trans) is not gettext.NullTranslations:  # a real catalogue was found
            break
    _translation = trans
    _initialised = True
    # also make plain gettext.gettext() and C-level bindtextdomain (GTK builder) aware
    for d in dirs:
        try:
            gettext.bindtextdomain(DOMAIN, d)
            gettext.textdomain(DOMAIN)
        except (AttributeError, OSError):
            pass
        break
    return _translation


def _(message: str) -> str:
    """Translate ``message`` (identity when no catalogue is installed)."""
    if not message:
        return message  # never return the catalogue header for ""
    if not _initialised:
        init()
    return _translation.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    if not _initialised:
        init()
    return _translation.ngettext(singular, plural, n)


__all__ = ["DOMAIN", "init", "_", "ngettext"]
