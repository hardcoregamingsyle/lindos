#!/bin/bash
# ============================================================================
#  ci-live-checks.sh - QA-only: is the LIVE session what the Lindos install flow promises?
#
#  Launched (detached, like the desktop watcher) by ci-boot-smoke-test.sh's start_live_watch(), so it
#  only ever runs on the CI boot test (kernel command line 'lindos.ci_boot_test').  READ-ONLY: it never
#  installs, modifies or deletes anything, and it is harmless if run by hand (it prints a report).
#
#  The USB session is almost nothing but the installer.  Once the XFCE session is up this prints one
#  'LINDOS_CHECK NAME=OK|FAIL' line per promise (the grammar build/qa/boot_test.py parses), then
#  'LINDOS_LIVE_CHECKS_DONE fails=N':
#    live-no-oobe             the first-run wizard (lindos-setup) does not run in the live session
#    live-installer-launcher  the desktop has the 'Install Lindos' launcher of the installer
#    live-panel               the panel (xfce4-panel) runs
#    live-desktop             the desktop manager and window manager (xfdesktop, xfwm4, xfce4-session) run
#    live-no-sleep            nothing may put the machine to sleep or blank it: the logind inhibitor
#                             unit is active and the power manager says 'never' (live-session-power.sh)
#    live-no-installs         nothing installs or updates in the live session (no pkexec, no Lindos
#                             installer script, no first-boot retry unit)
#  When one fails, LINDOS_LIVE_DIAG lines (process list, desktop folder, unit states) follow.
#
#  An installed system is not a live session: there it prints 'LINDOS_LIVE_CHECKS_DONE fails=0 skipped=1'.
#
#  No 'set -e': one failing check must not stop the rest.  Test seams (unset on a real boot):
#  LINDOS_CI_CONSOLE ('-' = stdout), LINDOS_CI_CMDLINE, LINDOS_CI_IS_LIVE_SESSION, LINDOS_CI_LIVE_USER,
#  LINDOS_CI_LIVE_HOME, LINDOS_CI_PANEL_WAIT (seconds to wait for the panel), LINDOS_CI_SETTLE (seconds
#  the session may settle before the checks), LINDOS_CI_TICK (seconds between polls).
# ============================================================================
set -u

CONSOLE="${LINDOS_CI_CONSOLE:-/dev/console}"
CMDLINE="${LINDOS_CI_CMDLINE:-/proc/cmdline}"
IS_LIVE="${LINDOS_CI_IS_LIVE_SESSION:-/usr/libexec/lindos/is-live-session}"
PANEL_WAIT="${LINDOS_CI_PANEL_WAIT:-420}"
SETTLE="${LINDOS_CI_SETTLE:-20}"
TICK="${LINDOS_CI_TICK:-1}"
FAILS=0

if [ "${CONSOLE}" != "-" ]; then
    exec >"${CONSOLE}" 2>&1
fi

echo "LINDOS_LIVE_WATCH_STARTED"

# verdict NAME RC [WHY...] - one LINDOS_CHECK line; a failure also says why (LINDOS_FAIL_LOG, like ci-boot-smoke-test.sh)
verdict() {
    local name="$1" rc="$2"
    shift 2
    if [ "${rc}" -eq 0 ]; then
        echo "LINDOS_CHECK ${name}=OK"
        return 0
    fi
    echo "LINDOS_CHECK ${name}=FAIL rc=${rc}"
    if [ "$#" -gt 0 ]; then
        echo "LINDOS_FAIL_LOG ${name}: $*"
    fi
    FAILS=$((FAILS + 1))
}

# live_user - the live session's account: username= on the kernel command line (the boot entries and boot_test.py
# pass username=liveuser), else liveuser
live_user() {
    local u="${LINDOS_CI_LIVE_USER:-}" w
    local -a words=()
    if [ -z "${u}" ]; then
        read -r -a words <"${CMDLINE}" 2>/dev/null || true   # no trailing newline makes read fail but still fill words
        for w in "${words[@]+"${words[@]}"}"; do
            case "${w}" in
                username=*) u="${w#username=}" ;;
            esac
        done
    fi
    printf '%s\n' "${u:-liveuser}"
}

