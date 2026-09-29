#!/bin/bash
# polkit-agent-start.sh - start a graphical polkit authentication agent for the XFCE session.
#
# Started by /etc/xdg/autostart/lindos-polkit-agent.desktop (OnlyShowIn=XFCE).
#
# Why: everything in Lindos that needs administrator rights (the first-boot setup, Lindos Settings,
# the Windows-app installers, Update) asks through pkexec.  pkexec needs an authentication agent
# registered for the login session to show the password dialog; without one it falls back to a text
# prompt (a terminal-like window, or a plain failure) and asks again for every call.  This wrapper
# makes sure a proper GTK dialog exists.
#
# What it does (never fails the session, always exits 0 unless it becomes the agent):
#   * an agent is already running for this user            -> does nothing;
#   * else starts the first installed of, in order: policykit-1-gnome, mate-polkit, lxpolkit,
#     xfce-polkit (replaces itself with it, so the agent IS this process);
#   * none installed                                        -> says so on stderr and exits 0.
#
# It never grants anything: no polkit rule is added, the password is still asked (the polkit action
# org.lindos.helper stays auth_admin_keep).
#
# Options: --print  show what would happen (agent path / "running" / "none") and exit.
# Test overrides: LINDOS_POLKIT_AGENTS (colon-separated candidate list),
#                 LINDOS_POLKIT_AGENT_PATTERN (pgrep -f pattern of "an agent is running").
set -Eeuo pipefail

log() { printf 'lindos-polkit-agent: %s\n' "$*" >&2; }

DEFAULT_AGENTS=(
    /usr/lib/policykit-1-gnome/polkit-gnome-authentication-agent-1
    /usr/libexec/polkit-gnome-authentication-agent-1
    /usr/libexec/polkit-mate-authentication-agent-1
    /usr/lib/mate-polkit/polkit-mate-authentication-agent-1
    /usr/bin/lxpolkit
    /usr/libexec/xfce-polkit
    /usr/lib/xfce-polkit/xfce-polkit
)
DEFAULT_PATTERN='polkit-gnome-authentication-agent-1|polkit-mate-authentication-agent-1|lxpolkit|xfce-polkit|polkit-kde-authentication-agent-1|lxqt-policykit-agent|ukui-polkit'

PRINT_ONLY=0
case "${1:-}" in
    --print) PRINT_ONLY=1 ;;
    -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    "") ;;
    *) log "unknown argument: $1"; exit 0 ;;
esac

AGENTS=("${DEFAULT_AGENTS[@]}")
if [ -n "${LINDOS_POLKIT_AGENTS:-}" ]; then
    IFS=':' read -r -a AGENTS <<< "${LINDOS_POLKIT_AGENTS}"
fi
PATTERN="${LINDOS_POLKIT_AGENT_PATTERN:-${DEFAULT_PATTERN}}"

agent_running() {
    command -v pgrep >/dev/null 2>&1 || return 1
    pgrep -u "$(id -u)" -f -- "${PATTERN}" >/dev/null 2>&1
}

if agent_running; then
    [ "${PRINT_ONLY}" -eq 1 ] && printf 'running\n'
    exit 0
fi

chosen=""
for agent in "${AGENTS[@]}"; do
    if [ -n "${agent}" ] && [ -x "${agent}" ]; then
        chosen="${agent}"
        break
    fi
done

if [ -n "${chosen}" ]; then
    if [ "${PRINT_ONLY}" -eq 1 ]; then
        printf '%s\n' "${chosen}"
        exit 0
    fi
    log "starting ${chosen}"
    exec "${chosen}"
fi

if [ "${PRINT_ONLY}" -eq 1 ]; then
    printf 'none\n'
else
    log "no graphical polkit authentication agent installed (policykit-1-gnome, mate-polkit or lxpolkit); pkexec will fall back to a text prompt"
fi
exit 0
