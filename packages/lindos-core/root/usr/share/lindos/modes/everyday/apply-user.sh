#!/bin/bash
# everyday/apply-user.sh — restore the balanced defaults when coming back from another mode:
# animations & compositing on, no night-light autostart, MangoHud hidden.  Guarded, never fatal.
set -Eeuo pipefail

PROG="lindos-mode[everyday]"
log() { printf '%s: %s\n' "${PROG}" "$*"; }
die() { printf '%s: ERROR: %s\n' "${PROG}" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

XFCONF="xfconf-query"

main() {
    if have "${XFCONF}"; then
        "${XFCONF}" -c xsettings -p /Gtk/EnableAnimations -n -t bool -s true 2>/dev/null || true
        "${XFCONF}" -c xfwm4 -p /general/use_compositing -n -t bool -s true 2>/dev/null || true
        "${XFCONF}" -c xfwm4 -p /general/box_move -n -t bool -s false 2>/dev/null || true
        "${XFCONF}" -c xfwm4 -p /general/box_resize -n -t bool -s false 2>/dev/null || true
        log "animations on"
    else
        log "xfconf-query not found; skipping xfconf tweaks"
    fi
    rm -f "${HOME}/.config/autostart/lindos-redshift.desktop" 2>/dev/null || true
    if have powerprofilesctl; then
        powerprofilesctl set balanced >/dev/null 2>&1 || true
    fi
    if have lindos-mangohud; then
        lindos-mangohud sync >/dev/null 2>&1 || true
    fi
    log "done"
}

main "$@"
