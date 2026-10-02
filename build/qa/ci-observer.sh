#!/bin/bash
# ============================================================================
#  ci-observer.sh - CI-ONLY guest-side observer for build/qa/install_test.py.
#
#  It is never shipped: the install test copies it into the guest at test time - into the live root
#  through a preseed early_command ('live'), into the installed disk before its first boot ('oem') - and
#  runs it as a throw-away systemd unit.  It only READS the system and prints to the serial console, so a
#  reproducible log of the run exists even when the guest dies half-way.
#
#    ci-observer.sh live   the live installer session (during the installation):
#                            LINDOS_HOOK_LOG ...              the installer hook's own log, as it grows
#                            LINDOS_UBIQUITY_LOG ...          error-looking lines of Ubiquity's debug log
#                            LINDOS_INSTALL_HEARTBEAT ...     every minute: Ubiquity state, target disk use
#                            LINDOS_CHECK live-* =OK|FAIL     ONCE, about two minutes in: the session really is the
#                                                             default 'Install Lindos' one (only-ubiquity: the
#                                                             installer on its own X server; no LightDM, no XFCE
#                                                             session, no first-run wizard, no pkexec) and the
#                                                             logind sleep inhibitor is held
#                            LINDOS_INSTALL_SESSION_CHECKED fails=N   after those checks (LINDOS_INSTALL_DIAG lines
#                                                             with the process list precede it when one failed)
#                            LINDOS_INSTALL_FINALIZED         finalize.sh (ubiquity/success_command) has run: only
#                                                             the unmount and the poweroff are left
#                            LINDOS_INSTALL_FAILED ...        ubiquity.service ended with an error
#                            LINDOS_INSTALL_UBIQUITY_EXIT ... ubiquity.service ended cleanly
#    ci-observer.sh oem    the first boot of the INSTALLED disk (oem-config must start, not a desktop):
#                            LINDOS_CHECK oem-* =OK|FAIL      the properties the install flow promises
#                            LINDOS_OEM_READY                 the wizard is up (screenshot time)
#                            LINDOS_OEM_TIMEOUT               it never came up (diagnostics follow)
#
#  No 'set -e': one failing probe must not stop the observer.  Test seams (unset on a real run):
#  LINDOS_CI_OUT ('-' = stdout), LINDOS_CI_STEP (seconds between probes), LINDOS_CI_OEM_WAIT (probes
#  before giving up), LINDOS_CI_SETTLE (seconds to wait after the wizard appeared), LINDOS_CI_BEAT_EVERY
#  (probes between heartbeats), LINDOS_CI_MAX_TICKS (stop after that many probes; 0 = never),
#  LINDOS_CI_HOOK_LOG, LINDOS_CI_UBIQUITY_DEBUG, LINDOS_CI_UBIQUITY_VERSION (the files 'live' reads),
#  LINDOS_CI_CMDLINE (the kernel command line 'live' reads), LINDOS_CI_LIVE_CHECK_TICKS (probes before the one-shot
#  session checks; 24 x 5 s = two minutes: Xorg and the installer window are up by then), LINDOS_CI_PROC (where the
#  environment of the GTK programs is read: /proc), LINDOS_CI_DM_LOG (ubiquity-dm's log, whose tail is printed).
# ============================================================================
set -u

MODE="${1:-}"
case "${MODE}" in
    live) DEFAULT_OUT=/dev/console ;;
    oem) DEFAULT_OUT=/dev/ttyS0 ;;
    *)
        echo "usage: ci-observer.sh live|oem" >&2
        exit 2
        ;;
esac