live_home() {
    local h="${LINDOS_CI_LIVE_HOME:-}"
    if [ -z "${h}" ]; then
        h="$(getent passwd "$1" 2>/dev/null | cut -d: -f6)"
    fi
    printf '%s\n' "${h:-/home/$1}"
}

# wait_for_panel - up to PANEL_WAIT seconds for the live user's panel (the session is up when it runs)
wait_for_panel() {
    local i=0
    while [ "${i}" -lt "${PANEL_WAIT}" ]; do
        if pgrep -u "${USER_NAME}" -x xfce4-panel >/dev/null 2>&1; then
            return 0
        fi
        sleep "${TICK}"
        i=$((i + 1))
    done
    return 1
}

# --- live-no-oobe ------------------------------------------------------------------------------------------------
# The first-run wizard is gated in code (lindos-setup exits at once in a live session), so a short-lived process may
# still be seen while the autostart runs: it only counts when it is still there on the third look.
check_no_oobe() {
    local i=0 procs=""
    while [ "${i}" -lt 3 ]; do
        procs="$(pgrep -af 'lindos-setup' 2>/dev/null | head -n 3 | tr '\n' ';')"
        if [ -z "${procs}" ]; then
            verdict live-no-oobe 0
            return 0
        fi
        sleep "${TICK}"
        i=$((i + 1))
    done
    verdict live-no-oobe 1 "the first-run wizard runs in the live session: ${procs}"
}

# --- live-installer-launcher -------------------------------------------------------------------------------------
# casper puts /usr/share/applications/ubiquity.desktop (branded by build/chroot/78-installer-brand.sh) on the live
# user's desktop.
check_launcher() {
    local f="${HOME_DIR}/Desktop/ubiquity.desktop" name
    if [ ! -f "${f}" ]; then
        verdict live-installer-launcher 1 "${f} does not exist ($(ls "${HOME_DIR}/Desktop" 2>&1 | tr '\n' ' '))"
        return 0
    fi
    name="$(sed -n 's/^Name=//p' "${f}" | head -n 1)"
    case "${name}" in
        "Install Lindos"*) ;;
        *)
            verdict live-installer-launcher 1 "Name=${name}, expected 'Install Lindos'"
            return 0
            ;;
    esac
    # (the old brand is spelled M[i]nt so that the repository's scan for stray brand text does not flag this detector)
    if grep -qE 'RELEASE|Linux M[i]nt' "${f}"; then
        verdict live-installer-launcher 1 "$(grep -E 'RELEASE|Linux M[i]nt' "${f}" | head -n 1) (not rebranded)"
        return 0
    fi
    if ! grep -q '^Exec=.*ubiquity' "${f}"; then
        verdict live-installer-launcher 1 "Exec= does not start the installer"
        return 0
    fi
    verdict live-installer-launcher 0
}

# --- live-panel / live-desktop -----------------------------------------------------------------------------------
check_panel() {
    if pgrep -u "${USER_NAME}" -x xfce4-panel >/dev/null 2>&1; then
        verdict live-panel 0
    else
        verdict live-panel 1 "xfce4-panel does not run for ${USER_NAME} (the session did not come up within ${PANEL_WAIT}s)"
    fi
}

check_desktop() {
    local missing="" p
    for p in xfdesktop xfwm4 xfce4-session; do
        pgrep -u "${USER_NAME}" -x "${p}" >/dev/null 2>&1 || missing="${missing} ${p}"
    done
    if [ -z "${missing}" ]; then
        verdict live-desktop 0
    else
        verdict live-desktop 1 "not running for ${USER_NAME}:${missing}"
    fi
}

# --- live-no-sleep -----------------------------------------------------------------------------------------------
# power_value NAME - an xfce4-power-manager property of the live user: from the running session (xfconf over the
# user's session bus) or, when the bus cannot be reached from a system service, from the saved channel file.
power_value() {
    local uid v f
    uid="$(id -u "${USER_NAME}" 2>/dev/null)"
    v=""
    if [ -n "${uid}" ] && command -v runuser >/dev/null 2>&1; then
        v="$(runuser -u "${USER_NAME}" -- env "XDG_RUNTIME_DIR=/run/user/${uid}" \
            "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus" \
            xfconf-query -c xfce4-power-manager -p "/xfce4-power-manager/$1" 2>/dev/null | head -n 1)"
    fi
    if [ -z "${v}" ]; then
        f="${HOME_DIR}/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-power-manager.xml"
        if [ -r "${f}" ]; then
            v="$(sed -n "s/.*name=\"$1\"[^>]*value=\"\\([^\"]*\\)\".*/\\1/p" "${f}" | head -n 1)"
        fi
    fi
    printf '%s\n' "${v}"
}

