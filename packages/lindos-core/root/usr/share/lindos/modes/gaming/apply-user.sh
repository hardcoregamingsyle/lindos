#!/bin/bash
# gaming/apply-user.sh — user-side extras of the Gaming mode (run by lindos.modes.apply_mode as
# the logged-in user, after config/panel/compositor).  Everything is optional & guarded: a
# missing tool is logged, never fatal.
set -Eeuo pipefail

PROG="lindos-mode[gaming]"
log() { printf '%s: %s\n' "${PROG}" "$*"; }
die() { printf '%s: ERROR: %s\n' "${PROG}" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

XFCONF="xfconf-query"

main() {
    # animations & compositing back on (Lite turns them off)
    if have "${XFCONF}"; then
        "${XFCONF}" -c xsettings -p /Gtk/EnableAnimations -n -t bool -s true 2>/dev/null || true
        "${XFCONF}" -c xfwm4 -p /general/use_compositing -n -t bool -s true 2>/dev/null || true
        log "animations on"
    else
        log "xfconf-query not found; skipping xfconf tweaks"
    fi

    # MangoHud: seed the per-user overlay file from the system template and honour the
    # 'mangohud' config key (default off; Right-Shift+F12 toggles in-game).  Game Mode itself needs
    # no autostart — lindos-run/Steam use gamemoderun when config gamemode_auto is true.
    if have lindos-mangohud; then
        lindos-mangohud sync >/dev/null 2>&1 && log "MangoHud config synced" || log "lindos-mangohud sync failed (ignored)"
    else
        log "lindos-mangohud not installed (package lindos-gaming); MangoHud toggle unavailable"
    fi

    # night-light autostart belongs to Work mode only
    rm -f "${HOME}/.config/autostart/lindos-redshift.desktop" 2>/dev/null || true
    log "done"
}

main "$@"