STEP="${LINDOS_CI_STEP:-5}"
OEM_WAIT="${LINDOS_CI_OEM_WAIT:-120}"
SETTLE="${LINDOS_CI_SETTLE:-15}"
BEAT_EVERY="${LINDOS_CI_BEAT_EVERY:-12}"
MAX_TICKS="${LINDOS_CI_MAX_TICKS:-0}"
HOOK_LOG="${LINDOS_CI_HOOK_LOG:-/var/log/lindos/installer-hook.log}"
UBIQUITY_DEBUG="${LINDOS_CI_UBIQUITY_DEBUG:-/var/log/installer/debug}"
UBIQUITY_VERSION="${LINDOS_CI_UBIQUITY_VERSION:-/var/log/installer/version}"
CMDLINE="${LINDOS_CI_CMDLINE:-/proc/cmdline}"
PROC_DIR="${LINDOS_CI_PROC:-/proc}"
DM_LOG="${LINDOS_CI_DM_LOG:-/var/log/installer/dm}"
LIVE_CHECK_TICKS="${LINDOS_CI_LIVE_CHECK_TICKS:-24}"
FAILS=0

OUT="${LINDOS_CI_OUT:-${DEFAULT_OUT}}"
if [ "${OUT}" != "-" ]; then
    # the serial port when it can be written; the console (which is the serial port on a CI boot) otherwise
    [ -w "${OUT}" ] || OUT=/dev/console
    exec >"${OUT}" 2>&1
fi

# running PATTERN - some process command line matches the (extended) regular expression
running() {
    pgrep -f "$1" >/dev/null 2>&1
}

# running_x NAME... - a process whose NAME (the kernel's comm, never its command line) is exactly one of these exists.
# By name on purpose: earlyoom's command line reads '--avoid (^|/)(Xorg|xfwm4|xfce4-panel|lightdm)$' and would make every
# pattern match on command lines see a panel and a display manager that are not there.
running_x() {
    local name
    for name in "$@"; do
        pgrep -x "${name}" >/dev/null 2>&1 && return 0
    done
    return 1
}

# verdict NAME RC [WHY...] - one LINDOS_CHECK line (the format build/qa/boot_test.py and install_test.py parse)
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

# prefixed PREFIX - stdin, line by line, each line behind PREFIX (capped so a runaway log cannot flood the serial log)
prefixed() {
    local n=0 line
    while IFS= read -r line; do
        echo "$1 ${line}"
        n=$((n + 1))
        if [ "${n}" -ge "${2:-60}" ]; then
            break
        fi
    done
}

# ---------------------------------------------------------------------------------------------
#  live: the installation
# ---------------------------------------------------------------------------------------------
live_heartbeat() {
    local tick="$1" state used
    state="$(systemctl show ubiquity.service -p ActiveState --value 2>/dev/null)"
    used="$(df -Pk /target 2>/dev/null | awk 'NR==2 {print $3 "/" $2 "KiB"}')"
    echo "LINDOS_INSTALL_HEARTBEAT tick=${tick} ubiquity=${state:-unknown} target=${used:-not-mounted} procs=$(pgrep -fc ubiquity 2>/dev/null)"
}

# gone KIND WHAT... - no process matches on at least one of three looks (a short-lived helper is not a session).
# KIND x: exact process names (one or more), f: one pattern against the whole command line.
gone() {
    local kind="$1" i=0
    shift
    while [ "${i}" -lt 3 ]; do
        if [ "${kind}" = x ]; then
            running_x "$@" || return 0
        else
            running "$1" || return 0
        fi
        i=$((i + 1))
        [ "${i}" -lt 3 ] && sleep "${STEP}"
    done
    return 1
}

# ui_theme - the GTK_THEME of the installer's / account wizard's GTK program (the environment it was started with:
# /proc/PID/environ), printed; fails when none of the processes matching PATTERN has one.  Ubiquity's GTK program
# inherits the environment of its systemd unit through start-ubiquity-dm / oem-config-firstboot and ubiquity-dm: this is
# what proves the drop-in with GTK_THEME=Lindos-Setup reached it.
ui_theme() {
    local pid value
    for pid in $(pgrep -f "$1" 2>/dev/null); do
        [ -r "${PROC_DIR}/${pid}/environ" ] || continue
        value="$(tr '\0' '\n' <"${PROC_DIR}/${pid}/environ" 2>/dev/null | grep -m1 '^GTK_THEME=')"
        if [ -n "${value}" ]; then
            printf '%s\n' "${value#GTK_THEME=}"
            return 0
        fi
    done
    return 1
}

