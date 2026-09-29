#!/bin/bash
# live-session-power.sh — LIVE USB session only: never sleep, blank the screen, lock or
# suspend on lid close, so a long installation is never interrupted.
#
# Started by /etc/xdg/autostart/lindos-live-session.desktop, whose Exec is gated by the shared
# is-live-session helper; this script asks the same helper again, so an installed system's session
# is never touched (its power settings are the Lindos defaults from xfce4-power-manager.xml).
#
# What it sets, all best effort (a missing tool or an unknown property is logged and skipped, the
# script always exits 0):
#   * xfce4-power-manager (xfconf channel of the live user): presentation mode on, no display
#     sleep/off, no blanking, no idle sleep, lid closed = do nothing, no lock on suspend, power
#     button = ask, sleep/hibernate keys = nothing;
#   * the screensaver/locker of the base image: xfce4-screensaver (xfconf) and light-locker
#     (gsettings) - whichever exists - get idle activation and locking switched off;
#   * the X server itself: xset s off / s noblank / -dpms.
# The system-wide half (lid, sleep key) is lindos-live-inhibit.service in lindos-core, which also
# covers the 'Install Lindos' boot entries that start the installer without any desktop session.
#
# Test overrides: LINDOS_IS_LIVE_SESSION, LINDOS_XFCONF_QUERY, LINDOS_GSETTINGS, LINDOS_XSET.
set -Eeuo pipefail

IS_LIVE_SESSION="${LINDOS_IS_LIVE_SESSION:-/usr/libexec/lindos/is-live-session}"
XFCONF_QUERY="${LINDOS_XFCONF_QUERY:-xfconf-query}"
GSETTINGS="${LINDOS_GSETTINGS:-gsettings}"
XSET="${LINDOS_XSET:-xset}"

log() { printf 'lindos-live-session: %s\n' "$*" >&2; }

have() { command -v "$1" >/dev/null 2>&1; }

# Unknown (helper missing) counts as "installed": never change an installed session's settings.
is_live_session() {
    [ -f "${IS_LIVE_SESSION}" ] && bash "${IS_LIVE_SESSION}"
}

# xf_set <channel> <property> <type> <value> — create or overwrite one xfconf property.
xf_set() {
    "${XFCONF_QUERY}" -c "$1" -p "$2" -n -t "$3" -s "$4" >/dev/null 2>&1 || \
        log "could not set $1 $2 (ignored)"
}

power_manager() {
    local ch="xfce4-power-manager"
    xf_set "${ch}" /xfce4-power-manager/presentation-mode bool true
    xf_set "${ch}" /xfce4-power-manager/dpms-enabled bool false
    xf_set "${ch}" /xfce4-power-manager/dpms-on-ac-sleep uint 0
    xf_set "${ch}" /xfce4-power-manager/dpms-on-ac-off uint 0
    xf_set "${ch}" /xfce4-power-manager/dpms-on-battery-sleep uint 0
    xf_set "${ch}" /xfce4-power-manager/dpms-on-battery-off uint 0
    xf_set "${ch}" /xfce4-power-manager/blank-on-ac int 0
    xf_set "${ch}" /xfce4-power-manager/blank-on-battery int 0
    # 14 is the slider's "Never" position (see lindos-desktop's xfce4-power-manager.xml)
    xf_set "${ch}" /xfce4-power-manager/inactivity-on-ac uint 14
    xf_set "${ch}" /xfce4-power-manager/inactivity-on-battery uint 14
    xf_set "${ch}" /xfce4-power-manager/lid-action-on-ac uint 0
    xf_set "${ch}" /xfce4-power-manager/lid-action-on-battery uint 0
    xf_set "${ch}" /xfce4-power-manager/lock-screen-suspend-hibernate bool false
    xf_set "${ch}" /xfce4-power-manager/power-button-action uint 3
    xf_set "${ch}" /xfce4-power-manager/sleep-button-action uint 0
    xf_set "${ch}" /xfce4-power-manager/hibernate-button-action uint 0
}

screensavers() {
    # xfce4-screensaver (from memory of its xfconf channel; harmless when the channel is unused)
    local ch="xfce4-screensaver"
    xf_set "${ch}" /saver/enabled bool false
    xf_set "${ch}" /saver/idle-activation/enabled bool false
    xf_set "${ch}" /lock/enabled bool false
    xf_set "${ch}" /lock/saver-activation/enabled bool false
    xf_set "${ch}" /lock/sleep-activation bool false
    # light-locker keeps its settings in GSettings
    if have "${GSETTINGS}"; then
        "${GSETTINGS}" set apps.light-locker late-locking false >/dev/null 2>&1 || true
        "${GSETTINGS}" set apps.light-locker lock-on-suspend false >/dev/null 2>&1 || true
        "${GSETTINGS}" set apps.light-locker lock-on-lid false >/dev/null 2>&1 || true
        "${GSETTINGS}" set apps.light-locker lock-after-screensaver 0 >/dev/null 2>&1 || true
    fi
}

x_server() {
    [ -n "${DISPLAY:-}" ] || return 0
    have "${XSET}" || return 0
    "${XSET}" s off >/dev/null 2>&1 || true
    "${XSET}" s noblank >/dev/null 2>&1 || true
    "${XSET}" -dpms >/dev/null 2>&1 || true
}

main() {
    if ! is_live_session; then
        exit 0
    fi
    if have "${XFCONF_QUERY}"; then
        power_manager
        screensavers
    else
        log "xfconf-query not found; only the X server settings are applied"
    fi
    x_server
    log "live session: sleep, screen blanking, lock and lid-suspend are off"
    exit 0
}

main "$@"
