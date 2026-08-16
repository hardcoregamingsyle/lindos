#!/bin/bash
# gamemode-start.sh — run by gamemoded ([custom] start= in /etc/gamemode.ini) as the
# user when the first game requests Game Mode.  Pauses the compositor for lower
# input latency / higher FPS and (optionally) shows a small notification.
#
# Quiet the notification with:  touch ~/.config/lindos/gamemode-quiet
set -Eeuo pipefail

STATE_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/lindos"
LOG_FILE="${STATE_DIR}/gamemode.log"
QUIET_FLAG="${XDG_CONFIG_HOME:-${HOME}/.config}/lindos/gamemode-quiet"

log() {
    mkdir -p "${STATE_DIR}" 2>/dev/null || true
    printf '%s gamemode-start: %s\n' "$(date '+%F %T')" "$*" >>"${LOG_FILE}" 2>/dev/null || true
}

die() {
    log "ERROR: $*"
    exit 1
}

main() {
    log "game mode requested (pid ${PPID:-?})"
    if command -v lindos-compositor >/dev/null 2>&1; then
        if lindos-compositor stop >>"${LOG_FILE}" 2>&1; then
            log "compositor paused"
        else
            log "warning: 'lindos-compositor stop' failed (ignored)"
        fi
    else
        log "lindos-compositor not found; compositor left as is"
    fi
    if [ ! -e "${QUIET_FLAG}" ] && command -v notify-send >/dev/null 2>&1 \
        && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
        notify-send -a "Lindos" -i input-gaming -u low -t 2500 \
            "Game Mode on" "Performance governor active, compositor paused (Shift_R+F12 toggles MangoHud)" \
            >/dev/null 2>&1 || true
    fi
    exit 0
}

main "$@"