# live_inhibitor - lindos-live-inhibit.service is active and systemd-inhibit lists its block on sleep (the 'Install Lindos'
# boot has no desktop session, so this unit is the only thing that keeps a laptop lid or an idle timeout from suspending
# the machine half-way through the installation)
live_inhibitor() {
    local list
    systemctl is-active --quiet lindos-live-inhibit.service 2>/dev/null || return 1
    list="$(systemd-inhibit --list --no-legend 2>/dev/null)"
    case "${list}" in
        *"Live session"*) ;;
        *) return 1 ;;
    esac
    case "${list}" in
        *sleep*) return 0 ;;
    esac
    return 1
}

live_diag() {
    pgrep -af 'ubiquity|lightdm|xfce4|xfwm4|metacity|marco|xfsettingsd|xfdesktop|lindos|pkexec|Xorg' 2>/dev/null | prefixed "LINDOS_INSTALL_DIAG ps:" 40
    echo "LINDOS_INSTALL_DIAG inhibit: unit=$(systemctl is-active lindos-live-inhibit.service 2>&1) list=$(systemd-inhibit --list --no-legend 2>&1 | head -n 5 | tr '\n' ';')"
}

# live_checks - the session this installation runs in must be the default 'Install Lindos' boot entry and nothing else.
# (A window manager is NOT a sign of a desktop here: ubiquity-dm starts its own for the installer window - metacity when
# the image has it, else xfwm4; the session proper is xfce4-session, its panel and xfdesktop.)
live_checks() {
    local only=1 w theme rc
    local -a words=()
    read -r -a words <"${CMDLINE}" 2>/dev/null || true       # no trailing newline makes read fail but still fill words
    for w in "${words[@]+"${words[@]}"}"; do
        [ "${w}" = only-ubiquity ] && only=0
    done
    verdict live-only-ubiquity "${only}" "the kernel command line has no only-ubiquity: this is not the 'Install Lindos' session"
    running 'bin/ubiquity|ubiquity-dm'
    verdict live-installer-up $? "no Ubiquity process runs: the installer session never started"
    gone x lightdm
    verdict live-no-lightdm $? "LightDM is running: the installer boot must not start a display manager (or the installer already quit)"
    gone x xfce4-session xfce4-panel xfdesktop
    verdict live-no-xfce-session $? "a desktop session runs (xfce4-session, xfce4-panel or xfdesktop): the installer boot must not start one"
    gone f 'lindos-setup'
    verdict live-no-lindos-setup $? "the first-run wizard runs in the installer session"
    gone x pkexec
    verdict live-no-pkexec $? "pkexec is running: something asks for privileges inside the installer session"
    live_inhibitor
    verdict live-inhibitor-active $? "no sleep inhibitor: lindos-live-inhibit.service is not active or systemd-inhibit does not list it"
    theme="$(ui_theme 'usr/lib/ubiquity/bin/ubiquity|bin/ubiquity ')"
    echo "LINDOS_INFO installer_gtk_theme=${theme:-none}"
    rc=1
    [ "${theme}" = Lindos-Setup ] && rc=0
    verdict live-installer-theme "${rc}" "the installer's GTK program has GTK_THEME='${theme:-unset}', not Lindos-Setup: the ubiquity.service drop-in did not reach it, so the installer shows Ubiquity's light default"
    # ubiquity-dm's own log: which window manager and settings daemon it started (evidence for the installer's look)
    [ ! -r "${DM_LOG}" ] || tail -n 40 "${DM_LOG}" 2>/dev/null | prefixed "LINDOS_INSTALL_DM" 40
    if [ "${FAILS}" -gt 0 ]; then
        live_diag
    fi
    echo "LINDOS_INSTALL_SESSION_CHECKED fails=${FAILS}"
}

