#!/bin/bash
# first-login-panel.sh — per-user, once: seed the taskbar layout of the effective Lindos
# mode into the user's XFCE config before xfce4-panel starts.
#
# Started by /etc/xdg/autostart/lindos-mode-apply-user.desktop (OnlyShowIn=XFCE, autostart
# phase "Initialization" so it runs before the panel).  It is the "user part" of a mode
# for accounts that never ran `lindos-mode set` (e.g. an OEM/system default other than
# everyday, or a freshly created user):
#
#   * mode = ~/.config/lindos/config.json "mode" → /etc/lindos/system.json "mode" → everyday
#   * if ~/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml does not exist and the
#     mode is not "everyday" (whose layout *is* the /etc/xdg default), copy
#     /usr/share/lindos/modes/<mode>/panel/xfce4-panel.xml there;
#   * copy the mode's plugin rc files (whiskermenu-1.rc, docklike-2.rc …) into
#     ~/.config/xfce4/panel/ when they do not exist yet (pins per mode, SPEC §3);
#   * write ~/.config/lindos/panel-init.done (mode + date) so this never runs twice; a
#     later `lindos-mode set` re-applies layouts through xfce4-panel-profiles anyway;
#   * if the panel is somehow already running (session manager without autostart phases)
#     it is stopped before seeding and started again afterwards.
#
# Never touches existing user files, never fails the session (exit 0 always), logs to
# ~/.local/state/lindos/first-login-panel.log.  Options: --force (ignore the stamp),
# --mode <id> (override), --dry-run.
set -Eeuo pipefail

MODES_DIR="${LINDOS_MODES_DIR:-/usr/share/lindos/modes}"
CONF_HOME="${XDG_CONFIG_HOME:-${HOME}/.config}"
STATE_HOME="${XDG_STATE_HOME:-${HOME}/.local/state}"
USER_CONF="${CONF_HOME}/lindos/config.json"
SYSTEM_CONF="/etc/lindos/system.json"
STAMP="${CONF_HOME}/lindos/panel-init.done"
XFCONF_DIR="${CONF_HOME}/xfce4/xfconf/xfce-perchannel-xml"
PANEL_DIR="${CONF_HOME}/xfce4/panel"
LOG_FILE="${STATE_HOME}/lindos/first-login-panel.log"
FORCE=0
DRY_RUN=0
MODE_OVERRIDE=""

log() {
    local line
    line="$(date '+%F %T') first-login-panel: $*"
    printf '%s\n' "${line}" >&2
    if mkdir -p "$(dirname "${LOG_FILE}")" 2>/dev/null; then
        printf '%s\n' "${line}" >>"${LOG_FILE}" 2>/dev/null || true
    fi
}
die() { log "ERROR: $*"; exit 0; }   # never break the login session

while [ $# -gt 0 ]; do
    case "$1" in
        --force) FORCE=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --mode) [ $# -ge 2 ] || die "--mode needs an argument"; MODE_OVERRIDE="$2"; shift 2 ;;
        --mode=*) MODE_OVERRIDE="${1#*=}"; shift ;;
        -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

# json_get FILE KEY → value or "" (python3 preferred, grep fallback)
json_get() {
    local file="$1" key="$2"
    [ -r "${file}" ] || return 0
    if command -v python3 >/dev/null 2>&1; then
        python3 - "${file}" "${key}" <<'PY' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
    value = data.get(sys.argv[2]) if isinstance(data, dict) else None
    if isinstance(value, str):
        print(value)
except Exception:
    pass
PY
    else
        sed -n 's/.*"'"${key}"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "${file}" | head -n1
    fi
}

effective_mode() {
    local m
    m="$(json_get "${USER_CONF}" mode)"
    [ -n "${m}" ] || m="$(json_get "${SYSTEM_CONF}" mode)"
    [ -n "${m}" ] || m="everyday"
    printf '%s\n' "${m}"
}

