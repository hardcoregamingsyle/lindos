#!/bin/bash
# lite/apply-user.sh — user-side extras of the Lite mode: no animations, no xfwm4 compositing
# (picom is stopped by lindos-compositor), no night-light autostart.  Guarded, never fatal.
set -Eeuo pipefail

PROG="lindos-mode[lite]"
log() { printf '%s: %s\n' "${PROG}" "$*"; }
die() { printf '%s: ERROR: %s\n' "${PROG}" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

XFCONF="xfconf-query"

main() {
    if have "${XFCONF}"; then
        "${XFCONF}" -c xsettings -p /Gtk/EnableAnimations -n -t bool -s false 2>/dev/null || true
        "${XFCONF}" -c xfwm4 -p /general/use_compositing -n -t bool -s false 2>/dev/null || true
        # cheaper window moves/resizes on old GPUs
        "${XFCONF}" -c xfwm4 -p /general/box_move -n -t bool -s true 2>/dev/null || true
        "${XFCONF}" -c xfwm4 -p /general/box_resize -n -t bool -s true 2>/dev/null || true
        # xfdesktop: plain colour background costs less than a 4K SVG re-render on every workspace switch
        "${XFCONF}" -c xfce4-desktop -p /desktop-icons/tooltip-size -n -t double -s 0 2>/dev/null || true
        log "animations and compositing off"
    else
        log "xfconf-query not found; skipping xfconf tweaks"
    fi
    if have lindos-mangohud; then
        lindos-mangohud off >/dev/null 2>&1 || true
    fi
    rm -f "${HOME}/.config/autostart/lindos-redshift.desktop" 2>/dev/null || true
    log "done"
}

main "$@"