live_mode() {
    local tick=0 active result reported="" finalized=0 checked=0
    echo "LINDOS_OBSERVER_STARTED mode=live uname=$(uname -r)"
    # the hook's log, whole and as it grows; Ubiquity's debug log is huge in automatic mode, so only its errors
    tail -n +1 -F "${HOOK_LOG}" 2>/dev/null | sed -u 's/^/LINDOS_HOOK_LOG /' &
    tail -n 0 -F "${UBIQUITY_DEBUG}" 2>/dev/null \
        | grep --line-buffered -iE 'traceback|error|exception|failed' \
        | sed -u 's/^/LINDOS_UBIQUITY_LOG /' &
    while :; do
        active="$(systemctl show ubiquity.service -p ActiveState --value 2>/dev/null)"
        result="$(systemctl show ubiquity.service -p Result --value 2>/dev/null)"
        case "${result}" in
            exit-code | signal | core-dump | timeout | watchdog | resources | start-limit-hit)
                if [ "${reported}" != failed ]; then
                    echo "LINDOS_INSTALL_FAILED ubiquity.service result=${result} state=${active:-unknown}"
                    reported=failed
                fi
                ;;
        esac
        # inactive + success is also what a service that has not started yet looks like: Ubiquity's version file
        # (written when it starts) tells the two apart
        if [ "${active}" = inactive ] && [ "${result}" = success ] && [ -e "${UBIQUITY_VERSION}" ] && [ -z "${reported}" ]; then
            echo "LINDOS_INSTALL_UBIQUITY_EXIT result=success"
            reported="exit"
        fi
        # finalize.sh is the last thing Ubiquity runs before it unmounts the target and powers off: the harness gives the
        # guest a few minutes from here, then shuts it down itself (a preseed that Ubiquity ignored leaves a dialog open)
        if [ "${finalized}" -eq 0 ] && grep -q 'finalize: done' "${HOOK_LOG}" 2>/dev/null; then
            echo "LINDOS_INSTALL_FINALIZED"
            finalized=1
        fi
        tick=$((tick + 1))
        if [ "${checked}" -eq 0 ] && [ "${tick}" -ge "${LIVE_CHECK_TICKS}" ]; then
            live_checks
            checked=1
        fi
        if [ $((tick % BEAT_EVERY)) -eq 0 ]; then
            live_heartbeat "${tick}"
        fi
        if [ "${MAX_TICKS}" -gt 0 ] && [ "${tick}" -ge "${MAX_TICKS}" ]; then
            break
        fi
        sleep "${STEP}"
    done
}

# ---------------------------------------------------------------------------------------------
#  oem: the first boot of the installed disk
# ---------------------------------------------------------------------------------------------
oem_wizard_up() {
    running 'ubiquity-dm|oem-config-wrapper' && { running_x Xorg || running_x X; }
}

