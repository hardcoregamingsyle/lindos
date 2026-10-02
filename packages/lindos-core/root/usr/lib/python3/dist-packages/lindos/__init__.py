"""``lindos`` — core Python library of Lindos (SPEC §4).

Pure Python 3.10+, standard library only.  Every sub-module is importable on any OS; all
Linux-specific calls are guarded and executed lazily.  Sub-modules:

``paths``     canonical filesystem locations (LINDOS_ROOT / LINDOS_HOME aware)
``config``    user config (~/.config/lindos/config.json) and system config
``modes``     the five Lindos modes and ``apply_mode``
``browsers``  Edge / Chrome / Firefox metadata, install and default handling
``hardware``  CPU / GPU / RAM / battery / governor / fans / refresh rates
``helper``    client for the privileged polkit helper (``lindos-helper``)
``theme``     dark/light, accent, wallpaper, fonts, taskbar (xfconf)
``compat``    Windows executable analysis, slugs, apps DB, runner choice
``ram``       RAM snapshot / report
``dualboot``  honest "restart into Windows" (efibootmgr/GRUB one-shot, Secure Boot/TPM facts)
``update``    reading update status: the Lindos apt source, upgradable packages, sideload (SPEC-UPDATE.md §36)
``updatestate``  the root refresh's update-state.json, apt-output parsers, reboot-required, history (§39-§41)
``session``   live USB session / installer chroot / temporary ``oem`` account detection
``installstate``  what the installer did and what is still pending (/var/lib/lindos/install-state.json)
"""

from __future__ import annotations

__version__ = "1.0.0"
__codename__ = "Aurora"
VERSION = __version__
CODENAME = __codename__
PRODUCT = "Lindos"

__all__ = [
    "__version__", "__codename__", "VERSION", "CODENAME", "PRODUCT",
    "paths", "config", "modes", "browsers", "hardware", "helper", "theme", "compat", "ram", "dualboot",
    "update", "updatestate", "session", "installstate",
]
