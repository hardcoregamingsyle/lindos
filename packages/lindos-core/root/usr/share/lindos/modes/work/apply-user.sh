#!/bin/bash
# work/apply-user.sh — user-side extras of the Work mode: animations on, night light (redshift)
# autostarted when installed, balanced power profile via powerprofilesctl.  Guarded, never fatal.
set -Eeuo pipefail

PROG="lindos-mode[work]"
log() { printf '%s: %s\n' "${PROG}" "$*"; }
die() { printf '%s: ERROR: %s\n' "${PROG}" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

XFCONF="xfconf-query"
AUTOSTART_DIR="${HOME}/.config/autostart"
REDSHIFT_ENTRY="${AUTOSTART_DIR}/lindos-redshift.desktop"

write_redshift_autostart() {
    mkdir -p "${AUTOSTART_DIR}"
    cat >"${REDSHIFT_ENTRY}.tmp" <<'EOF'
[Desktop Entry]
Type=Application
Version=1.0
Name=Night light (Lindos Work mode)
Comment=Warmer screen colours in the evening (redshift)
Exec=redshift-gtk -l geoclue2 -t 6500:4200
Icon=redshift
OnlyShowIn=XFCE;
X-GNOME-Autostart-enabled=true
X-Lindos-Mode=work
EOF
    mv -f "${REDSHIFT_ENTRY}.tmp" "${REDSHIFT_ENTRY}"
}

main() {
    if have "${XFCONF}"; then
        "${XFCONF}" -c xsettings -p /Gtk/EnableAnimations -n -t bool -s true 2>/dev/null || true
        "${XFCONF}" -c xfwm4 -p /general/use_compositing -n -t bool -s true 2>/dev/null || true
    fi
    if have redshift-gtk; then
        write_redshift_autostart
        if [[ -n "${DISPLAY:-}" ]] && ! pgrep -x redshift >/dev/null 2>&1 && ! pgrep -f redshift-gtk >/dev/null 2>&1; then
            (setsid redshift-gtk -l geoclue2 -t 6500:4200 >/dev/null 2>&1 &) || true
        fi
        log "night light: redshift-gtk autostart written"
    else
        log "redshift-gtk not installed; night light skipped (install it to enable)"
    fi
    if have powerprofilesctl; then
        powerprofilesctl set balanced >/dev/null 2>&1 && log "power profile: balanced" || log "powerprofilesctl unavailable (ignored)"
    fi
    if have lindos-mangohud; then
        lindos-mangohud off >/dev/null 2>&1 || true
    fi
    log "done"
}

main "$@"
