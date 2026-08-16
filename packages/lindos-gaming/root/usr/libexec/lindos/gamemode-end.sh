#!/bin/bash
# gamemode-end.sh — run by gamemoded ([custom] end= in /etc/gamemode.ini) as the
# user when the last game leaves Game Mode.  Restores the compositor
# ('lindos-compositor start' honours the Lindos mode: it stays off in Lite).
#
# Quiet the notification with:  touch ~/.config/lindos/gamemode-quiet
set -Eeuo pipefail

STATE_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/lindos"
LOG_FILE="${STATE_DIR}/gamemode.log"
QUIET_FLAG="${XDG_CONFIG_HOME:-${HOME}/.config}/lindos/gamemode-quiet"

log() {
    mkdir -p "${STATE_DIR}" 2>/dev/null || true
    printf '%s gamemode-end: %s\n' "$(date '+%F %T')" "$*" >>"${LOG_FILE}" 2>/dev/null || true
}

die() {
    log "ERROR: $*"
    exit 1
}

main() {
    log "game mode released"
    if command -v lindos-compositor >/dev/null 2>&1; then
        if lindos-compositor start >>"${LOG_FILE}" 2>&1; then
            log "compositor restored"
        else
            log "warning: 'lindos-compositor start' failed (ignored)"
        fi
    else
        log "lindos-compositor not found; nothing to restore"
    fi
    if [ ! -e "${QUIET_FLAG}" ] && command -v notify-send >/dev/null 2>&1 \
        && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
        notify-send -a "Lindos" -i input-gaming -u low -t 2000 \
            "Game Mode off" "Compositor restored" >/dev/null 2>&1 || true
    fi
    exit 0
}

main "$@"
