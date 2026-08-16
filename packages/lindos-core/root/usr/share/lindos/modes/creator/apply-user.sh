#!/bin/bash
# creator/apply-user.sh — user-side extras of the Creator mode: animations on, a colour-managed
# display hint (colord profile), Bottles/recipe readiness check.  Guarded, never fatal.
set -Eeuo pipefail

PROG="lindos-mode[creator]"
log() { printf '%s: %s\n' "${PROG}" "$*"; }
die() { printf '%s: ERROR: %s\n' "${PROG}" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

XFCONF="xfconf-query"

main() {
    if have "${XFCONF}"; then
        "${XFCONF}" -c xsettings -p /Gtk/EnableAnimations -n -t bool -s true 2>/dev/null || true
        "${XFCONF}" -c xfwm4 -p /general/use_compositing -n -t bool -s true 2>/dev/null || true
    fi
    rm -f "${HOME}/.config/autostart/lindos-redshift.desktop" 2>/dev/null || true

    # colour management hint: list the display profiles colord knows about (informational)
    if have colormgr; then
        if colormgr get-devices-by-kind display 2>/dev/null | grep -q 'Object Path'; then
            log "colord: display device registered — assign an ICC profile in Settings → System → Display for print-accurate colours"
        else
            log "colord: no display device yet (colord starts on demand)"
        fi
    fi

    # Windows-app support readiness for the creator recipes (Photoshop/Illustrator CC 2019–2021 …)
    if have lindos-compat; then
        lindos-compat doctor >/dev/null 2>&1 && log "lindos-compat doctor: ok" \
            || log "lindos-compat doctor reported missing pieces — run 'lindos-compat doctor' for fix commands"
    else
        log "lindos-compat not installed; Windows creative apps need package lindos-compat"
    fi
    if have flatpak && flatpak info com.usebottles.bottles >/dev/null 2>&1; then
        log "Bottles installed (Flatpak com.usebottles.bottles)"
    else
        log "Bottles not installed yet — 'lindos-mode set creator' installs it when online (or: lindos-compat install-bottles)"
    fi
    if have lindos-mangohud; then
        lindos-mangohud off >/dev/null 2>&1 || true
    fi
    log "done"
}

main "$@"