main() {
    if [ "${FORCE}" -eq 0 ] && [ -f "${STAMP}" ]; then
        exit 0
    fi
    local mode
    mode="${MODE_OVERRIDE:-$(effective_mode)}"
    case "${mode}" in
        everyday|gaming|work|creator|lite) ;;
        *) log "unknown mode '${mode}', using everyday"; mode="everyday" ;;
    esac
    local src="${MODES_DIR}/${mode}/panel"
    if [ ! -d "${src}" ]; then
        die "no panel layout for mode '${mode}' (${src} missing)"
    fi

    # --- what needs seeding? -----------------------------------------------------
    local want_xml=0 rc name
    local -a want_rc=()
    if [ "${mode}" != "everyday" ] && [ ! -f "${XFCONF_DIR}/xfce4-panel.xml" ] && [ -f "${src}/xfce4-panel.xml" ]; then
        want_xml=1
    fi
    for rc in "${src}"/*.rc; do
        [ -f "${rc}" ] || continue
        name="$(basename "${rc}")"
        [ -f "${PANEL_DIR}/${name}" ] || want_rc+=("${rc}")
    done
    local copied=0
    if [ "${want_xml}" -eq 0 ] && [ "${#want_rc[@]}" -eq 0 ]; then
        log "nothing to seed for mode ${mode} (user config already present)"
    elif [ "${DRY_RUN}" -eq 1 ]; then
        [ "${want_xml}" -eq 0 ] || log "would copy ${src}/xfce4-panel.xml → ${XFCONF_DIR}/"
        for rc in ${want_rc[@]+"${want_rc[@]}"}; do
            log "would copy ${rc} → ${PANEL_DIR}/"
        done
        copied=$(( want_xml + ${#want_rc[@]} ))
    else
        # Normally we run in the "Initialization" autostart phase, before the panel and before
        # xfconfd has loaded the xfce4-panel channel.  If a session manager ignores the phase
        # and the panel is already up, stop it (and xfconfd, which caches the channel, when the
        # xml is replaced) so the seeded files are what gets loaded, then start the panel again.
        local panel_was_running=0
        if command -v pgrep >/dev/null 2>&1 && pgrep -x xfce4-panel -u "$(id -u)" >/dev/null 2>&1; then
            panel_was_running=1
            xfce4-panel --quit >/dev/null 2>&1 || true
            if [ "${want_xml}" -eq 1 ]; then
                pkill -x xfconfd -u "$(id -u)" >/dev/null 2>&1 || true
            fi
            sleep 1
        fi
        if [ "${want_xml}" -eq 1 ]; then
            mkdir -p "${XFCONF_DIR}"
            cp -f "${src}/xfce4-panel.xml" "${XFCONF_DIR}/xfce4-panel.xml"
            chmod 0644 "${XFCONF_DIR}/xfce4-panel.xml"
            log "installed ${mode} panel layout → ${XFCONF_DIR}/xfce4-panel.xml"
            copied=$((copied + 1))
        fi
        for rc in ${want_rc[@]+"${want_rc[@]}"}; do
            name="$(basename "${rc}")"
            mkdir -p "${PANEL_DIR}"
            cp -f "${rc}" "${PANEL_DIR}/${name}"
            chmod 0644 "${PANEL_DIR}/${name}"
            log "installed ${name} for mode ${mode}"
            copied=$((copied + 1))
        done
        if [ "${panel_was_running}" -eq 1 ] && command -v xfce4-panel >/dev/null 2>&1; then
            (setsid xfce4-panel >/dev/null 2>&1 &) || (xfce4-panel >/dev/null 2>&1 &)
            log "panel restarted with the ${mode} layout"
        fi
    fi

    if [ "${DRY_RUN}" -eq 0 ]; then
        mkdir -p "$(dirname "${STAMP}")"
        printf 'mode=%s\ndate=%s\nfiles=%s\n' "${mode}" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "${copied}" >"${STAMP}"
    fi
    log "done (mode=${mode}, ${copied} file(s) seeded)"
}

main "$@" || true
exit 0