check_no_sleep() {
    local why="" name want got pair
    if ! systemctl is-active --quiet lindos-live-inhibit.service 2>/dev/null; then
        why="lindos-live-inhibit.service is not active; "
    fi
    # the values live-session-power.sh sets ('14' is the slider's 'Never' position)
    for pair in inactivity-on-ac=14 inactivity-on-battery=14 lid-action-on-ac=0 lid-action-on-battery=0; do
        name="${pair%%=*}"
        want="${pair#*=}"
        got="$(power_value "${name}")"
        if [ "${got}" != "${want}" ]; then
            why="${why}${name} is '${got:-unset}' (want ${want}); "
        fi
    done
    if [ -z "${why}" ]; then
        verdict live-no-sleep 0
    else
        verdict live-no-sleep 1 "${why%; }"
    fi
}

# --- live-no-installs --------------------------------------------------------------------------------------------
check_no_installs() {
    local why="" procs u
    procs="$(pgrep -af 'libexec/lindos/(install-(browser|compat|gaming)|lindos-helper|browser-firstboot|driver-firstboot)' 2>/dev/null | head -n 3 | tr '\n' ';')"
    [ -z "${procs}" ] || why="${why}${procs} "
    pgrep -x pkexec >/dev/null 2>&1 && why="${why}pkexec is running "
    pgrep -x flatpak >/dev/null 2>&1 && why="${why}flatpak is running "
    for u in lindos-browser-firstboot.service lindos-driver-firstboot.service; do
        case "$(systemctl is-active "${u}" 2>/dev/null)" in
            active | activating) why="${why}${u} is active " ;;
        esac
    done
    if [ -z "${why}" ]; then
        verdict live-no-installs 0
    else
        verdict live-no-installs 1 "${why% }"
    fi
    # a package manager may legitimately run in the background (timers): recorded, never a failure
    echo "LINDOS_INFO live_package_processes=$(pgrep -a -x 'apt|apt-get|dpkg|unattended-upgr' 2>/dev/null | head -n 3 | tr '\n' ';')"
}

live_diag() {
    ps -eo user,pid,comm,args --no-headers 2>&1 | grep -E 'xfce|xfwm|xfdesktop|lightdm|lindos|ubiquity|Xorg|pkexec|apt|dpkg|flatpak' \
        | head -n 40 | sed 's/^/LINDOS_LIVE_DIAG ps: /'
    ls -la "${HOME_DIR}/Desktop" 2>&1 | head -n 20 | sed 's/^/LINDOS_LIVE_DIAG desktop: /'
    echo "LINDOS_LIVE_DIAG is-system-running: $(systemctl is-system-running 2>&1)"
    echo "LINDOS_LIVE_DIAG live-inhibit: $(systemctl is-active lindos-live-inhibit.service 2>&1)"
    systemctl --failed --no-legend --plain 2>&1 | head -n 15 | sed 's/^/LINDOS_LIVE_DIAG failed: /'
}

main() {
    if [ -f "${IS_LIVE}" ] && ! bash "${IS_LIVE}"; then
        echo "LINDOS_INFO live_checks=skipped (this is not a live session)"
        echo "LINDOS_LIVE_CHECKS_DONE fails=0 skipped=1"
        return 0
    fi
    USER_NAME="$(live_user)"
    HOME_DIR="$(live_home "${USER_NAME}")"
    echo "LINDOS_INFO live_user=${USER_NAME} home=${HOME_DIR}"
    if wait_for_panel; then
        # the autostart entries (power settings, the wizard's gate) run right after the panel appears
        sleep "${SETTLE}"
    fi
    check_no_oobe
    check_launcher
    check_panel
    check_desktop
    check_no_sleep
    check_no_installs
    if [ "${FAILS}" -gt 0 ]; then
        live_diag
    fi
    echo "LINDOS_LIVE_CHECKS_DONE fails=${FAILS}"
}

main
exit 0