oem_checks() {
    local v rc theme
    v="$(systemctl get-default 2>/dev/null)"
    rc=1
    [ "${v}" = "oem-config.target" ] && rc=0
    verdict oem-default-target "${rc}" "the default target is '${v:-unknown}'"
    v="$(systemctl is-active oem-config.service 2>/dev/null)"
    case "${v}" in active | activating) rc=0 ;; *) rc=1 ;; esac
    verdict oem-config-service "${rc}" "oem-config.service is '${v:-unknown}'"
    running 'oem-config-firstboot|oem-config-wrapper|ubiquity-dm'
    verdict oem-wizard-process $? "no oem-config / ubiquity-dm process is running"
    running_x Xorg || running_x X
    verdict oem-x-server $? "no X server is running (the wizard has no screen)"
    ! running_x lightdm
    verdict oem-no-lightdm $? "LightDM is running before the account exists (an oem or login desktop instead of the wizard)"
    ! running 'lindos-setup'
    verdict oem-no-lindos-setup $? "the Lindos first-run wizard runs before the account exists"
    ! running 'libexec/lindos/(install-(browser|compat|gaming)|lindos-helper|browser-firstboot|driver-firstboot)'
    verdict oem-no-installs $? "a Lindos installer/retry script runs during the first boot"
    echo "LINDOS_INFO oem_default_target=$(systemctl get-default 2>/dev/null) is_system_running=$(systemctl is-system-running 2>/dev/null)"
    echo "LINDOS_INFO oem_account=$(id oem 2>&1 | tr '\n' ' ')"
    echo "LINDOS_INFO oem_os_release=$(sed -n 's/^PRETTY_NAME=//p' /etc/os-release 2>/dev/null | head -n 1)"
    echo "LINDOS_INFO oem_wizard_strings lindos=$(grep -c 'Lindos' /var/cache/debconf/templates.dat 2>/dev/null) mint=$(grep -c 'Linux Mint' /var/cache/debconf/templates.dat 2>/dev/null)"
    theme="$(ui_theme 'sbin/oem-config --only')"
    echo "LINDOS_INFO oem_wizard_gtk_theme=${theme:-none}"
    rc=1
    [ "${theme}" = Lindos-Setup ] && rc=0
    verdict oem-wizard-theme "${rc}" "the account wizard's GTK program has GTK_THEME='${theme:-unset}', not Lindos-Setup: the oem-config.service drop-in did not reach it, so the wizard shows Ubiquity's light default"
    echo "LINDOS_INFO oem_wizard_title=$(echo 'GET ubiquity/custom_title_text' | debconf-communicate 2>/dev/null | tr '\n' ' ')"
    echo "LINDOS_INFO oem_wizard_wm=$(pgrep -a 'metacity|marco|xfwm4|mutter' 2>/dev/null | head -n 2 | tr '\n' ';')"
    [ ! -r "${DM_LOG}" ] || tail -n 40 "${DM_LOG}" 2>/dev/null | prefixed "LINDOS_OEM_DM" 40
    pgrep -af 'ubiquity|oem-config|Xorg|lightdm|xfwm4|metacity|marco|xfsettingsd' 2>/dev/null | prefixed "LINDOS_OEM_PS" 30
}

oem_diag() {
    systemctl status oem-config.service --no-pager -n 30 2>&1 | prefixed "LINDOS_OEM_DIAG status:" 40
    systemctl list-jobs --no-legend 2>&1 | prefixed "LINDOS_OEM_DIAG jobs:" 20
    systemctl --failed --no-legend --plain 2>&1 | prefixed "LINDOS_OEM_DIAG failed:" 20
    journalctl -b -u oem-config --no-pager 2>&1 | tail -n 40 | prefixed "LINDOS_OEM_DIAG journal:" 40
    tail -n 40 /var/log/oem-config.log 2>&1 | prefixed "LINDOS_OEM_DIAG oem-config.log:" 40
    ps -eo user,pid,comm,args --no-headers 2>&1 | grep -E 'ubiquity|oem|Xorg|lightdm|xfwm4|metacity|xfsettingsd|plymouth' | prefixed "LINDOS_OEM_DIAG ps:" 30
}

oem_mode() {
    local i=0 tick=0
    echo "LINDOS_OBSERVER_STARTED mode=oem uname=$(uname -r)"
    while ! oem_wizard_up; do
        i=$((i + 1))
        if [ "${i}" -ge "${OEM_WAIT}" ]; then
            break
        fi
        sleep "${STEP}"
    done
    if oem_wizard_up; then
        # the wizard needs a moment to draw its first page: the harness takes its screenshot after READY
        sleep "${SETTLE}"
        oem_checks
        echo "LINDOS_OEM_READY fails=${FAILS}"
    else
        oem_diag
        oem_checks
        echo "LINDOS_OEM_TIMEOUT fails=${FAILS}"
    fi
    # keep talking, so that a screenshot taken later still has a matching line in the serial log
    while :; do
        tick=$((tick + 1))
        if [ $((tick % BEAT_EVERY)) -eq 0 ]; then
            echo "LINDOS_OEM_HEARTBEAT tick=${tick} wizard=$(oem_wizard_up && echo up || echo down)"
        fi
        if [ "${MAX_TICKS}" -gt 0 ] && [ "${tick}" -ge "${MAX_TICKS}" ]; then
            break
        fi
        sleep "${STEP}"
    done
}

if [ "${MODE}" = live ]; then
    live_mode
else
    oem_mode
fi
exit 0
